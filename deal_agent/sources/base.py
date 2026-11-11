"""Source plugin base class and registry.

A source turns some remote resource into a list of `Deal`s. Register built-in types with
`@register("name")`; third-party sources can be referenced from sources.yaml as
`type: "my_package.my_module:MySource"` without touching this package.

Sources that block automated access (HTTP 401/403/429, captcha pages, robots.txt) raise
`SourceBlocked`; the pipeline logs it, records it and backs off for that source.
"""

from __future__ import annotations

import importlib
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlsplit

import requests

from deal_agent.config import Preferences
from deal_agent.http import PoliteHttp, RobotsDisallowed
from deal_agent.models import Deal
from deal_agent.parsing import enrich

log = logging.getLogger(__name__)

REGISTRY: dict[str, type["Source"]] = {}
_BLOCK_MARKERS = re.compile(r"captcha|px-captcha|cf-chl-|Access Denied|Pardon Our Interruption|"
                            r"Request unsuccessful\. Incapsula|Please enable JS and disable any ad blocker", re.I)


class SourceBlocked(Exception):
    """The site refused automated access; back off instead of retrying immediately."""


def register(type_name: str):
    def deco(cls: type[Source]) -> type[Source]:
        cls.type_name = type_name
        REGISTRY[type_name] = cls
        return cls
    return deco


class Source(ABC):
    type_name: ClassVar[str] = "base"
    has_detail: ClassVar[bool] = False

    def __init__(self, config: dict, http: PoliteHttp, prefs: Preferences, tz: str = "Europe/Berlin"):
        self.config = config
        self.name: str = config.get("name") or self.type_name
        self.http = http
        self.prefs = prefs
        self.tz = tz
        self.min_interval = float(config.get("min_interval_minutes", 0)) * 60
        self.default_category: str | None = config.get("category")
        self.merchant: str | None = config.get("merchant")
        self.trust_hint: str | None = config.get("trust")
        self._last_fetch = 0.0

    @property
    def urls(self) -> list[str]:
        urls = list(self.config.get("urls") or [])
        if self.config.get("url"):
            urls.insert(0, self.config["url"])
        return urls

    def due(self) -> bool:
        return time.time() - self._last_fetch >= self.min_interval

    def get(self, url: str) -> requests.Response:
        """robots.txt-checked, throttled GET that turns bot-blocking into SourceBlocked."""
        try:
            resp = self.http.get(url, raise_for_status=False)
        except RobotsDisallowed as exc:
            raise SourceBlocked(str(exc)) from exc
        if resp.status_code in (401, 403, 429, 451):
            raise SourceBlocked(f"HTTP {resp.status_code} from {urlsplit(url).netloc}")
        if resp.status_code >= 400:
            resp.raise_for_status()
        if _BLOCK_MARKERS.search(resp.text[:5000]) and len(resp.text) < 60_000:
            raise SourceBlocked(f"bot-protection page from {urlsplit(url).netloc}")
        return resp

    @abstractmethod
    def fetch(self) -> list[Deal]:
        """Fetch raw deals. Implementations should not call enrich(); collect() does."""

    def detail(self, deal: Deal) -> dict | None:
        """Optional: fetch the deal's own page and return extra fields (see MydealzSource)."""
        return None

    def collect(self, force: bool = False) -> list[Deal]:
        if not force and not self.due():
            log.debug("Source %s fetched recently; skipping", self.name)
            return []
        deals = self.fetch()
        self._last_fetch = time.time()
        for d in deals:
            if d.category is None and self.default_category:
                d.category = self.default_category
            if d.merchant is None and self.merchant:
                d.merchant = self.merchant
            if d.trust_hint is None and self.trust_hint:
                d.trust_hint = self.trust_hint
            if d.shop_domain is None and self.config.get("shop_domain"):
                d.shop_domain = self.config["shop_domain"]
            enrich(d, self.prefs, tz=self.tz)
        return deals


def _resolve(type_name: str) -> type[Source]:
    if type_name in REGISTRY:
        return REGISTRY[type_name]
    if ":" in type_name:
        module_name, cls_name = type_name.split(":", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
        if not (isinstance(cls, type) and issubclass(cls, Source)):
            raise TypeError(f"{type_name} is not a Source subclass")
        return cls
    raise ValueError(f"Unknown source type {type_name!r}. Known: {sorted(REGISTRY)}")


def build_sources(configs: list[dict], http: PoliteHttp, prefs: Preferences,
                  tz: str = "Europe/Berlin", include_disabled: bool = False) -> list[Source]:
    sources: list[Source] = []
    for cfg in configs:
        if not include_disabled and not cfg.get("enabled", True):
            continue
        try:
            cls = _resolve(str(cfg.get("type", "rss")))
            sources.append(cls(cfg, http, prefs, tz=tz))
        except Exception as exc:  # noqa: BLE001 - one bad entry must not kill the agent
            log.error("Could not load source %s: %s", cfg.get("name"), exc)
    return sources
