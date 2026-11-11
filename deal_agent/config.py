"""Settings from .env and user preferences from preferences.yaml."""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from ruamel.yaml import YAML

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEGACY_DB = PROJECT_ROOT / "data" / "deals.db"


def app_home() -> Path:
    """Machine-local state dir (NOT synced by OneDrive: SQLite and sync clients don't mix)."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "DealAgent"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(value: str) -> Path:
    p = Path(os.path.expandvars(value)).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class Settings:
    llm_provider: str = "none"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)

    ntfy_enabled: bool = False
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    ntfy_feedback_topic: str = ""       # receives Snooze / Not interested button presses
    heartbeat_enabled: bool = False     # PC tells the cloud fallback it is online

    db_path: Path = field(default_factory=lambda: app_home() / "deals.db")
    excel_path: Path = PROJECT_ROOT / "data" / "deals.xlsx"
    log_file: Path | None = None
    preferences_file: Path = PROJECT_ROOT / "preferences.yaml"
    sources_file: Path = PROJECT_ROOT / "sources.yaml"
    watchlist_file: Path = PROJECT_ROOT / "watchlist.yaml"
    user_agent: str = "DealAgent/1.0 (personal deal watcher)"
    log_level: str = "INFO"
    timezone: str = "Europe/Berlin"

    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_token: str = ""
    cloud: bool = False                 # running in GitHub Actions
    profile: str = ""                   # "" = default (.env); "it" = .env.it, own DB/lock/topic

    @property
    def heartbeat_topic(self) -> str:
        return f"{self.ntfy_topic}-hb" if self.ntfy_topic else ""

    @classmethod
    def from_env(cls, env_file: str | Path | None = None, profile: str = "") -> "Settings":
        profile = (profile or os.environ.get("DEALAGENT_PROFILE") or "").strip().lower()
        if env_file is None:
            env_file = PROJECT_ROOT / (f".env.{profile}" if profile else ".env")
            if profile and not Path(env_file).exists():
                raise FileNotFoundError(f"profile '{profile}' needs {env_file}")
        load_dotenv(env_file)
        if profile and env_file is not None:
            # values the profile doesn't set (API keys, ...) come from the main .env; load_dotenv
            # never overrides, so the profile file wins
            load_dotenv(PROJECT_ROOT / ".env")
        e = os.environ.get
        provider = (e("LLM_PROVIDER") or "").strip().lower()
        if not provider:  # infer from whichever key is present
            provider = "groq" if e("GROQ_API_KEY") else "gemini" if e("GEMINI_API_KEY") else "none"
        topic = e("NTFY_TOPIC", "")
        db_default = app_home() / "deals.db"
        return cls(
            llm_provider=provider,
            groq_api_key=e("GROQ_API_KEY", ""),
            groq_model=e("GROQ_MODEL") or cls.groq_model,
            gemini_api_key=e("GEMINI_API_KEY", ""),
            gemini_model=e("GEMINI_MODEL") or cls.gemini_model,
            email_enabled=_bool(e("EMAIL_ENABLED")),
            smtp_host=e("SMTP_HOST", ""),
            smtp_port=int(e("SMTP_PORT") or 587),
            smtp_user=e("SMTP_USER", ""),
            smtp_password=e("SMTP_PASSWORD", ""),
            email_from=e("EMAIL_FROM") or e("SMTP_USER", ""),
            email_to=[a.strip() for a in (e("EMAIL_TO") or "").split(",") if a.strip()],
            ntfy_enabled=_bool(e("NTFY_ENABLED"), default=bool(topic)),
            ntfy_server=(e("NTFY_SERVER") or "https://ntfy.sh").rstrip("/"),
            ntfy_topic=topic,
            ntfy_token=e("NTFY_TOKEN", ""),
            ntfy_feedback_topic=e("NTFY_FEEDBACK_TOPIC") or (f"{topic}-fb" if topic else ""),
            heartbeat_enabled=_bool(e("HEARTBEAT_ENABLED")),
            db_path=_path(e("DB_PATH")) if e("DB_PATH") else db_default,
            excel_path=_path(e("EXCEL_PATH") or "data/deals.xlsx"),
            log_file=_path(e("LOG_FILE")) if e("LOG_FILE") else app_home() / "agent.log",
            preferences_file=_path(e("PREFERENCES_FILE") or "preferences.yaml"),
            sources_file=_path(e("SOURCES_FILE") or "sources.yaml"),
            watchlist_file=_path(e("WATCHLIST_FILE") or "watchlist.yaml"),
            user_agent=e("USER_AGENT") or cls.user_agent,
            log_level=(e("LOG_LEVEL") or "INFO").upper(),
            timezone=e("TIMEZONE") or "Europe/Berlin",
            dashboard_enabled=_bool(e("DASHBOARD_ENABLED"), default=True),
            dashboard_host=e("DASHBOARD_HOST") or "127.0.0.1",
            dashboard_port=int(e("DASHBOARD_PORT") or 8765),
            dashboard_token=e("DASHBOARD_TOKEN", ""),
            cloud=_bool(e("GITHUB_ACTIONS")),
            profile=profile,
        )


def migrate_legacy_db(settings: Settings) -> None:
    """v1 kept the DB inside the OneDrive project folder; move it to the local app dir once."""
    if settings.db_path == LEGACY_DB or settings.db_path.exists() or not LEGACY_DB.exists():
        return
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(LEGACY_DB), settings.db_path)
    log.info("Moved database out of OneDrive: %s -> %s", LEGACY_DB, settings.db_path)


# --------------------------------------------------------------------------- YAML

def _yaml() -> YAML:
    y = YAML()  # round-trip mode keeps comments and ordering
    y.preserve_quotes = True
    y.width = 100
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return _yaml().load(fh) or {}


def loads_yaml(text: str) -> Any:
    return _yaml().load(text) or {}


def save_yaml(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        _yaml().dump(data, fh)
    os.replace(tmp, path)


def validate_preferences(data: Any) -> list[str]:
    """Return a list of problems (empty = OK). Used before saving edits from the dashboard."""
    errors = []
    if not isinstance(data, dict):
        return ["top level must be a mapping"]
    cats = data.get("categories")
    if not isinstance(cats, dict) or not cats:
        errors.append("categories: must be a non-empty mapping")
    else:
        for name, spec in cats.items():
            if not isinstance(spec, dict) or not isinstance(spec.get("keywords", []), list):
                errors.append(f"categories.{name}: needs a 'keywords' list")
    brands = data.get("brands", {})
    if not isinstance(brands, dict) or any(not isinstance(brands.get(t, []), list) for t in ("budget", "premium")):
        errors.append("brands: needs 'budget' and 'premium' lists")
    for sec in ("scoring", "notifications", "schedule", "student", "gender", "excel", "recheck", "trust"):
        if sec in data and not isinstance(data[sec], dict):
            errors.append(f"{sec}: must be a mapping")
    try:
        float((data.get("scoring") or {}).get("notify_threshold", 7))
    except (TypeError, ValueError):
        errors.append("scoring.notify_threshold must be a number")
    qh = (data.get("notifications") or {}).get("quiet_hours", [])
    if qh and (not isinstance(qh, list) or len(qh) != 2):
        errors.append("notifications.quiet_hours must be [start, end] or []")
    times = (data.get("notifications") or {}).get("digest_times", [])
    if not isinstance(times, list) or any(
            not __import__("re").fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", str(t).strip()) for t in times):
        errors.append('notifications.digest_times must be a list like ["11:00", "14:30"]')
    return errors


class Preferences:
    """Typed accessors over the round-trip YAML document (so edits keep comments)."""

    def __init__(self, data: Any, path: Path | None = None):
        self.data = data if data is not None else {}
        self.path = path

    @classmethod
    def load(cls, path: Path) -> "Preferences":
        if not path.exists():
            raise FileNotFoundError(f"Preferences file not found: {path}")
        return cls(load_yaml(path), path)

    def save(self) -> None:
        if self.path is None:
            raise ValueError("Preferences have no file path")
        save_yaml(self.path, self.data)

    def reload(self) -> None:
        if self.path and self.path.exists():
            try:
                self.data = load_yaml(self.path)
            except Exception as exc:  # noqa: BLE001 - keep last good prefs on a bad hand edit
                log.error("preferences.yaml is invalid, keeping previous settings: %s", exc)

    # -- helpers
    def _section(self, name: str) -> dict:
        sec = self.data.get(name)
        return sec if isinstance(sec, dict) else {}

    def get(self, section: str, key: str, default: Any) -> Any:
        v = self._section(section).get(key, default)
        return default if v is None else v

    # -- categories / brands
    @property
    def categories(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for name, spec in self._section("categories").items():
            kws = spec.get("keywords", []) if isinstance(spec, dict) else (spec or [])
            out[str(name)] = [str(k).lower() for k in kws]
        return out

    @property
    def brands(self) -> dict[str, list[str]]:
        sec = self._section("brands")
        return {tier: [str(b) for b in (sec.get(tier) or [])] for tier in ("budget", "premium")}

    @property
    def all_brands(self) -> list[str]:
        b = self.brands
        return b["budget"] + b["premium"]

    def brand_tier(self, brand: str | None) -> str | None:
        if not brand:
            return None
        low = brand.casefold()
        for tier, names in self.brands.items():
            if any(n.casefold() == low for n in names):
                return tier
        return None

    @property
    def exclude_keywords(self) -> list[str]:
        return [str(k).lower() for k in (self.data.get("exclude_keywords") or [])]

    @property
    def men_only_categories(self) -> list[str]:
        return [str(c) for c in self.get("gender", "men_only_categories",
                                         ["clothing", "shoes", "jewelry_accessories"])]

    @property
    def shopper(self) -> str:
        """Who the LLM scores for, e.g. 'a male shopper in Germany' (profile.shopper)."""
        return str(self.get("profile", "shopper", "a male shopper in Germany"))

    # -- sections
    def scoring(self, key: str, default: Any) -> Any:
        return self.get("scoring", key, default)

    def notifications(self, key: str, default: Any) -> Any:
        return self.get("notifications", key, default)

    def schedule(self, key: str, default: Any) -> Any:
        return self.get("schedule", key, default)

    @property
    def notify_threshold(self) -> float:
        return float(self.scoring("notify_threshold", 7.0))

    @property
    def watchlist_threshold(self) -> float:
        return float(self.scoring("watchlist_threshold", 5.0))

    @property
    def quiet_hours(self) -> tuple[int, int] | None:
        qh = self.notifications("quiet_hours", None)
        if qh and len(qh) == 2:
            return int(qh[0]), int(qh[1])
        return None

    @property
    def student(self) -> dict:
        s = self._section("student")
        return {
            "is_student": bool(s.get("is_student", False)),
            "programs": [str(p) for p in (s.get("programs") or [])],
        }

    def summary_for_llm(self) -> dict:
        return {
            "categories": list(self.categories),
            "men_only_categories": self.men_only_categories,
            "budget_brands": self.brands["budget"],
            "premium_brands": self.brands["premium"],
            "min_discount_pct": self.scoring("min_discount_pct", 15),
            "exclude_keywords": self.exclude_keywords,
        }


def load_sources_config(path: Path) -> list[dict]:
    if not path.exists():
        log.warning("Sources file %s not found; no sources configured", path)
        return []
    data = load_yaml(path)
    return [dict(s) for s in (data.get("sources") or [])]


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stderr is not None:              # pythonw.exe has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass
    for noisy in ("urllib3", "apscheduler.executors.default", "httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
