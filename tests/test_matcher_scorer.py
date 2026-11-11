from deal_agent.config import Settings
from deal_agent.matcher import matches
from deal_agent.models import Deal, WatchItem
from deal_agent.scorer import LLMClient, LLMError, Scorer


def deal(title, price=None, **kw):
    return Deal(title=title, link=f"https://x/{abs(hash(title))}", source="t", price=price, **kw)


def test_matcher_rules():
    w = WatchItem(query="Sony WH-1000XM5", max_price=260)
    assert matches(w, deal("Sony WH-1000XM5 Kopfhörer schwarz", 249))
    assert not matches(w, deal("Sony WH-1000XM5 Kopfhörer", 279))
    assert not matches(w, deal("Sony WH-1000XM5 Kopfhörer"))
    assert not matches(w, deal("Sony WH-1000XM4 Kopfhörer", 199))
    assert matches(WatchItem(query="airpods pro"), deal("Apple AirPods Pro 2 USB-C", 189))
    assert not matches(WatchItem(query="airpods pro"), deal("Apple AirPods 4", 119))
    assert matches(WatchItem(query="sneaker", brand="Nike"), deal("Nike Sneakers Air Force 1", 79))
    assert not matches(WatchItem(query="sneaker", brand="Nike"), deal("Adidas Sneaker", 79))
    assert matches(WatchItem(query="", brand="Dyson", max_price=300), deal("Dyson V8 Akkusauger", 249, brand="Dyson"))
    assert not matches(WatchItem(query="jacke", category="clothing", min_discount=30),
                       deal("Jacke", 50, category="clothing", discount_pct=20))


def test_matcher_german_english_synonyms():
    mouse = WatchItem(query="gaming mouse", max_price=25)
    assert matches(mouse, deal("Logitech G305 Lightspeed Gaming-Maus", 24.99))
    assert matches(mouse, deal("Razer Gamingmaus kabellos", 19))           # German compound
    assert matches(mouse, deal("SteelSeries Rival 3 Gaming Mouse", 22))
    assert not matches(mouse, deal("Logitech G305 Gaming-Maus", 39))        # price still applies
    assert not matches(mouse, deal("Gaming Headset RGB", 20))
    phones = WatchItem(query="headphones")
    assert matches(phones, deal("Sony WH-1000XM5 Kopfhörer schwarz", 249))
    assert matches(phones, deal("JBL Bluetooth-Funkkopfhörer", 49))
    assert matches(WatchItem(query="Maus"), deal("Logitech MX Master 3S Mouse", 79))  # works both ways
    assert matches(WatchItem(query="watch"), deal("Garmin Smartwatch Forerunner", 199))
    assert not matches(WatchItem(query="keyboard"), deal("Logitech Maus", 20))


def test_heuristic_scores(prefs):
    s = Scorer(Settings(llm_provider="none"), prefs)
    good = deal("Sony Kopfhörer", 199, brand="Sony", category="electronics", discount_pct=40, temperature=400,
                trust_label="major_retailer")
    meh = deal("Unbekanntes Ding", 20, discount_pct=5)
    risky = deal("Sony Kopfhörer", 99, brand="Sony", category="electronics", discount_pct=40, temperature=400,
                 trust_label="unknown", warnings=["unknown shop"])
    sg, sm, sr = s.score_deals([good, meh, risky])
    assert sg.score >= 9 > sr.score and sm.score < prefs.notify_threshold

    w = WatchItem(query="iPhone 16", max_price=800)
    phone, case = s.score_matches([(w, deal("Apple iPhone 16 128GB", 699)), (w, deal("iPhone 16 Hülle Silikon", 9))])
    assert phone.is_match and phone.score >= prefs.watchlist_threshold
    assert not case.is_match and case.score == 0


class FakeLLM(LLMClient):
    name = "fake"

    def __init__(self, reply=None, fail=False):
        self.reply, self.fail, self.calls = reply, fail, []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if self.fail:
            raise LLMError("boom")
        return self.reply


def test_llm_scoring_parses_and_falls_back(prefs):
    reply = {"results": [
        {"id": 0, "relevance": 9, "quality": 8, "brand": "Sony", "category": "electronics", "audience": None,
         "product": "Sony WH-1000XM5", "expires_at": "2026-11-12T23:59:00+01:00", "reason": "great price"},
        {"id": 1, "relevance": 2, "quality": 3, "category": "garden", "audience": "women", "reason": "irrelevant"},
    ]}
    llm = FakeLLM(reply)
    s = Scorer(Settings(), prefs, client=llm)
    a, b, c = s.score_deals([deal("A", trust_label="major_retailer"), deal("B"), deal("C")])
    assert abs(a.score - 8.55) < 0.06 and a.category == "electronics" and a.expires_at.hour == 22
    assert a.product == "Sony WH-1000XM5"
    assert b.category is None and b.audience == "women"
    assert "heuristic" in c.reason and c.fallback
    assert "men_only_categories" in llm.calls[0][1] and "trust_label" in llm.calls[0][1]

    (r,) = Scorer(Settings(), prefs, client=FakeLLM(fail=True)).score_deals([deal("X")])
    assert "LLM unavailable" in r.reason and r.fallback


def test_llm_watch_verification(prefs):
    reply = {"results": [{"id": 0, "is_match": True, "quality": 10, "reason": "real deal", "audience": "men"},
                         {"id": 1, "is_match": False, "quality": 9, "reason": "a case"}]}
    s = Scorer(Settings(), prefs, client=FakeLLM(reply))
    w = WatchItem(query="iphone")
    ok, no = s.score_matches([(w, deal("iPhone")), (w, deal("iPhone case"))])
    assert ok.score == 10 and ok.audience == "men" and no.score == 0 and not no.is_match
