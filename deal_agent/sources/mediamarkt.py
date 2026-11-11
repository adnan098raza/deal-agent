"""MediaMarkt / Saturn offer pages.

Both shops render product teasers server-side into `window.__PRELOADED_STATE__.apolloState`
(an Apollo GraphQL cache). Per product there is a `CofrCoreFeature:<id>` (name, manufacturer,
URL, EAN) and a `CofrPriceFeature:{"id":"<id>",...}` (price, discount, marketplace seller).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlsplit

from deal_agent.models import Deal
from deal_agent.sources.base import Source, SourceBlocked, register

log = logging.getLogger(__name__)
_STATE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


def parse_apollo(html: str, base_url: str, merchant: str) -> list[Deal]:
    m = _STATE.search(html)
    if not m:
        return []
    raw = re.sub(r'(?<=[:\[,])\s*undefined\b', "null", m.group(1))
    try:
        apollo = json.loads(raw).get("apolloState") or {}
    except json.JSONDecodeError as exc:
        log.warning("MediaMarkt state not parseable: %s", exc)
        return []
    prices = {}
    for k, v in apollo.items():
        if k.startswith("CofrPriceFeature:") and isinstance(v, dict):
            prices[v.get("id")] = v
    domain = urlsplit(base_url).netloc.removeprefix("www.")
    deals = []
    for k, core in apollo.items():
        if not k.startswith("CofrCoreFeature:") or not isinstance(core, dict):
            continue
        pid = core.get("id")
        pf = prices.get(pid) or {}
        p = pf.get("price") or {}
        amount = p.get("amount")
        url = core.get("urlRelative")
        name = core.get("productName")
        if not (name and url and isinstance(amount, (int, float))):
            continue
        discount = p.get("discount") if isinstance(p.get("discount"), (int, float)) else None
        seller = pf.get("marketplaceSeller") if pf.get("isProductOfTypeMarketplace") else None
        brand = (core.get("manufacturerName") or "").strip()
        deals.append(Deal(
            title=name,
            link=urljoin(base_url, url),
            source="",
            price=float(amount),
            original_price=float(amount + discount) if discount else None,
            discount_pct=float(p["discountPercentage"]) if isinstance(p.get("discountPercentage"), (int, float)) else None,
            brand=brand.title() if brand.isupper() else (brand or None),
            merchant=merchant,
            shop_domain=domain,
            shipping=float(p["shippingCost"]) if isinstance(p.get("shippingCost"), (int, float)) else None,
            seller_name=(seller or {}).get("sellerName"),
            seller_rating=(seller or {}).get("sellerRating"),
            ean=core.get("ean") or None,
        ))
    return deals


@register("mediamarkt")
class MediaMarktSource(Source):
    def fetch(self) -> list[Deal]:
        deals: list[Deal] = []
        for url in self.urls:
            try:
                resp = self.get(url)
            except SourceBlocked:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] fetch failed for %s: %s", self.name, url, exc)
                continue
            found = parse_apollo(resp.text, resp.url, self.merchant or urlsplit(url).netloc)
            for d in found:
                d.source = self.name
            if not found:
                log.warning("[%s] no products found on %s (layout may have changed)", self.name, url)
            log.info("[%s] %d items from %s", self.name, len(found), url)
            deals.extend(found)
        return deals
