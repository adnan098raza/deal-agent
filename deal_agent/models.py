"""Plain data objects shared across the agent."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

TRUST_LABELS = {
    "official_store": "Official brand store",
    "major_retailer": "Major retailer",
    "marketplace": "Marketplace (3rd-party seller)",
    "unknown": "Unknown shop",
}
STATUSES = ("Live", "Price changed", "Expired", "Out of stock")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_link(link: str) -> str:
    """Strip query/fragment so tracking params don't create duplicate deals."""
    parts = urlsplit(link.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def link_uid(link: str) -> str:
    return hashlib.sha1(canonical_link(link).encode()).hexdigest()[:20]


@dataclass
class Deal:
    title: str
    link: str
    source: str
    price: float | None = None
    original_price: float | None = None
    discount_pct: float | None = None
    currency: str = "EUR"
    brand: str | None = None
    category: str | None = None
    merchant: str | None = None           # shop name as shown by the source
    shop_domain: str | None = None        # e.g. bestsecret.com
    description: str = ""
    image_url: str | None = None
    expires_at: datetime | None = None
    published_at: datetime | None = None
    temperature: int | None = None        # mydealz community "heat" (votes)
    source_category: str | None = None    # category label as given by the source
    coupon_code: str | None = None
    shipping: float | None = None
    market_price: float | None = None     # comparison price (idealo via mydealz, UVP, ...)
    seller_name: str | None = None        # marketplace seller (MediaMarkt, eBay, ...)
    seller_rating: float | None = None    # 0-5
    ean: str | None = None
    comment_count: int | None = None
    trust_hint: str | None = None         # source-level hint: marketplace / comparison / retailer
    # derived / stored
    id: int | None = None
    score: float | None = None
    score_reason: str | None = None
    audience: str | None = None           # men / women / kids / unisex
    product_key: str | None = None
    trust_label: str | None = None
    trust_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    student: dict | None = None           # {program, pct, stacks, final_est, note, url}
    final_price_est: float | None = None
    status: str = "Live"
    current_price: float | None = None
    last_checked: datetime | None = None
    detail_fetched: bool = False
    notified: bool = False
    dismissed: bool = False
    snoozed_until: datetime | None = None
    penalty: float = 0.0
    queued_digest: bool = False           # held during quiet hours for the next digest
    scored_at: datetime | None = None
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return link_uid(self.link)

    @property
    def effective_price(self) -> float | None:
        return self.current_price if self.current_price is not None else self.price

    @property
    def shop(self) -> str:
        name = self.merchant or ""
        if self.seller_name and self.seller_name.casefold() not in name.casefold():
            name = f"{name} – seller {self.seller_name}" if name else self.seller_name
        if self.shop_domain and self.shop_domain.casefold() not in name.casefold().replace(" ", ""):
            name = f"{name} ({self.shop_domain})" if name else self.shop_domain
        return name or "?"

    @property
    def trust_text(self) -> str:
        return TRUST_LABELS.get(self.trust_label or "unknown", "Unknown shop")

    def time_left(self, now: datetime | None = None) -> str:
        return format_time_left(self.expires_at, now)


def format_time_left(expires_at: datetime | None, now: datetime | None = None) -> str:
    if expires_at is None:
        return "unknown"
    delta = expires_at - (now or utcnow())
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "expired"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


@dataclass
class WatchItem:
    query: str
    brand: str | None = None
    category: str | None = None
    max_price: float | None = None
    min_discount: float | None = None
    active: bool = True
    id: int | None = None
    created_at: datetime | None = None

    def describe(self) -> str:
        bits = [repr(self.query) if self.query else "(any product)"]
        if self.brand:
            bits.append(f"brand={self.brand}")
        if self.category:
            bits.append(f"category={self.category}")
        if self.max_price is not None:
            bits.append(f"max {self.max_price:.2f}€")
        if self.min_discount:
            bits.append(f"min -{self.min_discount:.0f}%")
        return " ".join(bits)

    def key(self) -> tuple:
        return ((self.query or "").casefold(), (self.brand or "").casefold(), self.category or "",
                self.max_price, self.min_discount)


@dataclass
class ScoreResult:
    score: float
    reason: str
    relevance: float | None = None
    quality: float | None = None
    brand: str | None = None
    category: str | None = None
    expires_at: datetime | None = None
    audience: str | None = None
    product: str | None = None            # canonical product name from the LLM
    is_match: bool = True                 # used for watchlist verification
    fallback: bool = False                # heuristic used because the LLM failed
