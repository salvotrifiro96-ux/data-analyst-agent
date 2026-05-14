"""HubSpot Forms API client per il data-analyst.

Conta i lead opt-in da un form nel periodo [since, until] usando l'endpoint
legacy `/form-integrations/v1/submissions/forms/{formId}` che e` l'unico modo
affidabile per ottenere TUTTE le submission (la ricerca contatti per
`recent_conversion_event_name` mostra solo l'ultima conversione per contatto).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

BASE = "https://api.hubapi.com"


class HubSpotError(RuntimeError):
    """Raised when the HubSpot API returns an error response."""


@dataclass(frozen=True)
class FormSubmissionStats:
    form_id: str
    form_name: str
    total: int
    unique_emails: int


def _to_ms(date_str: str, end_of_day: bool = False) -> int:
    """YYYY-MM-DD -> epoch ms in UTC."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("HubSpot access_token is required")
        self.token = access_token
        self.headers = {"Authorization": f"Bearer {access_token}"}

    def count_submissions(
        self,
        form_id: str,
        since: str,
        until: str,
        form_name: str = "",
    ) -> FormSubmissionStats:
        """Conta tutte le submission del form nel periodo, deduplicate per email.

        Usa paginazione `after` (limit 50 hardcoded HubSpot).
        """
        since_ms = _to_ms(since, end_of_day=False)
        until_ms = _to_ms(until, end_of_day=True)

        total = 0
        emails: set[str] = set()
        after: Optional[str] = None

        while True:
            params: dict[str, str | int] = {"limit": 50}
            if after:
                params["after"] = after
            r = requests.get(
                f"{BASE}/form-integrations/v1/submissions/forms/{form_id}",
                headers=self.headers,
                params=params,
                timeout=30,
            )
            if r.status_code != 200:
                raise HubSpotError(f"GET form {form_id}: {r.status_code} {r.text[:200]}")
            body = r.json()
            results = body.get("results", [])
            stop_paging = False
            for sub in results:
                ts = int(sub.get("submittedAt", 0))
                if ts < since_ms:
                    # le submission sono ordinate desc per submittedAt: appena
                    # scendo sotto la soglia inferiore posso fermarmi.
                    stop_paging = True
                    break
                if ts > until_ms:
                    # ancora troppo recente, skip
                    continue
                total += 1
                # email tipicamente nei values come {name:"email", value:"..."}
                for v in sub.get("values", []):
                    if v.get("name") == "email":
                        emails.add(v.get("value", "").lower())
                        break

            paging = body.get("paging", {}).get("next", {})
            after = paging.get("after") if paging else None
            if stop_paging or not after:
                break

        return FormSubmissionStats(
            form_id=form_id,
            form_name=form_name,
            total=total,
            unique_emails=len(emails),
        )
