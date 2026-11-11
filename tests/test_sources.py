"""Source parsers against trimmed copies of the real page structures (checked 2026-11-08)."""

import json

import pytest

from deal_agent.sources.base import SourceBlocked
from deal_agent.sources.html import HtmlSource
from deal_agent.sources.jsonld import JsonLdSource, offer_info, products_from_html
from deal_agent.sources.mediamarkt import parse_apollo
from deal_agent.sources.mydealz import MydealzSource, parse_thread

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:pepper="http://www.pepper.com/rss" xmlns:media="http://search.yahoo.com/mrss/" version="2.0"><channel>
<item><category><![CDATA[Elektronik]]></category><pepper:merchant name="MediaMarkt" price="549\xe2\x82\xac"/>
<media:content medium="image" url="https://img/1.jpg"/>
<title><![CDATA[106\xc2\xb0 - ASUS VivoBook 16 f\xc3\xbcr 549 \xe2\x82\xac]]></title>
<description><![CDATA[<strong>549\xe2\x82\xac - MediaMarkt</strong><p>Gilt bis 30.11.</p>]]></description>
<link>https://www.mydealz.de/deals/asus-vivobook-2866667</link>
<pubDate>Sun, 08 Nov 2026 02:22:54 +0100</pubDate></item>
</channel></rss>"""

THREAD = {"threadDetail": {
    "price": 47.89, "nextBestPrice": 95.95, "standardPrice": 0, "linkHost": "www.bestsecret.com",
    "voucherCode": "SD26", "isVoucherCodeHidden": False, "isExpired": False, "status": "Activated",
    "temperature": 176.46, "commentCount": 2, "endDate": {"timestamp": 1794700000}, "shipping": {"price": 5},
    "merchant": {"merchantName": "Best Secret"}, "groups": [{"threadGroupName": "Fashion & Accessoires"}]}}
THREAD_HTML = f"<html><script>window.__INITIAL_STATE__ = {json.dumps(THREAD)};</script></html>"

MM_STATE = ('{"apolloState":{"CofrCoreFeature:Media:de:1":{"id":"Media:de:1","manufacturerName":"LENOVO",'
            '"productName":"LENOVO IdeaPad 5 Notebook","urlRelative":"/de/product/_lenovo-1.html","ean":"4262560968893"},'
            '"CofrPriceFeature:{\\"id\\":\\"Media:de:1\\"}":{"id":"Media:de:1","isProductOfTypeMarketplace":true,'
            '"marketplaceSeller":{"sellerName":"cybernerds","sellerRating":4.32},'
            '"price":{"amount":359,"discount":240,"discountPercentage":40,"shippingCost":0},'
            '"pdpAssetVideoSal":undefined}}}')
MM_HTML = f"<html><script>window.__PRELOADED_STATE__ = {MM_STATE};</script></html>"

LD_HTML = """<script type="application/ld+json">{"@type":"CollectionPage","mainEntity":{"@type":"ItemList",
"itemListElement":[{"@type":"ListItem","item":{"@type":"Product","name":"GIVENCHY Gentleman EdP","brand":{"@type":"Brand",
"name":"GIVENCHY"},"gtin13":"3274872511811","offers":{"@type":"Offer","price":"61.95","availability":"https://schema.org/InStock",
"url":"https://www.flaconi.de/parfum/givenchy/gentleman.html?variant=1"}}},
{"@type":"ListItem","item":{"@type":"Product","name":"Sold out thing","offers":{"price":"9","availability":"https://schema.org/OutOfStock","url":"/x"}}}]}}</script>"""

IDEALO_HTML = """<div data-testid="result-item"><a href="https://www.idealo.de/preisvergleich/OffersOfProduct/214149541_-charles.html">
<span data-testid="info-tooltip-trigger">- 14 %</span><div data-testid="product-summary-title">Blauer USA Charles Herren-Steppjacke</div>
<div data-testid="detailed-price-info-price">ab 179,99 €</div></a></div>"""


class Resp:
    def __init__(self, text="", status=200, url="https://example.org/", content=None):
        self.text, self.status_code, self.url = text, status, url
        self.content = content if content is not None else text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    def __init__(self, resp):
        self.resp = resp

    def get(self, url, raise_for_status=True):
        return self.resp


def test_mydealz_rss_and_detail(prefs):
    src = MydealzSource({"name": "mydealz", "url": "https://www.mydealz.de/rss/alles"},
                        FakeHttp(Resp(content=RSS, text="")), prefs)
    (d,) = src.collect(force=True)
    assert d.title == "ASUS VivoBook 16 für 549 €"
    assert (d.price, d.merchant, d.temperature, d.category) == (549.0, "MediaMarkt", 106, "electronics")
    assert d.published_at.isoformat() == "2026-11-08T01:22:54+00:00"

    info = parse_thread(THREAD_HTML)
    assert info["shop_domain"] == "bestsecret.com" and info["coupon_code"] == "SD26"
    assert info["market_price"] == 95.95 and info["shipping"] == 5 and not info["expired"]
    assert info["expires_at"].year == 2026 and info["original_price"] is None
    src.http = FakeHttp(Resp(THREAD_HTML))
    assert src.detail(d)["merchant"] == "Best Secret"
    assert parse_thread("<html>no state</html>") is None


def test_mediamarkt_apollo():
    (d,) = parse_apollo(MM_HTML, "https://www.mediamarkt.de/de/campaign/angebote-aktionen", "MediaMarkt")
    assert d.title == "LENOVO IdeaPad 5 Notebook" and d.brand == "Lenovo"
    assert (d.price, d.original_price, d.discount_pct) == (359.0, 599.0, 40.0)
    assert d.link == "https://www.mediamarkt.de/de/product/_lenovo-1.html"
    assert d.seller_name == "cybernerds" and d.seller_rating == 4.32 and d.ean == "4262560968893"


def test_jsonld(prefs):
    products = products_from_html(LD_HTML)
    assert len(products) == 2 and offer_info(products[0])["availability"] == "InStock"
    src = JsonLdSource({"name": "flaconi", "url": "https://www.flaconi.de/kategorie/sale/", "merchant": "Flaconi",
                        "category": "cosmetics"}, FakeHttp(Resp(LD_HTML, url="https://www.flaconi.de/kategorie/sale/")), prefs)
    (d,) = src.collect(force=True)                         # out-of-stock product skipped
    assert (d.title, d.price, d.brand, d.shop_domain, d.category) == \
        ("GIVENCHY Gentleman EdP", 61.95, "GIVENCHY", "flaconi.de", "cosmetics")


def test_html_selectors_idealo(prefs):
    cfg = {"name": "idealo", "url": "https://www.idealo.de/preisvergleich/deals", "merchant": "idealo",
           "trust": "comparison", "selectors": {
               "item": "[data-testid='result-item']", "title": "[data-testid='product-summary-title']",
               "link": "a[href]", "price": "[data-testid='detailed-price-info-price']",
               "discount": "[data-testid='info-tooltip-trigger']"}}
    src = HtmlSource(cfg, FakeHttp(Resp(IDEALO_HTML, url=cfg["url"])), prefs)
    (d,) = src.collect(force=True)
    assert (d.price, d.discount_pct, d.trust_hint, d.category) == (179.99, 14.0, "comparison", "clothing")
    assert d.original_price == pytest.approx(209.29, abs=0.01)


def test_blocked_responses_raise(prefs):
    for resp in (Resp("nope", status=403), Resp("<html>Please solve the captcha</html>")):
        src = JsonLdSource({"name": "x", "url": "https://shop.example/sale"}, FakeHttp(resp), prefs)
        with pytest.raises(SourceBlocked):
            src.collect(force=True)
