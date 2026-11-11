"""Command-line interface.

    python -m deal_agent start [--no-dashboard]      # scheduler + dashboard (what autostart runs)
    python -m deal_agent dashboard                   # dashboard only
    python -m deal_agent cron [--skip-if-pc-online]  # run whatever is due once (GitHub Actions)
    python -m deal_agent run discovery|watchlist|digest|reminders|recheck|eod|feedback|excel|all [--dry-run]
    python -m deal_agent watch add "Sony WH-1000XM5" --max-price 250 --category electronics
    python -m deal_agent watch list | remove ID | pause ID | resume ID
    python -m deal_agent prefs show | edit | set KEY VALUE | student on|off | add-brand TIER NAME
                               | remove-brand NAME | add-category NAME [KW ...] | remove-category NAME
                               | add-keyword CAT KW... | exclude WORD
    python -m deal_agent deals [--limit N] [--notified] [--min-score X] [-l]
    python -m deal_agent sources [--check]
    python -m deal_agent test-notify
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from deal_agent import watchfile
from deal_agent.config import Preferences, Settings, load_sources_config, migrate_legacy_db, setup_logging
from deal_agent.db import Database
from deal_agent.models import Deal, WatchItem, utcnow

_BOOL = lambda v: v.lower() in {"1", "true", "yes", "on"}  # noqa: E731
SETTABLE = {
    "scoring.notify_threshold": float,
    "scoring.watchlist_threshold": float,
    "scoring.min_discount_pct": float,
    "scoring.batch_size": int,
    "scoring.max_deals_per_run": int,
    "scoring.details_per_run": int,
    "scoring.prefilter": _BOOL,
    "notifications.digest_times": lambda v: [t.strip() for t in v.split(",") if t.strip()],
    "notifications.digest_size": int,
    "notifications.instant_score": float,
    "notifications.instant_expiry_hours": float,
    "notifications.watchlist_instant": _BOOL,
    "notifications.snooze_hours": float,
    "notifications.reminder_hours_before_expiry": float,
    "notifications.eod_email_time": str,
    "notifications.quiet_hours": lambda v: [] if v.lower() in {"", "none", "off"} else [int(x) for x in v.replace("-", ",").split(",")],
    "student.is_student": _BOOL,
    "trust.below_market_pct": float,
    "trust.fakeshop_link": _BOOL,
    "excel.min_score": float,
    "excel.active_days": int,
    "recheck.time": str,
    "recheck.max_per_run": int,
    "schedule.discovery_hours": float,
    "schedule.watchlist_hours": float,
    "schedule.reminder_minutes": float,
    "schedule.feedback_minutes": float,
}


def _out(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:   # old Windows consoles
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc))


def _fmt_price(v: float | None) -> str:
    return f"{v:.2f}€" if v is not None else "-"


def _db(settings: Settings) -> Database:
    migrate_legacy_db(settings)
    return Database(settings.db_path)


# ------------------------------------------------------------------ watch

def cmd_watch(args, settings: Settings) -> int:
    db = _db(settings)
    if args.watch_cmd == "add":
        query = " ".join(args.query).strip()
        if not (query or args.brand or args.category):
            _out("Give a product query, --brand or --category.")
            return 2
        prefs = Preferences.load(settings.preferences_file)
        if args.category and args.category not in prefs.categories:
            _out(f"Note: category {args.category!r} is not in preferences.yaml ({', '.join(prefs.categories)}).")
        item = WatchItem(query=query, brand=args.brand, category=args.category,
                         max_price=args.max_price, min_discount=args.min_discount)
        item.id = db.add_watch(item)
        _out(f"Added watch #{item.id}: {item.describe()}")
        _out("It is checked against the last 7 days of deals on the next hourly run; hits are pushed instantly.")
    elif args.watch_cmd == "list":
        items = db.list_watch()
        if not items:
            _out('Watchlist is empty. Add one with: python -m deal_agent watch add "product" --max-price 100')
        for w in items:
            _out(f"#{w.id:<3} {'active' if w.active else 'paused':7} {w.describe()}")
        return 0
    elif args.watch_cmd == "remove":
        _out("Removed." if db.remove_watch(args.id) else f"No watch item #{args.id}.")
    elif args.watch_cmd in ("pause", "resume"):
        ok = db.set_watch_active(args.id, args.watch_cmd == "resume")
        _out(f"Watch #{args.id} {args.watch_cmd}d." if ok else f"No watch item #{args.id}.")
    watchfile.export(db, settings.watchlist_file)
    return 0


# ------------------------------------------------------------------ prefs

def _find_brand(prefs: Preferences, name: str) -> tuple[str, int] | None:
    for tier in ("budget", "premium"):
        for i, b in enumerate(prefs.data.get("brands", {}).get(tier) or []):
            if str(b).casefold() == name.casefold():
                return tier, i
    return None


def cmd_prefs(args, settings: Settings) -> int:
    prefs = Preferences.load(settings.preferences_file)
    data = prefs.data
    c = args.prefs_cmd

    if c == "show":
        _out(f"File: {prefs.path}")
        _out("Categories:")
        for name, kws in prefs.categories.items():
            men = " (men's only)" if name in prefs.men_only_categories else ""
            _out(f"  {name}{men}: {', '.join(kws[:10])}{' ...' if len(kws) > 10 else ''}")
        for tier, names in prefs.brands.items():
            _out(f"Brands ({tier}): {', '.join(names) or '-'}")
        _out(f"Exclude: {', '.join(prefs.exclude_keywords) or '-'}")
        st = prefs.student
        _out(f"Student: {'yes' if st['is_student'] else 'no'} ({', '.join(st['programs']) or 'all programs'})")
        for section in ("scoring", "notifications", "trust", "excel", "recheck", "schedule"):
            sec = data.get(section) or {}
            _out(f"{section}: " + ", ".join(f"{k}={v}" for k, v in sec.items()))
        return 0

    if c == "edit":
        editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
        return subprocess.call([editor, str(prefs.path)])

    if c == "student":
        data.setdefault("student", CommentedMap())["is_student"] = args.state == "on"
        msg = f"Student status: {args.state}"
    elif c == "set":
        if args.key not in SETTABLE:
            _out(f"Unknown key. Settable: {', '.join(SETTABLE)}")
            return 2
        section, key = args.key.split(".")
        try:
            value = SETTABLE[args.key](args.value)
        except ValueError:
            _out(f"Invalid value for {args.key}: {args.value!r}")
            return 2
        sec = data.setdefault(section, CommentedMap())
        if isinstance(value, list) and isinstance(sec.get(key), CommentedSeq):
            sec[key][:] = value          # in place: keeps the list's comments
            sec[key].fa.set_flow_style()
        elif isinstance(value, list):
            sec[key] = CommentedSeq(value)
            sec[key].fa.set_flow_style()
        else:
            sec[key] = value
        msg = f"{args.key} = {value}"
    elif c == "add-brand":
        if _find_brand(prefs, args.name):
            _out(f"{args.name} is already listed.")
            return 0
        data.setdefault("brands", CommentedMap()).setdefault(args.tier, CommentedSeq()).append(args.name)
        msg = f"Added {args.name} to {args.tier} brands"
    elif c == "remove-brand":
        hit = _find_brand(prefs, args.name)
        if not hit:
            _out(f"{args.name} not found.")
            return 1
        del data["brands"][hit[0]][hit[1]]
        msg = f"Removed {args.name} from {hit[0]} brands"
    elif c == "add-category":
        cats = data.setdefault("categories", CommentedMap())
        if args.name in cats:
            _out(f"Category {args.name} exists; use add-keyword to extend it.")
            return 1
        cats[args.name] = CommentedMap(keywords=CommentedSeq([k.lower() for k in args.keywords] or [args.name.lower()]))
        msg = f"Added category {args.name}"
    elif c == "remove-category":
        if args.name not in (data.get("categories") or {}):
            _out(f"Category {args.name} not found.")
            return 1
        del data["categories"][args.name]
        msg = f"Removed category {args.name}"
    elif c == "add-keyword":
        cat = (data.get("categories") or {}).get(args.category)
        if cat is None:
            _out(f"Category {args.category} not found.")
            return 1
        cat.setdefault("keywords", CommentedSeq()).extend(k.lower() for k in args.keywords)
        msg = f"Added {len(args.keywords)} keyword(s) to {args.category}"
    elif c == "exclude":
        data.setdefault("exclude_keywords", CommentedSeq()).append(args.word.lower())
        msg = f"Deals mentioning {args.word!r} will be ignored"
    else:
        return 2
    prefs.save()
    _out(msg + f"  (saved to {prefs.path.name})")
    return 0


# ------------------------------------------------------------------ deals / sources

def _deal_row(d: Deal) -> str:
    score = f"{d.score:4.1f}" if d.score is not None else "  - "
    disc = f"-{d.discount_pct:.0f}%" if d.discount_pct else ""
    flag = "*" if d.notified else " "
    trust = {"official_store": "OFF", "major_retailer": "MAJ", "marketplace": "MKT", "unknown": "UNK"}.get(d.trust_label or "", "  ?")
    return (f"{flag}#{d.id:<5} {score}  {_fmt_price(d.effective_price):>9} {disc:>5}  {trust} "
            f"{(d.category or '-')[:11]:11} {d.title[:68]}")


def cmd_deals(args, settings: Settings) -> int:
    db = _db(settings)
    deals = db.list_deals(limit=args.limit, notified_only=args.notified, min_score=args.min_score)
    if not deals:
        _out("No deals stored yet. Try: python -m deal_agent run discovery --dry-run")
        return 0
    _out(" id      score     price  disc  shop category    title   (* = alerted; OFF/MAJ/MKT/UNK = trust)")
    for d in deals:
        _out(_deal_row(d))
        if args.links:
            _out(f"         {d.link}\n         {d.score_reason or ''} | {d.shop} | {d.status} | left: {d.time_left()}")
            for w in d.warnings:
                _out(f"         ! {w}")
    st = db.stats()
    _out(f"\n{st['deals']} deals stored, {st['scored']} scored, {st['alerts']} alerts, "
         f"{st['watch_items']} active watch items")
    return 0


def cmd_sources(args, settings: Settings) -> int:
    from deal_agent.http import PoliteHttp
    from deal_agent.sources import REGISTRY
    http = PoliteHttp(settings.user_agent)
    db = _db(settings)
    status = {s["name"]: s for s in db.source_statuses()}
    _out(f"Registered types: {', '.join(sorted(REGISTRY))}")
    for cfg in load_sources_config(settings.sources_file):
        urls = ([cfg["url"]] if cfg.get("url") else []) + list(cfg.get("urls") or [])
        state = "enabled " if cfg.get("enabled", True) else "disabled"
        st = status.get(cfg.get("name"), {})
        extra = f"  last ok {st['last_ok']}" if st.get("last_ok") else ""
        if st.get("last_error"):
            extra += f"  last error: {st['last_error'][:80]}"
        _out(f"- {cfg.get('name')} [{cfg.get('type', 'rss')}] {state}{extra}")
        for u in urls:
            ok = http.allowed(u) if args.check else None
            mark = "" if ok is None else ("  robots: allowed" if ok else "  robots: DISALLOWED")
            _out(f"    {u}{mark}")
    return 0


# ------------------------------------------------------------------ run / start / cron / test

def _agent(settings: Settings):
    from deal_agent.pipeline import Agent
    return Agent(settings)


def _print_alerts(job: str, alerts, dry: bool) -> None:
    verb = "would send" if dry else "sent"
    _out(f"\n{job}: {verb} {len(alerts)} alert(s)")
    for a in alerts:
        _out(f"  [{a.kind}] {a.deal.title[:90]}")
        for line in a.lines():
            _out(f"        {line}")
        _out(f"        {a.deal.link}")


def cmd_run(args, settings: Settings) -> int:
    agent = _agent(settings)
    jobs = ["feedback", "watchlist", "discovery", "digest", "reminders"] if args.job == "all" else [args.job]
    for job in jobs:
        if job in ("discovery", "watchlist"):
            fn = agent.run_discovery if job == "discovery" else agent.run_watchlist
            _print_alerts(job, fn(dry_run=args.dry_run, force_fetch=True), args.dry_run)
        elif job in ("digest", "reminders"):
            fn = agent.run_digest if job == "digest" else agent.run_reminders
            _print_alerts(job, fn(dry_run=args.dry_run), args.dry_run)
        elif job == "recheck":
            _out(f"recheck: {agent.run_recheck()}")
        elif job == "eod":
            _out("eod: " + ("built (dry run)" if agent.run_eod(dry_run=args.dry_run) else "nothing sent"))
        elif job == "feedback":
            _out(f"feedback: {agent.run_feedback()} button press(es) applied")
        elif job == "excel":
            _out(f"excel: {agent.export_excel()} -> {settings.excel_path}")
    return 0


def cmd_cron(args, settings: Settings) -> int:
    ran = _agent(settings).run_due(skip_if_pc_online=args.skip_if_pc_online)
    _out(f"ran: {', '.join(ran) or 'nothing'}")
    return 0


def cmd_start(args, settings: Settings) -> int:
    from deal_agent import scheduler
    try:
        scheduler.start(_agent(settings), dashboard=not args.no_dashboard)
    except scheduler.AlreadyRunning as exc:
        _out(str(exc))
        return 0
    return 0


def cmd_dashboard(args, settings: Settings) -> int:
    import uvicorn

    from deal_agent.dashboard.app import create_app
    agent = _agent(settings)
    _out(f"Dashboard on http://{settings.dashboard_host}:{settings.dashboard_port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(agent), host=settings.dashboard_host, port=settings.dashboard_port, log_level="warning",
                log_config=None)
    return 0


def cmd_test_notify(args, settings: Settings) -> int:
    from datetime import timedelta

    from deal_agent.notifier import Alert, Notifier
    n = Notifier(settings)
    if not n.push_enabled:
        _out("ntfy not configured. Set NTFY_TOPIC in .env.")
        return 1
    deal = Deal(title="Test deal – Sony WH-1000XM5 Kopfhörer", link="https://www.mydealz.de/",
                source="test", price=249.0, original_price=329.0, discount_pct=24.3, brand="Sony",
                category="electronics", merchant="Amazon", shop_domain="amazon.de", trust_label="major_retailer",
                shipping=0, coupon_code="TEST10", expires_at=utcnow() + timedelta(hours=5, minutes=30),
                student={"program": "UNiDAYS", "pct": 10, "stacks": "no", "why": "only in the brand's own shop",
                         "final_est": None})
    sent = n.push([Alert("test", deal, score=9.2, reason="This is a test notification", badge="Lowest in 90 days")])
    _out("Sent via ntfy" if sent else "Delivery failed; see log output above.")
    if args.email:
        ok = n.email("Deal agent test email", "<p>Test from your deal agent.</p>", "Test from your deal agent.")
        _out("Email sent" if ok else "Email failed or not configured (EMAIL_ENABLED / SMTP_*).")
    return 0 if sent else 1


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deal_agent", description="AI shopping deal agent")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-p", "--profile", default="", help="settings profile, e.g. 'it' loads .env.it")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="manage watchlist").add_subparsers(dest="watch_cmd", required=True)
    a = w.add_parser("add", help="add a watch item")
    a.add_argument("query", nargs="*", help="product words, e.g. Sony WH-1000XM5")
    a.add_argument("--brand")
    a.add_argument("--category")
    a.add_argument("--max-price", type=float)
    a.add_argument("--min-discount", type=float, help="minimum discount in percent")
    w.add_parser("list")
    for name in ("remove", "pause", "resume"):
        w.add_parser(name).add_argument("id", type=int)

    pr = sub.add_parser("prefs", help="view/edit preferences.yaml").add_subparsers(dest="prefs_cmd", required=True)
    pr.add_parser("show")
    pr.add_parser("edit", help="open preferences.yaml in $EDITOR / notepad")
    s = pr.add_parser("set", help=f"set a value: {', '.join(SETTABLE)}")
    s.add_argument("key")
    s.add_argument("value")
    pr.add_parser("student", help="set student status").add_argument("state", choices=["on", "off"])
    ab = pr.add_parser("add-brand")
    ab.add_argument("tier", choices=["budget", "premium"])
    ab.add_argument("name")
    pr.add_parser("remove-brand").add_argument("name")
    ac = pr.add_parser("add-category")
    ac.add_argument("name")
    ac.add_argument("keywords", nargs="*")
    pr.add_parser("remove-category").add_argument("name")
    ak = pr.add_parser("add-keyword")
    ak.add_argument("category")
    ak.add_argument("keywords", nargs="+")
    pr.add_parser("exclude", help="ignore deals containing a word").add_argument("word")

    r = sub.add_parser("run", help="run a job once")
    r.add_argument("job", choices=["discovery", "watchlist", "digest", "reminders", "recheck", "eod",
                                   "feedback", "excel", "all"])
    r.add_argument("--dry-run", action="store_true", help="fetch and score, but send/mark nothing")

    cr = sub.add_parser("cron", help="run every job that is due, once (for GitHub Actions / schedulers)")
    cr.add_argument("--skip-if-pc-online", action="store_true", help="exit if the PC agent sent a recent heartbeat")

    st = sub.add_parser("start", help="start the scheduler (and dashboard)")
    st.add_argument("--no-dashboard", action="store_true")
    sub.add_parser("dashboard", help="run only the web dashboard")

    d = sub.add_parser("deals", help="list stored deals")
    d.add_argument("--limit", type=int, default=25)
    d.add_argument("--notified", action="store_true")
    d.add_argument("--min-score", type=float)
    d.add_argument("--links", "-l", action="store_true", help="show links, reasons and warnings")

    so = sub.add_parser("sources", help="list configured sources")
    so.add_argument("--check", action="store_true", help="check robots.txt for each URL")

    tn = sub.add_parser("test-notify", help="send a test push (and optionally email)")
    tn.add_argument("--email", action="store_true")
    return p


HANDLERS = {"watch": cmd_watch, "prefs": cmd_prefs, "deals": cmd_deals, "sources": cmd_sources,
            "run": cmd_run, "cron": cmd_cron, "start": cmd_start, "dashboard": cmd_dashboard,
            "test-notify": cmd_test_notify}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env(profile=args.profile)
    setup_logging("DEBUG" if args.verbose else settings.log_level, settings.log_file)
    if sys.stderr is None and settings.log_file:     # pythonw.exe: dump hard crashes next to the log
        import faulthandler
        sys.stdout = sys.stdout or open(os.devnull, "w", encoding="utf-8")   # libraries that print/isatty
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        try:
            global _crash_fh
            _crash_fh = open(settings.log_file.with_name("crash.log"), "a", encoding="utf-8")
            faulthandler.enable(_crash_fh)
        except OSError:
            pass
    try:
        return HANDLERS[args.cmd](args, settings)
    except BaseException as exc:  # pythonw.exe has no console: make crashes visible in the log
        if not isinstance(exc, (KeyboardInterrupt, SystemExit)) or (isinstance(exc, SystemExit) and exc.code):
            logging.getLogger("deal_agent").exception("Fatal error in '%s'", args.cmd)
        raise
