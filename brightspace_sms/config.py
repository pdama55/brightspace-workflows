"""Environment-backed configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / ".state.json"
FEED_CACHE_DIR = ROOT / ".feed_cache"
SCHEDULE_DIR = ROOT / "schedules"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _parse_send_at(raw: str) -> time:
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ConfigError(f"SEND_AT must look like HH:MM, got {raw!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise ConfigError(f"SEND_AT must look like HH:MM, got {raw!r}") from exc


def _parse_feeds(raw: str) -> List[Tuple[Optional[str], str]]:
    """Parse BRIGHTSPACE_ICAL_URLS.

    Accepts newline- or comma-separated entries, each either a bare URL or
    ``LABEL=URL``. The label names the course for events that don't identify
    their own — useful for a single-course feed, harmless on an all-courses one.
    """
    feeds: List[Tuple[Optional[str], str]] = []
    for chunk in raw.replace(",", "\n").splitlines():
        entry = chunk.strip()
        if not entry or entry.startswith("#"):
            continue
        label: Optional[str] = None
        # Split on the first '=' only if it comes before the scheme, so that
        # query strings full of '=' survive intact.
        if "=" in entry and "://" in entry and entry.index("=") < entry.index("://"):
            label, entry = entry.split("=", 1)
            label = label.strip() or None
            entry = entry.strip()
        feeds.append((label, entry))
    return feeds


@dataclass
class Config:
    feeds: List[Tuple[Optional[str], str]] = field(default_factory=list)
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    tz_name: str = "UTC"
    lookahead_days: int = 7
    send_at: time = field(default_factory=lambda: time(7, 30))
    poll_interval: int = 1800
    alert_on_change: bool = True
    send_when_empty: bool = True

    dry_run: bool = True
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_from: str = ""
    twilio_to: str = ""

    anthropic_model: str = "claude-opus-5"

    # Optional Valence REST credentials (only used by the `valence` command).
    bs_base_url: str = ""
    bs_client_id: str = ""
    bs_client_secret: str = ""
    bs_refresh_token: str = ""
    bs_access_token: str = ""

    # Session-cookie access (grades + submission status without an OAuth app).
    bs_cookie: str = ""
    bs_lp_version: str = ""
    bs_le_version: str = ""

    @property
    def has_cookie(self) -> bool:
        return bool(self.bs_base_url and self.bs_cookie)

    @property
    def has_valence(self) -> bool:
        return bool(self.bs_base_url and (self.bs_access_token or self.bs_refresh_token))

    def require_twilio(self) -> None:
        missing = [
            name
            for name, value in (
                ("TWILIO_ACCOUNT_SID", self.twilio_sid),
                ("TWILIO_AUTH_TOKEN", self.twilio_token),
                ("TWILIO_FROM_NUMBER", self.twilio_from),
                ("TWILIO_TO_NUMBER", self.twilio_to),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing Twilio config: " + ", ".join(missing) + ". Set DRY_RUN=true to skip sending."
            )


def _cookie() -> str:
    """Normalize whatever shape the cookie was pasted in."""
    raw = os.getenv("BRIGHTSPACE_COOKIE", "").strip()
    if not raw:
        return ""
    from .session import extract_cookie_pairs

    return extract_cookie_pairs(raw)


def load_config() -> Config:
    load_dotenv(ROOT / ".env")

    tz_name = (os.getenv("TIMEZONE") or "").strip()
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001 - surfaced as a config error
            raise ConfigError(f"Unknown TIMEZONE {tz_name!r}") from exc
    else:
        from datetime import datetime

        local = datetime.now().astimezone().tzinfo
        tz_name = str(local)
        tz = local  # type: ignore[assignment]

    return Config(
        feeds=_parse_feeds(os.getenv("BRIGHTSPACE_ICAL_URLS", "")),
        tz=tz,
        tz_name=tz_name,
        lookahead_days=_int("LOOKAHEAD_DAYS", 7),
        send_at=_parse_send_at(os.getenv("SEND_AT") or "07:30"),
        poll_interval=_int("POLL_INTERVAL_SECONDS", 1800),
        alert_on_change=_bool("ALERT_ON_CHANGE", True),
        send_when_empty=_bool("SEND_WHEN_EMPTY", True),
        dry_run=_bool("DRY_RUN", True),
        twilio_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
        twilio_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
        twilio_from=os.getenv("TWILIO_FROM_NUMBER", "").strip(),
        twilio_to=os.getenv("TWILIO_TO_NUMBER", "").strip(),
        anthropic_model=(os.getenv("ANTHROPIC_MODEL") or "claude-opus-5").strip(),
        bs_base_url=os.getenv("BRIGHTSPACE_BASE_URL", "").strip().rstrip("/"),
        bs_client_id=os.getenv("BRIGHTSPACE_CLIENT_ID", "").strip(),
        bs_client_secret=os.getenv("BRIGHTSPACE_CLIENT_SECRET", "").strip(),
        bs_refresh_token=os.getenv("BRIGHTSPACE_REFRESH_TOKEN", "").strip(),
        bs_access_token=os.getenv("BRIGHTSPACE_ACCESS_TOKEN", "").strip(),
        bs_cookie=_cookie(),
        bs_lp_version=os.getenv("BRIGHTSPACE_LP_VERSION", "").strip(),
        bs_le_version=os.getenv("BRIGHTSPACE_LE_VERSION", "").strip(),
    )
