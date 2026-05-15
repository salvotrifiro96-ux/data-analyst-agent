"""Archivio Supabase delle analisi prodotte dal data-analyst.

Riusa la tabella condivisa `agent_outputs`:
    agent_type = 'analyst'
    subtype    = 'analysis'
    title      = "<campagna> · <since>→<until>"
    payload    = { ...payload completo + ai_reco, "_inputs": {...} per re-run }

Le env vars SUPABASE_URL e SUPABASE_SECRET_KEY (o SUPABASE_SERVICE_KEY come
fallback legacy) devono essere settate. `AnalysisStore.from_env()` ritorna
None se mancano, e l'app continua senza archivio.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import requests

_TABLE = "agent_outputs"
_AGENT_TYPE = "analyst"
_SUBTYPE = "analysis"


@dataclass(frozen=True)
class AnalysisRow:
    id: str
    title: str
    payload: dict[str, Any]
    inputs: dict[str, Any]
    created_at: str


class AnalysisStore:
    def __init__(self, url: str, secret_key: str) -> None:
        if not url or not secret_key:
            raise ValueError("SUPABASE_URL e SUPABASE_SECRET_KEY obbligatori")
        self.url = url.rstrip("/")
        self.secret_key = secret_key
        self._rest = f"{self.url}/rest/v1"
        self._h_read = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }
        self._h_write = {
            **self._h_read,
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    @classmethod
    def from_env(cls) -> "AnalysisStore | None":
        try:
            import streamlit as st
            url = os.getenv("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
            key = (
                os.getenv("SUPABASE_SECRET_KEY")
                or os.getenv("SUPABASE_SERVICE_KEY")
                or st.secrets.get("SUPABASE_SECRET_KEY", "")
                or st.secrets.get("SUPABASE_SERVICE_KEY", "")
            )
        except Exception:
            url = os.getenv("SUPABASE_URL", "")
            key = (
                os.getenv("SUPABASE_SECRET_KEY", "")
                or os.getenv("SUPABASE_SERVICE_KEY", "")
            )
        if not url or not key:
            return None
        return cls(url=url, secret_key=key)

    @staticmethod
    def _row_to_analysis(row: dict[str, Any]) -> AnalysisRow:
        payload = row.get("payload") or {}
        inputs = payload.pop("_inputs", {}) if isinstance(payload, dict) else {}
        return AnalysisRow(
            id=str(row["id"]),
            title=row.get("title", "") or "(senza titolo)",
            payload=payload if isinstance(payload, dict) else {},
            inputs=inputs if isinstance(inputs, dict) else {},
            created_at=row.get("created_at", ""),
        )

    def save_analysis(
        self,
        *,
        payload: dict[str, Any],
        ai_reco: str,
        inputs: dict[str, Any],
    ) -> AnalysisRow:
        period = payload.get("period") or {}
        title = (
            f"{payload.get('campagna', '?')} · "
            f"{period.get('since', '?')}→{period.get('until', '?')}"
        )[:200]
        body = {
            "agent_type": _AGENT_TYPE,
            "subtype": _SUBTYPE,
            "title": title,
            "payload": {**payload, "ai_reco": ai_reco, "_inputs": inputs},
            "preview": (ai_reco or "")[:500],
            "metadata": {
                "campaign_id": payload.get("campaign_id"),
                "account_slug": payload.get("account_slug"),
                "tipo_campagna": payload.get("tipo_campagna"),
                "period": period,
            },
        }
        r = requests.post(
            f"{self._rest}/{_TABLE}",
            data=json.dumps(body),
            headers=self._h_write,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Insert analysis fallito {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"Risposta inattesa: {data!r}")
        return self._row_to_analysis(data[0])

    def list_recent(self, limit: int = 30) -> list[AnalysisRow]:
        r = requests.get(
            f"{self._rest}/{_TABLE}",
            params={
                "select": "*",
                "agent_type": f"eq.{_AGENT_TYPE}",
                "subtype": f"eq.{_SUBTYPE}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            headers=self._h_read,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"List analisi fallito {r.status_code}: {r.text[:300]}")
        rows = r.json() or []
        return [self._row_to_analysis(row) for row in rows]

    def get(self, analysis_id: str) -> AnalysisRow | None:
        r = requests.get(
            f"{self._rest}/{_TABLE}",
            params={"select": "*", "id": f"eq.{analysis_id}", "limit": "1"},
            headers=self._h_read,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Get analisi fallito {r.status_code}: {r.text[:300]}")
        rows = r.json() or []
        if not rows:
            return None
        return self._row_to_analysis(rows[0])

    def delete(self, analysis_id: str) -> None:
        r = requests.delete(
            f"{self._rest}/{_TABLE}",
            params={"id": f"eq.{analysis_id}"},
            headers=self._h_read,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Delete analisi fallito {r.status_code}: {r.text[:300]}")
