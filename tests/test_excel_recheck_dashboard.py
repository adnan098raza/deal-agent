import json
import os

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from deal_agent import recheck
from deal_agent.dashboard.app import create_app, parse_search
from deal_agent.excel import Exporter
from deal_agent.models import Deal
from tests.test_pipeline import D, digest_set, hot
from tests.test_sources import Resp


def test_excel_sheets_and_lock(make_agent, settings, tmp_path, monkeypatch):
    agent, _, _ = make_agent([hot(), *digest_set()])
    agent.run_discovery()
    wb = load_workbook(settings.excel_path)
    assert wb.sheetnames == ["Active", "Expired", "History"]
    ws = wb["Active"]
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref.startswith("A1:")
    header = [c.value for c in ws[1]]
    for col in ("Title", "Brand", "Category", "Price", "Original price", "Discount %", "Shop", "Trust",
                "Student discount", "Score", "Expiry", "Status", "Last checked", "Current price", "Link"):
        assert col in header
    relevant = [d for d in agent.db.list_deals(limit=50) if (d.score or 0) >= 5 or d.notified]
    assert 4 <= len(relevant) < 6                      # the Logitech mouse has no category keyword -> low score
    assert ws.max_row == 1 + len(relevant) and ws.cell(2, 1).hyperlink is not None
    assert wb["History"].max_row == 1 + 6

    # locked by Excel -> pending, no crash; retried later
    real_replace = os.replace

    def locked(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(os, "replace", locked)
    ex = Exporter(agent.db, agent.prefs, settings.excel_path, pending=tmp_path / "p.xlsx")
    assert ex.export() == "pending" and (tmp_path / "p.xlsx").exists()
    monkeypatch.setattr(os, "replace", real_replace)
    assert ex.flush_pending() == "ok" and not (tmp_path / "p.xlsx").exists()


def test_expired_deals_move_sheets(make_agent, settings):
    agent, _, _ = make_agent([hot()])
    agent.run_discovery()
    d = agent.db.list_deals()[0]
    page = f"<script>window.__INITIAL_STATE__ = {json.dumps({'threadDetail': {'price': 249, 'isExpired': True}})};</script>"

    class Http:
        def get(self, url, raise_for_status=True):
            return Resp(page, url="https://www.mydealz.de/deals/x")

    agent.http = Http()
    counts = agent.run_recheck()
    assert counts["Expired"] == 1 and agent.db.get_deal(d.id).status == "Expired"
    wb = load_workbook(settings.excel_path)
    assert wb["Active"].max_row == 1 and wb["Expired"].max_row == 2


def test_recheck_price_and_stock():
    deal = Deal(title="x", link="https://shop.example/p", source="s", price=100.0)
    ld = '<script type="application/ld+json">{"@type":"Product","name":"x","offers":{"price":"89.00","availability":"https://schema.org/InStock"}}</script>'

    class Http:
        def __init__(self, r):
            self.r = r

        def get(self, url, raise_for_status=True):
            return self.r

    assert recheck.check(Http(Resp(ld, url=deal.link)), deal) == {"status": "Price changed", "current_price": 89.0}
    assert recheck.check(Http(Resp(ld.replace("InStock", "OutOfStock"), url=deal.link)), deal)["status"] == "Out of stock"
    assert recheck.check(Http(Resp("", status=404, url=deal.link)), deal)["status"] == "Expired"


def test_parse_search():
    assert parse_search("last 7 days matching sony headphones") == ("sony headphones", 7)
    assert parse_search("deals today with nike") == ("nike", 1)
    assert parse_search("letzte 2 wochen mit levi's") == ("levi's", 14)
    assert parse_search("air force 1") == ("air force 1", None)


def test_dashboard(make_agent, settings):
    agent, _, _ = make_agent([hot(), *digest_set()])
    agent.run_discovery()
    client = TestClient(create_app(agent))
    same = {"origin": "http://testserver"}

    r = client.get("/", params={"q": "last 7 days matching sony"})
    assert r.status_code == 200 and "WH-1000XM5" in r.text and "Levi" not in r.text
    deal_id = agent.db.list_deals()[0].id
    assert client.get(f"/deal/{deal_id}").status_code == 200
    assert client.get("/status").status_code == 200

    assert client.post("/watchlist/add", data={"query": "AirPods Pro"}).status_code == 403   # CSRF guard
    r = client.post("/watchlist/add", data={"query": "AirPods Pro", "max_price": "180"}, headers=same,
                    follow_redirects=False)
    assert r.status_code == 303 and [w.query for w in agent.db.list_watch()] == ["AirPods Pro"]
    assert "AirPods Pro" in settings.watchlist_file.read_text(encoding="utf-8")

    r = client.post("/prefs/raw", data={"yaml_text": "categories: [oops"}, headers=same, follow_redirects=False)
    assert "errors" in r.headers["location"]
    good = settings.preferences_file.read_text(encoding="utf-8").replace("notify_threshold: 7.0", "notify_threshold: 7.5")
    r = client.post("/prefs/raw", data={"yaml_text": good}, headers=same, follow_redirects=False)
    assert "msg=Saved" in r.headers["location"] and agent.prefs.notify_threshold == 7.5

    r = client.post("/prefs/quick", headers=same, follow_redirects=False, data={
        "notify_threshold": "8", "instant_score": "9.5", "digest_times": "11:00, 14:30, 18:00", "quiet_start": "22", "quiet_end": "6",
        "is_student": "on", "programs": ["unidays", "apple_education"]})
    agent.prefs.reload()
    assert agent.prefs.quiet_hours == (22, 6) and agent.prefs.student["programs"] == ["unidays", "apple_education"]
    assert "quiet_hours: [22, 6]" in settings.preferences_file.read_text(encoding="utf-8")   # style preserved

    r = client.post(f"/deal/{deal_id}/dismiss", data={"back": "/"}, headers=same, follow_redirects=False)
    assert r.status_code == 303 and agent.db.get_deal(deal_id).dismissed
    assert "msg=" in r.headers["location"]


def test_dashboard_views_drawer_and_jobs(make_agent):
    import time

    agent, _, _ = make_agent([hot(), *digest_set()])
    agent.run_discovery()
    client = TestClient(create_app(agent))
    same = {"origin": "http://testserver"}

    grid = client.get("/")
    assert grid.status_code == 200 and 'class="cards"' in grid.text and "New in last 24 h" in grid.text
    table = client.get("/", params={"view": "table"})
    assert '<table class="t">' in table.text and table.cookies.get("da_view") == "table"
    assert '<table class="t">' in client.get("/").text                      # remembered via cookie

    deal_id = agent.db.list_deals()[0].id
    part = client.get(f"/deal/{deal_id}", params={"partial": 1})
    assert part.status_code == 200 and "<html" not in part.text and "Price history" in part.text
    assert "<html" in client.get(f"/deal/{deal_id}").text

    gate = __import__("threading").Event()
    agent.run_recheck = lambda: gate.wait(5)
    r = client.post("/run/recheck", data={"back": "/?q=x&msg=old"}, headers=same, follow_redirects=False)
    assert r.headers["location"] == "/?q=x&msg=Started%20recheck"
    assert client.get("/api/running").json() == {"running": ["recheck"]}
    r = client.post("/run/recheck", data={"back": "/"}, headers=same, follow_redirects=False)
    assert "already%20running" in r.headers["location"]
    gate.set()
    for _ in range(50):
        if not client.get("/api/running").json()["running"]:
            break
        time.sleep(0.05)
    assert client.get("/api/running").json() == {"running": []}


def test_dashboard_metrics(make_agent):
    from datetime import datetime, timezone

    agent, _, _ = make_agent([hot(), *digest_set()])
    agent.run_discovery()
    m = agent.db.dashboard_metrics(datetime(2000, 1, 1, tzinfo=timezone.utc), agent.prefs.notify_threshold)
    assert m["new_24h"] == 6 and m["prev_24h"] == 0
    assert m["on_sale"] >= 1 and m["avg_discount"] > 0 and m["alerts_today"] >= 1
