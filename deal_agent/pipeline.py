"""The agent: collect -> details -> annotate -> score -> notify, plus digests, reminders,
feedback, daily re-check, end-of-day email and the Excel log."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from deal_agent import recheck, report, students, trust, watchfile
from deal_agent.config import Preferences, Settings, load_sources_config, migrate_legacy_db
from deal_agent.db import Database
from deal_agent.excel import Exporter
from deal_agent.feedback import NtfyBridge, similarity_penalty
from deal_agent.filters import detect_audience, gender_block_reason
from deal_agent.http import PoliteHttp
from deal_agent.matcher import find_matches
from deal_agent.models import Deal, ScoreResult, utcnow
from deal_agent.notifier import Alert, Notifier
from deal_agent.parsing import keyword_hit, normalize
from deal_agent.products import best_offer, lowest_badge, offer_sort_key, product_key
from deal_agent.scorer import Scorer
from deal_agent.sources import Source, SourceBlocked, build_sources

log = logging.getLogger(__name__)
DEFAULT_DIGEST_TIMES = ["11:00", "14:30", "17:30", "22:30"]


def parse_times(values) -> list[tuple[int, int]]:
    """["11:00", "14:30"] -> [(11, 0), (14, 30)]; invalid entries are skipped."""
    out = []
    for v in values or []:
        try:
            h, m = (int(x) for x in str(v).strip().split(":"))
        except ValueError:
            log.warning("Ignoring invalid digest time %r (use HH:MM)", v)
            continue
        if 0 <= h < 24 and 0 <= m < 60:
            out.append((h, m))
    return sorted(set(out))


class Agent:
    LLM_RETRY_HOURS = 3
    BLOCK_BACKOFF_HOURS = 6

    def __init__(self, settings: Settings, prefs: Preferences | None = None, db: Database | None = None,
                 sources: list[Source] | None = None, scorer: Scorer | None = None,
                 notifier: Notifier | None = None, bridge: NtfyBridge | None = None,
                 http: PoliteHttp | None = None):
        self.settings = settings
        if db is None:
            migrate_legacy_db(settings)
        self.prefs = prefs or Preferences.load(settings.preferences_file)
        self.db = db or Database(settings.db_path)
        self.http = http or PoliteHttp(settings.user_agent)
        self.sources = sources if sources is not None else build_sources(
            load_sources_config(settings.sources_file), self.http, self.prefs, tz=settings.timezone)
        self.scorer = scorer or Scorer(settings, self.prefs)
        self.notifier = notifier or Notifier(settings)
        self.bridge = bridge or NtfyBridge(settings)
        self.zone = ZoneInfo(settings.timezone)
        self._lock = threading.RLock()   # jobs never overlap
        self.excel_status = "n/a"

    # ------------------------------------------------------------- helpers
    @property
    def sources_by_name(self) -> dict[str, Source]:
        return {s.name: s for s in self.sources}

    def local_now(self) -> datetime:
        return datetime.now(self.zone)

    def _in_quiet_hours(self) -> bool:
        qh = self.prefs.quiet_hours
        if not qh:
            return False
        start, end = qh
        hour = self.local_now().hour
        return start <= hour < end if start < end else hour >= start or hour < end

    def _is_excluded(self, d: Deal) -> str | None:
        text = normalize(f"{d.title} {d.description}")
        return next((kw for kw in self.prefs.exclude_keywords if keyword_hit(text, kw)), None)

    def _retry_llm(self, d: Deal) -> bool:
        """After an LLM failure, keep retrying a fresh deal; accept the heuristic once it's older."""
        if d.first_seen is None:
            return False
        return utcnow() - d.first_seen < timedelta(hours=self.LLM_RETRY_HOURS)

    def _gender_reason(self, d: Deal, audience: str | None = None, category: str | None = None) -> str | None:
        return gender_block_reason(category or d.category, audience or d.audience, self.prefs.men_only_categories)

    # ------------------------------------------------------------- collect
    def collect(self, force: bool = False) -> int:
        new = 0
        for src in self.sources:
            until = self.db.source_blocked_until(src.name)
            if until and not force:
                log.info("Source %s is backed off until %s (blocked earlier)", src.name, until.astimezone(self.zone))
                continue
            before = getattr(src, "_last_fetch", None)
            try:
                deals = src.collect(force=force)
            except SourceBlocked as exc:
                n = self.db.source_failed(src.name, f"blocked: {exc}", self.BLOCK_BACKOFF_HOURS)
                log.warning("Source %s blocked automated access (%s); skipping it (failure #%d)", src.name, exc, n)
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("Source %s crashed", src.name)
                self.db.source_failed(src.name, f"error: {exc}", None)
                continue
            if deals or getattr(src, "_last_fetch", None) != before:
                self.db.source_ok(src.name, len(deals))
            for d in deals:
                deal_id, is_new = self.db.upsert_deal(d)
                if is_new:
                    new += 1
                    stored = self.db.get_deal(deal_id)
                    if stored:
                        self.annotate(stored)
        log.info("Collected %d new deals from %d sources", new, len(self.sources))
        return new

    def backfill_v1(self) -> int:
        """One-time upgrade of v1 rows: re-classify, annotate, and queue for re-scoring."""
        if self.db.kv_get("v2_backfill") == "done":
            return 0
        from deal_agent.parsing import detect_category
        rows = self.db._deals("SELECT * FROM deals WHERE trust_label IS NULL")
        for d in rows:
            cat = self.sources_by_name[d.source].default_category if d.source in self.sources_by_name else None
            d.category = cat or detect_category(f"{d.source_category or ''} {d.title}", self.prefs.categories) or d.category
            self.db.update_deal(d.id, category=d.category, score=None, score_reason=None)
            self.annotate(d)
        self.db.kv_set("v2_backfill", "done")
        if rows:
            log.info("Upgraded %d deals from the v1 database (re-scored on the next runs)", len(rows))
        return len(rows)

    def annotate(self, d: Deal) -> Deal:
        """Trust label + warnings, audience, product key, final price, student discount."""
        res = trust.assess(d, self.prefs.all_brands,
                           below_market_pct=float(self.prefs.get("trust", "below_market_pct", 50)),
                           fakeshop_link=bool(self.prefs.get("trust", "fakeshop_link", True)))
        d.trust_label, d.trust_reason, d.warnings = res.label, res.reason, res.warnings
        if d.audience is None:
            d.audience = detect_audience(f"{d.title} {d.source_category or ''} {d.description[:160]}")
        if not d.product_key:
            d.product_key = product_key(d)
        d.final_price_est = students.final_price(d)
        d.student = students.evaluate(d, self.prefs)
        if d.id is not None:
            self.db.update_deal(d.id, trust_label=d.trust_label, trust_reason=d.trust_reason, warnings=d.warnings,
                                audience=d.audience, product_key=d.product_key, final_price_est=d.final_price_est,
                                student=d.student)
        return d

    def enrich_details(self, deals: list[Deal] | None = None, limit: int | None = None) -> int:
        """Fetch full deal pages (mydealz) for promising deals: shop domain, coupon, market price."""
        by_name = self.sources_by_name
        detail_sources = [n for n, s in by_name.items() if s.has_detail]
        limit = limit if limit is not None else int(self.prefs.scoring("details_per_run", 40))
        todo = deals if deals is not None else self.db.deals_needing_detail(detail_sources, limit)
        blocked: set[str] = set()
        done = 0
        for d in todo[:limit]:
            src = by_name.get(d.source)
            if not src or not src.has_detail or d.detail_fetched or src.name in blocked:
                continue
            try:
                info = src.detail(d)
            except SourceBlocked as exc:
                log.warning("Deal pages of %s blocked (%s); skipping details this run", src.name, exc)
                blocked.add(src.name)
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("detail fetch failed for %s: %s", d.link, exc)
                self.db.update_deal(d.id, detail_fetched=True)
                continue
            fields: dict = {"detail_fetched": True}
            if info:
                for k in ("shop_domain", "coupon_code", "shipping", "market_price", "comment_count", "temperature"):
                    if info.get(k) is not None:
                        fields[k] = info[k]
                if info.get("merchant") and not d.merchant:
                    fields["merchant"] = info["merchant"]
                if info.get("expires_at") and not d.expires_at:
                    fields["expires_at"] = info["expires_at"]
                if info.get("price") is not None and d.price is None:
                    fields["price"] = info["price"]
                if info.get("expired"):
                    fields["status"] = "Expired"
            self.db.update_deal(d.id, **fields)
            fresh = self.db.get_deal(d.id)
            if fresh:
                self.annotate(fresh)
            done += 1
        if done:
            log.info("Fetched details for %d deals", done)
        return done

    # --------------------------------------------------------------- scoring
    def score_pending(self) -> int:
        limit = int(self.prefs.scoring("max_deals_per_run", 60))
        pending = self.db.unscored_deals(limit=5000)
        if not pending:
            return 0
        to_llm: list[Deal] = []
        prefilter = bool(self.prefs.scoring("prefilter", True))
        for d in pending:
            kw = self._is_excluded(d)
            gender = self._gender_reason(d)
            if kw:
                self.db.save_score(d.id, ScoreResult(score=0, reason=f"excluded keyword: {kw}"))
            elif gender:
                self.db.save_score(d.id, ScoreResult(score=0, reason=f"filtered: {gender}"))
            elif prefilter and d.category is None and d.brand is None:
                self.db.save_score(d.id, ScoreResult(score=0, reason="prefilter: outside preferred categories/brands"))
            else:
                to_llm.append(d)
        if len(to_llm) > limit:
            log.info("%d deals need scoring; capping at %d this run (rest next run)", len(to_llm), limit)
            to_llm = to_llm[:limit]
        if not to_llm:
            return len(pending)
        log.info("Scoring %d deals with %s", len(to_llm), self.scorer.mode)
        dismissed = self.db.dismissed_deals()
        held = 0
        for d, res in zip(to_llm, self.scorer.score_deals(to_llm)):
            if res.fallback and self._retry_llm(d):
                held += 1
                continue
            gender = self._gender_reason(d, audience=res.audience, category=res.category)
            if gender:
                res.score, res.reason = 0.0, f"filtered: {gender}"
            key = product_key(d, res.product)
            d.product_key = key
            penalty = similarity_penalty(d, dismissed)
            if penalty and res.score > 0:
                res.score = max(0.0, res.score - penalty)
                res.reason = f"{res.reason} (−{penalty:g}: similar to deals you dismissed)"
            self.db.save_score(d.id, res, key)
            if penalty:
                self.db.update_deal(d.id, penalty=penalty)
        if held:
            log.warning("LLM unavailable: %d deals left unscored for retry", held)
        return len(pending)

    # ---------------------------------------------------------------- alerts
    def _effective(self, d: Deal, dismissed: list[Deal]) -> float:
        """Stored score with the *current* dismiss-penalty (feedback may arrive after scoring)."""
        base = (d.score or 0) + (d.penalty or 0)
        return round(max(0.0, base - similarity_penalty(d, dismissed)) if d.score else 0.0, 2)

    def _group_best(self, deals: list[Deal]) -> list[tuple[Deal, list[Deal]]]:
        """Collapse offers of the same product to the cheapest trustworthy *unalerted* one.
        Groups where a better offer was already alerted are dropped (marked 'grouped')."""
        groups: dict[str, list[Deal]] = {}
        order: list[str] = []
        for d in deals:
            key = d.product_key or f"id{d.id}"
            if key not in groups:
                order.append(key)
                groups[key] = []
            groups[key].append(d)
        out = []
        for key in order:
            cands = groups[key]
            best, rest = best_offer(cands)
            others = list(rest)
            if not key.startswith("id"):
                ids = {o.id for o in cands}
                others += [o for o in self.db.offers_for(key) if o.id not in ids]
            better_sent = [o for o in others if self.db.has_alert(o.id)
                           and offer_sort_key(o) < offer_sort_key(best)]
            if better_sent:
                for c in cands:
                    self.db.record_alert(c.id, "grouped", mark_notified=False)
                continue
            out.append((best, others))
        return out

    def _alert(self, kind: str, d: Deal, others: list[Deal] | None = None, **kw) -> Alert:
        badge = None
        if d.product_key:
            badge = lowest_badge(self.db.price_stats(d.product_key, 90, exclude_deal_id=d.id), d.effective_price)
        return Alert(kind, d, score=kw.pop("score", d.score), reason=kw.pop("reason", d.score_reason),
                     badge=badge, also_at=others or [], **kw)

    def _deliver(self, alerts: list[Alert], dry_run: bool) -> list[Alert]:
        sent = self.notifier.push(alerts, dry_run=dry_run)
        for a in sent:
            self.db.record_alert(a.deal.id, a.kind, a.watch.id if a.watch else None)
            for o in a.also_at:                     # same product elsewhere: don't alert it separately
                if not self.db.has_alert(o.id):
                    self.db.record_alert(o.id, "grouped", mark_notified=False)
        return alerts if dry_run else sent

    def sync_sent(self) -> int:
        """Treat deals already pushed (by this PC or the cloud runner) as alerted."""
        try:
            uids = self.bridge.sent_uids("12h")
        except Exception as exc:  # noqa: BLE001
            log.debug("ntfy sent-sync failed: %s", exc)
            return 0
        n = 0
        for uid in uids:
            d = self.db.get_by_uid(uid)
            if d and not self.db.has_alert(d.id):
                self.db.record_alert(d.id, "synced")
                n += 1
        if n:
            log.info("Marked %d deals as already sent (seen on the ntfy topic)", n)
        return n

    def dispatch_instant(self, dry_run: bool = False) -> list[Alert]:
        """Great deals only: score >= instant_score, or above threshold and ending within N hours.
        During quiet hours they are queued for the next digest instead of being pushed."""
        quiet = self._in_quiet_hours() and not dry_run
        base = self.prefs.notify_threshold
        inst = float(self.prefs.notifications("instant_score", 9.0))
        exp_h = float(self.prefs.notifications("instant_expiry_hours", 3))
        now = utcnow()
        dismissed = self.db.dismissed_deals()
        chosen = []
        for d in self.db.alert_candidates(base, max_age_hours=24, include_queued=False):
            eff = self._effective(d, dismissed)
            ending = d.expires_at is not None and d.expires_at - now <= timedelta(hours=exp_h)
            if eff >= inst or (eff >= base and ending):
                d.score = eff
                chosen.append(d)
        if quiet:
            for d in chosen:
                self.db.update_deal(d.id, queued_digest=True)
            if chosen:
                log.info("Quiet hours: %d instant alert(s) queued for the next digest", len(chosen))
            return []
        alerts = [self._alert("instant", best, others) for best, others in self._group_best(chosen)]
        alerts = alerts[: int(self.prefs.notifications("max_instant_per_run", 5))]
        return self._deliver(alerts, dry_run)

    def digest_candidates(self) -> list[Alert]:
        """Deals queued during quiet hours (always included), then the top N other deals that
        became candidates (seen or scored) since the last digest."""
        since = self.db.last_run("digest") or utcnow() - timedelta(hours=24)
        since = max(since, utcnow() - timedelta(hours=24))
        dismissed = self.db.dismissed_deals()
        base = self.prefs.notify_threshold
        queued = self.db.queued_deals()
        queued_ids = {d.id for d in queued}
        fresh = []
        for d in self.db.alert_candidates(base, max_age_hours=24, new_since=since):
            if d.id in queued_ids:
                continue
            eff = self._effective(d, dismissed)
            if eff >= base:
                d.score = eff
                fresh.append(d)
        held = self._group_best(queued)
        grouped = self._group_best(sorted(fresh, key=lambda x: -(x.score or 0)))
        grouped.sort(key=lambda bo: -(bo[0].score or 0))
        size = int(self.prefs.notifications("digest_size", 3))
        top = held + grouped[: max(0, size - len(held))]
        n = len(top)
        return [self._alert("digest", best, others, position=(i, n)) for i, (best, others) in enumerate(top, 1)]

    def digest_times(self) -> list[tuple[int, int]]:
        return parse_times(self.prefs.notifications("digest_times", DEFAULT_DIGEST_TIMES))

    def last_digest_slot(self, now: datetime | None = None) -> datetime | None:
        """Most recent scheduled digest time at or before now (local), within the last 2 days."""
        now = (now or self.local_now()).astimezone(self.zone)
        slots = [datetime.combine(now.date() - timedelta(days=back), time(h, m), self.zone)
                 for back in (0, 1) for h, m in self.digest_times()]
        past = [t for t in slots if t <= now]
        return max(past) if past else None

    # ------------------------------------------------------------------ jobs
    def run_discovery(self, dry_run: bool = False, force_fetch: bool = False) -> list[Alert]:
        with self._lock:
            self.prefs.reload()
            self.backfill_v1()
            self.collect(force=force_fetch)
            self.enrich_details()
            self.score_pending()
            if dry_run:
                instant = self.dispatch_instant(dry_run=True)
                seen = {a.deal.id for a in instant}
                return instant + [a for a in self.digest_candidates() if a.deal.id not in seen]
            self.sync_sent()
            sent = self.dispatch_instant()
            self.db.set_last_run("discovery")
            self.export_excel()
            log.info("Discovery done: %d instant alert(s)", len(sent))
            return sent

    def run_digest(self, dry_run: bool = False) -> list[Alert]:
        with self._lock:
            self.prefs.reload()
            if self._in_quiet_hours() and not dry_run:
                log.info("Quiet hours: digest held")
                return []
            if not dry_run and self.prefs.notifications("digest_refresh", True):
                self.collect()                  # fresh content right before the digest
                self.enrich_details()
                self.score_pending()
            if not dry_run:
                self.sync_sent()
            watch = self._watch_alerts(queued=True)
            alerts = self.digest_candidates()
            if not alerts and not watch:
                log.info("Digest: nothing new above the threshold; skipped")
                if not dry_run:
                    self.db.set_last_run("digest")
                return []
            sent = self._deliver(watch, dry_run) + self._deliver(alerts, dry_run)
            if not dry_run:
                for a in sent:
                    if a.watch:
                        self.db.mark_watch_notified(a.watch.id, a.deal.id)
                self.db.set_last_run("digest")
                self.export_excel()
            log.info("Digest: %d deal(s) %s", len(sent), "previewed" if dry_run else "sent")
            return sent

    def _watch_alerts(self, queued: bool | None = None) -> list[Alert]:
        alerts = []
        for w, d, s, r in self.db.pending_watch_matches(self.prefs.watchlist_threshold, 10, queued=queued):
            others = [o for o in self.db.offers_for(d.product_key) if o.id != d.id] if d.product_key else []
            alerts.append(self._alert("watchlist", d, others, score=s, reason=r, watch=w))
        return alerts

    def run_watchlist(self, dry_run: bool = False, force_fetch: bool = False) -> list[Alert]:
        with self._lock:
            self.prefs.reload()
            items = self.db.list_watch(active_only=True)
            if not items:
                log.info("Watchlist is empty")
                self.db.set_last_run("watchlist")
                return []
            self.collect(force=force_fetch)
            if not dry_run:
                self.enrich_details()
                self.score_pending()          # hourly scoring keeps up with the feed volume
            recent = self.db.recent_deals(days=7)
            pairs = [(w, d) for w, d in find_matches(items, recent) if d.id not in self.db.checked_deal_ids(w.id)]
            if pairs:
                self.enrich_details([d for _, d in pairs], limit=len(pairs))
                pairs = [(w, self.db.get_deal(d.id) or d) for w, d in pairs]
                verify = []
                for w, d in pairs:
                    reason = self._gender_reason(d, category=d.category or w.category)
                    if reason:
                        self.db.save_watch_match(w.id, d.id, 0, f"filtered: {reason}")
                    else:
                        verify.append((w, d))
                if verify:
                    log.info("Verifying %d watchlist candidates with %s", len(verify), self.scorer.mode)
                    for (w, d), res in zip(verify, self.scorer.score_matches(verify)):
                        if res.fallback and self._retry_llm(d):
                            continue
                        reason = self._gender_reason(d, audience=res.audience, category=d.category or w.category)
                        if reason:
                            res.score, res.reason = 0.0, f"filtered: {reason}"
                        if res.product and not d.ean:
                            self.db.update_deal(d.id, product_key=product_key(d, res.product))
                        self.db.save_watch_match(w.id, d.id, res.score, res.reason)
            quiet = self._in_quiet_hours() and not dry_run
            if quiet or not self.prefs.notifications("watchlist_instant", True):
                if not dry_run:
                    n = self.db.queue_watch_matches(self.prefs.watchlist_threshold)
                    if n:
                        log.info("Watchlist: %d hit(s) queued for the next digest", n)
                    self.db.set_last_run("watchlist")
                return []
            if not dry_run:
                self.sync_sent()
            sent = self._deliver(self._watch_alerts(queued=False), dry_run)
            if not dry_run:
                for a in sent:
                    self.db.mark_watch_notified(a.watch.id, a.deal.id)
                self.db.set_last_run("watchlist")
                self.export_excel()
            log.info("Watchlist: %d new candidates, %d alert(s)", len(pairs), len(sent))
            return sent

    def run_reminders(self, dry_run: bool = False) -> list[Alert]:
        with self._lock:
            self.prefs.reload()
            if self._in_quiet_hours() and not dry_run:
                return []
            hours = float(self.prefs.notifications("reminder_hours_before_expiry", 6))
            out = []
            due = [self._alert("reminder", d) for d in self.db.reminders_due(hours)]
            for a in self._deliver(due, dry_run):
                if not dry_run:
                    self.db.mark_reminded(a.deal.id)
                out.append(a)
            snoozed = [self._alert("snoozed", d) for d in self.db.snooze_resends_due()]
            for a in self._deliver(snoozed, dry_run):
                if not dry_run:
                    self.db.update_deal(a.deal.id, snooze_resend=False)
                out.append(a)
            out += self.dispatch_instant(dry_run)     # catches deals that now end within 3 h
            if out:
                log.info("Reminders/instant: %d", len(out))
            return out

    def run_feedback(self) -> int:
        with self._lock:
            if not self.bridge.enabled:
                return 0
            try:
                cmds, cursor = self.bridge.feedback_commands(self.db.kv_get("ntfy_feedback_cursor"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not read ntfy feedback: %s", exc)
                return 0
            hours = float(self.prefs.notifications("snooze_hours", 24))
            for action, uid in cmds:
                d = self.db.snooze(uid, hours) if action == "snooze" else self.db.dismiss(uid)
                log.info("Feedback: %s %s", action, d.title[:60] if d else f"(unknown deal {uid})")
            if cursor:
                self.db.kv_set("ntfy_feedback_cursor", cursor)
            return len(cmds)

    def run_recheck(self) -> dict:
        with self._lock:
            self.prefs.reload()
            counts = recheck.run(self.db, self.http, int(self.prefs.get("recheck", "max_per_run", 150)),
                                 float(self.prefs.get("excel", "min_score", 5.0)))
            self.db.set_last_run("recheck")
            self.export_excel()
            return counts

    def run_eod(self, dry_run: bool = False) -> bool:
        with self._lock:
            self.prefs.reload()
            self.export_excel()
            note = "" if self.excel_status == "ok" else " · Excel file was locked; attached copy is the newest"
            built = report.build(self.db, self.prefs, self.settings.timezone, note)
            today = self.local_now().date().isoformat()
            if built is None:
                log.info("End-of-day email: nothing was shown today; skipped")
                if not dry_run:
                    self.db.kv_set("eod_sent", today)
                return False
            subject, html_body, text = built
            if dry_run:
                log.info("End-of-day email preview: %s", subject)
                return True
            attach = self.settings.excel_path
            from deal_agent.excel import pending_path
            if self.excel_status == "pending" and pending_path().exists():
                attach = pending_path()
            ok = self.notifier.email(subject, html_body, text, attachments=[attach])
            if ok:
                self.db.kv_set("eod_sent", today)
            return ok

    def export_excel(self) -> str:
        try:
            self.excel_status = Exporter(self.db, self.prefs, self.settings.excel_path, self.settings.timezone).export()
        except Exception:  # noqa: BLE001 - the log must never break a job
            log.exception("Excel export failed")
            self.excel_status = "error"
        return self.excel_status

    def retry_excel(self) -> str:
        with self._lock:
            ex = Exporter(self.db, self.prefs, self.settings.excel_path, self.settings.timezone)
            if ex.pending.exists():
                self.excel_status = ex.flush_pending()
            return self.excel_status

    def heartbeat(self) -> None:
        if self.settings.heartbeat_enabled:
            try:
                self.bridge.heartbeat()
            except Exception as exc:  # noqa: BLE001
                log.debug("heartbeat failed: %s", exc)

    # ---------------------------------------------------------- cloud / cron
    def _due(self, job: str, hours: float) -> bool:
        last = self.db.last_run(job)
        return last is None or utcnow() - last >= timedelta(hours=hours) - timedelta(minutes=10)

    def digest_due(self, now: datetime | None = None) -> bool:
        slot = self.last_digest_slot(now)
        last = self.db.last_run("digest")
        return slot is not None and (last is None or last < slot)

    def _daily_due(self, job_key: str, at: str, kv: bool = False) -> bool:
        now = self.local_now()
        hh, mm = (int(x) for x in str(at).split(":"))
        if now.time() < now.replace(hour=hh, minute=mm).time():
            return False
        if kv:
            return self.db.kv_get(job_key) != now.date().isoformat()
        last = self.db.last_run(job_key)
        return last is None or last.astimezone(self.zone).date() < now.date()

    def run_due(self, skip_if_pc_online: bool = False) -> list[str]:
        """Run every job that is due. Used by `deal_agent cron` (GitHub Actions, Task Scheduler)."""
        if skip_if_pc_online and self.bridge.pc_online():
            log.info("PC agent is online (recent heartbeat); cloud run skipped")
            return []
        ran = []
        self.prefs.reload()
        if self.settings.cloud:
            watchfile.sync_into_db(self.db, self.settings.watchlist_file)
        self.run_feedback()
        if self._due("watchlist", float(self.prefs.schedule("watchlist_hours", 1))):
            self.run_watchlist(); ran.append("watchlist")
        if self._due("discovery", float(self.prefs.schedule("discovery_hours", 3))):
            self.run_discovery(); ran.append("discovery")
        if self.digest_due():
            self.run_digest(); ran.append("digest")
        self.run_reminders(); ran.append("reminders")
        if self._daily_due("recheck", self.prefs.get("recheck", "time", "12:00")):
            self.run_recheck(); ran.append("recheck")
        if self._daily_due("eod_sent", self.prefs.notifications("eod_email_time", "21:00"), kv=True):
            self.run_eod(); ran.append("eod")
        self.export_excel()
        return ran
