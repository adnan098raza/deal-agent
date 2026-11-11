"""Deal sources. Importing this package registers the built-in source types."""

from deal_agent.sources.base import REGISTRY, Source, SourceBlocked, build_sources, register
from deal_agent.sources import html, jsonld, mediamarkt, mydealz, rss  # noqa: F401  (registration)

__all__ = ["REGISTRY", "Source", "SourceBlocked", "build_sources", "register"]
