"""Meta Graph API wrapper read-only per il data-analyst.

Capabilities:
  - list_recent_campaigns(): campagne ACTIVE oppure PAUSED-aggiornate-da-meno-di-30gg
  - get_campaign_insights(): spend, impressions, clicks, ctr, cpm, leads, conversions
  - detect_campaign_type(): "lead_ad" se adset.destination_type=ON_AD/INSTANT_FORM,
                            "landing" se WEBSITE, "unknown" altrimenti
  - get_ad_breakdown(): performance per singolo ad/creative (breakdown UI step 5)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

GRAPH = "https://graph.facebook.com/v22.0"


@dataclass(frozen=True)
class CampaignSummary:
    """Campagna come arriva dalla list_campaigns. Aggiunge slug account."""

    id: str
    name: str
    status: str
    effective_status: str
    objective: str
    updated_time: str
    account_slug: str
    account_name: str


@dataclass(frozen=True)
class InsightTotals:
    spend: float
    impressions: int
    clicks: int
    ctr: float
    cpm: float
    cpc: float
    reach: int
    leads_meta: int  # count della metrica "Risultati" per come la campagna e` configurata
    leads_action_type: str  # action_type usato per leads_meta (debug/audit)
    conversions_offsite: int  # somma di tutti gli offsite_conversion.*
    raw_actions: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignMetaInfo:
    """Info ricavate dagli adset+promoted_object: tipo campagna e action_type
    da usare per contare i "Risultati" come fa Meta Ads Manager."""

    campaign_type: str          # "lead_ad" | "landing" | "messenger" | "app" | "unknown"
    leads_action_type: str      # es. "leadgen.other", "offsite_conversion.custom.123", ...
    custom_conversion_id: str = ""
    pixel_id: str = ""


@dataclass(frozen=True)
class AdBreakdownRow:
    ad_id: str
    ad_name: str
    spend: float
    impressions: int
    clicks: int
    ctr: float
    leads_meta: int
    conversions_offsite: int


class MetaError(RuntimeError):
    """Raised when the Graph API returns an error response."""


class MetaClient:
    def __init__(self, access_token: str, ad_account_id: str, account_slug: str = "", account_name: str = "") -> None:
        if not access_token:
            raise ValueError("Meta access_token is required")
        if not ad_account_id.startswith("act_"):
            raise ValueError("ad_account_id must start with 'act_'")
        self.token = access_token
        self.account = ad_account_id
        self.account_slug = account_slug or ad_account_id
        self.account_name = account_name or ad_account_id

    # ── low-level helpers ─────────────────────────────────────────────
    def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        p: dict[str, Any] = {"access_token": self.token, **(params or {})}
        r = requests.get(f"{GRAPH}/{endpoint}", params=p, timeout=60)
        body = r.json()
        if "error" in body:
            raise MetaError(f"GET {endpoint}: {body['error']}")
        return body

    def _paged_get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        first = self._get(endpoint, params)
        out.extend(first.get("data", []))
        next_url = first.get("paging", {}).get("next")
        while next_url:
            r = requests.get(next_url, timeout=60)
            body = r.json()
            if "error" in body:
                raise MetaError(f"GET paged {endpoint}: {body['error']}")
            out.extend(body.get("data", []))
            next_url = body.get("paging", {}).get("next")
        return out

    # ── campaigns ─────────────────────────────────────────────────────
    def list_recent_campaigns(self, days_paused: int = 30, limit: int = 200) -> list[CampaignSummary]:
        """Ritorna campagne ACTIVE + PAUSED aggiornate negli ultimi `days_paused` giorni.

        Esclude DELETED/ARCHIVED automaticamente filtrando per effective_status.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_paused)
        raw = self._paged_get(
            f"{self.account}/campaigns",
            {
                "fields": "id,name,status,effective_status,objective,updated_time",
                "limit": limit,
                "filtering": json.dumps([
                    {"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]}
                ]),
            },
        )
        out: list[CampaignSummary] = []
        for c in raw:
            eff = c.get("effective_status", "")
            updated = c.get("updated_time", "")
            # PAUSED -> richiede updated_time recente
            if eff != "ACTIVE":
                try:
                    ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                except Exception:
                    continue
                if ts < cutoff:
                    continue
            out.append(
                CampaignSummary(
                    id=c["id"],
                    name=c["name"],
                    status=c.get("status", ""),
                    effective_status=eff,
                    objective=c.get("objective", ""),
                    updated_time=updated,
                    account_slug=self.account_slug,
                    account_name=self.account_name,
                )
            )
        return out

    # ── tipo campagna (Lead Ad vs Landing) ────────────────────────────
    def get_campaign_meta_info(self, campaign_id: str) -> CampaignMetaInfo:
        """Heuristic basata su adset.destination_type + promoted_object.

        Ritorna anche l'action_type Meta che corrisponde ai "Risultati" della
        campagna (es. offsite_conversion.custom.{cc_id} per Landing con
        Custom Conversion).
        """
        adsets = self._get(
            f"{campaign_id}/adsets",
            {"fields": "destination_type,promoted_object,optimization_goal", "limit": 5},
        ).get("data", [])
        if not adsets:
            return CampaignMetaInfo(campaign_type="unknown", leads_action_type="lead")

        # priorita`: scegli il primo adset attivo "utile" — qui prendo il primo
        primary = adsets[0]
        dt = primary.get("destination_type", "")
        promoted = primary.get("promoted_object", {}) or {}
        opt_goal = primary.get("optimization_goal", "")

        cc_id = str(promoted.get("custom_conversion_id", "") or "")
        pixel_id = str(promoted.get("pixel_id", "") or "")
        custom_event_type = str(promoted.get("custom_event_type", "") or "").upper()

        # Tipo campagna
        if dt in ("ON_AD", "INSTANT_FORM"):
            camp_type = "lead_ad"
        elif dt == "WEBSITE":
            camp_type = "landing"
        elif dt == "MESSENGER":
            camp_type = "messenger"
        elif dt == "APP":
            camp_type = "app"
        elif opt_goal == "OFFSITE_CONVERSIONS" and pixel_id:
            # destination UNDEFINED ma ottimizzazione conversioni pixel = landing
            camp_type = "landing"
        else:
            camp_type = "unknown"

        # Action type corrispondente ai "Risultati"
        # Priorita`:
        #   1. Custom Conversion (cc_id) → offsite_conversion.custom.{cc_id}
        #   2. Pixel + LEAD              → offsite_conversion.fb_pixel_lead
        #   3. Pixel + OTHER             → offsite_conversion.fb_pixel_custom
        #                                  (Meta usa "fb_pixel_custom" per custom event nominati
        #                                   tipo "AcquisizioneContatti")
        #   4. Pixel + std event noto    → offsite_conversion.fb_pixel_{event_lower}
        #   5. Lead Ad form Meta         → onsite_conversion.lead_grouped (Outcome) o leadgen.other
        leads_action_type = "lead"  # fallback
        if cc_id:
            leads_action_type = f"offsite_conversion.custom.{cc_id}"
        elif pixel_id and custom_event_type == "LEAD":
            leads_action_type = "offsite_conversion.fb_pixel_lead"
        elif pixel_id and custom_event_type == "OTHER":
            leads_action_type = "offsite_conversion.fb_pixel_custom"
        elif pixel_id and custom_event_type:
            leads_action_type = f"offsite_conversion.fb_pixel_{custom_event_type.lower()}"
        elif camp_type == "lead_ad":
            # Outcome-Leads moderno
            leads_action_type = "onsite_conversion.lead_grouped"

        return CampaignMetaInfo(
            campaign_type=camp_type,
            leads_action_type=leads_action_type,
            custom_conversion_id=cc_id,
            pixel_id=pixel_id,
        )

    def detect_campaign_type(self, campaign_id: str) -> str:
        """Backward compat: ritorna solo la stringa tipo campagna."""
        return self.get_campaign_meta_info(campaign_id).campaign_type

    # ── insights aggregate ────────────────────────────────────────────
    def get_campaign_insights(
        self,
        campaign_id: str,
        since: str,
        until: str,
        leads_action_type: str = "",
    ) -> InsightTotals:
        """Insights aggregate sul periodo [since, until] (YYYY-MM-DD).

        Se `leads_action_type` e` specificato, usa quell'action_type per
        valorizzare `leads_meta` (es. "offsite_conversion.custom.123" per
        landing con Custom Conversion). Altrimenti fallback sul primo
        action_type lead-like trovato fra: leadgen.other,
        onsite_conversion.lead_grouped, offsite_conversion.fb_pixel_lead, lead.
        """
        data = self._get(
            f"{campaign_id}/insights",
            {
                "fields": "spend,impressions,clicks,ctr,cpm,cpc,reach,actions",
                "time_range": json.dumps({"since": since, "until": until}),
                "level": "campaign",
            },
        ).get("data", [])
        if not data:
            return InsightTotals(0.0, 0, 0, 0.0, 0.0, 0.0, 0, 0, "", 0)
        row = data[0]
        actions = {a["action_type"]: float(a["value"]) for a in row.get("actions", [])}

        used_type = leads_action_type
        leads_meta = int(actions.get(leads_action_type, 0)) if leads_action_type else 0
        # Fallback se l'action_type richiesto non e` presente nelle insights
        if not leads_meta:
            for fallback in (
                "leadgen.other",
                "onsite_conversion.lead_grouped",
                "offsite_conversion.fb_pixel_lead",
                "offsite_conversion.fb_pixel_custom",
                "lead",
            ):
                if actions.get(fallback):
                    leads_meta = int(actions[fallback])
                    used_type = fallback
                    break
        conv_offsite = 0
        for k, v in actions.items():
            if k.startswith("offsite_conversion."):
                conv_offsite += int(v)
        return InsightTotals(
            spend=float(row.get("spend", 0) or 0),
            impressions=int(row.get("impressions", 0) or 0),
            clicks=int(row.get("clicks", 0) or 0),
            ctr=float(row.get("ctr", 0) or 0),
            cpm=float(row.get("cpm", 0) or 0),
            cpc=float(row.get("cpc", 0) or 0),
            reach=int(row.get("reach", 0) or 0),
            leads_meta=leads_meta,
            leads_action_type=used_type or "(nessuno)",
            conversions_offsite=conv_offsite,
            raw_actions=actions,
        )

    # ── breakdown per ad/creative ─────────────────────────────────────
    def get_ad_breakdown(
        self,
        campaign_id: str,
        since: str,
        until: str,
        leads_action_type: str = "",
    ) -> list[AdBreakdownRow]:
        data = self._paged_get(
            f"{campaign_id}/insights",
            {
                "fields": "ad_id,ad_name,spend,impressions,clicks,ctr,actions",
                "time_range": json.dumps({"since": since, "until": until}),
                "level": "ad",
                "limit": 200,
            },
        )
        rows: list[AdBreakdownRow] = []
        for row in data:
            actions = {a["action_type"]: float(a["value"]) for a in row.get("actions", [])}
            leads_meta = 0
            if leads_action_type:
                leads_meta = int(actions.get(leads_action_type, 0))
            if not leads_meta:
                for fallback in (
                    "leadgen.other",
                    "onsite_conversion.lead_grouped",
                    "offsite_conversion.fb_pixel_lead",
                    "lead",
                ):
                    if actions.get(fallback):
                        leads_meta = int(actions[fallback])
                        break
            conv_offsite = sum(int(v) for k, v in actions.items() if k.startswith("offsite_conversion."))
            rows.append(
                AdBreakdownRow(
                    ad_id=row.get("ad_id", ""),
                    ad_name=row.get("ad_name", ""),
                    spend=float(row.get("spend", 0) or 0),
                    impressions=int(row.get("impressions", 0) or 0),
                    clicks=int(row.get("clicks", 0) or 0),
                    ctr=float(row.get("ctr", 0) or 0),
                    leads_meta=leads_meta,
                    conversions_offsite=conv_offsite,
                )
            )
        # ordinati per spesa decrescente
        rows.sort(key=lambda r: r.spend, reverse=True)
        return rows
