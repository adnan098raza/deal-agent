"""ntfy as a two-way channel.

* Button presses: alerts carry "Snooze" / "Not interested" ntfy *http actions*. Pressing one
  makes the phone POST "snooze <uid>" / "dismiss <uid>" to a private feedback topic on the ntfy
  server. The agent polls that topic, so buttons work from anywhere (no port forwarding) and
  also when the agent runs in GitHub Actions.
* Sent-sync: before alerting, the agent reads the last 12 h of the main topic and treats any
  deal whose link was already pushed as alerted (PC and cloud runner don't double-notify).
* Heartbeat: the PC posts to "<topic>-hb" hourly; the cloud runner skips when it's recent.
* Similarity penalty: deals similar to ones you dismissed lose score.
"""

from __future__ import annotations

import json
import logging
import re

import requests

from deal_agent.config import Settings
from deal_agent.models import Deal, link_uid
from deal_agent.parsing import normalize

log = logging.getLogger(__name__)
_CMD = re.compile(r"^\s*(snooze|dismiss)\s+([0-9a-f]{20})\s*$", re.I)
_PENALTY_STOP = {"fur", "bei", "mit", "und", "der", "die", "das", "in", "von", "ab", "statt", "eur", "gr", "inkl"}


class NtfyBridge:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.session = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.s.ntfy_topic)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.ntfy_token}"} if self.s.ntfy_token else {}

    def poll(self, topic: str, since: str) -> list[dict]:
        url = f"{self.s.ntfy_server}/{topic}/json"
        r = self.session.get(url, params={"poll": "1", "since": since}, headers=self._headers(), timeout=20)
        r.raise_for_status()
        out = []
        for line in r.text.splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("event") == "message":
                out.append(msg)
        return out

    def feedback_commands(self, cursor: str | None) -> tuple[list[tuple[str, str]], str | None]:
        """[(action, uid)], new cursor. Cursor = last processed ntfy message id."""
        if not (self.enabled and self.s.ntfy_feedback_topic):
            return [], cursor
        msgs = self.poll(self.s.ntfy_feedback_topic, cursor or "48h")
        cmds = []
        for m in msgs:
            hit = _CMD.match(m.get("message") or "")
            if hit:
                cmds.append((hit.group(1).lower(), hit.group(2).lower()))
        return cmds, (msgs[-1]["id"] if msgs else cursor)

    def sent_uids(self, since: str = "12h") -> set[str]:
        if not self.enabled:
            return set()
        return {link_uid(m["click"]) for m in self.poll(self.s.ntfy_topic, since) if m.get("click")}

    def heartbeat(self) -> None:
        if not self.s.heartbeat_topic:
            return
        self.session.post(f"{self.s.ntfy_server}/{self.s.heartbeat_topic}", data=b"alive",
                          headers={**self._headers(), "Title": "deal-agent heartbeat", "Priority": "1"}, timeout=15)

    def pc_online(self, within: str = "90m") -> bool:
        if not self.s.heartbeat_topic:
            return False
        try:
            return bool(self.poll(self.s.heartbeat_topic, within))
        except requests.RequestException as exc:
            log.warning("Heartbeat check failed (%s); assuming PC is offline", exc)
            return False


def _tokens(d: Deal) -> set[str]:
    return {w for w in normalize(d.title).split() if len(w) > 2 and w not in _PENALTY_STOP and not w.isdigit()}


def similarity_penalty(deal: Deal, dismissed: list[Deal]) -> float:
    """Score penalty (0-5) for deals resembling ones marked "Not interested"."""
    if not dismissed:
        return 0.0
    toks = _tokens(deal)
    best = 0.0
    same_brand_cat = 0
    for p in dismissed:
        if p.id == deal.id:
            continue
        if deal.product_key and p.product_key == deal.product_key:
            best = max(best, 4.0)
            continue
        pt = _tokens(p)
        if toks and pt:
            j = len(toks & pt) / len(toks | pt)
            if j >= 0.5:
                best = max(best, 3.0)
            elif j >= 0.3:
                best = max(best, 1.5)
        if p.brand and deal.brand and p.brand.casefold() == deal.brand.casefold() and p.category == deal.category:
            same_brand_cat += 1
    return round(min(best + min(0.5 * same_brand_cat, 2.0), 5.0), 1)
