"""Generic HTML sale-page scraper driven by CSS selectors in sources.yaml.

Example:
    - name: some-shop-sale
      type: html
      url: https://shop.example/sale
      merchant: Some Shop
      selectors:
        item: ".product-tile"          # required: one element per deal
        title: ".product-name"         # required
        link: "a[href]"                # href is resolved relative to the page
        price: ".price-now"
        original_price: ".price-was"
        discount: ".badge"
        image: "img[src]"
      require: "%"                     # optional regex: skip items whose title doesn't match
      marketplace_link: "/p/mp/"       # optional: links containing this are third-party sellers

Works only for server-rendered pages; JavaScript-rendered pages return an empty shell and are
reported in the log. robots.txt and bot-blocking are handled by Source.get().
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from deal_agent.models import Deal
from deal_agent.parsing import parse_discount, parse_price
from deal_agent.sources.base import Source, SourceBlocked, register

log = logging.getLogger(__name__)


def _text(node, selector: str | None) -> str:
    if not selector:
        return ""
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


@register("html")
class HtmlSource(Source):
    def fetch(self) -> list[Deal]:
        sel = self.config.get("selectors") or {}
        if not sel.get("item") or not sel.get("title"):
            log.error("[%s] html source needs selectors.item and selectors.title", self.name)
            return []
        require = re.compile(self.config["require"], re.I) if self.config.get("require") else None
        mp_link = self.config.get("marketplace_link")
        deals: list[Deal] = []
        for url in self.urls:
            try:
                resp = self.get(url)
            except SourceBlocked:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] fetch failed for %s: %s", self.name, url, exc)
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(sel["item"])
            if not items:
                log.warning("[%s] no items matched %r on %s (page may be JS-rendered or layout changed)",
                            self.name, sel["item"], url)
            domain = self.config.get("shop_domain") or urlsplit(resp.url).netloc.removeprefix("www.")
            for node in items:
                title = _text(node, sel["title"])
                link_el = node.select_one(sel.get("link") or "a[href]")
                href = link_el.get("href") if link_el else None
                if not title or not href or (require and not require.search(title)):
                    continue
                img_el = node.select_one(sel["image"]) if sel.get("image") else None
                deals.append(Deal(
                    title=title,
                    link=urljoin(resp.url, href),
                    source=self.name,
                    price=parse_price(_text(node, sel.get("price"))),
                    original_price=parse_price(_text(node, sel.get("original_price"))),
                    discount_pct=parse_discount(_text(node, sel.get("discount"))),
                    image_url=urljoin(resp.url, img_el.get("src")) if img_el and img_el.get("src") else None,
                    merchant=self.merchant,
                    shop_domain=domain,
                    trust_hint="marketplace" if mp_link and mp_link in href else self.config.get("trust"),
                ))
            log.info("[%s] %d items from %s", self.name, len(items), url)
        return deals
