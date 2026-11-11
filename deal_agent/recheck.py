"""Daily re-check of stored deals: Live / Price changed / Expired / Out of stock.

mydealz links are read from the deal page state (expired flag + current price). Shop links
use schema.org JSON-LD (price + availability). 404/410 means Expired. robots.txt is honoured;
disallowed or blocked links are skipped and retried next time.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import requests

from deal_agent.db import Database
from deal_agent.http import PoliteHttp, RobotsDisallowed
from deal_agent.models import Deal, utcnow
from deal_agent.sources.jsonld import offer_info, products_from_html
from deal_agent.sources.mydealz import parse_thread

log = logging.getLogger(__name__)
OUT_OF_STOCK = {"OutOfStock", "SoldOut", "Discontinued", "BackOrder"}


def check(http: PoliteHttp, deal: Deal) -> dict | None:
    """Returns {status, current_price} or None if the link could not be checked now."""
    try:
        resp = http.get(deal.link, raise_for_status=False)
    except RobotsDisallowed:
        return None
    except requests.RequestException as exc:
        log.debug("recheck failed for %s: %s", deal.link, exc)
        return None
    if resp.status_code in (404, 410):
        return {"status": "Expired", "current_price": None}
    if resp.status_code >= 400:
        return None
    host = urlsplit(resp.url).netloc
    price, status = None, "Live"
    if "mydealz.de" in host:
        info = parse_thread(resp.text)
        if info is None:
            return None
        if info["expired"]:
            return {"status": "Expired", "current_price": info.get("price")}
        price = info.get("price")
    else:
        products = products_from_html(resp.text)
        if products:
            offer = offer_info(products[0])
            price = offer["price"]
            if offer["availability"] in OUT_OF_STOCK:
                return {"status": "Out of stock", "current_price": price}
    if price is not None and deal.price is not None and abs(price - deal.price) > max(0.01, deal.price * 0.005):
        status = "Price changed"
    return {"status": status, "current_price": price}


def run(db: Database, http: PoliteHttp, limit: int, min_score: float) -> dict:
    counts = {"checked": 0, "skipped": 0}
    expired_by_date = db.expire_passed()
    for d in db.recheck_candidates(limit, min_score):
        res = check(http, d)
        if res is None:
            counts["skipped"] += 1
            continue
        fields = {"status": res["status"], "last_checked": utcnow()}
        if res["current_price"] is not None:
            fields["current_price"] = res["current_price"]
            prev = d.current_price if d.current_price is not None else d.price
            if prev is None or abs(res["current_price"] - prev) > 0.005:
                db.add_price(d.id, d.product_key, res["current_price"], d.shop)
        db.update_deal(d.id, **fields)
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        counts["checked"] += 1
    counts["expired_by_date"] = expired_by_date
    log.info("Re-check: %s", counts)
    return counts
