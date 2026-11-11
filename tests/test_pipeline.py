"""End-to-end flows with fake sources, notifier and ntfy bridge (no network)."""

from datetime import timedelta

from deal_agent.models import Deal, WatchItem, utcnow
from deal_agent.scorer import Scorer
from tests.test_matcher_scorer import FakeLLM


def D(title, merchant="Amazon", domain="amazon.de", **kw):
    return Deal(title=title, link=f"https://m/{abs(hash(title + merchant))}", source="fake", merchant=merchant,
                shop_domain=domain, **kw)


def hot():           # heuristic score >= 9 -> instant
    return D("420° - Sony WH-1000XM5 Kopfhörer für 249€ statt 399€")


def digest_set():    # premium brand, preferred category, ~20% off -> score ~8 (digest, not instant)
    return [D("Levi's 501 Jeans Herren für 64€ statt 80€"), D("Adidas Samba OG Herren für 80€ statt 100€"),
            D("Tissot PRX Herrenuhr für 400€ statt 500€"), D("Logitech MX Master 3S Maus für 80€ statt 100€"),
            D("Garmin Forerunner 265 Smartwatch für 300€ statt 375€")]


def test_instant_and_digest(make_agent):
    deals = [hot(), *digest_set(),
             D("Gartenschlauch 20m für 15€"),
             D("Handyvertrag mit Samsung Galaxy für 10€")]
    agent, notifier, _ = make_agent(deals)

    preview = agent.run_discovery(dry_run=True)
    assert preview and not notifier.sent                       # dry run sends nothing

    instant = agent.run_discovery()
    assert [a.kind for a in instant] == ["instant"] and instant[0].deal.title.startswith("Sony")
    assert agent.run_discovery() == []                         # no duplicates

    digest = agent.run_digest()
    assert len(digest) == 3 and all(a.kind == "digest" for a in digest)
    assert [a.position for a in digest] == [(1, 3), (2, 3), (3, 3)]
    assert "Gartenschlauch" not in " ".join(a.deal.title for a in digest)
    assert agent.run_digest() == []                            # nothing new -> skipped

    stored = {d.title: d for d in agent.db.list_deals(limit=50)}
    assert "excluded" in stored["Handyvertrag mit Samsung Galaxy für 10€"].score_reason
    assert "prefilter" in stored["Gartenschlauch 20m für 15€"].score_reason
    sony = stored["Sony WH-1000XM5 Kopfhörer für 249€ statt 399€"]
    assert sony.trust_label == "major_retailer" and sony.product_key


def test_mens_only_filter(make_agent):
    deals = [D("Nike Air Max 90 Damen Sneaker für 59€ statt 140€", merchant="Zalando", domain="zalando.de"),
             D("Adidas Kinderschuhe Superstar für 30€ statt 70€", merchant="Zalando", domain="zalando.de"),
             D("Nike Air Max 90 Herren Sneaker für 69€ statt 140€", merchant="Zalando", domain="zalando.de"),
             D("Clinique for Women Serum für 20€ statt 40€", merchant="Douglas", domain="douglas.de")]
    agent, notifier, _ = make_agent(deals)
    agent.run_discovery()
    stored = {d.title.split(" für")[0]: d for d in agent.db.list_deals(limit=50)}
    assert stored["Nike Air Max 90 Damen Sneaker"].score == 0
    assert "women" in stored["Nike Air Max 90 Damen Sneaker"].score_reason
    assert stored["Adidas Kinderschuhe Superstar"].score == 0
    assert stored["Nike Air Max 90 Herren Sneaker"].score > 7
    assert stored["Clinique for Women Serum"].score > 0           # cosmetics: no gender restriction


def test_llm_audience_filter(make_agent, settings, prefs):
    agent, _, _ = make_agent([D("Nike Dunk Low Retro für 70€ statt 120€", merchant="Snipes", domain="snipes.com")])
    agent.scorer = Scorer(settings, prefs, client=FakeLLM({"results": [
        {"id": 0, "relevance": 9, "quality": 9, "category": "shoes", "audience": "women", "reason": "nice"}]}))
    agent.run_discovery()
    (d,) = agent.db.list_deals()
    assert d.score == 0 and "women" in d.score_reason


def test_same_product_grouped_to_best_trusted_offer(make_agent):
    amazon = D("420° - Sony WH-1000XM5 Kopfhörer für 249€ statt 399€")
    unknown = D("400° - Sony WH-1000XM5 Kopfhörer für 199€ statt 399€", merchant="Cheapo", domain="cheapo-tech.shop")
    agent, notifier, _ = make_agent([amazon, unknown])
    sent = agent.run_discovery()
    assert len(sent) == 1
    a = sent[0]
    assert a.deal.merchant == "Amazon" and len(a.also_at) == 1   # unknown-shop offer only listed as "also at"
    assert any("unknown shop" in w for w in a.also_at[0].warnings)
    assert agent.run_digest() == []                               # grouped offer is not pushed separately


def test_watchlist_instant_and_gender(make_agent):
    deals = [D("Apple iPhone 16 128GB für 699€ statt 949€"), D("Apple iPhone 16 Hülle für 9€"),
             D("Nike Air Force 1 Damen für 70€", merchant="Zalando", domain="zalando.de"),
             D("Nike Air Force 1 '07 Herren für 75€", merchant="Zalando", domain="zalando.de")]
    agent, notifier, _ = make_agent(deals)
    agent.db.add_watch(WatchItem(query="iPhone 16", max_price=750))
    agent.db.add_watch(WatchItem(query="Air Force 1", brand="Nike", max_price=90))
    sent = agent.run_watchlist()
    titles = sorted(a.deal.title for a in sent)
    assert titles == ["Apple iPhone 16 128GB für 699€ statt 949€", "Nike Air Force 1 '07 Herren für 75€"]
    assert all(a.kind == "watchlist" and a.watch for a in sent)
    assert agent.run_watchlist() == []


def test_feedback_dismiss_penalises_similar_and_snooze_resends(make_agent):
    first = D("420° - Sony WH-1000XM5 Kopfhörer für 249€ statt 399€")
    agent, notifier, bridge = make_agent([first])
    agent.run_discovery()
    sony = agent.db.list_deals()[0]
    bridge.commands = [("dismiss", sony.uid)]
    assert agent.run_feedback() == 1
    assert agent.db.get_deal(sony.id).dismissed

    # a near-identical deal from another shop is now penalised below the instant threshold
    agent.sources[0].deals = [D("410° - Sony WH-1000XM5 Kopfhörer für 239€ statt 399€", merchant="Otto", domain="otto.de")]
    assert agent.run_discovery() == []
    again = [d for d in agent.db.list_deals() if d.merchant == "Otto"][0]
    assert again.penalty >= 3 and "dismissed" in again.score_reason

    # snooze: hidden now, re-sent once the snooze has passed
    agent.sources[0].deals = [D("430° - Bose QC Ultra Kopfhörer für 249€ statt 449€")]
    agent.run_discovery()
    bose = [d for d in agent.db.list_deals() if d.brand == "Bose"][0]
    bridge.commands = [("snooze", bose.uid)]
    agent.run_feedback()
    assert [a for a in agent.run_reminders() if a.kind == "snoozed"] == []
    agent.db.update_deal(bose.id, snoozed_until=utcnow() - timedelta(minutes=1))
    resent = [a for a in agent.run_reminders() if a.kind == "snoozed"]
    assert len(resent) == 1 and resent[0].deal.id == bose.id
    assert [a for a in agent.run_reminders() if a.kind == "snoozed"] == []


def test_expiry_reminder_and_instant_for_ending_deals(make_agent):
    ending = D("Levi's 511 Jeans Herren für 40€ statt 50€", expires_at=utcnow() + timedelta(hours=2))
    agent, notifier, _ = make_agent([ending])
    agent.run_discovery()
    assert [a.kind for a in notifier.sent] == ["instant"]     # score < 9 but ends within 3 h
    reminders = agent.run_reminders()
    assert [a.kind for a in reminders] == ["reminder"]
    assert agent.run_reminders() == []


def test_sync_sent_prevents_duplicates(make_agent):
    deal = hot()
    agent, notifier, bridge = make_agent([deal])
    bridge.uids = {deal.uid}                                    # the cloud runner already pushed it
    assert agent.run_discovery() == []


def test_eod_email_and_excel(make_agent, settings):
    agent, notifier, _ = make_agent([hot(), *digest_set()])
    agent.run_discovery()
    agent.run_digest()
    assert settings.excel_path.exists()
    assert agent.run_eod()
    subject, html, text, attachments = notifier.emails[-1]
    assert "4 deal(s)" in subject and "Electronics" in html and "Major retailer" in html
    assert attachments == [settings.excel_path]


def test_run_due_cloud_mode(make_agent, settings):
    agent, notifier, bridge = make_agent([hot()])
    settings.cloud = True
    settings.watchlist_file.write_text("watchlist:\n  - query: iPhone 16\n    max_price: 750\n", encoding="utf-8")
    ran = agent.run_due(skip_if_pc_online=True)
    assert {"watchlist", "discovery", "digest", "reminders"} <= set(ran)
    assert [w.query for w in agent.db.list_watch()] == ["iPhone 16"]
    assert agent.run_due() == ["reminders"] or "eod" in agent.run_due()     # nothing else due right after
    bridge.online = True
    assert agent.run_due(skip_if_pc_online=True) == []


def test_blocked_source_is_backed_off(make_agent, prefs):
    from deal_agent.sources import SourceBlocked

    class Blocked:
        name, has_detail, _last_fetch = "blocked-shop", False, 0
        calls = 0

        def collect(self, force=False):
            Blocked.calls += 1
            raise SourceBlocked("HTTP 403 from shop.example")

    agent, _, _ = make_agent([], extra_sources=[Blocked()])
    agent.collect()
    agent.collect()
    assert Blocked.calls == 1                                  # second run skipped (back-off)
    (st,) = [s for s in agent.db.source_statuses() if s["name"] == "blocked-shop"]
    assert "403" in st["last_error"] and st["blocked_until"]
