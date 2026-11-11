"""watchlist.yaml: a plain-text mirror of the watchlist.

On the PC the database is the source of truth and this file is rewritten after every change,
so committing it carries the watchlist to the GitHub Actions runner, which syncs its own
database from the file.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from deal_agent.config import load_yaml, save_yaml
from deal_agent.db import Database
from deal_agent.models import WatchItem

log = logging.getLogger(__name__)
HEADER = ("Auto-generated from the agent's database whenever the watchlist changes.\n"
          "Commit it if you use the GitHub Actions fallback; edit via CLI or dashboard.")


def export(db: Database, path: Path) -> None:
    items = CommentedSeq()
    for w in db.list_watch():
        m = CommentedMap(query=w.query)
        for k in ("brand", "category", "max_price", "min_discount"):
            if getattr(w, k) not in (None, ""):
                m[k] = getattr(w, k)
        if not w.active:
            m["active"] = False
        items.append(m)
    doc = CommentedMap(watchlist=items)
    doc.yaml_set_start_comment(HEADER)
    try:
        save_yaml(path, doc)
    except OSError as exc:
        log.warning("Could not write %s: %s", path, exc)


def read(path: Path) -> list[WatchItem]:
    if not path.exists():
        return []
    data = load_yaml(path) or {}
    out = []
    for it in data.get("watchlist") or []:
        out.append(WatchItem(query=str(it.get("query") or ""), brand=it.get("brand"), category=it.get("category"),
                             max_price=float(it["max_price"]) if it.get("max_price") is not None else None,
                             min_discount=float(it["min_discount"]) if it.get("min_discount") is not None else None,
                             active=bool(it.get("active", True))))
    return out


def sync_into_db(db: Database, path: Path) -> int:
    """Make the database watchlist equal to the file (cloud runner). Returns #changes."""
    if not path.exists():
        return 0
    wanted = {w.key(): w for w in read(path)}
    have = {w.key(): w for w in db.list_watch()}
    changes = 0
    for key, w in have.items():
        if key not in wanted:
            db.remove_watch(w.id)
            changes += 1
        elif wanted[key].active != w.active:
            db.set_watch_active(w.id, wanted[key].active)
            changes += 1
    for key, w in wanted.items():
        if key not in have:
            db.add_watch(w)
            changes += 1
    if changes:
        log.info("Synced %d watchlist change(s) from %s", changes, path.name)
    return changes
