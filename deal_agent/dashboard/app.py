"""Local web dashboard (FastAPI): search deals, manage the watchlist, edit preferences.

Binds to 127.0.0.1 by default. POST requests must come from the dashboard itself (Origin /
Referer check) so other websites can't change your settings. When exposed on the network
(DASHBOARD_HOST=0.0.0.0), DASHBOARD_TOKEN is required: open /?token=... once to log in.
"""

from __future__ import annotations

import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from ruamel.yaml.comments import CommentedSeq

from deal_agent import watchfile
from deal_agent.config import loads_yaml, validate_preferences
from deal_agent.models import STATUSES, TRUST_LABELS, WatchItem
from deal_agent.notifier import eur
from deal_agent.students import load_programs
from deal_agent.students import short as student_short

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["eur"] = eur
TEMPLATES.env.filters["student"] = student_short
JOBS = ("discovery", "watchlist", "digest", "recheck", "excel")


def as_dt(value):
    """Accept datetimes or ISO strings (source_status stores strings)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def ago(dt) -> str:
    dt = as_dt(dt)
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{secs // 60:.0f} min ago"
    if secs < 86400:
        return f"{secs // 3600:.0f} h ago"
    return f"{secs // 86400:.0f} d ago"


def score_class(score) -> str:
    if score is None:
        return "s-none"
    return "s-top" if score >= 9 else "s-good" if score >= 7 else "s-mid" if score >= 5 else "s-low"


def real_image(url: str | None) -> str | None:
    """Drop tracking pixels / spacer gifs (eBay sends a 1x2 gif)."""
    if not url or re.search(r"(s_1x2|spacer|pixel|blank)\.gif", url, re.I):
        return None
    return url


TEMPLATES.env.filters["ago"] = ago
TEMPLATES.env.filters["score_class"] = score_class
TEMPLATES.env.filters["image"] = real_image
_TIME = re.compile(r"\b(?:(?:last|past|letzte[nr]?)\s+(\d+)\s+(day|days|tag|tage|tagen|week|weeks|woche|wochen)|"
                   r"(today|heute)|(this week|diese woche)|(this month|diesen monat))\b", re.I)
_MATCH = re.compile(r"\b(?:matching|with|for|mit|für|containing|about)\s+(.+)$", re.I)


def parse_search(text: str) -> tuple[str, int | None]:
    """'last 7 days matching sony headphones' -> ('sony headphones', 7)."""
    text = (text or "").strip()
    days = None
    m = _TIME.search(text)
    if m:
        if m.group(1):
            n = int(m.group(1))
            days = n * 7 if m.group(2).lower().startswith(("week", "woche")) else n
        elif m.group(3):
            days = 1
        elif m.group(4):
            days = 7
        elif m.group(5):
            days = 30
        text = (text[:m.start()] + " " + text[m.end():]).strip()
    mm = _MATCH.search(text)
    if mm:
        text = mm.group(1)
    text = re.sub(r"^(?:deals?|angebote?)\b", "", text, flags=re.I).strip(" ,:")
    return text, days


def create_app(agent) -> FastAPI:
    app = FastAPI(title="Deal Agent", docs_url=None, redoc_url=None)
    s = agent.settings
    db = agent.db
    prefs = agent.prefs

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if s.dashboard_token:
            tok = request.query_params.get("token")
            if tok and secrets.compare_digest(tok, s.dashboard_token):
                resp = RedirectResponse(request.url.path, status_code=303)
                resp.set_cookie("da_token", tok, httponly=True, samesite="strict")
                return resp
            if not secrets.compare_digest(request.cookies.get("da_token", ""), s.dashboard_token):
                return PlainTextResponse("Open /?token=<DASHBOARD_TOKEN> first.", status_code=401)
        if request.method == "POST":
            src = request.headers.get("origin") or request.headers.get("referer") or ""
            if urlsplit(src).netloc != request.url.netloc:
                return PlainTextResponse("Cross-site request refused.", status_code=403)
        return await call_next(request)

    running: set[str] = set()
    app.state.running = running

    def next_digest() -> datetime | None:
        now = datetime.now(agent.zone)
        for day in (0, 1):
            for h, m in agent.digest_times():
                t = (now + timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
                if t > now:
                    return t
        return None

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("nav", name.split(".")[0])
        ctx["trust_labels"] = TRUST_LABELS
        ctx["running"] = sorted(running)
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        return TEMPLATES.TemplateResponse(request, name, ctx)

    def local(dt):
        dt = as_dt(dt)
        return dt.astimezone(agent.zone).strftime("%d.%m. %H:%M") if dt else ""

    TEMPLATES.env.filters["local"] = local

    # ------------------------------------------------------------------ deals
    @app.get("/", response_class=HTMLResponse)
    def deals(request: Request, q: str = "", days: str = "7", category: str = "", status: str = "",
              min_score: str = "", relevant: str = "1", view: str = ""):
        prefs.reload()
        query, parsed_days = parse_search(q)
        d = parsed_days if parsed_days is not None else (int(days) if days.strip().isdigit() else None)
        ms = float(min_score) if re.fullmatch(r"\d+(\.\d+)?", min_score.strip() or "x") else (
            float(prefs.get("excel", "min_score", 5.0)) if relevant == "1" and not query else None)
        rows = db.search_deals(query, days=d, category=category or None, min_score=ms,
                               status=status or None, limit=300)
        kinds = db.alert_kinds([r.id for r in rows])
        mode = view if view in ("grid", "table") else request.cookies.get("da_view", "grid")
        midnight = datetime.now(agent.zone).replace(hour=0, minute=0, second=0, microsecond=0)
        params = {k: v for k, v in request.query_params.items() if k != "view"}
        resp = render(request, "deals.html", deals=rows, kinds=kinds, q=q, days=d or "", category=category,
                      status=status, min_score=min_score, relevant=relevant, query=query, view=mode,
                      params=params, categories=list(prefs.categories), statuses=STATUSES, stats=db.stats(),
                      m=db.dashboard_metrics(midnight, prefs.notify_threshold), next_digest=next_digest(),
                      threshold=prefs.notify_threshold)
        if view in ("grid", "table"):
            resp.set_cookie("da_view", view, max_age=365 * 86400, samesite="lax")
        return resp

    @app.get("/deal/{deal_id}", response_class=HTMLResponse)
    def deal(request: Request, deal_id: int, partial: int = 0):
        d = db.get_deal(deal_id)
        if not d:
            return PlainTextResponse("not found", status_code=404)
        series = db.price_series(d.product_key, d.id)
        offers = [o for o in db.offers_for(d.product_key, days=30) if o.id != d.id] if d.product_key else []
        pts = ""
        if len(series) >= 2:
            ps = [p for _, p, _ in series]
            lo, hi = min(ps), max(ps)
            span = (hi - lo) or 1
            n = len(ps) - 1
            pts = " ".join(f"{i * 300 / n:.1f},{60 - (p - lo) / span * 50 - 5:.1f}" for i, p in enumerate(ps))
        return render(request, "_deal_body.html" if partial else "deal.html", nav="deals", d=d, series=series,
                      offers=offers, points=pts, kinds=db.alert_kinds([d.id]).get(d.id, ""),
                      lo=min((p for _, p, _ in series), default=None), hi=max((p for _, p, _ in series), default=None))

    @app.post("/deal/{deal_id}/{action}")
    def deal_action(deal_id: int, action: str, back: str = Form("/")):
        d = db.get_deal(deal_id)
        msg = ""
        if d and action == "dismiss":
            db.dismiss(d.uid)
            msg = "Marked not interested; similar deals will score lower"
        elif d and action == "snooze":
            hours = float(prefs.notifications("snooze_hours", 24))
            db.snooze(d.uid, hours)
            msg = f"Snoozed for {hours:g} h"
        elif d and action == "restore":
            db.update_deal(d.id, dismissed=False, snoozed_until=None, snooze_resend=False)
            msg = "Restored"
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        back = re.sub(r"([?&])msg=[^&]*&?", r"\1", back).rstrip("?&")
        if msg:
            back += ("&" if "?" in back else "?") + "msg=" + quote(msg)
        return RedirectResponse(back, status_code=303)

    # -------------------------------------------------------------- watchlist
    @app.get("/watchlist", response_class=HTMLResponse)
    def watchlist(request: Request, msg: str = ""):
        return render(request, "watchlist.html", items=db.list_watch(), categories=list(prefs.categories), msg=msg)

    def _num(v: str) -> float | None:
        v = (v or "").strip().replace(",", ".")
        return float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None

    @app.post("/watchlist/add")
    def watch_add(query: str = Form(""), brand: str = Form(""), category: str = Form(""),
                  max_price: str = Form(""), min_discount: str = Form("")):
        if not (query.strip() or brand.strip() or category):
            return RedirectResponse("/watchlist?msg=Enter+a+product,+brand+or+category", status_code=303)
        db.add_watch(WatchItem(query=query.strip(), brand=brand.strip() or None, category=category or None,
                               max_price=_num(max_price), min_discount=_num(min_discount)))
        watchfile.export(db, s.watchlist_file)
        return RedirectResponse("/watchlist?msg=Added", status_code=303)

    @app.post("/watchlist/{watch_id}/{action}")
    def watch_action(watch_id: int, action: str):
        if action == "remove":
            db.remove_watch(watch_id)
        elif action in ("pause", "resume"):
            db.set_watch_active(watch_id, action == "resume")
        watchfile.export(db, s.watchlist_file)
        return RedirectResponse("/watchlist", status_code=303)

    # ----------------------------------------------------------- preferences
    @app.get("/prefs", response_class=HTMLResponse)
    def prefs_page(request: Request, msg: str = "", errors: str = ""):
        prefs.reload()
        raw = prefs.path.read_text(encoding="utf-8") if prefs.path else ""
        return render(request, "prefs.html", p=prefs, raw=raw, msg=msg, errors=errors.split("|") if errors else [],
                      programs=load_programs(), qh=prefs.quiet_hours or ("", ""))

    @app.post("/prefs/quick")
    def prefs_quick(notify_threshold: str = Form(...), instant_score: str = Form(...), digest_times: str = Form(...),
                    quiet_start: str = Form(""), quiet_end: str = Form(""), is_student: str = Form(""),
                    programs: list[str] = Form([])):
        from deal_agent.pipeline import parse_times
        prefs.reload()
        data = prefs.data
        raw_times = [t.strip() for t in digest_times.split(",") if t.strip()]
        if not raw_times or len(parse_times(raw_times)) != len(raw_times):
            return RedirectResponse("/prefs?errors=Digest+times+must+be+HH:MM,+comma-separated", status_code=303)
        try:
            data.setdefault("scoring", {})["notify_threshold"] = float(notify_threshold)
            data.setdefault("notifications", {})["instant_score"] = float(instant_score)
            times = CommentedSeq(f"{h:02d}:{m:02d}" for h, m in parse_times(raw_times))
            times.fa.set_flow_style()
            data["notifications"]["digest_times"] = times
            data["notifications"].pop("digest_hours", None)
            new_qh = CommentedSeq([int(quiet_start), int(quiet_end)] if quiet_start.strip() and quiet_end.strip() else [])
            new_qh.fa.set_flow_style()                      # keep "quiet_hours: [23, 7]"
            qh = data["notifications"].get("quiet_hours")
            if isinstance(qh, CommentedSeq):
                qh[:] = list(new_qh)
                qh.fa.set_flow_style()
            else:
                data["notifications"]["quiet_hours"] = new_qh
            st = data.setdefault("student", {})
            st["is_student"] = is_student == "on"
            if isinstance(st.get("programs"), list):
                st["programs"][:] = programs
            else:
                st["programs"] = programs
        except ValueError:
            return RedirectResponse("/prefs?errors=Numbers+expected", status_code=303)
        prefs.save()
        return RedirectResponse("/prefs?msg=Saved", status_code=303)

    @app.post("/prefs/raw")
    def prefs_raw(yaml_text: str = Form(...)):
        try:
            data = loads_yaml(yaml_text)
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(f"/prefs?errors=YAML+error:+{str(exc)[:200]}", status_code=303)
        errs = validate_preferences(data)
        if errs:
            return RedirectResponse("/prefs?errors=" + "|".join(errs), status_code=303)
        tmp = prefs.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml_text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        tmp.replace(prefs.path)
        prefs.reload()
        return RedirectResponse("/prefs?msg=Saved", status_code=303)

    # ----------------------------------------------------------------- status
    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        jobs = {j: db.last_run(j) for j in ("watchlist", "discovery", "digest", "recheck")}
        return render(request, "status.html", sources=db.source_statuses(), jobs=jobs, stats=db.stats(),
                      excel=agent.excel_status, excel_path=s.excel_path, scorer=agent.scorer.mode,
                      push=agent.notifier.push_enabled, email=agent.notifier.email_enabled,
                      configured=[src.name for src in agent.sources], eod=db.kv_get("eod_sent"))

    @app.post("/run/{job}")
    def run_job(job: str, back: str = Form("/status")):
        fn = {"discovery": agent.run_discovery, "watchlist": agent.run_watchlist, "digest": agent.run_digest,
              "recheck": agent.run_recheck, "excel": agent.export_excel}.get(job)
        back = back if back.startswith("/") and not back.startswith("//") else "/status"
        back = re.sub(r"([?&])msg=[^&]*&?", r"\1", back).rstrip("?&")
        if not fn:
            return RedirectResponse(back, status_code=303)
        if job in running:
            msg = f"{job} is already running"
        else:
            running.add(job)

            def work():
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception("Dashboard job %s failed", job)
                finally:
                    running.discard(job)

            threading.Thread(target=work, name=f"dash-{job}", daemon=True).start()
            msg = f"Started {job}"
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}msg={quote(msg)}", status_code=303)

    @app.get("/api/running")
    def api_running():
        return JSONResponse({"running": sorted(running)})

    app.state.utc = timezone.utc
    return app
