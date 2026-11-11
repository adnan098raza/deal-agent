"""Student-discount matching and final-price estimates.

The catalog (data/student_programs.yaml) is curated and editable: program percentages are
typical values, so every estimate is shown as "≈" and should be verified at checkout.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from deal_agent.config import Preferences, load_yaml
from deal_agent.models import Deal
from deal_agent.parsing import normalize

DATA = Path(__file__).resolve().parent / "data" / "student_programs.yaml"


@lru_cache(maxsize=4)
def load_programs(path: str = str(DATA)) -> dict[str, dict]:
    data = load_yaml(Path(path))
    return {str(k): dict(v) for k, v in (data.get("programs") or {}).items()}


def _domain_match(domain: str | None, shops: list[str]) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return bool(d) and any(d == s or d.endswith("." + s) for s in shops)


def _applies(program: dict, deal: Deal) -> bool:
    brands = {normalize(b) for b in program.get("brands") or []}
    merchants = {normalize(m) for m in program.get("merchants") or []}
    shops = [str(s).lower() for s in program.get("shops") or []]
    return ((deal.brand and normalize(deal.brand) in brands)
            or _domain_match(deal.shop_domain, shops)
            or (deal.merchant and normalize(deal.merchant) in merchants))


def final_price(deal: Deal) -> float | None:
    p = deal.effective_price
    if p is None:
        return None
    return round(p + (deal.shipping or 0), 2)


def evaluate(deal: Deal, prefs: Preferences, programs: dict[str, dict] | None = None) -> dict | None:
    """Best applicable student program for this deal, with stacking verdict and estimate."""
    st = prefs.student
    if not st["is_student"]:
        return None
    programs = programs if programs is not None else load_programs()
    enabled = set(st["programs"]) or set(programs)
    price = deal.effective_price
    in_store = "lokal" in normalize(deal.title).split()        # mydealz "[Lokal ...]" = in-store only
    best = None
    for key, prog in programs.items():
        if key not in enabled or not _applies(prog, deal):
            continue
        pct = prog.get("pct")
        shops = [str(s).lower() for s in prog.get("shops") or []]
        at_store = deal.trust_label == "official_store" or _domain_match(deal.shop_domain, shops) or (
            prog.get("merchants") and deal.merchant and normalize(deal.merchant) in {normalize(m) for m in prog["merchants"]})
        stacks_cfg = prog.get("stacks_with_sale", "unknown")
        if in_store and pct:
            stacks, why = "no", "in-store deal; online student codes don't apply"
        elif prog.get("applies_to", "official_store") == "official_store" and not at_store:
            stacks, why = "no", f"only valid in the brand's own shop ({', '.join(shops) or 'official store'})"
        elif deal.coupon_code and pct:
            stacks, why = "unlikely", f"deal already uses code {deal.coupon_code} (usually one code per order)"
        elif stacks_cfg is True:
            stacks, why = "yes", "combines with the sale price"
        elif stacks_cfg is False:
            stacks, why = "no", "does not combine with sale prices"
        else:
            stacks, why = "maybe", "may combine with the sale price – check at checkout"
        est = None
        if price is not None and pct and stacks in ("yes", "maybe"):
            est = round(price * (1 - float(pct) / 100) + (deal.shipping or 0), 2)
        cand = {"program": prog.get("name", key), "key": key, "pct": pct, "stacks": stacks, "why": why,
                "final_est": est, "note": prog.get("note"), "url": prog.get("url")}
        rank = ({"yes": 3, "maybe": 2, "unlikely": 1, "no": 0}[stacks], float(pct or 0))
        if best is None or rank > best[0]:
            best = (rank, cand)
    return best[1] if best else None


def describe(info: dict | None) -> str:
    if not info:
        return ""
    pct = f" ≈{info['pct']:g}%" if info.get("pct") else ""
    s = f"{info['program']}{pct}: {info['stacks']} – {info['why']}"
    if info.get("final_est") is not None:
        s += f" → est. {info['final_est']:.2f} €"
    return s


def short(info: dict | None) -> str:
    if not info:
        return ""
    pct = f" ≈{info['pct']:g}%" if info.get("pct") else ""
    return f"{info['program']}{pct} ({info['stacks']})"
