import shutil
from pathlib import Path

import pytest

from deal_agent.config import Preferences, Settings
from deal_agent.db import Database
from deal_agent.parsing import enrich

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def prefs(tmp_path) -> Preferences:
    p = tmp_path / "preferences.yaml"
    shutil.copy(ROOT / "preferences.yaml", p)
    prefs = Preferences.load(p)
    prefs.data["notifications"]["quiet_hours"][:] = []   # tests must not depend on the clock
    prefs.save()                                       # agent jobs reload prefs from disk
    return prefs


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "deals.db", preferences_file=tmp_path / "preferences.yaml",
                    sources_file=tmp_path / "sources.yaml", watchlist_file=tmp_path / "watchlist.yaml",
                    excel_path=tmp_path / "out" / "deals.xlsx", llm_provider="none")


class FakeSource:
    has_detail = False

    def __init__(self, prefs, deals, name="fake"):
        self.prefs, self.deals, self.name = prefs, deals, name
        self._last_fetch = 0

    def collect(self, force=False):
        self._last_fetch += 1
        for d in self.deals:
            d.source = self.name
        return [enrich(d, self.prefs) for d in self.deals]

    def due(self):
        return True


class RecordingNotifier:
    push_enabled = True
    email_enabled = True
    channels = ["ntfy", "email"]

    def __init__(self):
        self.sent = []
        self.emails = []

    def push(self, alerts, dry_run=False):
        if dry_run:
            return []
        self.sent.extend(alerts)
        return alerts

    def email(self, subject, html, text, attachments=None):
        self.emails.append((subject, html, text, attachments))
        return True


class FakeBridge:
    enabled = True

    def __init__(self):
        self.commands = []
        self.uids = set()
        self.online = False

    def feedback_commands(self, cursor):
        cmds, self.commands = self.commands, []
        return cmds, "cursor-1" if cmds else cursor

    def sent_uids(self, since="12h"):
        return set(self.uids)

    def heartbeat(self):
        pass

    def pc_online(self, within="90m"):
        return self.online


@pytest.fixture
def make_agent(settings, prefs, tmp_path, monkeypatch):
    import deal_agent.excel as excel
    monkeypatch.setattr(excel, "pending_path", lambda: tmp_path / "pending.xlsx")

    def factory(deals, extra_sources=None):
        from deal_agent.pipeline import Agent
        from deal_agent.scorer import Scorer
        notifier, bridge = RecordingNotifier(), FakeBridge()
        sources = [FakeSource(prefs, deals)] + (extra_sources or [])
        agent = Agent(settings, prefs=prefs, db=Database(settings.db_path), sources=sources,
                      scorer=Scorer(settings, prefs), notifier=notifier, bridge=bridge)
        return agent, notifier, bridge
    return factory
