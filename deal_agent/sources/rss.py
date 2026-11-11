"""Generic RSS/Atom source, with extra support for Pepper-network feeds (mydealz.de)."""

from __future__ import annotations

import logging
from calendar import timegm
from datetime import datetime, timezone

import feedparser

from deal_agent.models import Deal
from deal_agent.parsing import parse_price, strip_html
from deal_agent.sources.base import Source, SourceBlocked, register

log = logging.getLogger(__name__)


def _ts(struct) -> datetime | None:
    return datetime.fromtimestamp(timegm(struct), tz=timezone.utc) if struct else None


@register("rss")
class RssSource(Source):
    def fetch(self) -> list[Deal]:
        deals: list[Deal] = []
        blocked: list[str] = []
        for url in self.urls:
            try:
                resp = self.get(url)
            except SourceBlocked as exc:
                log.warning("[%s] skipped: %s", self.name, exc)
                blocked.append(str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] fetch failed for %s: %s", self.name, url, exc)
                continue
            feed = feedparser.parse(resp.content)
            if feed.bozo and not feed.entries:
                log.error("[%s] could not parse feed %s: %s", self.name, url, feed.bozo_exception)
                continue
            for entry in feed.entries:
                deal = self.entry_to_deal(entry)
                if deal:
                    deals.append(deal)
            log.info("[%s] %d items from %s", self.name, len(feed.entries), url)
        if blocked and len(blocked) == len(self.urls):
            raise SourceBlocked("; ".join(blocked))
        return deals

    def entry_to_deal(self, e) -> Deal | None:
        title = (e.get("title") or "").strip()
        link = e.get("link") or e.get("id")
        if not title or not link:
            return None
        merchant = price = None
        pm = e.get("pepper_merchant")             # <pepper:merchant name=".." price=".."/>
        if isinstance(pm, dict):
            merchant = pm.get("name") or None
            price = parse_price(pm.get("price") or "")
        tags = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        image = None
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url")
                break
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e["content"][0].get("value", "")
        return Deal(
            title=title,
            link=link,
            source=self.name,
            price=price,
            merchant=merchant,
            description=strip_html(summary, limit=600),
            image_url=image,
            published_at=_ts(e.get("published_parsed") or e.get("updated_parsed")),
            source_category=", ".join(tags) or None,
        )
