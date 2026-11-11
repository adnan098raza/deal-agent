"""Fixed-time digests, quiet-hours queueing, instant rules."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from deal_agent.models import WatchItem, utcnow
from deal_agent.pipeline import parse_times
from tests.test_pipeline import D, digest_set, hot

BERLIN = ZoneInfo("Europe/Berlin")


def set_quiet(agent, hours):
    agent.prefs.data["notifications"]["quiet_hours"][:] = hours
    agent.prefs.save()


def test_parse_times():
    assert parse_times(["22:30", "11:00", "14:30", "17:30", "bad", "25:00"]) == [(11, 0), (14, 30), (17, 30), (22, 30)]


def test_digest_slots_and_due(make_agent):
    agent, _, _ = make_agent([])
    assert agent.digest_times() == [(11, 0), (14, 30), (17, 30), (22, 30)]
    at = lambda h, m, day=10: datetime(2026, 11, day, h, m, tzinfo=BERLIN)  # noqa: E731
    assert agent.last_digest_slot(at(10, 59)) == at(22, 30, 9)       # before 11:00 -> yesterday's 22:30
    assert agent.last_digest_slot(at(11, 0)) == at(11, 0)
    assert agent.last_digest_slot(at(16, 0)) == at(14, 30)
    assert agent.last_digest_slot(at(23, 50)) == at(22, 30)

    agent.db.set_last_run("digest", at(11, 0))
    assert not agent.digest_due(at(14, 0))                           # 11:00 already done
    assert agent.digest_due(at(14, 45))                              # 14:30 slot passed (cloud cron runs late)
    agent.db.set_last_run("digest", at(14, 46))
    assert not agent.digest_due(at(17, 0))


def test_digest_top3_new_since_last_and_skip(make_agent):
    agent, notifier, _ = make_agent([hot(), *digest_set()])
    agent.run_discovery()
    assert [a.kind for a in notifier.sent] == ["instant"]            # only the 9+ deal is pushed between digests
    first = agent.run_digest()
    assert len(first) == 3 and all(a.kind == "digest" for a in first)
    assert agent.run_digest() == []                                  # nothing new -> skipped

    # a deal seen before the last digest but scored after it still counts as new
    later = D("Garmin Fenix 8 Smartwatch für 600€ statt 750€")
    agent.sources[0].deals = [later]
    agent.collect()
    agent.score_pending()
    stored = agent.db.list_deals(limit=1)[0]
    assert stored.title.startswith("Garmin Fenix") and stored.score >= agent.prefs.notify_threshold
    agent.db.set_last_run("digest", utcnow() - timedelta(minutes=1))
    old = utcnow() - timedelta(hours=2)
    for d in agent.db.list_deals(limit=50):                          # everything else predates that digest
        agent.db.update_deal(d.id, first_seen=old, scored_at=old)
    agent.db.update_deal(stored.id, first_seen=utcnow() - timedelta(hours=1), scored_at=utcnow())
    assert [a.deal.id for a in agent.digest_candidates()] == [stored.id]
    agent.db.update_deal(stored.id, scored_at=utcnow() - timedelta(hours=1))     # neither seen nor scored since
    assert agent.digest_candidates() == []


def test_quiet_hours_queue_instant_and_watchlist_for_next_digest(make_agent):
    deals = [hot(), *digest_set(), D("Apple iPhone 16 128GB für 699€ statt 949€")]
    agent, notifier, _ = make_agent(deals)
    agent.db.add_watch(WatchItem(query="iPhone 16", max_price=750))
    set_quiet(agent, [0, 24])                                        # "night" all day for the test

    assert agent.run_discovery() == [] and agent.run_watchlist() == []
    assert notifier.sent == []
    sony = [d for d in agent.db.list_deals(limit=20) if d.brand == "Sony"][0]
    assert sony.queued_digest

    set_quiet(agent, [])                                             # 07:00 - quiet hours over
    assert agent.run_discovery() == [] and agent.run_watchlist() == [] and agent.run_reminders() == []
    assert notifier.sent == []                                       # queued deals wait for the digest

    digest = agent.run_digest()                                      # the 11:00 digest
    kinds = [a.kind for a in digest]
    assert kinds[0] == "watchlist" and digest[0].watch.query == "iPhone 16"
    deal_alerts = [a for a in digest if a.kind == "digest"]
    assert deal_alerts[0].deal.id == sony.id                         # queued instant deal leads the digest
    assert len(deal_alerts) == 3
    assert agent.run_digest() == []


def test_ending_soon_is_instant_outside_quiet_hours(make_agent):
    ending = D("Levi's 511 Jeans Herren für 40€ statt 50€", expires_at=utcnow() + timedelta(hours=2))
    normal = D("Levi's 512 Jeans Herren für 40€ statt 50€")
    agent, notifier, _ = make_agent([ending, normal])
    agent.run_discovery()
    assert [a.deal.title[:12] for a in notifier.sent] == ["Levi's 511 J"]   # the other waits for a digest
