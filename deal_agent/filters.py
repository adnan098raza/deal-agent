"""Audience (gender/age) detection for the men's-only categories.

Clothing, shoes and jewelry/accessories should be men's (or unisex) items; cosmetics are not
restricted. Detection here is keyword based; the LLM also returns an `audience` and the
pipeline applies the same rule to it.
"""

from __future__ import annotations

from deal_agent.parsing import normalize

# matched as substrings of words (German compounds: "Damenuhr", "Kinderschuhe", "Herrenjacke")
_WOMEN_SUB = ["damen", "frauen", "madchen", "women", "womens", "ladies", "schwangerschaft", "umstands"]
_KIDS_SUB = ["kinder", "kleinkind", "jugend"]
_MEN_SUB = ["herren", "manner", "maenner"]
# matched as whole words
_WOMEN_WORDS = {"woman", "lady", "girl", "girls", "she", "wmns", "w"}
_KIDS_WORDS = {"kids", "kid", "baby", "babys", "babies", "junior", "juniors", "teens", "jungen", "boys",
               "toddler", "gs", "ps", "td"}            # GS/PS/TD = Nike/Jordan kids' sizing
_MEN_WORDS = {"men", "mens", "man", "male", "unisex", "m"}
_PHRASES_WOMEN = ["fur sie", "for her"]
_PHRASES_MEN = ["fur ihn", "for him"]
# garments that are (almost) always women's when no men's marker is present
_WOMEN_ITEMS = {"kleid", "kleider", "rock", "rocke", "bluse", "blusen", "bh", "bhs", "dessous", "bikini",
                "badeanzug", "handtasche", "pumps", "ballerinas", "strumpfhose", "leggings", "highheels",
                "sport bh", "negligee", "tankini"}
# the same inside compounds ("Sommerkleid", "Seidenbluse"); "kleidung" is excluded explicitly
_WOMEN_ITEM_SUB = ["kleid", "bluse", "dessous", "bikini", "handtasche", "strumpfhose", "badeanzug"]


def detect_audience(text: str) -> str | None:
    norm = normalize(text)
    words = norm.split()
    wset = set(words)
    padded = f" {norm} "

    def sub(keys):
        return any(k in w for w in words for k in keys)

    women = sub(_WOMEN_SUB) or bool(wset & _WOMEN_WORDS - {"w"}) or any(f" {p} " in padded for p in _PHRASES_WOMEN)
    kids = sub(_KIDS_SUB) or bool(wset & _KIDS_WORDS)
    men = sub(_MEN_SUB) or bool(wset & (_MEN_WORDS - {"m"})) or any(f" {p} " in padded for p in _PHRASES_MEN)
    if not (women or men) and (wset & _WOMEN_ITEMS or any(
            k in w for w in words for k in _WOMEN_ITEM_SUB if "kleidung" not in w)):
        women = True
    if men and (women or kids):
        return "unisex"
    if men:
        return "men"
    if women and kids:
        return "kids"
    if women:
        return "women"
    if kids:
        return "kids"
    return None


def gender_block_reason(category: str | None, audience: str | None, men_only: list[str]) -> str | None:
    """Reason string if the deal must be dropped, else None."""
    if category not in men_only:
        return None
    if audience == "women":
        return "women's item (men's only for this category)"
    if audience == "kids":
        return "kids' item (men's only for this category)"
    return None
