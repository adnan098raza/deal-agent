"""Product identity across shops: canonical keys, "lowest in 90 days" and best-offer picking."""

from __future__ import annotations

import re

from deal_agent.models import Deal
from deal_agent.parsing import normalize

_STOP = {
    "fur", "bei", "mit", "und", "oder", "inkl", "inklusive", "statt", "nur", "ab", "von", "versand", "versandkostenfrei",
    "gratis", "kostenlos", "der", "die", "das", "den", "dem", "ein", "eine", "im", "in", "am", "zum", "zur", "auf",
    "gr", "grosse", "grossen", "grosen", "size", "sizes", "eur", "euro", "prime", "amazon", "ebay", "otto", "mediamarkt",
    "saturn", "deal", "sale", "angebot", "neu", "new", "verschiedene", "farben", "colors", "set", "z", "b", "zb",
    "schwarz", "weiss", "black", "white", "grau", "grey", "gray", "blau", "blue", "rot", "red", "grun", "green",
    "beige", "braun", "brown", "navy", "pink", "rosa", "silber", "silver", "gold", "the", "for", "and", "with",
    "herren", "men", "mens", "unisex", "ca", "via", "lokal", "online", "abholung", "cb", "shoop",
}
_CUT = re.compile(r"\s(?:für|fur|bei|ab|statt|um|zum preis von|@|\||–|—)\s|\s-\s", re.I)
_PRICE = re.compile(r"\d+(?:[.,]\d+)?\s?(?:€|eur\b)|(?:€|eur)\s?\d+(?:[.,]\d+)?", re.I)
_BRACKETS = re.compile(r"^\s*(?:\[[^\]]*\]\s*|\([^)]*\)\s*)+")


def canonical(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text))


def heuristic_key(title: str, brand: str | None = None) -> str | None:
    t = _BRACKETS.sub("", title or "")
    t = _CUT.split(t, maxsplit=1)[0]
    t = _PRICE.sub(" ", t)
    t = re.sub(r"\b(?:gr\.?|größen?|grossen?)\s*[\d,./\s\-–&]+", " ", t, flags=re.I)  # size ranges
    toks = [w for w in normalize(t).split() if w not in _STOP and not re.fullmatch(r"\d{1,2}", w)]
    if brand:
        b = normalize(brand).split()
        toks = [w for w in toks if w not in b]
        toks = b + toks
    toks = toks[:6]
    if len(toks) < 2:
        return None
    return "".join(toks)


def product_key(deal: Deal, llm_product: str | None = None) -> str | None:
    if deal.ean:
        return f"ean{deal.ean}"
    if llm_product:
        key = canonical(llm_product)
        if len(key) >= 4:
            return key
    return heuristic_key(deal.title, deal.brand)


TRUST_RANK = {"official_store": 0, "major_retailer": 1, "marketplace": 2, "unknown": 3, None: 3}


def offer_sort_key(d: Deal) -> tuple:
    trusted = d.trust_label in ("official_store", "major_retailer") and not d.warnings
    price = d.final_price_est if d.final_price_est is not None else (d.effective_price or 1e9)
    return (0 if trusted else 1, price, TRUST_RANK.get(d.trust_label, 3))


def best_offer(offers: list[Deal]) -> tuple[Deal, list[Deal]]:
    """Cheapest trustworthy offer first; the rest are 'also at' alternatives."""
    ranked = sorted(offers, key=offer_sort_key)
    return ranked[0], ranked[1:]


def lowest_badge(stats: dict, price: float | None, min_obs: int = 2, min_span_days: int = 3) -> str | None:
    """'Lowest in 90 days' only when there is enough history to make the claim meaningful."""
    if price is None or not stats or stats.get("min") is None:
        return None
    if stats["count"] < min_obs or not stats.get("first") or not stats.get("last"):
        return None
    if (stats["last"] - stats["first"]).days < min_span_days:
        return None
    return "Lowest in 90 days" if price <= stats["min"] + 0.005 else None
