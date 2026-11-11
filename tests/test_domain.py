"""Gender filter, seller trust, student discounts, product keys."""

from datetime import timedelta

from deal_agent.filters import detect_audience, gender_block_reason
from deal_agent.models import Deal, utcnow
from deal_agent.products import best_offer, heuristic_key, lowest_badge, product_key
from deal_agent.students import evaluate
from deal_agent.trust import FAKESHOP_FINDER_URL, assess, classify


def deal(title="X", **kw):
    return Deal(title=title, link=f"https://x/{abs(hash(title + str(kw)))}", source="t", **kw)


def test_audience():
    assert detect_audience("Nike Air Max 90 Damen Sneaker") == "women"
    assert detect_audience("Adidas Damenuhr Gold") == "women"
    assert detect_audience("Puma Kinderschuhe Gr. 30") == "kids"
    assert detect_audience("Jordan 1 Mid (GS)") == "kids"
    assert detect_audience("Levi's 501 Herren Jeans") == "men"
    assert detect_audience("Herrenuhr Automatik") == "men"
    assert detect_audience("Sneaker Sale für Damen und Herren") == "unisex"
    assert detect_audience("Sommerkleid Blumen") == "women"
    assert detect_audience("Bose QC Ultra Kopfhörer") is None
    men_only = ["clothing", "shoes", "jewelry_accessories"]
    assert gender_block_reason("shoes", "women", men_only)
    assert gender_block_reason("clothing", "kids", men_only)
    assert gender_block_reason("cosmetics", "women", men_only) is None      # no restriction
    assert gender_block_reason("shoes", "unisex", men_only) is None


def test_trust_labels(prefs):
    b = prefs.all_brands
    assert classify(deal(merchant="Nike", shop_domain="nike.com"), b).label == "official_store"
    assert classify(deal(shop_domain="store.sony.de"), b).label == "official_store"
    assert classify(deal(merchant="Amazon", shop_domain="amazon.de"), b).label == "major_retailer"
    assert classify(deal(merchant="Best Secret", shop_domain="bestsecret.com"), b).label == "major_retailer"
    assert classify(deal("Kabel [Amazon Mp]", merchant="Amazon"), b).label == "marketplace"
    assert classify(deal(merchant="eBay", shop_domain="ebay.de"), b).label == "marketplace"
    assert classify(deal(merchant="MediaMarkt", seller_name="cybernerds", seller_rating=4.3), b).label == "marketplace"
    assert classify(deal(merchant="Super-Deals24", shop_domain="super-deals24.shop"), b).label == "unknown"


def test_trust_warnings(prefs):
    b = prefs.all_brands
    scam = assess(deal(merchant="Cheapo", shop_domain="cheapo-brands.shop", price=59, market_price=349,
                       discount_pct=83), b)
    assert scam.label == "unknown"
    text = " ".join(scam.warnings)
    assert "below the market" in text and "very high discount" in text and FAKESHOP_FINDER_URL in text
    ok = assess(deal(merchant="Amazon", shop_domain="amazon.de", price=249, market_price=289), b)
    assert ok.warnings == []
    error = assess(deal(merchant="Amazon", shop_domain="amazon.de", price=49, market_price=289), b)
    assert "price error" in error.warnings[0]
    weak = assess(deal(merchant="MediaMarkt", seller_name="x", seller_rating=3.6, price=100), b)
    assert any("seller rating" in w for w in weak.warnings)


def test_student_discounts(prefs):
    nike_store = deal("Nike Air Max", brand="Nike", shop_domain="nike.com", price=100.0, trust_label="official_store")
    info = evaluate(nike_store, prefs)
    assert info["program"] == "UNiDAYS" and info["stacks"] == "maybe" and info["final_est"] == 90.0

    nike_elsewhere = deal("Nike Air Max", brand="Nike", shop_domain="zalando.de", price=100.0, trust_label="major_retailer")
    assert evaluate(nike_elsewhere, prefs)["stacks"] == "no"

    with_code = deal("Nike Air Max", brand="Nike", shop_domain="nike.com", price=100.0, trust_label="official_store",
                     coupon_code="EXTRA20")
    assert evaluate(with_code, prefs)["stacks"] == "unlikely"

    apple = deal("Apple MacBook Air", brand="Apple", shop_domain="apple.com", price=1199.0, trust_label="official_store")
    assert evaluate(apple, prefs)["stacks"] == "no"          # Apple Education doesn't combine with sales

    amazon = deal("Anker Powerbank", brand="Anker", merchant="Amazon", shop_domain="amazon.de", price=30.0, shipping=0)
    assert evaluate(amazon, prefs)["program"] == "Amazon Prime Student"

    prefs.data["student"]["is_student"] = False
    assert evaluate(nike_store, prefs) is None


def test_product_keys_and_offers():
    a = deal("[Amazon] Sony WH-1000XM5 Kopfhörer schwarz für 249€ statt 399€", brand="Sony")
    b = deal("Sony WH-1000XM5 Kopfhörer bei MediaMarkt für 259 €", brand="Sony")
    assert heuristic_key(a.title, a.brand) == heuristic_key(b.title, b.brand) == "sonywh1000xm5kopfhorer"
    assert product_key(deal("x", ean="4548736132610")) == "ean4548736132610"
    assert product_key(deal("Some long title", brand="Sony"), llm_product="Sony WH-1000XM5") == "sonywh1000xm5"

    cheap_unknown = deal("A", price=199.0, trust_label="unknown", warnings=["unknown shop"])
    trusted = deal("B", price=249.0, trust_label="major_retailer")
    official = deal("C", price=249.0, trust_label="official_store")
    best, rest = best_offer([cheap_unknown, trusted, official])
    assert best is official and rest[-1] is cheap_unknown     # cheapest *trustworthy* offer wins

    now = utcnow()
    stats = {"min": 259.0, "count": 4, "first": now - timedelta(days=20), "last": now}
    assert lowest_badge(stats, 249.0) == "Lowest in 90 days"
    assert lowest_badge(stats, 279.0) is None
    assert lowest_badge({"min": 300.0, "count": 1, "first": now, "last": now}, 249.0) is None   # not enough history
