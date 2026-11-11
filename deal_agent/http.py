"""Polite HTTP client: honours robots.txt (incl. Crawl-delay) and rate-limits per host."""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    pass


class PoliteHttp:
    def __init__(self, user_agent: str, min_delay: float = 2.0, robots_ttl: float = 24 * 3600,
                 timeout: float = 20.0):
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.robots_ttl = robots_ttl
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        })
        self._robots: dict[str, tuple[float, RobotFileParser | None, bool]] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ robots.txt
    def _robots_for(self, url: str) -> tuple[RobotFileParser | None, bool]:
        """Returns (parser, allow_all). parser None + allow_all False means deny all."""
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(base)
        if cached and time.time() - cached[0] < self.robots_ttl:
            return cached[1], cached[2]
        rp, allow_all = None, False
        try:
            resp = self.session.get(f"{base}/robots.txt", timeout=self.timeout)
            if resp.status_code >= 500:
                log.warning("robots.txt for %s returned %s; treating site as disallowed", base, resp.status_code)
            elif resp.status_code >= 400:
                allow_all = True        # no robots.txt => everything allowed (RFC 9309)
            else:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            log.warning("Could not fetch robots.txt for %s (%s); treating as disallowed", base, exc)
        self._robots[base] = (time.time(), rp, allow_all)
        return rp, allow_all

    def allowed(self, url: str) -> bool:
        rp, allow_all = self._robots_for(url)
        if allow_all:
            return True
        if rp is None:
            return False
        return rp.can_fetch(self.user_agent, url)

    def _crawl_delay(self, url: str) -> float:
        rp, _ = self._robots_for(url)
        delay = None
        if rp is not None:
            try:
                delay = rp.crawl_delay(self.user_agent)
            except Exception:  # noqa: BLE001 - malformed directive
                delay = None
        return max(self.min_delay, float(delay or 0))

    # ------------------------------------------------------------------ fetch
    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = self._crawl_delay(url)
        with self._lock:
            wait = self._last_hit.get(host, 0) + delay - time.time()
            self._last_hit[host] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, raise_for_status: bool = True, **kwargs) -> requests.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        self._throttle(url)
        resp = self.session.get(url, timeout=kwargs.pop("timeout", self.timeout), **kwargs)
        if resp.url != url and not self.allowed(resp.url):
            raise RobotsDisallowed(f"robots.txt disallows redirect target {resp.url}")
        if raise_for_status:
            resp.raise_for_status()
        return resp
