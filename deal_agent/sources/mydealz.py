"""mydealz.de: RSS feeds plus the full deal page.

The deal page embeds `window.__INITIAL_STATE__` with the thread details the feed lacks:
shop domain (`linkHost`), coupon code, shipping cost, idealo comparison price
(`nextBestPrice`), end date, expired flag and comment count. mydealz's `/visit/` redirect
links are disallowed by robots.txt, so the agent never follows them; the shop domain is
enough for trust labelling.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from deal_agent.models import Deal
from deal_agent.sources.base import register
from deal_agent.sources.rss import RssSource

log = logging.getLogger(__name__)
_STATE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>", re.S)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_thread(html: str) -> dict | None:
    """Extract useful fields from a mydealz deal page. None if the page has no thread state."""
    m = _STATE.search(html)
    if not m:
        return None
    try:
        td = json.loads(m.group(1)).get("threadDetail") or {}
    except json.JSONDecodeError:
        return None
    if not td:
        return None
    end = (td.get("endDate") or {}).get("timestamp") if isinstance(td.get("endDate"), dict) else None
    merchant = (td.get("merchant") or {}).get("merchantName") if isinstance(td.get("merchant"), dict) else None
    shipping = (td.get("shipping") or {}).get("price") if isinstance(td.get("shipping"), dict) else None
    host = (td.get("linkHost") or "").lower().removeprefix("www.") or None
    expired = bool(td.get("isExpired")) or td.get("status") in ("Expired",)
    out = {
        "price": _num(td.get("price")),
        "market_price": _num(td.get("nextBestPrice")),
        "original_price": _num(td.get("standardPrice")),
        "shop_domain": host,
        "merchant": merchant,
        "coupon_code": (td.get("voucherCode") or None) if not td.get("isVoucherCodeHidden") else None,
        "shipping": float(shipping) if isinstance(shipping, (int, float)) and shipping >= 0 else None,
        "expires_at": datetime.fromtimestamp(end, tz=timezone.utc) if isinstance(end, (int, float)) and end > 0 else None,
        "temperature": int(td["temperature"]) if isinstance(td.get("temperature"), (int, float)) else None,
        "comment_count": td.get("commentCount") if isinstance(td.get("commentCount"), int) else None,
        "expired": expired,
        "groups": [g.get("threadGroupName") for g in td.get("groups") or [] if isinstance(g, dict)],
    }
    return out


@register("mydealz")
class MydealzSource(RssSource):
    has_detail = True

    def detail(self, deal: Deal) -> dict | None:
        resp = self.get(deal.link)
        info = parse_thread(resp.text)
        if info is None:
            log.debug("[%s] no thread state on %s", self.name, deal.link)
        return info
