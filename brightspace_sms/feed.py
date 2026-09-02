"""Fetch and normalize Brightspace iCal calendar feeds.

Brightspace exposes a per-user calendar subscription (Calendar -> Settings ->
Enable Calendar Feeds -> Subscribe) that needs no admin involvement and no
OAuth app registration. That feed is the primary data source here.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

from .config import FEED_CACHE_DIR

USER_AGENT = "brightspace-sms/1.0 (+personal calendar digest)"

# Brightspace due-date events are usually zero-length or all-day, while class
# meetings are timed blocks. Keywords break the ties. Tune these against the
# output of `python app.py dump-feed` for your own school's feed.
ASSIGNMENT_WORDS = (
    "due", "assignment", "quiz", "exam", "midterm", "final", "submit",
    "submission", "dropbox", "homework", "hw", "problem set", "pset", "lab report",
    "essay", "paper", "project", "discussion", "checkpoint", "milestone",
    "reading response", "reflection", "worksheet", "test", "presentation",
)
CLASS_WORDS = (
    "lecture", "lab", "seminar", "tutorial", "section", "recitation",
    "class", "studio", "workshop", "office hours", "meeting", "practicum",
)

KIND_ASSIGNMENT = "assignment"
KIND_CLASS = "class"
KIND_AVAILABLE = "available"   # content unlocked, not a thing you have to do
KIND_OTHER = "other"

# D2L appends the event type to the title: "Homework #1 - Due", "Week 2 - Available".
TYPE_SUFFIXES = {
    "due": KIND_ASSIGNMENT,
    "available": KIND_AVAILABLE,
    "starts": KIND_AVAILABLE,
    "start": KIND_AVAILABLE,
    "ends": KIND_ASSIGNMENT,
    "end": KIND_ASSIGNMENT,
}

# "Fall 2026 STAT 35000-123 LEC" -> ("STAT", "35000")
_COURSE_CODE = re.compile(r"\b([A-Z]{2,5})\s*[- ]\s*(\d{3,5})\b")
_TERM_PREFIX = re.compile(
    r"^\s*(spring|summer|fall|autumn|winter)\s+\d{4}\s+", re.IGNORECASE
)


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    kind: str
    course: Optional[str]
    location: str
    description: str
    feed_label: Optional[str]

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def dedupe_key(self) -> str:
        return f"{self.summary.strip().lower()}|{self.start.isoformat()}"


def normalize_feed_url(url: str) -> str:
    """Brightspace hands out `webcal://` links; requests only speaks http(s)."""
    url = url.strip()
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://") :]
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return FEED_CACHE_DIR / f"{digest}.ics"


def fetch_feed(url: str, timeout: int = 30, use_cache_on_error: bool = True) -> bytes:
    """Download one iCal feed, falling back to the last good copy on failure."""
    normalized = normalize_feed_url(url)
    cache = _cache_path(normalized)
    try:
        response = requests.get(
            normalized, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        response.raise_for_status()
        body = response.content
        if b"BEGIN:VCALENDAR" not in body:
            raise RuntimeError(
                "Feed did not return iCal data. Check that the subscription URL is "
                "still valid (Brightspace invalidates it if you regenerate the feed)."
            )
        FEED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
        return body
    except Exception:
        if use_cache_on_error and cache.exists():
            return cache.read_bytes()
        raise


def _as_aware(value, tz: ZoneInfo) -> Tuple[datetime, bool]:
    """Return (aware datetime in `tz`, is_all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz), False
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=tz), True
    raise TypeError(f"Unsupported date value: {value!r}")


def _text(component, key: str) -> str:
    raw = component.get(key)
    if raw is None:
        return ""
    return str(raw).strip()


def _categories(component) -> List[str]:
    raw = component.get("CATEGORIES")
    if raw is None:
        return []
    values = getattr(raw, "cats", None)
    if values:
        return [str(v).strip() for v in values if str(v).strip()]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _split_course_from_summary(summary: str) -> Tuple[str, Optional[str]]:
    """Brightspace often renders titles as ``Thing - Course Name``.

    Returns (title, course) where course is None when the split looks wrong.
    """
    for sep in (" - ", " – ", " — "):
        if sep in summary:
            title, _, tail = summary.rpartition(sep)
            title, tail = title.strip(), tail.strip()
            # A course name has some substance; a trailing "Part 2" does not.
            if title and len(tail) >= 3 and not tail.isdigit():
                return title, tail
    return summary.strip(), None


def _classify(
    summary: str, all_day: bool, duration: timedelta, type_word: Optional[str] = None
) -> str:
    # An explicit "- Due"/"- Available" suffix is authoritative; no need to guess.
    if type_word:
        return TYPE_SUFFIXES[type_word]

    lowered = summary.lower()
    has_assignment_word = any(word in lowered for word in ASSIGNMENT_WORDS)
    has_class_word = any(word in lowered for word in CLASS_WORDS)

    # A timed block that names itself a lecture/lab is a class, whatever else
    # the title says ("Lab 4" is a class; "Lab 4 Report due" is not).
    if not all_day and duration >= timedelta(minutes=20):
        if has_class_word and not has_assignment_word:
            return KIND_CLASS
        if has_assignment_word:
            return KIND_ASSIGNMENT
        return KIND_CLASS

    # Zero-length or all-day: Brightspace's shape for a due date.
    if has_class_word and not has_assignment_word:
        return KIND_CLASS
    if has_assignment_word or all_day or duration <= timedelta(minutes=1):
        return KIND_ASSIGNMENT
    return KIND_OTHER


def parse_feed(
    raw: bytes,
    tz: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
    feed_label: Optional[str] = None,
) -> List[Event]:
    """Parse one feed and expand recurring events across the window."""
    calendar = Calendar.from_ical(raw)
    calendar_name = _text(calendar, "X-WR-CALNAME") or None

    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)

    events: List[Event] = []
    for component in occurrences:
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        start, all_day = _as_aware(dtstart.dt, tz)

        dtend = component.get("DTEND")
        if dtend is not None:
            end, _ = _as_aware(dtend.dt, tz)
        else:
            duration_prop = component.get("DURATION")
            if duration_prop is not None:
                end = start + duration_prop.dt
            else:
                end = start + (timedelta(days=1) if all_day else timedelta(0))

        raw_summary = _text(component, "SUMMARY") or "(untitled)"
        summary, type_word = _strip_type_suffix(raw_summary)

        # D2L schools disagree about where the course name lives. Purdue puts the
        # org-unit name in LOCATION and never sets CATEGORIES; others do the
        # reverse, or bury it in the title. Try each in turn.
        raw_location = _text(component, "LOCATION")
        categories = _categories(component)
        course_from_title = None
        if not raw_location and not categories:
            summary, course_from_title = _split_course_from_summary(summary)

        course = None
        for candidate in (
            raw_location if _looks_like_course(raw_location) else None,
            categories[0] if categories else None,
            course_from_title,
            feed_label,
            calendar_name if calendar_name and not _looks_like_aggregate(calendar_name) else None,
        ):
            if candidate:
                course = clean_course(candidate)
                break

        # Drop a trailing course name that duplicates the course we resolved.
        if course_from_title and course and _same_course(course_from_title, course):
            summary = summary.strip()

        # LOCATION doubles as the course name here, so it is not also a room.
        room = "" if course and _looks_like_course(raw_location) else raw_location

        events.append(
            Event(
                uid=_text(component, "UID"),
                summary=summary or raw_summary,
                start=start,
                end=end,
                all_day=all_day,
                kind=_classify(raw_summary, all_day, end - start, type_word),
                course=course,
                location=room,
                description=_text(component, "DESCRIPTION"),
                feed_label=feed_label,
            )
        )
    return events


def clean_course(raw: str) -> str:
    """Turn a D2L org-unit name into something that fits in a text message.

    "Fall 2026 STAT 35000-123 LEC" -> "STAT 350"
    Purdue writes 5-digit course numbers where the last two digits are the
    variant, so only the first three are meaningful.
    """
    name = raw.strip()
    if not name:
        return name
    match = _COURSE_CODE.search(name)
    if match:
        subject, number = match.group(1), match.group(2)
        if len(number) == 5:
            number = number[:3]
        return f"{subject} {number}"
    # No recognizable code — just drop the term prefix and any trailing noise.
    return _TERM_PREFIX.sub("", name).strip() or name


def _strip_type_suffix(summary: str) -> Tuple[str, Optional[str]]:
    """Split "Homework #1 - Due" into ("Homework #1", "due")."""
    for sep in (" - ", " – ", " — "):
        if sep in summary:
            head, _, tail = summary.rpartition(sep)
            key = tail.strip().lower()
            if head.strip() and key in TYPE_SUFFIXES:
                return head.strip(), key
    return summary.strip(), None


def _same_course(a: str, b: str) -> bool:
    """Loose match: 'BIOL 110' and 'BIOL 110 Cell Biology' are the same course."""
    x, y = a.strip().lower(), b.strip().lower()
    return x == y or x in y or y in x


def _looks_like_course(name: str) -> bool:
    """Distinguish a course org-unit name in LOCATION from an actual room/URL."""
    if not name or name.startswith("http"):
        return False
    return bool(_COURSE_CODE.search(name) or _TERM_PREFIX.search(name))


def _looks_like_aggregate(name: str) -> bool:
    """`X-WR-CALNAME` on an all-courses feed is not a course name."""
    lowered = name.lower()
    return bool(re.search(r"\ball\b|calendar|brightspace|d2l|my courses", lowered))


def collect_events(
    feeds: Sequence[Tuple[Optional[str], str]],
    tz: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
) -> Tuple[List[Event], List[str]]:
    """Fetch every configured feed. Returns (events, per-feed error messages)."""
    events: List[Event] = []
    errors: List[str] = []
    for label, url in feeds:
        try:
            raw = fetch_feed(url)
            events.extend(parse_feed(raw, tz, window_start, window_end, feed_label=label))
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            errors.append(f"{label or url}: {exc}")
    return dedupe(events), errors


def dedupe(events: Iterable[Event]) -> List[Event]:
    """Subscribing to both the all-courses feed and a per-course feed duplicates."""
    seen = set()
    unique: List[Event] = []
    for event in sorted(events, key=lambda e: (e.start, e.summary)):
        if event.dedupe_key in seen:
            continue
        seen.add(event.dedupe_key)
        unique.append(event)
    return unique
