"""Extract a class schedule from a web page an instructor links from Content.

Some instructors skip the syllabus document and link out to a hand-maintained
schedule page (STAT 350 does this). Those pages are almost always a week-grid
table: one row per week, one column per weekday. That structure is regular
enough to parse directly, and it needs no API key — unlike the syllabus path.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

import requests

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SCHEDULE_HINT = re.compile(r"schedule|calendar|syllab|outline|course ?plan", re.I)

# Link text on these pages repeats itself for screen readers; strip the noise.
_NOISE = re.compile(
    r"\(opens? in a new tab\)|\(new window\)|click to open[^.]*|Skip to [a-z ]+", re.I
)
_MONTH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})\b", re.I
)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}


class _TableParser(HTMLParser):
    """Collect every table on the page as a list of rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._table: Optional[List[List[str]]] = None
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" · ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(clean_cell(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if not self._skip and self._cell is not None:
            self._cell.append(data)


def clean_cell(text: str) -> str:
    text = _NOISE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" ·|-—–")
    # "Ch 3 — Chapter 3 course notes (Numerical Summaries)" -> "Ch 3 — Numerical Summaries"
    text = re.sub(r"—\s*Chapter\s*\d+\s*course notes\s*", "— ", text, flags=re.I)
    text = re.sub(r"\s*·\s*", " · ", text)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s+", " ", text).strip(" ·")


def _resolve(month_day: str, term_start: date) -> Optional[date]:
    """Turn 'Aug 31' into a real date, rolling the year over in the spring."""
    match = _MONTH.search(month_day)
    if not match:
        return None
    month = _MONTHS[match.group(1)[:3].lower()]
    day = int(match.group(2))
    year = term_start.year + (1 if month < term_start.month else 0)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _weekday_columns(header: List[str]) -> Dict[int, int]:
    """Map column index -> weekday offset (0=Monday), for columns that name a day."""
    columns: Dict[int, int] = {}
    for index, cell in enumerate(header):
        name = cell.strip().lower()
        for offset, day in enumerate(WEEKDAYS):
            if name.startswith(day[:3]) and len(name) <= len(day) + 2:
                columns[index] = offset
                break
    return columns


def parse_week_grid(html: str, term_start: date) -> List[dict]:
    """Parse a week-per-row / weekday-per-column schedule table."""
    parser = _TableParser()
    parser.feed(html)

    for table in parser.tables:
        for header_index, header in enumerate(table[:3]):
            columns = _weekday_columns(header)
            if len(columns) < 3:
                continue

            # The date column is whichever earlier column holds "Week of"-style dates.
            meetings: List[dict] = []
            for row in table[header_index + 1:]:
                week_start = None
                for cell in row[: min(columns) if columns else 2]:
                    week_start = _resolve(cell, term_start)
                    if week_start:
                        break
                if not week_start:
                    continue
                # Normalize to that week's Monday so weekday offsets line up.
                week_start -= timedelta(days=week_start.weekday())

                # "Week 2 This week" — strip the page's own here-you-are badge.
                week_label = re.sub(
                    r"\s*(this|next|last)\s+week\s*$", "", row[0].strip(), flags=re.I
                ) if row else ""
                for column, offset in sorted(columns.items(), key=lambda kv: kv[1]):
                    if column >= len(row):
                        continue
                    topic = row[column].strip()
                    if not topic:
                        continue
                    day = week_start + timedelta(days=offset)
                    meetings.append(
                        {
                            "date": day.isoformat(),
                            "label": f"{week_label}, {WEEKDAYS[offset].title()}".strip(", "),
                            "topic": topic,
                            "readings": None,
                            "notes": None,
                        }
                    )
            if meetings:
                return meetings
    return []


def fetch(url: str, timeout: int = 30) -> str:
    response = requests.get(
        url, headers={"User-Agent": "brightspace-sms/1.0"}, timeout=timeout
    )
    response.raise_for_status()
    return response.text


def schedule_from_url(url: str, course: str, term_start: date) -> Optional[dict]:
    """Fetch a linked schedule page and shape it like a parsed syllabus."""
    meetings = parse_week_grid(fetch(url), term_start)
    if not meetings:
        return None
    return {"course": course, "source": url, "meetings": meetings}
