"""Optional Brightspace Valence (REST) client.

Only usable if an admin at your school registers an OAuth app for you
(Admin Tools -> Manage Extensibility -> OAuth 2.0 -> Register an app). Without
that, the iCal feed in `feed.py` is the working path — this module exists so the
upgrade is a config change rather than a rewrite.

Docs: https://docs.valence.desire2learn.com/
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

from .config import Config, ConfigError

# The OAuth 2.0 token endpoint is D2L's central auth service, NOT a path on your
# school's Brightspace host.
TOKEN_URL = "https://auth.brightspace.com/core/connect/token"
LP_VERSION = "1.43"
LE_VERSION = "1.67"


@dataclass
class Assignment:
    course: str
    title: str
    due: Optional[datetime]


class ValenceClient:
    def __init__(self, config: Config):
        if not config.bs_base_url:
            raise ConfigError("BRIGHTSPACE_BASE_URL is required for Valence API access.")
        self.config = config
        self.base_url = config.bs_base_url
        self._token = config.bs_access_token or None

    def token(self) -> str:
        if self._token:
            return self._token
        cfg = self.config
        if not (cfg.bs_refresh_token and cfg.bs_client_id and cfg.bs_client_secret):
            raise ConfigError(
                "Set BRIGHTSPACE_ACCESS_TOKEN, or all of BRIGHTSPACE_REFRESH_TOKEN, "
                "BRIGHTSPACE_CLIENT_ID and BRIGHTSPACE_CLIENT_SECRET."
            )
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": cfg.bs_refresh_token,
                "client_id": cfg.bs_client_id,
                "client_secret": cfg.bs_client_secret,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Token refresh failed ({response.status_code}): {response.text[:400]}")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"Token response had no access_token: {payload}")
        # Refresh tokens rotate — the new one must be persisted or the next run fails.
        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != cfg.bs_refresh_token:
            print(
                "[valence] Brightspace rotated your refresh token. Update .env:\n"
                f"BRIGHTSPACE_REFRESH_TOKEN={new_refresh}"
            )
        self._token = token
        return token

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = requests.get(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"},
            params=params,
            timeout=30,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} for {path}: {response.text[:400]}")
        return response.json() if response.text else None

    def whoami(self) -> Dict[str, Any]:
        payload = self.get(f"/d2l/api/lp/{LP_VERSION}/users/whoami")
        if not payload:
            raise RuntimeError("whoami returned nothing — the token is probably invalid.")
        return payload

    def enrollments(self) -> List[Dict[str, Any]]:
        """Active course enrollments for the authenticated user."""
        items: List[Dict[str, Any]] = []
        bookmark: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"orgUnitTypeId": 3}
            if bookmark:
                params["bookmark"] = bookmark
            payload = self.get(f"/d2l/api/lp/{LP_VERSION}/enrollments/myenrollments/", params)
            if not payload:
                break
            items.extend(payload.get("Items", []))
            if not payload.get("PagingInfo", {}).get("HasMoreItems"):
                break
            bookmark = payload.get("PagingInfo", {}).get("Bookmark")
            if not bookmark:
                break
        return items

    def dropbox_due(self, org_unit_id: int, course: str) -> List[Assignment]:
        payload = self.get(f"/d2l/api/le/{LE_VERSION}/{org_unit_id}/dropbox/folders/")
        if not isinstance(payload, list):
            return []
        out: List[Assignment] = []
        for folder in payload:
            due_raw = folder.get("DueDate")
            due = None
            if isinstance(due_raw, str):
                try:
                    due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
                except ValueError:
                    due = None
            out.append(
                Assignment(course=course, title=str(folder.get("Name") or "Assignment"), due=due)
            )
        return out


def upcoming_assignments(config: Config, days: int = 7) -> List[Assignment]:
    """Assignments due within `days`, via the REST API. Requires OAuth access."""
    client = ValenceClient(config)
    horizon = datetime.now(timezone.utc) + timedelta(days=days)
    results: List[Assignment] = []
    for enrollment in client.enrollments():
        org_unit = enrollment.get("OrgUnit") or {}
        org_unit_id = org_unit.get("Id")
        if not org_unit_id:
            continue
        name = str(org_unit.get("Name") or org_unit.get("Code") or org_unit_id)
        for assignment in client.dropbox_due(int(org_unit_id), name):
            if assignment.due and datetime.now(timezone.utc) <= assignment.due <= horizon:
                results.append(assignment)
    return sorted(results, key=lambda a: a.due or horizon)
