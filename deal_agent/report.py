"""End-of-day HTML email: everything shown today, grouped by category."""

from __future__ import annotations

import html
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from deal_agent.config import Preferences
from deal_agent.db import Database
from deal_agent.models import Deal
from deal_agent.notifier import eur
from deal_agent.students import short as student_short

KIND_LABEL = {"instant": "Instant", "digest": "Digest", "watchlist": "Watchlist", "reminder": "Reminder",
              "snoozed": "Snoozed", "synced": "Sent elsewhere"}
STATUS_COLOR = {"Live": "#1a7f37", "Price changed": "#9a6700", "Expired": "#888", "Out of stock": "#cf222e"}
TRUST_COLOR = {"official_store": "#1a7f37", "major_retailer": "#0969da", "marketplace": "#9a6700", "unknown": "#cf222e"}


def day_bounds(tz: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    local = (now or datetime.now(zone)).astimezone(zone)
    start = datetime.combine(local.date(), time(0, 0), tzinfo=zone)
    return start, start + timedelta(days=1)


def collect_today(db: Database, prefs: Preferences, tz: str) -> list[tuple[Deal, str]]:
    start, end = day_bounds(tz)
    seen: dict[int, tuple[Deal, set[str]]] = {}
    for deal, kind, _ in db.alerts_between(start, end):
        seen.setdefault(deal.id, (deal, set()))[1].add(KIND_LABEL.get(kind, kind))
    return [(d, ", ".join(sorted(k))) for d, k in seen.values()]


def build(db: Database, prefs: Preferences, tz: str, excel_note: str = "") -> tuple[str, str, str] | None:
    rows = collect_today(db, prefs, tz)
    if not rows:
        return None
    start, _ = day_bounds(tz)
    order = list(prefs.categories) + ["other"]
    groups: dict[str, list[tuple[Deal, str]]] = {}
    for d, kinds in rows:
        groups.setdefault(d.category if d.category in prefs.categories else "other", []).append((d, kinds))
    subject = f"Deal digest {start:%a %d.%m.}: {len(rows)} deal(s) shown today"

    th = ('style="text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:12px;'
          'color:#555;text-transform:uppercase"')
    td = 'style="padding:6px 8px;border-bottom:1px solid #eee;font-size:13px;vertical-align:top"'
    parts = [f'<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#222;max-width:960px">'
             f'<h2 style="margin:0 0 4px">Deals shown today</h2>'
             f'<p style="color:#666;margin:0 0 16px">{start:%A, %d %B %Y} · {len(rows)} deal(s){html.escape(excel_note)}</p>']
    text = [subject, ""]
    for cat in order:
        items = sorted(groups.get(cat, []), key=lambda x: -(x[0].score or 0))
        if not items:
            continue
        title = cat.replace("_", " / ").title()
        parts.append(f'<h3 style="margin:20px 0 6px">{html.escape(title)} ({len(items)})</h3>'
                     f'<table style="border-collapse:collapse;width:100%"><tr>'
                     + "".join(f"<th {th}>{h}</th>" for h in
                               ("Deal", "Price", "Discount", "Shop", "Trust", "Student", "Status", "Score", "Alert"))
                     + "</tr>")
        text.append(f"== {title} ==")
        for d, kinds in items:
            warn = ("<br>" + "".join(f'<span style="color:#cf222e;font-size:12px">⚠ {html.escape(w)}</span><br>'
                                     for w in d.warnings[:2])) if d.warnings else ""
            price = eur(d.effective_price)
            if d.original_price:
                price += f'<br><span style="color:#888;text-decoration:line-through">{eur(d.original_price)}</span>'
            stu = html.escape(student_short(d.student)) if d.student else ""
            if d.student and d.student.get("final_est") is not None:
                stu += f"<br>≈ {eur(d.student['final_est'])}"
            parts.append(
                f"<tr><td {td}><a href=\"{html.escape(d.link)}\" style=\"color:#0969da;font-weight:600;text-decoration:none\">"
                f"{html.escape(d.title)}</a>{warn}</td>"
                f"<td {td}>{price}</td>"
                f"<td {td}>{f'−{d.discount_pct:.0f}%' if d.discount_pct else '–'}</td>"
                f"<td {td}>{html.escape(d.shop)}</td>"
                f"<td {td}><span style=\"color:{TRUST_COLOR.get(d.trust_label or 'unknown')}\">{html.escape(d.trust_text)}</span></td>"
                f"<td {td}>{stu or '–'}</td>"
                f"<td {td}><span style=\"color:{STATUS_COLOR.get(d.status, '#222')}\">{html.escape(d.status)}</span></td>"
                f"<td {td}>{(d.score or 0):.1f}</td>"
                f"<td {td}>{html.escape(kinds)}</td></tr>")
            text.append(f"- {d.title} | {eur(d.effective_price)} | {d.shop} | {d.trust_text} | {d.status} | {d.link}")
        parts.append("</table>")
        text.append("")
    parts.append('<p style="color:#999;font-size:12px;margin-top:24px">Sent by your deal agent. '
                 'The full log is attached as deals.xlsx.</p></body></html>')
    return subject, "".join(parts), "\n".join(text)
