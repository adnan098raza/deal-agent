"""Cheap, deterministic pre-matching of deals against watchlist items.

Every candidate that passes here is then verified by the scorer (LLM or heuristic), which
filters out accessories and wrong models.
"""

from __future__ import annotations

from deal_agent.models import Deal, WatchItem
from deal_agent.parsing import contains_phrase, normalize


# English <-> German <-> Italian product words that mean the same thing (already normalized: no umlauts,
# lower-case). A query word from a group matches a title containing any word of that group.
SYNONYM_GROUPS: list[tuple[str, ...]] = [
    ("mouse", "mice", "maus", "mause"),
    ("headphones", "headphone", "kopfhorer", "cuffie", "cuffia"),
    ("earbuds", "earphones", "ohrhorer", "auricolari"),
    ("keyboard", "tastatur", "tastiera"),
    ("monitor", "bildschirm"),
    ("laptop", "notebook", "portatile"),
    ("speaker", "lautsprecher", "altoparlante"),
    ("smartphone", "handy", "cellulare"),
    ("charger", "ladegerat", "caricabatterie", "caricatore"),
    ("vacuum", "staubsauger", "aspirapolvere"),
    ("backpack", "rucksack", "zaino"),
    ("shoes", "schuhe", "scarpe"),
    ("jacket", "jacke", "giacca", "giubbotto"),
    ("perfume", "parfum", "profumo"),
    ("watch", "uhr", "orologio", "orologi"),
    ("tv", "fernseher", "televisore"),
]
_SYNONYMS: dict[str, tuple[str, ...]] = {w: g for g in SYNONYM_GROUPS for w in g}


def _word_hit(token: str, words: list[str], compound: bool = False) -> bool:
    if len(token) <= 3 or token.isdigit():
        return token in words               # short tokens / model numbers: exact word
    # prefix match ("sneaker" ~ "sneakers"); for synonym words also the end of a German
    # compound ("Gamingmaus", "Funkkopfhörer", "Smartwatch")
    return any(w.startswith(token) or (compound and w.endswith(token)) for w in words)


def token_matches(token: str, words: list[str]) -> bool:
    group = _SYNONYMS.get(token)
    if group is None:
        return _word_hit(token, words)
    return any(_word_hit(v, words, compound=True) for v in group)


def matches(item: WatchItem, deal: Deal) -> bool:
    title = normalize(f"{deal.title} {deal.brand or ''}")
    words = title.split()

    # every query token (or one of its synonyms) must appear as a word
    for token in normalize(item.query).split():
        if not token_matches(token, words):
            return False

    if item.brand:
        brand_ok = (deal.brand and normalize(deal.brand) == normalize(item.brand)) or contains_phrase(title, item.brand)
        if not brand_ok:
            return False

    # a category-restricted entry needs a deal known to be in that category: unclassified deals
    # (microSD "TF card" vs. "TF" football shoes) would otherwise slip through
    if item.category and deal.category != item.category:
        return False

    if item.max_price is not None:
        if deal.price is None or deal.price > item.max_price:
            return False

    if item.min_discount:
        if deal.discount_pct is None or deal.discount_pct < item.min_discount:
            return False

    return bool(item.query.strip() or item.brand or item.category)


def find_matches(items: list[WatchItem], deals: list[Deal]) -> list[tuple[WatchItem, Deal]]:
    return [(w, d) for w in items for d in deals if matches(w, d)]
