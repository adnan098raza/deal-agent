"""Generic schema.org JSON-LD source: works for any shop listing page that embeds
`Product` objects (directly or inside an `ItemList`), e.g. Flaconi's sale page.
Also used by the daily re-check to read price/availability from product pages.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin, urlsplit

from deal_agent.models import Deal
from deal_agent.sources.base import Source, SourceBlocked, register

log = logging.getLogger(__name__)
_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _walk(obj):
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types:
            yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def products_from_html(html: str) -> list[dict]:
    out = []
    for blk in _LD.findall(html):
        try:
            data = json.loads(blk.strip())
        except json.JSONDecodeError:
            continue
        out.extend(_walk(data))
    return out


def offer_info(product: dict) -> dict:
    """price / availability / url from a Product's offers (Offer or AggregateOffer)."""
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    offers = offers if isinstance(offers, dict) else {}
    price = offers.get("price", offers.get("lowPrice"))
    try:
        price = float(str(price).replace(",", ".")) if price not in (None, "") else None
    except ValueError:
        price = None
    avail = str(offers.get("availability") or "").rsplit("/", 1)[-1] or None
    return {"price": price, "availability": avail, "url": offers.get("url"),
            "high_price": offers.get("highPrice")}


def _brand(p: dict) -> str | None:
    b = p.get("brand")
    if isinstance(b, dict):
        b = b.get("name")
    return str(b).strip() if b else None


@register("jsonld")
class JsonLdSource(Source):
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
            products = products_from_html(resp.text)
            domain = urlsplit(resp.url).netloc.removeprefix("www.")
            for p in products:
                info = offer_info(p)
                link = info["url"] or p.get("url")
                if not p.get("name") or not link or info["price"] is None:
                    continue
                if info["availability"] in ("OutOfStock", "SoldOut", "Discontinued"):
                    continue
                img = p.get("image")
                deals.append(Deal(
                    title=str(p["name"]), link=urljoin(resp.url, link), source=self.name,
                    price=info["price"], brand=_brand(p), merchant=self.merchant, shop_domain=domain,
                    image_url=img[0] if isinstance(img, list) and img else (img if isinstance(img, str) else None),
                    ean=str(p.get("gtin13") or p.get("gtin") or "") or None,
                ))
            if not products:
                log.warning("[%s] no JSON-LD products on %s", self.name, url)
            log.info("[%s] %d items from %s", self.name, len(products), url)
        return deals
