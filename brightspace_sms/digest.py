"""Build the SMS body from parsed events plus cached syllabus topics."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

from .feed import KIND_ASSIGNMENT, KIND_CLASS, Event
from .syllabus import load_schedules, topic_for, topics_on


@dataclass
class Digest:
    header: str
    body: str
    data: Dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.header}\n\n{self.body}" if self.body else self.header

    @property
    def fingerprint(self) -> str:
        """Hash of the body only — the header carries the date and changes daily."""
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()[:16]

    @property
    def is_empty(self) -> bool:
        return not self.body.strip()


def match_key(course: Optional[str], name: str) -> str:
    """Join key for pairing a calendar event with an API assignment record.

    Both sides render the same underlying item, but with incidental differences
    in spacing and punctuation, so normalize both hard.
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    return f"{(course or '').lower().strip()}|{normalized}"


def _clock(moment: datetime) -> str:
    stamp = moment.strftime("%I:%M%p").lstrip("0").lower()
    return stamp.replace(":00", "")


def _due_when(event: Event, today: date) -> str:
    day = event.start.date()
    delta = (day - today).days
    if delta == 0:
        prefix = "Today"
    elif delta == 1:
        prefix = "Tmrw"
    else:
        prefix = event.start.strftime("%a")
    if event.all_day:
        return prefix
    return f"{prefix} {_clock(event.start)}"


def _label(event: Event, fallback: str = "") -> str:
    course = (event.course or "").strip()
    summary = event.summary.strip()
    if course and course.lower() not in summary.lower():
        return f"{course}: {summary}"
    return summary or fallback


def build_digest(
    events: Sequence[Event],
    now: datetime,
    lookahead_days: int,
    errors: Optional[List[str]] = None,
    statuses: Optional[Dict[str, "object"]] = None,
    status_note: Optional[str] = None,
) -> Digest:
    today = now.date()
    horizon = today + timedelta(days=lookahead_days)
    schedules = load_schedules()
    statuses = statuses or {}

    classes_today = [
        e
        for e in events
        if e.kind == KIND_CLASS and e.start.date() == today and e.end >= now
    ]
    upcoming = [
        e
        for e in events
        if e.kind == KIND_ASSIGNMENT and today <= e.start.date() <= horizon and e.end >= now
    ]

    # Split what's left to do from what's already handed in. An event with no
    # matching API record stays in the to-do list — unknown is not the same as done.
    remaining: List[Event] = []
    done: List[tuple] = []
    for event in upcoming:
        status = statuses.get(match_key(event.course, event.summary))
        if status is not None and getattr(status, "submitted", False):
            done.append((event, status))
        else:
            remaining.append(event)

    lines: List[str] = []
    data: Dict[str, object] = {
        "date": today.isoformat(),
        "generated_at": now.isoformat(),
        "lookahead_days": lookahead_days,
        "in_class_today": [],
        "classes_today": [],
        "todo": [],
        "done": [],
        "notes": [],
        "errors": list(errors or []),
    }

    def row(event: Event, status=None) -> Dict[str, object]:
        return {
            "course": event.course,
            "title": event.summary,
            "due": event.start.isoformat(),
            "all_day": event.all_day,
            "kind": event.kind,
            "submitted": bool(getattr(status, "submitted", False)),
            "graded": bool(getattr(status, "graded", False)),
            "score": getattr(status, "score", None),
            "status_known": status is not None,
        }

    if classes_today:
        lines.append("CLASS TODAY")
        for event in classes_today:
            when = "all day" if event.all_day else _clock(event.start)
            line = f"{when} {_label(event, 'Class')}"
            if event.location:
                line += f" ({event.location})"
            topic = topic_for(schedules, event.course, today)
            lines.append(line)
            if topic:
                lines.append(f"   {topic}")
            data["classes_today"].append(
                {
                    "course": event.course,
                    "title": event.summary,
                    "start": event.start.isoformat(),
                    "all_day": event.all_day,
                    "location": event.location,
                    "topic": topic,
                }
            )
        lines.append("")

    # No class events in the feed? Fall back to the cached schedules directly.
    if not classes_today:
        planned = topics_on(schedules, today)
        if planned:
            lines.append("IN CLASS TODAY")
            for course, text in planned:
                lines.append(f"{course}: {text}")
                data["in_class_today"].append({"course": course, "topic": text})
            lines.append("")

    if remaining:
        lines.append(f"TO DO (next {lookahead_days}d)")
        for event in remaining:
            lines.append(f"{_due_when(event, today)} — {_label(event, 'Assignment')}")
            data["todo"].append(row(event, statuses.get(match_key(event.course, event.summary))))
    elif not classes_today:
        lines.append(f"Nothing left to do in the next {lookahead_days} days.")

    if done:
        lines.append("")
        lines.append(f"DONE ({len(done)})")
        for event, status in done:
            score = getattr(status, "score", None)
            suffix = f" — {score}" if score else ""
            lines.append(f"✓ {_label(event, 'Assignment')}{suffix}")
            data["done"].append(row(event, status))

    if status_note:
        lines.append("")
        lines.append(status_note)
        data["notes"].append(status_note)

    if errors:
        lines.append("")
        lines.append(f"[{len(errors)} feed error(s) — run `python app.py check`]")

    header = now.strftime("%a %b %-d")
    return Digest(header=header, body="\n".join(lines).strip(), data=data)
