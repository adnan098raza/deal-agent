"""APScheduler wiring for the always-on PC agent (+ the dashboard in a background thread).

    watchlist   every 1 h          discovery   every 3 h (instant alerts for great deals only)
    digest      11:00 14:30 17:30 22:30 (notifications.digest_times)
    reminders   every 30 min (expiry, snoozed, ending < 3 h)
    feedback    every 5 min        recheck     daily 12:00
    eod email   daily 21:00        excel retry every 10 min (if the file was locked)
    heartbeat   hourly (only with HEARTBEAT_ENABLED, for the cloud fallback)
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from deal_agent.config import app_home
from deal_agent.pipeline import Agent

log = logging.getLogger(__name__)


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    """Prevents two agents (e.g. the login task and a manual start) from running at once."""

    def __init__(self, path=None):
        self.path = path or app_home() / "agent.lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fh.close()
            raise AlreadyRunning(f"another deal agent is already running (lock: {self.path})") from exc
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()


def _safe(fn, name: str):
    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("Job %s failed", name)
    return run


def _hm(value: str, default: str) -> tuple[int, int]:
    try:
        h, m = str(value or default).split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def start_dashboard_thread(agent: Agent) -> threading.Thread | None:
    s = agent.settings
    if not s.dashboard_enabled:
        return None
    try:
        import uvicorn

        from deal_agent.dashboard.app import create_app
    except ImportError as exc:
        log.warning("Dashboard disabled (missing dependency: %s)", exc)
        return None
    if s.dashboard_host not in ("127.0.0.1", "localhost", "::1") and not s.dashboard_token:
        log.error("DASHBOARD_HOST=%s exposes the dashboard to your network; set DASHBOARD_TOKEN first. "
                  "Dashboard not started.", s.dashboard_host)
        return None
    server = uvicorn.Server(uvicorn.Config(create_app(agent), host=s.dashboard_host, port=s.dashboard_port,
                                           log_level="warning", access_log=False,
                                           log_config=None))   # use our handlers (pythonw has no stdout)
    t = threading.Thread(target=server.run, name="dashboard", daemon=True)
    t.start()
    log.info("Dashboard: http://%s:%d", "127.0.0.1" if s.dashboard_host == "0.0.0.0" else s.dashboard_host,
             s.dashboard_port)
    return t


def start(agent: Agent, dashboard: bool = True) -> None:
    profile = agent.settings.profile
    with InstanceLock(app_home() / (f"agent-{profile}.lock" if profile else "agent.lock")):
        _start(agent, dashboard)


def _start(agent: Agent, dashboard: bool) -> None:
    tz = agent.zone
    p = agent.prefs
    now = datetime.now(tz)
    sched = BlockingScheduler(timezone=tz, job_defaults={
        "coalesce": True, "max_instances": 1, "misfire_grace_time": 30 * 60})

    def every(name, fn, offset_min=0, **interval):
        sched.add_job(_safe(fn, name), "interval", id=name, name=name, **interval,
                      next_run_time=now + timedelta(minutes=offset_min, seconds=10))
        log.info("Scheduled %-9s every %s", name, ", ".join(f"{v:g} {k}" for k, v in interval.items()))

    def daily(name, fn, hm):
        sched.add_job(_safe(fn, name), "cron", id=name, name=name, hour=hm[0], minute=hm[1])
        log.info("Scheduled %-9s daily at %02d:%02d", name, *hm)

    every("feedback", agent.run_feedback, 0, minutes=float(p.schedule("feedback_minutes", 5)))
    every("watchlist", agent.run_watchlist, 0, hours=float(p.schedule("watchlist_hours", 1)))
    every("discovery", agent.run_discovery, 2, hours=float(p.schedule("discovery_hours", 3)))
    for h, m in agent.digest_times():
        daily(f"digest-{h:02d}{m:02d}", agent.run_digest, (h, m))
    slot = agent.last_digest_slot()
    if agent.digest_due() and slot and now - slot < timedelta(minutes=90):
        # PC started shortly after a digest time (e.g. booted at 11:20): catch up once
        sched.add_job(_safe(agent.run_digest, "digest-catchup"), "date", id="digest-catchup",
                      run_date=now + timedelta(minutes=6))
        log.info("Missed the %s digest; catching up in a few minutes", slot.strftime("%H:%M"))
    every("reminders", agent.run_reminders, 4, minutes=float(p.schedule("reminder_minutes", 30)))
    every("excel", agent.retry_excel, 10, minutes=10)
    if agent.settings.heartbeat_enabled:
        every("heartbeat", agent.heartbeat, 0, minutes=60)
    daily("recheck", agent.run_recheck, _hm(p.get("recheck", "time", "12:00"), "12:00"))
    daily("eod", agent.run_eod, _hm(p.notifications("eod_email_time", "21:00"), "21:00"))

    if dashboard:
        start_dashboard_thread(agent)
    log.info("Deal agent running (scorer: %s, push: %s, email: %s). Ctrl+C to stop.",
             agent.scorer.mode, "ntfy" if agent.notifier.push_enabled else "off",
             "on" if agent.notifier.email_enabled else "off")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopping")
