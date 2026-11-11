"""Excel log (data/deals.xlsx in the OneDrive project folder, so it syncs to your phone).

Sheets: Active, Expired, History - each with filters and a frozen header row. The workbook
is always built in a local temp file first. If deals.xlsx is open in Excel (Windows locks it),
the new version stays pending and is copied over on a later run instead of crashing.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from deal_agent.config import Preferences, app_home
from deal_agent.db import Database
from deal_agent.models import Deal
from deal_agent.students import short as student_short

log = logging.getLogger(__name__)

EUR = '#,##0.00 "€"'
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="0563C1", underline="single")
STATUS_FILL = {"Live": "E2F0D9", "Price changed": "FFF2CC", "Expired": "EDEDED", "Out of stock": "F8CBAD"}

ACTIVE_COLS = [
    ("Title", 55), ("Brand", 14), ("Category", 16), ("Price", 11), ("Original price", 13), ("Discount %", 11),
    ("Shop", 26), ("Trust", 20), ("Student discount", 30), ("Final est.", 11), ("Score", 7), ("Expiry", 17),
    ("Status", 13), ("Last checked", 17), ("Current price", 13), ("Lowest seen", 12), ("Warnings", 40), ("Link", 10),
]
HISTORY_COLS = [
    ("Title", 55), ("Brand", 14), ("Category", 16), ("Shop", 26), ("Trust", 20), ("First seen", 17),
    ("Last seen", 17), ("First price", 11), ("Lowest price", 12), ("Current price", 13), ("Status", 13),
    ("Score", 7), ("Source", 18), ("Link", 10),
]


def pending_path() -> Path:
    return app_home() / "deals.pending.xlsx"


class Exporter:
    def __init__(self, db: Database, prefs: Preferences, path: Path, tz: str = "Europe/Berlin",
                 pending: Path | None = None):
        self.db, self.prefs, self.path, self.zone = db, prefs, path, ZoneInfo(tz)
        self.pending = pending or pending_path()

    def _local(self, dt: datetime | None):
        return dt.astimezone(self.zone).replace(tzinfo=None) if dt else None

    def _header(self, ws, cols):
        ws.append([c for c, _ in cols])
        for i, (_, width) in enumerate(cols, start=1):
            cell = ws.cell(row=1, column=i)
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
            cell.alignment = Alignment(vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A2"

    def _finish(self, ws, cols):
        ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(ws.max_row, 1)}"

    @staticmethod
    def _link(cell, url: str, text: str | None = None):
        cell.value = text if text is not None else cell.value
        cell.hyperlink = url
        cell.font = LINK_FONT

    def _deal_row(self, ws, d: Deal, lowest: float | None):
        ws.append([
            d.title, d.brand, d.category, d.price, d.original_price,
            (d.discount_pct / 100) if d.discount_pct else None, d.shop, d.trust_text,
            student_short(d.student) if d.student else None,
            (d.student or {}).get("final_est") or d.final_price_est, d.score, self._local(d.expires_at),
            d.status, self._local(d.last_checked), d.current_price if d.current_price is not None else d.price,
            lowest, "; ".join(d.warnings) or None, "Open",
        ])
        r = ws.max_row
        self._link(ws.cell(r, 1), d.link)
        self._link(ws.cell(r, len(ACTIVE_COLS)), d.link, "Open")
        for c in (4, 5, 10, 15, 16):
            ws.cell(r, c).number_format = EUR
        ws.cell(r, 6).number_format = "0%"
        ws.cell(r, 12).number_format = ws.cell(r, 14).number_format = "dd.mm.yyyy hh:mm"
        if d.status in STATUS_FILL:
            ws.cell(r, 13).fill = PatternFill("solid", fgColor=STATUS_FILL[d.status])

    def build(self) -> Workbook:
        min_score = float(self.prefs.get("excel", "min_score", 5.0))
        days = int(self.prefs.get("excel", "active_days", 30))
        active, expired = self.db.excel_sets(min_score, days)
        wb = Workbook()
        for name, deals in (("Active", active), ("Expired", expired)):
            ws = wb.active if name == "Active" else wb.create_sheet(name)
            ws.title = name
            self._header(ws, ACTIVE_COLS)
            for d in deals:
                self._deal_row(ws, d, self.db.lowest_price(d))
            self._finish(ws, ACTIVE_COLS)
        ws = wb.create_sheet("History")
        self._header(ws, HISTORY_COLS)
        for d, lowest in self.db.history_rows():
            ws.append([d.title, d.brand, d.category, d.shop, d.trust_text, self._local(d.first_seen),
                       self._local(d.last_seen), d.price, lowest,
                       d.current_price if d.current_price is not None else d.price, d.status, d.score, d.source,
                       "Open"])
            r = ws.max_row
            self._link(ws.cell(r, 1), d.link)
            self._link(ws.cell(r, len(HISTORY_COLS)), d.link, "Open")
            for c in (8, 9, 10):
                ws.cell(r, c).number_format = EUR
            ws.cell(r, 6).number_format = ws.cell(r, 7).number_format = "dd.mm.yyyy hh:mm"
        self._finish(ws, HISTORY_COLS)
        return wb

    def export(self) -> str:
        """'ok' when deals.xlsx was updated, 'pending' when it is locked (retried later)."""
        tmp = self.pending
        tmp.parent.mkdir(parents=True, exist_ok=True)
        self.build().save(tmp)
        return self.flush_pending()

    def flush_pending(self) -> str:
        tmp = self.pending
        if not tmp.exists():
            return "ok"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                os.replace(tmp, self.path)
            except OSError as exc:
                if isinstance(exc, PermissionError):
                    raise
                shutil.copyfile(tmp, self.path)       # different volume
                tmp.unlink(missing_ok=True)
            log.info("Excel log updated: %s", self.path)
            return "ok"
        except PermissionError:
            log.warning("%s is locked (open in Excel?); keeping the update pending and retrying later", self.path.name)
            return "pending"
