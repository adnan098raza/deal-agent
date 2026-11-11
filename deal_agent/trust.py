"""Seller trust labels and scam warnings.

Labels: official_store, major_retailer, marketplace, unknown.
Warnings are heuristics, not verdicts: price far below the market/comparison price, unknown
shop, weak seller rating, or a community that voted the deal down. Shops can additionally be
checked by hand with the Verbraucherzentrale Fakeshop-Finder (it has no public API and is
built for manual use, so the agent links to it instead of automating it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from deal_agent.config import load_yaml
from deal_agent.models import Deal
from deal_agent.parsing import normalize

FAKESHOP_FINDER_URL = "https://www.verbraucherzentrale.de/fakeshopfinder-71560"
DATA = Path(__file__).resolve().parent / "data" / "retailers.yaml"


@dataclass
class TrustResult:
    label: str
    reason: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Catalog:
    retailer_names: dict[str, str]     # normalized name -> display
    retailer_domains: dict[str, str]   # domain -> display
    market_names: dict[str, str]
    market_domains: dict[str, str]
    markers: list[str]
    brand_domains: dict[str, str]      # domain -> brand


def _domain(d: str | None) -> str:
    d = (d or "").lower().strip()
    return d[4:] if d.startswith("www.") else d


@lru_cache(maxsize=4)
def _catalog(path: str = str(DATA)) -> _Catalog:
    data = load_yaml(Path(path))

    def split(section):
        names, domains = {}, {}
        for name, doms in (data.get(section) or {}).items():
            names[normalize(str(name))] = str(name)
            for dom in doms or []:
                domains[_domain(str(dom))] = str(name)
        return names, domains

    rn, rd = split("major_retailers")
    mn, md = split("marketplaces")
    brands = {}
    for brand, doms in (data.get("brand_stores") or {}).items():
        for dom in doms or []:
            brands[_domain(str(dom))] = str(brand)
    return _Catalog(rn, rd, mn, md, [normalize(m) for m in data.get("marketplace_markers") or []], brands)


def _match_domain(domain: str, table: dict[str, str]) -> str | None:
    """'shop.adidas.de' matches 'adidas.de'."""
    while domain:
        if domain in table:
            return table[domain]
        if "." not in domain:
            return None
        domain = domain.split(".", 1)[1]
        if domain.count(".") == 0:   # don't match bare TLDs
            return table.get(domain)
    return None


def classify(deal: Deal, known_brands: list[str] | None = None) -> TrustResult:
    cat = _catalog()
    dom = _domain(deal.shop_domain)
    merchant = normalize(deal.merchant or "")
    text = normalize(f"{deal.title} {deal.merchant or ''} {deal.description[:200]}")

    # 1. third-party marketplace sellers (explicit seller, marketplace hint or markers)
    if deal.seller_name or deal.trust_hint == "marketplace":
        seller = f" (seller: {deal.seller_name})" if deal.seller_name else ""
        return TrustResult("marketplace", f"third-party seller on {deal.merchant or dom or 'a marketplace'}{seller}")
    if any(m and f" {m} " in f" {text} " for m in cat.markers):
        return TrustResult("marketplace", "sold by a marketplace third-party seller")
    if dom and _match_domain(dom, cat.market_domains):
        return TrustResult("marketplace", f"{_match_domain(dom, cat.market_domains)} marketplace")
    if merchant and merchant in cat.market_names:
        return TrustResult("marketplace", f"{cat.market_names[merchant]} marketplace")

    # 2. official brand store
    brand = _match_domain(dom, cat.brand_domains) if dom else None
    if brand:
        return TrustResult("official_store", f"official {brand} store ({dom})")
    brands = {normalize(b): b for b in (known_brands or []) + sorted(set(cat.brand_domains.values()))}
    if merchant and merchant in brands and merchant not in cat.retailer_names:
        return TrustResult("official_store", f"official {brands[merchant]} store")

    # 3. major retailer
    if dom and _match_domain(dom, cat.retailer_domains):
        return TrustResult("major_retailer", f"{_match_domain(dom, cat.retailer_domains)} ({dom})")
    if merchant and merchant in cat.retailer_names:
        return TrustResult("major_retailer", cat.retailer_names[merchant])
    if merchant and merchant in brands:
        return TrustResult("official_store", f"official {brands[merchant]} store")

    if deal.trust_hint == "comparison":
        return TrustResult("unknown", "price comparison result – the seller is chosen on the comparison site")
    return TrustResult("unknown", f"unknown shop{f' ({dom})' if dom else (f' ({deal.merchant})' if deal.merchant else '')}")


def assess(deal: Deal, known_brands: list[str] | None = None, below_market_pct: float = 50,
           fakeshop_link: bool = True) -> TrustResult:
    res = classify(deal, known_brands)
    w: list[str] = []
    price = deal.effective_price
    untrusted = res.label in ("unknown", "marketplace")

    if price and deal.market_price and deal.market_price > 0:
        ratio = price / deal.market_price
        if ratio < 0.3:
            w.append(f"price is {100 - ratio * 100:.0f}% below the market price ({deal.market_price:.2f} €) – "
                     "possible price error or scam")
        elif untrusted and ratio < 1 - below_market_pct / 100:
            w.append(f"price far below market ({price:.2f} € vs {deal.market_price:.2f} €)")
    if untrusted and deal.discount_pct and deal.discount_pct >= 70:
        w.append(f"very high discount (-{deal.discount_pct:.0f}%) from a {('marketplace seller' if res.label == 'marketplace' else 'shop we do not know')}")
    if deal.seller_rating is not None and deal.seller_rating < 4.0:
        w.append(f"low seller rating ({deal.seller_rating:.1f}/5)")
    if res.label == "unknown" and deal.trust_hint != "comparison":
        nothing = (deal.temperature is None or deal.temperature < 20) and not deal.comment_count
        w.append("unknown shop" + (" with no reviews/community feedback yet" if nothing else "")
                 + (f" – check it: {FAKESHOP_FINDER_URL}" if fakeshop_link else ""))
    if deal.temperature is not None and deal.temperature < 0:
        w.append(f"community voted this deal cold ({deal.temperature}°)")
    res.warnings = w
    return res
