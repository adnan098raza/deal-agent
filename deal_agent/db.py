"""SQLite persistence: deals, alerts, price history, watchlist, feedback, job state.

Schema changes are applied in place (missing columns are added), so a v1 database keeps
its data when the agent is upgraded.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from deal_agent.models import Deal, ScoreResult, WatchItem, utcnow

# column -> SQL type/default. Order matters only for readability.
DEAL_COLUMNS: dict[str, str] = {
    "uid": "TEXT NOT NULL UNIQUE",
    "title": "TEXT NOT NULL",
    "brand": "TEXT",
    "category": "TEXT",
    "price": "REAL",
    "original_price": "REAL",
    "discount_pct": "REAL",
    "currency": "TEXT DEFAULT 'EUR'",
    "link": "TEXT NOT NULL",
    "expires_at": "TEXT",
    "source": "TEXT NOT NULL",
    "merchant": "TEXT",
    "shop_domain": "TEXT",
    "description": "TEXT",
    "image_url": "TEXT",
    "temperature": "INTEGER",
    "source_category": "TEXT",
    "published_at": "TEXT",
    "coupon_code": "TEXT",
    "shipping": "REAL",
    "market_price": "REAL",
    "seller_name": "TEXT",
    "seller_rating": "REAL",
    "ean": "TEXT",
    "comment_count": "INTEGER",
    "trust_hint": "TEXT",
    "score": "REAL",
    "score_reason": "TEXT",
    "scored_at": "TEXT",
    "audience": "TEXT",
    "product_key": "TEXT",
    "trust_label": "TEXT",
    "trust_reason": "TEXT",
    "warnings": "TEXT",
    "student": "TEXT",
    "final_price_est": "REAL",
    "status": "TEXT NOT NULL DEFAULT 'Live'",
    "current_price": "REAL",
    "last_checked": "TEXT",
    "detail_fetched": "INTEGER NOT NULL DEFAULT 0",
    "notified": "INTEGER NOT NULL DEFAULT 0",
    "notified_at": "TEXT",
    "reminder_sent": "INTEGER NOT NULL DEFAULT 0",
    "dismissed": "INTEGER NOT NULL DEFAULT 0",
    "snoozed_until": "TEXT",
    "snooze_resend": "INTEGER NOT NULL DEFAULT 0",
    "penalty": "REAL NOT NULL DEFAULT 0",
    "queued_digest": "INTEGER NOT NULL DEFAULT 0",
    "first_seen": "TEXT NOT NULL",
    "last_seen": "TEXT NOT NULL",
}
JSON_COLS = {"warnings", "student"}
DT_COLS = {"expires_at", "published_at", "scored_at", "last_checked", "notified_at", "snoozed_until",
           "first_seen", "last_seen"}
BOOL_COLS = {"detail_fetched", "notified", "reminder_sent", "dismissed", "snooze_resend", "queued_digest"}
# fields copied from a freshly fetched Deal on insert
SOURCE_FIELDS = ["title", "brand", "category", "price", "original_price", "discount_pct", "currency", "link",
                 "expires_at", "source", "merchant", "shop_domain", "description", "image_url", "temperature",
                 "source_category", "published_at", "coupon_code", "shipping", "market_price", "seller_name",
                 "seller_rating", "ean", "comment_count", "trust_hint", "audience", "product_key"]
# fields refreshed on re-sighting if the source provides them
REFRESH_FIELDS = ["temperature", "original_price", "discount_pct", "merchant", "shop_domain", "coupon_code",
                  "shipping", "market_price", "seller_name", "seller_rating", "ean", "comment_count"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    {", ".join(f"{c} {t}" for c, t in DEAL_COLUMNS.items())}
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL DEFAULT '', brand TEXT, category TEXT, max_price REAL, min_discount REAL,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_matches (
    watch_id INTEGER NOT NULL REFERENCES watchlist(id) ON DELETE CASCADE,
    deal_id INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    score REAL, reason TEXT, notified INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (watch_id, deal_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, watch_id INTEGER, sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
    product_key TEXT, price REAL NOT NULL, shop TEXT, seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER REFERENCES deals(id) ON DELETE SET NULL,
    action TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_status (
    name TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT, last_error_at TEXT,
    blocked_until TEXT, failures INTEGER NOT NULL DEFAULT 0, items INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_deals_first_seen ON deals(first_seen);
CREATE INDEX IF NOT EXISTS idx_deals_pending ON deals(notified, score);
CREATE INDEX IF NOT EXISTS idx_deals_product ON deals(product_key);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_deal ON alerts(deal_id);
CREATE INDEX IF NOT EXISTS idx_ph_product ON price_history(product_key, seen_at);
CREATE INDEX IF NOT EXISTS idx_ph_deal ON price_history(deal_id);
"""


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _encode(col: str, value: Any) -> Any:
    if col in JSON_COLS:
        return json.dumps(value, ensure_ascii=False) if value not in (None, [], {}) else None
    if col in DT_COLS:
        return _iso(value) if isinstance(value, datetime) else value
    if col in BOOL_COLS:
        return int(bool(value))
    return value


def _row_to_deal(r: sqlite3.Row) -> Deal:
    keys = r.keys()

    def g(k, default=None):
        return r[k] if k in keys else default

    d = Deal(title=r["title"], link=r["link"], source=r["source"])
    for col in DEAL_COLUMNS:
        if col in ("uid",) or col not in keys:
            continue
        v = g(col)
        if col in JSON_COLS:
            v = json.loads(v) if v else ([] if col == "warnings" else None)
        elif col in DT_COLS:
            v = _dt(v)
        elif col in BOOL_COLS:
            v = bool(v)
        if hasattr(d, col):
            setattr(d, col, v)
    d.id = r["id"]
    d.currency = d.currency or "EUR"
    d.description = d.description or ""
    d.status = d.status or "Live"
    d.penalty = d.penalty or 0.0
    d.extra = {k: r[k] for k in keys if k.startswith("x_")}
    return d


def _row_to_watch(r: sqlite3.Row) -> WatchItem:
    return WatchItem(id=r["id"], query=r["query"], brand=r["brand"], category=r["category"],
                     max_price=r["max_price"], min_discount=r["min_discount"], active=bool(r["active"]),
                     created_at=_dt(r["created_at"]))


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        memory = str(path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False) if memory else None
        with self.connect() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.executescript(INDEXES)

    def _migrate(self, c: sqlite3.Connection) -> None:
        have = {row[1] for row in c.execute("PRAGMA table_info(deals)")}
        for col, typ in DEAL_COLUMNS.items():
            if col not in have:
                typ = typ.replace(" UNIQUE", "")   # SQLite can't add UNIQUE columns
                c.execute(f"ALTER TABLE deals ADD COLUMN {col} {typ}")
        c.execute("UPDATE deals SET category = 'clothing' WHERE category = 'clothes'")   # v1 name
        if "queued" not in {row[1] for row in c.execute("PRAGMA table_info(watch_matches)")}:
            c.execute("ALTER TABLE watch_matches ADD COLUMN queued INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._memory_conn or sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                if self._memory_conn is None:
                    conn.close()

    def _deals(self, sql: str, args: tuple | list = ()) -> list[Deal]:
        with self.connect() as c:
            return [_row_to_deal(r) for r in c.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ deals
    def upsert_deal(self, d: Deal) -> tuple[int, bool]:
        """Insert a new deal or refresh an existing one. Returns (id, is_new)."""
        now = utcnow()
        with self.connect() as c:
            row = c.execute("SELECT id, price, product_key FROM deals WHERE uid = ?", (d.uid,)).fetchone()
            if row is None:
                cols = ["uid", *SOURCE_FIELDS, "first_seen", "last_seen"]
                vals = [d.uid, *[_encode(f, getattr(d, f)) for f in SOURCE_FIELDS], _iso(now), _iso(now)]
                cur = c.execute(f"INSERT INTO deals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
                deal_id = cur.lastrowid
                if d.price is not None:
                    self._add_price(c, deal_id, d.product_key, d.price, d.shop, now)
                return deal_id, True
            deal_id = row["id"]
            sets, args = ["last_seen = ?"], [_iso(now)]
            for f in REFRESH_FIELDS:
                v = getattr(d, f)
                if v is not None:
                    sets.append(f"{f} = ?")
                    args.append(_encode(f, v))
            if d.expires_at is not None:
                sets.append("expires_at = COALESCE(expires_at, ?)")
                args.append(_iso(d.expires_at))
            if d.price is not None and row["price"] is not None and abs(d.price - row["price"]) > 0.005:
                sets += ["current_price = ?", "status = 'Price changed'"]
                args.append(d.price)
                self._add_price(c, deal_id, row["product_key"], d.price, d.shop, now)
            elif d.price is not None and row["price"] is None:
                sets.append("price = ?")
                args.append(d.price)
            c.execute(f"UPDATE deals SET {', '.join(sets)} WHERE id = ?", (*args, deal_id))
            return deal_id, False

    def update_deal(self, deal_id: int, **fields: Any) -> None:
        bad = set(fields) - set(DEAL_COLUMNS)
        if bad:
            raise ValueError(f"unknown deal columns: {bad}")
        if not fields:
            return
        with self.connect() as c:
            c.execute(f"UPDATE deals SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                      (*[_encode(k, v) for k, v in fields.items()], deal_id))
            if "product_key" in fields and fields["product_key"]:
                c.execute("UPDATE price_history SET product_key = ? WHERE deal_id = ?", (fields["product_key"], deal_id))

    def get_deal(self, deal_id: int) -> Deal | None:
        rows = self._deals("SELECT * FROM deals WHERE id = ?", (deal_id,))
        return rows[0] if rows else None

    def get_by_uid(self, uid: str) -> Deal | None:
        rows = self._deals("SELECT * FROM deals WHERE uid = ?", (uid,))
        return rows[0] if rows else None

    def unscored_deals(self, limit: int, max_age_hours: int = 48) -> list[Deal]:
        since = _iso(utcnow() - timedelta(hours=max_age_hours))
        return self._deals("SELECT * FROM deals WHERE score IS NULL AND first_seen >= ? "
                           "ORDER BY first_seen DESC, id DESC LIMIT ?", (since, limit))

    def deals_needing_detail(self, sources: list[str], limit: int, max_age_hours: int = 48) -> list[Deal]:
        if not sources:
            return []
        since = _iso(utcnow() - timedelta(hours=max_age_hours))
        marks = ",".join("?" * len(sources))
        return self._deals(
            f"""SELECT * FROM deals WHERE detail_fetched = 0 AND source IN ({marks}) AND first_seen >= ?
                  AND (score IS NULL OR score >= 5) AND (category IS NOT NULL OR brand IS NOT NULL)
                ORDER BY (score IS NULL), score DESC, first_seen DESC LIMIT ?""",
            (*sources, since, limit))

    def save_score(self, deal_id: int, res: ScoreResult, product_key: str | None = None) -> None:
        with self.connect() as c:
            c.execute(
                """UPDATE deals SET score = ?, score_reason = ?, scored_at = ?,
                       brand = COALESCE(brand, ?), category = COALESCE(?, category),
                       expires_at = COALESCE(expires_at, ?), audience = COALESCE(?, audience),
                       product_key = COALESCE(?, product_key)
                   WHERE id = ?""",
                (round(res.score, 2), res.reason, _iso(utcnow()), res.brand, res.category,
                 _iso(res.expires_at), res.audience, product_key, deal_id))
            if product_key:
                c.execute("UPDATE price_history SET product_key = ? WHERE deal_id = ? AND product_key IS NULL",
                          (product_key, deal_id))

    def recent_deals(self, days: int = 7, limit: int = 3000) -> list[Deal]:
        now = utcnow()
        return self._deals(
            """SELECT * FROM deals WHERE first_seen >= ? AND dismissed = 0
                 AND status NOT IN ('Expired', 'Out of stock')
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY first_seen DESC LIMIT ?""",
            (_iso(now - timedelta(days=days)), _iso(now), limit))

    def list_deals(self, limit: int = 20, notified_only: bool = False, min_score: float | None = None) -> list[Deal]:
        sql, args = "SELECT * FROM deals WHERE 1=1", []
        if notified_only:
            sql += " AND notified = 1"
        if min_score is not None:
            sql += " AND score >= ?"
            args.append(min_score)
        sql += " ORDER BY first_seen DESC, id DESC LIMIT ?"
        args.append(limit)
        return self._deals(sql, args)

    def search_deals(self, q: str = "", days: int | None = 7, category: str | None = None,
                     min_score: float | None = None, status: str | None = None, limit: int = 200) -> list[Deal]:
        sql, args = "SELECT * FROM deals WHERE 1=1", []
        for tok in q.lower().split():
            sql += " AND LOWER(title || ' ' || COALESCE(brand,'') || ' ' || COALESCE(merchant,'') || ' ' || COALESCE(shop_domain,'')) LIKE ?"
            args.append(f"%{tok}%")
        if days:
            sql += " AND last_seen >= ?"
            args.append(_iso(utcnow() - timedelta(days=days)))
        if category:
            sql += " AND category = ?"
            args.append(category)
        if min_score is not None:
            sql += " AND score >= ?"
            args.append(min_score)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY COALESCE(score, -1) DESC, first_seen DESC LIMIT ?"
        args.append(limit)
        return self._deals(sql, args)

    # --------------------------------------------------------------- alerts
    def alert_candidates(self, min_score: float, max_age_hours: int = 24, include_queued: bool = True,
                         new_since: datetime | None = None) -> list[Deal]:
        """Unalerted live deals. new_since: only deals seen or scored after that time."""
        now = utcnow()
        extra = "" if include_queued else " AND d.queued_digest = 0"
        args: list = []
        if new_since is not None:
            extra += " AND (d.first_seen > ? OR d.scored_at > ?)"
            args = [_iso(new_since), _iso(new_since)]
        return self._deals(
            f"""SELECT * FROM deals d
               WHERE d.score >= ? AND d.dismissed = 0 AND d.first_seen >= ?{extra}
                 AND d.status NOT IN ('Expired', 'Out of stock')
                 AND (d.expires_at IS NULL OR d.expires_at > ?)
                 AND (d.snoozed_until IS NULL OR d.snoozed_until <= ?)
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.deal_id = d.id)
               ORDER BY d.score DESC""",
            (min_score, _iso(now - timedelta(hours=max_age_hours)), *args, _iso(now), _iso(now)))

    def queued_deals(self) -> list[Deal]:
        """Instant-worthy deals held back during quiet hours, waiting for the next digest."""
        now = utcnow()
        return self._deals(
            """SELECT * FROM deals d WHERE d.queued_digest = 1 AND d.dismissed = 0
                 AND d.status NOT IN ('Expired', 'Out of stock') AND (d.expires_at IS NULL OR d.expires_at > ?)
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.deal_id = d.id)
               ORDER BY d.score DESC""", (_iso(now),))

    def record_alert(self, deal_id: int, kind: str, watch_id: int | None = None, mark_notified: bool = True) -> None:
        now = _iso(utcnow())
        with self.connect() as c:
            c.execute("INSERT INTO alerts (deal_id, kind, watch_id, sent_at) VALUES (?,?,?,?)",
                      (deal_id, kind, watch_id, now))
            if mark_notified:
                c.execute("UPDATE deals SET notified = 1, notified_at = COALESCE(notified_at, ?) WHERE id = ?",
                          (now, deal_id))

    def has_alert(self, deal_id: int) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM alerts WHERE deal_id = ? LIMIT 1", (deal_id,)).fetchone() is not None

    def alerts_between(self, start: datetime, end: datetime) -> list[tuple[Deal, str, datetime]]:
        with self.connect() as c:
            rows = c.execute(
                """SELECT d.*, a.kind AS a_kind, a.sent_at AS a_sent FROM alerts a JOIN deals d ON d.id = a.deal_id
                   WHERE a.sent_at >= ? AND a.sent_at < ? ORDER BY a.sent_at""",
                (_iso(start), _iso(end))).fetchall()
            return [(_row_to_deal(r), r["a_kind"], _dt(r["a_sent"])) for r in rows]

    def alert_kinds(self, deal_ids: list[int]) -> dict[int, str]:
        if not deal_ids:
            return {}
        with self.connect() as c:
            marks = ",".join("?" * len(deal_ids))
            rows = c.execute(f"SELECT deal_id, GROUP_CONCAT(DISTINCT kind) k FROM alerts WHERE deal_id IN ({marks}) "
                             "GROUP BY deal_id", deal_ids).fetchall()
            return {r["deal_id"]: r["k"] for r in rows}

    def reminders_due(self, hours_before: float) -> list[Deal]:
        now = utcnow()
        return self._deals(
            """SELECT * FROM deals WHERE notified = 1 AND reminder_sent = 0 AND dismissed = 0
                 AND status NOT IN ('Expired', 'Out of stock') AND expires_at IS NOT NULL
                 AND expires_at > ? AND expires_at <= ?
                 AND (snoozed_until IS NULL OR snoozed_until <= ?)
               ORDER BY expires_at""",
            (_iso(now), _iso(now + timedelta(hours=hours_before)), _iso(now)))

    def mark_reminded(self, deal_id: int) -> None:
        self.update_deal(deal_id, reminder_sent=True)

    # ---------------------------------------------------- snooze / feedback
    def snooze(self, uid: str, hours: float) -> Deal | None:
        d = self.get_by_uid(uid)
        if d:
            self.update_deal(d.id, snoozed_until=utcnow() + timedelta(hours=hours), snooze_resend=True)
            self._feedback(d.id, "snooze")
        return d

    def dismiss(self, uid: str) -> Deal | None:
        d = self.get_by_uid(uid)
        if d:
            self.update_deal(d.id, dismissed=True, snooze_resend=False)
            self._feedback(d.id, "dismiss")
        return d

    def _feedback(self, deal_id: int, action: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO feedback (deal_id, action, created_at) VALUES (?,?,?)",
                      (deal_id, action, _iso(utcnow())))

    def snooze_resends_due(self) -> list[Deal]:
        now = utcnow()
        return self._deals(
            """SELECT * FROM deals WHERE snooze_resend = 1 AND dismissed = 0 AND snoozed_until <= ?
                 AND status NOT IN ('Expired', 'Out of stock') AND (expires_at IS NULL OR expires_at > ?)""",
            (_iso(now), _iso(now)))

    def dismissed_deals(self, days: int = 90) -> list[Deal]:
        return self._deals(
            """SELECT d.* FROM deals d JOIN feedback f ON f.deal_id = d.id
               WHERE f.action = 'dismiss' AND f.created_at >= ? GROUP BY d.id""",
            (_iso(utcnow() - timedelta(days=days)),))

    # --------------------------------------------------------- price history
    @staticmethod
    def _add_price(c, deal_id: int, product_key: str | None, price: float, shop: str | None, when: datetime) -> None:
        c.execute("INSERT INTO price_history (deal_id, product_key, price, shop, seen_at) VALUES (?,?,?,?,?)",
                  (deal_id, product_key, price, shop, _iso(when)))

    def add_price(self, deal_id: int, product_key: str | None, price: float, shop: str | None = None) -> None:
        with self.connect() as c:
            self._add_price(c, deal_id, product_key, price, shop, utcnow())

    def price_stats(self, product_key: str, days: int = 90, exclude_deal_id: int | None = None) -> dict:
        """Min price + number/span of observations for a product, optionally excluding one deal."""
        since = _iso(utcnow() - timedelta(days=days))
        with self.connect() as c:
            r = c.execute(
                "SELECT MIN(price) mn, COUNT(*) n, MIN(seen_at) first, MAX(seen_at) last FROM price_history "
                "WHERE product_key = ? AND seen_at >= ? AND deal_id IS NOT ?",
                (product_key, since, exclude_deal_id)).fetchone()
            return {"min": r["mn"], "count": r["n"], "first": _dt(r["first"]), "last": _dt(r["last"])}

    def price_series(self, product_key: str | None, deal_id: int, days: int = 90) -> list[tuple[datetime, float, str]]:
        since = _iso(utcnow() - timedelta(days=days))
        with self.connect() as c:
            if product_key:
                rows = c.execute("SELECT seen_at, price, shop FROM price_history WHERE product_key = ? AND seen_at >= ? "
                                 "ORDER BY seen_at", (product_key, since)).fetchall()
            else:
                rows = c.execute("SELECT seen_at, price, shop FROM price_history WHERE deal_id = ? ORDER BY seen_at",
                                 (deal_id,)).fetchall()
            return [(_dt(r["seen_at"]), r["price"], r["shop"] or "") for r in rows]

    def lowest_price(self, deal: Deal) -> float | None:
        with self.connect() as c:
            if deal.product_key:
                r = c.execute("SELECT MIN(price) FROM price_history WHERE product_key = ?", (deal.product_key,)).fetchone()
            else:
                r = c.execute("SELECT MIN(price) FROM price_history WHERE deal_id = ?", (deal.id,)).fetchone()
            return r[0]

    def offers_for(self, product_key: str, days: int = 7) -> list[Deal]:
        now = utcnow()
        return self._deals(
            """SELECT * FROM deals WHERE product_key = ? AND last_seen >= ? AND dismissed = 0
                 AND status NOT IN ('Expired', 'Out of stock') AND (expires_at IS NULL OR expires_at > ?)""",
            (product_key, _iso(now - timedelta(days=days)), _iso(now)))

    # -------------------------------------------------------------- recheck
    def recheck_candidates(self, limit: int, min_score: float, days: int = 90) -> list[Deal]:
        now = utcnow()
        return self._deals(
            """SELECT * FROM deals
               WHERE status != 'Expired' AND first_seen >= ?
                 AND (score >= ? OR notified = 1 OR id IN (SELECT deal_id FROM watch_matches))
                 AND (last_checked IS NULL OR last_checked < ?)
               ORDER BY COALESCE(last_checked, '') ASC, score DESC LIMIT ?""",
            (_iso(now - timedelta(days=days)), min_score, _iso(now - timedelta(hours=20)), limit))

    def expire_passed(self) -> int:
        """Deals whose stated expiry has passed become Expired."""
        with self.connect() as c:
            return c.execute("UPDATE deals SET status = 'Expired' WHERE status != 'Expired' AND expires_at IS NOT NULL "
                             "AND expires_at <= ?", (_iso(utcnow()),)).rowcount

    # -------------------------------------------------------------- excel
    def excel_sets(self, min_score: float, active_days: int) -> tuple[list[Deal], list[Deal]]:
        now = utcnow()
        relevant = "(score >= ? OR notified = 1 OR id IN (SELECT deal_id FROM watch_matches))"
        active = self._deals(
            f"""SELECT * FROM deals WHERE {relevant} AND dismissed = 0 AND status NOT IN ('Expired')
                  AND (expires_at IS NULL OR expires_at > ?) AND last_seen >= ?
                ORDER BY score DESC""",
            (min_score, _iso(now), _iso(now - timedelta(days=active_days))))
        expired = self._deals(
            f"""SELECT * FROM deals WHERE {relevant}
                  AND (status = 'Expired' OR (expires_at IS NOT NULL AND expires_at <= ?)
                       OR last_seen < ?)
                ORDER BY COALESCE(last_checked, last_seen) DESC""",
            (min_score, _iso(now), _iso(now - timedelta(days=active_days))))
        return active, expired

    def history_rows(self) -> list[tuple[Deal, float | None]]:
        with self.connect() as c:
            rows = c.execute(
                """SELECT d.*, (SELECT MIN(price) FROM price_history p WHERE p.deal_id = d.id) AS lowest
                   FROM deals d ORDER BY d.first_seen DESC""").fetchall()
            return [(_row_to_deal(r), r["lowest"]) for r in rows]

    # ------------------------------------------------------------ kv / status
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as c:
            r = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return r[0] if r else default

    def kv_set(self, key: str, value: str | None) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (key, value))

    def last_run(self, job: str) -> datetime | None:
        return _dt(self.kv_get(f"last_run:{job}"))

    def set_last_run(self, job: str, when: datetime | None = None) -> None:
        self.kv_set(f"last_run:{job}", _iso(when or utcnow()))

    def source_ok(self, name: str, items: int) -> None:
        with self.connect() as c:
            c.execute("""INSERT INTO source_status (name, last_ok, failures, items, blocked_until) VALUES (?,?,0,?,NULL)
                         ON CONFLICT(name) DO UPDATE SET last_ok = excluded.last_ok, failures = 0,
                           items = excluded.items, blocked_until = NULL""",
                      (name, _iso(utcnow()), items))

    def source_failed(self, name: str, error: str, block_hours: float | None) -> int:
        now = utcnow()
        with self.connect() as c:
            r = c.execute("SELECT failures FROM source_status WHERE name = ?", (name,)).fetchone()
            failures = (r[0] if r else 0) + 1
            until = _iso(now + timedelta(hours=block_hours * min(2 ** (failures - 1), 8))) if block_hours else None
            c.execute("""INSERT INTO source_status (name, last_error, last_error_at, failures, blocked_until)
                         VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET last_error = excluded.last_error,
                           last_error_at = excluded.last_error_at, failures = excluded.failures,
                           blocked_until = excluded.blocked_until""",
                      (name, error[:300], _iso(now), failures, until))
            return failures

    def source_blocked_until(self, name: str) -> datetime | None:
        with self.connect() as c:
            r = c.execute("SELECT blocked_until FROM source_status WHERE name = ?", (name,)).fetchone()
            until = _dt(r[0]) if r and r[0] else None
            return until if until and until > utcnow() else None

    def source_statuses(self) -> list[dict]:
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM source_status ORDER BY name")]

    def stats(self) -> dict:
        with self.connect() as c:
            q = lambda s: c.execute(s).fetchone()[0]  # noqa: E731
            return {
                "deals": q("SELECT COUNT(*) FROM deals"),
                "scored": q("SELECT COUNT(*) FROM deals WHERE score IS NOT NULL"),
                "notified": q("SELECT COUNT(*) FROM deals WHERE notified = 1"),
                "alerts": q("SELECT COUNT(*) FROM alerts"),
                "watch_items": q("SELECT COUNT(*) FROM watchlist WHERE active = 1"),
            }

    def dashboard_metrics(self, today_start: datetime, min_score: float) -> dict:
        """Numbers for the dashboard tiles (today_start: local midnight)."""
        now = utcnow()
        live = ("status NOT IN ('Expired', 'Out of stock') AND dismissed = 0 "
                "AND (expires_at IS NULL OR expires_at > ?) AND last_seen >= ?")
        live_args = (_iso(now), _iso(now - timedelta(days=7)))
        with self.connect() as c:
            one = lambda sql, *a: c.execute(sql, a).fetchone()  # noqa: E731
            new_24h = one("SELECT COUNT(*) FROM deals WHERE first_seen >= ?", _iso(now - timedelta(hours=24)))[0]
            prev_24h = one("SELECT COUNT(*) FROM deals WHERE first_seen >= ? AND first_seen < ?",
                           _iso(now - timedelta(hours=48)), _iso(now - timedelta(hours=24)))[0]
            on_sale, avg_disc, good = one(
                f"SELECT COUNT(*), AVG(discount_pct), COALESCE(SUM(score >= ?), 0) FROM deals "
                f"WHERE discount_pct > 0 AND {live}", min_score, *live_args)
            alerts_today, watch_hits = one(
                "SELECT COUNT(*), COALESCE(SUM(kind = 'watchlist'), 0) FROM alerts WHERE sent_at >= ?",
                _iso(today_start))
            watch_active, watch_paused = one(
                "SELECT COALESCE(SUM(active = 1), 0), COALESCE(SUM(active = 0), 0) FROM watchlist")
        return {"new_24h": new_24h, "prev_24h": prev_24h, "on_sale": on_sale, "avg_discount": avg_disc or 0,
                "good_on_sale": good, "alerts_today": alerts_today, "watch_hits_today": watch_hits,
                "watch_active": watch_active, "watch_paused": watch_paused}

    # -------------------------------------------------------------- watchlist
    def add_watch(self, w: WatchItem) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO watchlist (query, brand, category, max_price, min_discount, active, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (w.query, w.brand, w.category, w.max_price, w.min_discount, int(w.active), _iso(utcnow())))
            return cur.lastrowid

    def remove_watch(self, watch_id: int) -> bool:
        with self.connect() as c:
            return c.execute("DELETE FROM watchlist WHERE id = ?", (watch_id,)).rowcount > 0

    def set_watch_active(self, watch_id: int, active: bool) -> bool:
        with self.connect() as c:
            return c.execute("UPDATE watchlist SET active = ? WHERE id = ?", (int(active), watch_id)).rowcount > 0

    def list_watch(self, active_only: bool = False) -> list[WatchItem]:
        sql = "SELECT * FROM watchlist" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        with self.connect() as c:
            return [_row_to_watch(r) for r in c.execute(sql).fetchall()]

    def get_watch(self, watch_id: int) -> WatchItem | None:
        with self.connect() as c:
            r = c.execute("SELECT * FROM watchlist WHERE id = ?", (watch_id,)).fetchone()
            return _row_to_watch(r) if r else None

    def checked_deal_ids(self, watch_id: int) -> set[int]:
        with self.connect() as c:
            return {r[0] for r in c.execute("SELECT deal_id FROM watch_matches WHERE watch_id = ?", (watch_id,))}

    def save_watch_match(self, watch_id: int, deal_id: int, score: float, reason: str) -> None:
        with self.connect() as c:
            c.execute("INSERT OR REPLACE INTO watch_matches (watch_id, deal_id, score, reason, notified, created_at) "
                      "VALUES (?,?,?,?,0,?)", (watch_id, deal_id, round(score, 2), reason, _iso(utcnow())))

    def queue_watch_matches(self, threshold: float) -> int:
        """Quiet hours: hold pending watchlist hits for the next digest."""
        with self.connect() as c:
            return c.execute("UPDATE watch_matches SET queued = 1 WHERE notified = 0 AND queued = 0 AND score >= ?",
                             (threshold,)).rowcount

    def pending_watch_matches(self, threshold: float, limit: int,
                              queued: bool | None = None) -> list[tuple[WatchItem, Deal, float, str]]:
        now = utcnow()
        qfilter = "" if queued is None else f" AND m.queued = {1 if queued else 0}"
        with self.connect() as c:
            rows = c.execute(
                f"""SELECT m.watch_id, m.deal_id, m.score AS m_score, m.reason AS m_reason
                   FROM watch_matches m
                   JOIN watchlist w ON w.id = m.watch_id AND w.active = 1
                   JOIN deals d ON d.id = m.deal_id
                   WHERE m.notified = 0 AND m.score >= ? AND d.dismissed = 0{qfilter}
                     AND d.status NOT IN ('Expired', 'Out of stock')
                     AND (d.expires_at IS NULL OR d.expires_at > ?)
                   ORDER BY m.score DESC LIMIT ?""",
                (threshold, _iso(now), limit)).fetchall()
            out = []
            for r in rows:
                w = _row_to_watch(c.execute("SELECT * FROM watchlist WHERE id = ?", (r["watch_id"],)).fetchone())
                d = _row_to_deal(c.execute("SELECT * FROM deals WHERE id = ?", (r["deal_id"],)).fetchone())
                out.append((w, d, r["m_score"], r["m_reason"]))
            return out

    def mark_watch_notified(self, watch_id: int, deal_id: int) -> None:
        with self.connect() as c:
            c.execute("UPDATE watch_matches SET notified = 1 WHERE watch_id = ? AND deal_id = ?", (watch_id, deal_id))
