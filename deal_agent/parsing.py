"""Heuristic extraction of prices, discounts, expiry, brand and category from deal text.

Deal sites in Germany use formats like "1.299,99 €", "549€", "statt 299€", "UVP 79,99 €",
"-25%" and "gültig bis 12.11.". The LLM can refine these later, but cheap regexes give the
scorer and the watchlist matcher something to work with even without an API key.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

_NUM = r"\d{1,3}(?:[.   ]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CUR_AFTER = re.compile(rf"(?<![\d.,])({_NUM})\s?(?:€|eur\b|euro\b)", re.I)
_CUR_BEFORE = re.compile(rf"(?:€|eur\b)\s?({_NUM})(?![\d])", re.I)
_ORIG_KW = re.compile(
    rf"(?:statt|uvp|pvg|vgp|vergleichspreis|vorher|bisher|regulär|normalpreis|ursprünglich)"
    rf"[:\s]*(?:ca\.?\s*)?(?:€\s?({_NUM})|({_NUM})\s?(?:€|eur\b|euro\b))",
    re.I,
)
_DISCOUNT = re.compile(r"(?:-\s?(\d{1,2}(?:[.,]\d)?)\s?%|(\d{1,2}(?:[.,]\d)?)\s?%\s?(?:rabatt|off|reduziert|günstiger|sparen))", re.I)
_TEMPERATURE = re.compile(r"^\s*(-?\d{1,5})\s?°\s*[-–]\s*")
# "08.11.2026 - 09.11.2026" / "01.11. – 14.11.2026": a validity range, the second date is the end
_RANGE = re.compile(r"\b\d{1,2}\.\d{1,2}\.(?:\d{2,4})?\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})?()()")
_DATE = re.compile(
    r"(?:bis(?:\s+(?:zum|einschl\.?|einschließlich|inkl\.?))?|gültig bis|endet(?:\s+am)?|ends?|until|läuft bis)"
    r"\s*(?:(?:mo|di|mi|do|fr|sa|so)[a-z]*\.?,?\s*)?"
    r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})?"
    r"(?:\s*(?:um|,)?\s*(\d{1,2})[:.](\d{2})\s*(?:uhr)?)?",
    re.I,
)
_TODAY_ONLY = re.compile(r"\b(nur (?:noch )?heute|heute bis mitternacht|today only)\b", re.I)


def to_float(num: str) -> float | None:
    s = num.strip().replace(" ", "").replace(" ", "").replace(" ", "")
    if "," in s:                       # German: 1.299,99
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):  # 1.299 (thousands)
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def find_prices(text: str) -> list[float]:
    """All euro amounts in text, in order of appearance."""
    hits: list[tuple[int, float]] = []
    for rx in (_CUR_AFTER, _CUR_BEFORE):
        for m in rx.finditer(text or ""):
            v = to_float(m.group(1))
            if v is not None and 0 < v < 100_000:
                hits.append((m.start(), v))
    hits.sort()
    return [v for _, v in hits]


def parse_price(text: str) -> float | None:
    prices = find_prices(text)
    return prices[0] if prices else None


def parse_original_price(text: str) -> float | None:
    m = _ORIG_KW.search(text or "")
    return to_float(m.group(1) or m.group(2)) if m else None


def parse_discount(text: str) -> float | None:
    m = _DISCOUNT.search(text or "")
    if not m:
        return None
    return to_float(m.group(1) or m.group(2))


def compute_discount(price: float | None, original: float | None) -> float | None:
    if price is None or not original or original <= price:
        return None
    return round((original - price) / original * 100, 1)


def split_temperature(title: str) -> tuple[int | None, str]:
    """mydealz prefixes titles with community heat, e.g. '345° - Sony ...'."""
    m = _TEMPERATURE.match(title or "")
    if not m:
        return None, title
    return int(m.group(1)), title[m.end():].strip()


def parse_expiry(text: str, now: datetime | None = None, tz: str = "Europe/Berlin") -> datetime | None:
    """Best-effort expiry from phrases like 'gültig bis 12.11.' or 'nur heute'. Returns UTC."""
    zone = ZoneInfo(tz)
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    text = text or ""
    m = _DATE.search(text) or _RANGE.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (23, 59)
        if year:
            y = int(year)
            y = y + 2000 if y < 100 else y
        else:
            y = local_now.year
        try:
            dt = datetime(y, month, day, hour, minute, tzinfo=zone)
        except ValueError:
            return None
        if not year and dt < local_now - timedelta(days=1):
            dt = dt.replace(year=y + 1)          # "bis 03.01." seen in December
        return dt.astimezone(timezone.utc)
    if _TODAY_ONLY.search(text):
        end = local_now.replace(hour=23, minute=59, second=0, microsecond=0)
        return end.astimezone(timezone.utc)
    return None


def strip_html(markup: str, limit: int | None = None) -> str:
    if not markup:
        return ""
    text = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit] if limit else text


def normalize(text: str) -> str:
    """Lower-case, strip accents and punctuation: 'Lancôme Rénergie!' -> 'lancome renergie'."""
    t = unicodedata.normalize("NFKD", (text or "").casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def contains_phrase(haystack_norm: str, phrase: str) -> bool:
    p = normalize(phrase)
    return bool(p) and f" {p} " in f" {haystack_norm} "


def detect_brand(text: str, brands: list[str]) -> str | None:
    norm = normalize(text)
    for b in sorted(brands, key=len, reverse=True):   # "Hugo Boss" before "Boss"
        if contains_phrase(norm, b):
            return b
    return None


def keyword_hit(norm_text: str, keyword: str) -> bool:
    """Keyword match tuned for German product titles (norm_text must be normalize()d)."""
    k = normalize(keyword)
    if not k:
        return False
    if " " in k:
        return contains_phrase(norm_text, k)
    words = norm_text.split()
    if len(k) >= 5:                  # substring: German compounds ("Feuchtigkeitscreme")
        return any(k in w for w in words)
    return any(w.startswith(k) for w in words)   # short: prefix only ("tv" mustn't hit "atv")


def detect_category(text: str, categories: dict[str, list[str]]) -> str | None:
    norm = normalize(text)
    best, best_hits = None, 0
    for name, keywords in categories.items():
        hits = sum(keyword_hit(norm, kw) for kw in keywords)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


def enrich(deal, prefs, tz: str = "Europe/Berlin"):
    """Fill missing fields of a Deal using the heuristics above. Mutates and returns it."""
    temp, clean_title = split_temperature(deal.title)
    if temp is not None:
        deal.temperature = deal.temperature if deal.temperature is not None else temp
        deal.title = clean_title
    text = f"{deal.title} {deal.description}"
    if deal.price is None:
        deal.price = parse_price(deal.title) or parse_price(deal.description)
    if deal.original_price is None:
        deal.original_price = parse_original_price(text)
        if deal.original_price is not None and deal.price is not None and deal.original_price <= deal.price:
            deal.original_price = None
    if deal.discount_pct is None:
        deal.discount_pct = compute_discount(deal.price, deal.original_price) or parse_discount(text)
    if deal.original_price is None and deal.price and deal.discount_pct and 0 < deal.discount_pct < 100:
        deal.original_price = round(deal.price / (1 - deal.discount_pct / 100), 2)
    if deal.expires_at is None:
        deal.expires_at = parse_expiry(text, tz=tz)
    if deal.brand is None:
        deal.brand = detect_brand(f"{deal.title} {deal.merchant or ''}", prefs.all_brands)
    if deal.category is None:
        deal.category = detect_category(f"{deal.source_category or ''} {deal.title}", prefs.categories)
    return deal
