"""Cookie-authenticated Brightspace client.

Brightspace's own web UI calls the documented Valence REST endpoints using your
session cookies. Reusing those cookies gets the same API surface — grades,
submission status — without an OAuth app your school won't register for you.
You are calling as yourself, with your own credentials, for your own data.

The tradeoff: Purdue fronts Brightspace with BoilerKey/Duo SSO, so the session
cannot be renewed unattended. When it expires you re-paste the cookie.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from .config import Config, ConfigError

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


class AccessDenied(RuntimeError):
    """Authenticated fine, but not allowed to read this particular resource.

    Normal for finished or archived courses — not a reason to re-auth.
    """


class SessionExpired(RuntimeError):
    """The pasted cookie is no longer valid."""

    def __init__(self, detail: str = ""):
        super().__init__(
            "Brightspace session cookie has expired or was rejected"
            + (f" ({detail})" if detail else "")
            + ".\nLog into Brightspace in your browser and re-copy the cookie into "
            "BRIGHTSPACE_COOKIE in .env — see README 'Refreshing the cookie'."
        )


_FULL_CODE = re.compile(r"\b([A-Z]{2,5})\s*[-. ]\s*(\d{5})\b")


@dataclass
class Course:
    org_unit_id: int
    name: str
    code: str
    label_override: Optional[str] = None

    @property
    def label(self) -> str:
        from .feed import clean_course

        return self.label_override or clean_course(self.name or self.code)


@dataclass
class AssignmentStatus:
    course: str
    name: str
    due: Optional[datetime]
    submitted: bool
    submitted_at: Optional[datetime]
    graded: bool
    score: Optional[str]


@dataclass
class Topic:
    """One item in a course's content tree (a file, a link, an embedded page)."""

    id: int
    title: str
    url: str
    topic_type: Optional[int]
    path: str          # module breadcrumb, e.g. "Weekly Class Content / Week 3"

    TYPE_NAMES = {1: "file", 2: "html", 3: "link"}

    @property
    def kind(self) -> str:
        return self.TYPE_NAMES.get(self.topic_type or 0, str(self.topic_type))

    @property
    def extension(self) -> str:
        tail = self.url.rsplit("/", 1)[-1]
        return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""


@dataclass
class GradeValue:
    course: str
    name: str
    displayed: Optional[str]
    points: Optional[float]
    weight: Optional[float]


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class SessionClient:
    """Talks to Valence endpoints using browser session cookies."""

    def __init__(self, config: Config):
        if not config.bs_base_url:
            raise ConfigError(
                "BRIGHTSPACE_BASE_URL is required (e.g. https://your-school.brightspace.com)."
            )
        if not config.bs_cookie:
            raise ConfigError(
                "BRIGHTSPACE_COOKIE is not set. See README 'Grades and submission status'."
            )
        self.base_url = config.bs_base_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Cookie": config.bs_cookie,
                "User-Agent": BROWSER_UA,
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/d2l/home",
            }
        )
        self._lp = config.bs_lp_version or None
        self._le = config.bs_le_version or None
        self._user_id: Optional[int] = None

    # -- plumbing ---------------------------------------------------------

    def raw_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self.session.get(f"{self.base_url}{path}", params=params, timeout=30, allow_redirects=False)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET a JSON endpoint. Raises SessionExpired on an auth bounce."""
        response = self.raw_get(path, params)

        # An expired session redirects to the SSO login page instead of 401ing.
        if response.status_code in (301, 302, 303, 307, 308):
            raise SessionExpired(f"redirected to {response.headers.get('Location', '?')[:80]}")
        if response.status_code == 401:
            raise SessionExpired("HTTP 401")
        if response.status_code == 403:
            raise AccessDenied(f"HTTP 403 for {path} — no permission on this course")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} for {path}: {response.text[:300]}")

        body = response.text.strip()
        if not body:
            return None
        if body.lstrip().startswith("<"):
            raise SessionExpired("got an HTML login page instead of JSON")
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Non-JSON response from {path}: {body[:200]}") from exc

    # -- version discovery ------------------------------------------------

    def versions(self) -> Dict[str, str]:
        """Ask the server which API versions it supports, newest per product."""
        payload = self.get("/d2l/api/versions/")
        found: Dict[str, str] = {}
        if isinstance(payload, list):
            for product in payload:
                code = product.get("ProductCode")
                latest = product.get("LatestVersion")
                if code and latest:
                    found[code] = latest
        return found

    @property
    def lp(self) -> str:
        if not self._lp:
            self._lp = self.versions().get("lp", "1.43")
        return self._lp

    @property
    def le(self) -> str:
        if not self._le:
            self._le = self.versions().get("le", "1.67")
        return self._le

    # -- data -------------------------------------------------------------

    def whoami(self) -> Dict[str, Any]:
        payload = self.get(f"/d2l/api/lp/{self.lp}/users/whoami")
        if not payload:
            raise SessionExpired("whoami returned nothing")
        return payload

    def courses(self) -> List[Course]:
        out: List[Course] = []
        bookmark: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"orgUnitTypeId": 3}
            if bookmark:
                params["bookmark"] = bookmark
            payload = self.get(f"/d2l/api/lp/{self.lp}/enrollments/myenrollments/", params)
            if not isinstance(payload, dict):
                break
            for item in payload.get("Items", []):
                unit = item.get("OrgUnit") or {}
                if unit.get("Id"):
                    out.append(
                        Course(
                            org_unit_id=int(unit["Id"]),
                            name=str(unit.get("Name") or ""),
                            code=str(unit.get("Code") or ""),
                        )
                    )
            paging = payload.get("PagingInfo") or {}
            if not paging.get("HasMoreItems") or not paging.get("Bookmark"):
                break
            bookmark = paging["Bookmark"]
        return out

    def current_courses(self) -> List[Course]:
        """Just this term's courses.

        Purdue encodes the term in the course code (wl.202710.STAT.35000.123,
        where 202710 is Fall 2026), so the highest term code wins. Schools that
        don't do that fall back to the newest org unit IDs.
        """
        courses = self.courses()
        if not courses:
            return []
        terms = [
            int(m.group(1))
            for c in courses
            for m in [re.search(r"\b(20\d{4})\b", c.code)]
            if m
        ]
        if terms:
            newest = max(terms)
            current = [c for c in courses if re.search(rf"\b{newest}\b", c.code)]
            if current:
                disambiguate(current)
                return current
        recent = sorted(courses, key=lambda c: c.org_unit_id, reverse=True)[:8]
        disambiguate(recent)
        return recent

    @property
    def user_id(self) -> int:
        if self._user_id is None:
            self._user_id = int(self.whoami().get("Identifier"))
        return self._user_id

    def _my_submission(self, org_unit_id: int, folder_id: int) -> Optional[Dict[str, Any]]:
        """Your own entry in a dropbox folder, or None if you never submitted.

        The folder object itself carries no student-visible submission state
        (its counters come back as -1), but this endpoint returns the caller's
        own row — and returns nothing at all when there is no submission.
        """
        try:
            rows = self.get(
                f"/d2l/api/le/{self.le}/{org_unit_id}/dropbox/folders/{folder_id}/submissions/"
            )
        except AccessDenied:
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            entity = row.get("Entity") or {}
            if entity.get("EntityId") == self.user_id:
                return row
        return None

    def assignments(
        self,
        course: Course,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        with_status: bool = True,
    ) -> List[AssignmentStatus]:
        """Assignments for a course, optionally narrowed to a due-date window.

        Submission state costs one extra request per folder, so the window
        matters: without it this walks every folder the course has ever had.
        """
        payload = self.get(f"/d2l/api/le/{self.le}/{course.org_unit_id}/dropbox/folders/")
        if not isinstance(payload, list):
            return []

        out: List[AssignmentStatus] = []
        for folder in payload:
            due = _parse_dt(folder.get("DueDate"))
            if since and (due is None or due < since):
                continue
            if until and (due is None or due > until):
                continue

            submitted_at = None
            submitted = False
            graded = False
            score = None

            if with_status:
                row = self._my_submission(course.org_unit_id, folder["Id"])
                if row is not None:
                    submitted = True
                    for item in row.get("Submissions") or []:
                        stamp = _parse_dt(item.get("SubmissionDate"))
                        if stamp and (submitted_at is None or stamp > submitted_at):
                            submitted_at = stamp
                    feedback = row.get("Feedback") or {}
                    raw_score = feedback.get("Score")
                    graded = bool(feedback.get("IsGraded")) or raw_score is not None
                    if raw_score is not None:
                        denominator = (folder.get("Assessment") or {}).get("ScoreDenominator")
                        score = (
                            f"{raw_score:g}/{denominator:g}"
                            if isinstance(denominator, (int, float))
                            else f"{raw_score:g}"
                        )

            out.append(
                AssignmentStatus(
                    course=course.label,
                    name=str(folder.get("Name") or "Assignment"),
                    due=due,
                    submitted=submitted,
                    submitted_at=submitted_at,
                    graded=graded,
                    score=score,
                )
            )
        return out

    def content_tree(self, org_unit_id: int, max_modules: int = 120) -> List[Topic]:
        """Every topic in a course, recursing through submodules.

        content/root/ returns modules with only a partial Structure, so any
        module whose children are missing gets an explicit structure call. The
        module budget stops a pathological course from issuing hundreds.
        """
        try:
            root = self.get(f"/d2l/api/le/{self.le}/{org_unit_id}/content/root/")
        except AccessDenied:
            return []
        if not isinstance(root, list):
            return []

        topics: List[Topic] = []
        budget = [max_modules]

        def children(node: Dict[str, Any]) -> List[Dict[str, Any]]:
            structure = node.get("Structure")
            if structure:
                return structure
            if budget[0] <= 0:
                return []
            budget[0] -= 1
            try:
                fetched = self.get(
                    f"/d2l/api/le/{self.le}/{org_unit_id}/content/modules/{node['Id']}/structure/"
                )
            except AccessDenied:
                return []
            return fetched if isinstance(fetched, list) else []

        def walk(node: Dict[str, Any], trail: List[str]) -> None:
            for child in children(node):
                title = str(child.get("Title") or "")
                if child.get("Type") == 1:
                    topics.append(
                        Topic(
                            id=int(child.get("Id") or 0),
                            title=title,
                            url=str(child.get("Url") or ""),
                            topic_type=child.get("TopicType"),
                            path=" / ".join(trail),
                        )
                    )
                elif child.get("Type") == 0:
                    walk(child, trail + [title])

        for module in root:
            walk(module, [str(module.get("Title") or "")])
        return topics

    def overview_syllabus(self, course: Course, dest_dir) -> Optional["pathlib.Path"]:
        """Download the Content Overview attachment, which is where most
        instructors put the syllabus. It sits outside the module tree, so the
        content endpoints never surface it.
        """
        import pathlib
        import re as _re
        from urllib.parse import unquote

        try:
            overview = self.get(f"/d2l/api/le/{self.le}/{course.org_unit_id}/overview")
        except AccessDenied:
            return None
        if not isinstance(overview, dict) or not overview.get("HasAttachment"):
            return None

        response = self.raw_get(f"/d2l/api/le/{self.le}/{course.org_unit_id}/overview/attachment")
        if response.status_code != 200 or not response.content:
            return None

        disposition = response.headers.get("Content-Disposition", "")
        match = _re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disposition)
        filename = unquote(match.group(1)) if match else f"{course.label}-overview"
        filename = _re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip() or "syllabus"

        dest_dir = pathlib.Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{course.label.replace(' ', '')}-{filename}"
        path.write_bytes(response.content)
        return path

    def topic_detail(self, org_unit_id: int, topic_id: int) -> Dict[str, Any]:
        """Full topic record — content/root/ omits Url and TopicType."""
        try:
            return self.get(f"/d2l/api/le/{self.le}/{org_unit_id}/content/topics/{topic_id}") or {}
        except AccessDenied:
            return {}

    def grades(self, course: Course) -> List[GradeValue]:
        payload = self.get(f"/d2l/api/le/{self.le}/{course.org_unit_id}/grades/values/myGradeValues/")
        if not isinstance(payload, list):
            return []
        out: List[GradeValue] = []
        for value in payload:
            obj = value.get("GradeObjectName") or value.get("GradeObjectIdentifier") or "Item"
            out.append(
                GradeValue(
                    course=course.label,
                    name=str(obj),
                    displayed=value.get("DisplayedGrade"),
                    points=value.get("PointsNumerator"),
                    weight=value.get("WeightedNumerator"),
                )
            )
        return out


def disambiguate(courses: List[Course]) -> None:
    """Purdue's 5-digit numbers collapse ambiguously to 3 (IBE 29150 and 29120
    both read as 'IBE 291'). When two courses collide, give both the full number.
    """
    from collections import Counter

    counts = Counter(c.label for c in courses)
    for course in courses:
        if counts[course.label] > 1:
            match = _FULL_CODE.search(course.name or course.code)
            if match:
                course.label_override = f"{match.group(1)} {match.group(2)}"


def extract_cookie_pairs(raw: str) -> str:
    """Accept a whole `Cookie:` header, or a devtools copy, and normalize it."""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    pairs = [p.strip() for p in raw.split(";") if "=" in p]
    return "; ".join(pairs)


def has_session_cookies(raw: str) -> bool:
    return bool(re.search(r"d2lSessionVal\s*=", raw or ""))
