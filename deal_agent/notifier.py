"""ntfy.sh pushes (with Open / Snooze / Not interested buttons) and SMTP email."""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import requests

from deal_agent.config import Settings
from deal_agent.models import Deal, WatchItem
from deal_agent.students import short as student_short

log = logging.getLogger(__name__)

KIND_TAGS = {
    "instant": ["fire"], "digest": ["shopping_cart"], "watchlist": ["dart"],
    "reminder": ["hourglass_flowing_sand"], "snoozed": ["alarm_clock"], "test": ["test_tube"],
}
KIND_PREFIX = {
    "instant": "Top deal", "digest": "Deal", "watchlist": "Watchlist hit", "reminder": "Ends soon",
    "snoozed": "Snoozed deal", "test": "Test",
}


def eur(v: float | None) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if v is not None else "–"


@dataclass
class Alert:
    kind: str
    deal: Deal
    score: float | None = None
    reason: str | None = None
    watch: WatchItem | None = None
    badge: str | None = None                       # e.g. "Lowest in 90 days"
    also_at: list[Deal] = field(default_factory=list)
    position: tuple[int, int] | None = None        # (i, n) inside a digest

    @property
    def headline(self) -> str:
        d = self.deal
        pre = KIND_PREFIX.get(self.kind, "Deal")
        if self.position:
            pre = f"{pre} {self.position[0]}/{self.position[1]}"
        price = f" – {eur(d.effective_price)}" if d.effective_price is not None else ""
        return f"{pre}: {d.title[:110]}{price}"[:200]

    def price_line(self) -> str:
        d = self.deal
        if d.effective_price is None:
            return "Price: see deal"
        s = eur(d.effective_price)
        if d.current_price is not None and d.price and abs(d.current_price - d.price) > 0.005:
            s += f" (alerted at {eur(d.price)})"
        if d.original_price:
            s += f", was {eur(d.original_price)}"
        if d.discount_pct:
            s += f" −{d.discount_pct:.0f}%"
        if self.badge:
            s += f" · ⬇ {self.badge}"
        return s

    def lines(self) -> list[str]:
        d = self.deal
        out = [self.price_line()]
        extras = []
        if d.coupon_code:
            extras.append(f"Code {d.coupon_code}")
        if d.shipping:
            extras.append(f"+{eur(d.shipping)} shipping")
        stu = d.student or {}
        if stu.get("final_est") is None and d.final_price_est is not None and d.shipping:
            extras.append(f"final ≈ {eur(d.final_price_est)}")
        if extras:
            out.append(" · ".join(extras))
        if stu:
            line = f"🎓 {student_short(stu)}"
            if stu.get("final_est") is not None:
                line += f" → final ≈ {eur(stu['final_est'])}"
            elif stu.get("stacks") in ("no", "unlikely"):
                line += f": {stu.get('why')}"
            out.append(line)
        out.append(f"🏪 {d.shop} · {d.trust_text}")
        for w in d.warnings[:3]:
            out.append(f"⚠️ {w}")
        out.append(f"⏳ Time left: {d.time_left()}")
        if self.also_at:
            cheapest = min((o.effective_price for o in self.also_at if o.effective_price), default=None)
            out.append(f"Also at {len(self.also_at)} other shop(s)" + (f" from {eur(cheapest)}" if cheapest else ""))
        if self.watch:
            out.append(f"🎯 Watch #{self.watch.id}: {self.watch.describe()}")
        if self.score is not None:
            out.append(f"Score {self.score:.1f}/10 – {self.reason or ''}".rstrip(" –"))
        return out


class Notifier:
    def __init__(self, settings: Settings, http_session: requests.Session | None = None):
        self.s = settings
        self.session = http_session or requests.Session()

    @property
    def push_enabled(self) -> bool:
        return bool(self.s.ntfy_enabled and self.s.ntfy_topic)

    @property
    def email_enabled(self) -> bool:
        return bool(self.s.email_enabled and self.s.smtp_host and self.s.email_to)

    @property
    def channels(self) -> list[str]:
        return (["ntfy"] if self.push_enabled else []) + (["email"] if self.email_enabled else [])

    # ---------------------------------------------------------------- push
    def push(self, alerts: list[Alert], dry_run: bool = False) -> list[Alert]:
        """Send each alert as an ntfy push. Returns the ones delivered (all of them when no
        channel is configured, after logging them, so the pipeline still progresses)."""
        if not alerts:
            return []
        if dry_run or not self.push_enabled:
            if not dry_run:
                log.warning("ntfy not configured; alerts are only logged")
            for a in alerts:
                log.info("[%s] %s | %s | %s", "DRY-RUN" if dry_run else "ALERT", a.headline,
                         " | ".join(a.lines()), a.deal.link)
            return [] if dry_run else alerts
        return [a for a in alerts if self._ntfy(a)]

    def ntfy_payload(self, a: Alert) -> dict:
        actions = [{"action": "view", "label": "Open deal", "url": a.deal.link, "clear": True}]
        if self.s.ntfy_feedback_topic and a.kind != "test":
            fb_url = f"{self.s.ntfy_server}/{self.s.ntfy_feedback_topic}"
            hdrs = {"Authorization": f"Bearer {self.s.ntfy_token}"} if self.s.ntfy_token else {}
            for label, cmd in (("Snooze", "snooze"), ("Not interested", "dismiss")):
                act = {"action": "http", "label": label, "url": fb_url, "method": "POST",
                       "body": f"{cmd} {a.deal.uid}", "clear": True}
                if hdrs:
                    act["headers"] = hdrs
                actions.append(act)
        payload = {
            "topic": self.s.ntfy_topic,
            "title": a.headline,
            "message": "\n".join(a.lines()),
            "click": a.deal.link,
            "tags": KIND_TAGS.get(a.kind, ["shopping_cart"]),
            "priority": 4 if a.kind in ("instant", "watchlist", "reminder") else 3,
            "actions": actions,
        }
        if a.deal.image_url:
            payload["icon"] = a.deal.image_url
        return payload

    def _ntfy(self, a: Alert) -> bool:
        headers = {"Authorization": f"Bearer {self.s.ntfy_token}"} if self.s.ntfy_token else {}
        try:
            r = self.session.post(self.s.ntfy_server + "/", json=self.ntfy_payload(a), headers=headers, timeout=15)
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("ntfy push failed for %r: %s", a.deal.title[:60], exc)
            return False

    # --------------------------------------------------------------- email
    def email(self, subject: str, html: str, text: str, attachments: list[Path] | None = None) -> bool:
        if not self.email_enabled:
            log.info("Email not configured; skipping %r", subject)
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.s.email_from or self.s.smtp_user
        msg["To"] = ", ".join(self.s.email_to)
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        for path in attachments or []:
            if path and path.exists():
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        try:
            ctx = ssl.create_default_context()
            if self.s.smtp_port == 465:
                with smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, context=ctx, timeout=30) as smtp:
                    self._login_send(smtp, msg)
            else:
                with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
                    smtp.starttls(context=ctx)
                    self._login_send(smtp, msg)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Email delivery failed: %s", exc)
            return False

    def _login_send(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.s.smtp_user and self.s.smtp_password:
            smtp.login(self.s.smtp_user, self.s.smtp_password)
        smtp.send_message(msg)
