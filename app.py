"""Data Analyst Agent - Streamlit UI.

Flow:
  1) Selezione campagna (dropdown campagne attive + spente <30gg su 3 account Meta).
  2) Selezione periodo (default 30gg, range custom).
  3) Multi-select blocchi dati da analizzare.
  4) Output: metriche + grafici testuali + raccomandazioni AI.

Sorgenti dati:
  - Meta API: spesa, impressioni, CTR, CPM, lead Meta (Lead Ads), breakdown ads.
  - HubSpot Forms API: lead reali su landing (auto-mappato via campaigns_config.json).
  - Postgres daily_kpi_campaign: funnel post-lead, boom_value, ROAS.
  - Claude API: raccomandazioni operative.
"""
from __future__ import annotations

import os
import traceback
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

import streamlit as st
from dotenv import load_dotenv

from agent.accounts import MetaAccount, load_accounts
from agent.config import find_form_for_campaign, load_mappings
from agent.db import DBConfig, get_funnel_metrics
from agent.hubspot_api import HubSpotClient, HubSpotError
from agent.meta_api import (
    CampaignSummary,
    MetaClient,
    MetaError,
)
from agent.orch_link import linked_project_id, save_to_project_button, sidebar_project_picker
from agent.recommendations import generate_recommendations
from agent.store import AnalysisRow, AnalysisStore

load_dotenv()

# ── Secrets helpers ──────────────────────────────────────────────────
def _secret(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val:
        return val
    try:
        return st.secrets.get(key, default)
    except (FileNotFoundError, AttributeError):
        return default


APP_PASSWORD = _secret("APP_PASSWORD")
ANTHROPIC_KEY = _secret("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _secret("CLAUDE_MODEL", "claude-sonnet-4-6")
HUBSPOT_TOKEN = _secret("HUBSPOT_ACCESS_TOKEN")

PG_CFG = DBConfig(
    host=_secret("POSTGRES_HOST", "217.154.117.118"),
    port=int(_secret("POSTGRES_PORT", "5432") or "5432"),
    dbname=_secret("POSTGRES_DB", "db_kpi"),
    user=_secret("POSTGRES_USER", "ummeister"),
    password=_secret("POSTGRES_PASSWORD"),
)


# ── Page setup ───────────────────────────────────────────────────────
st.set_page_config(page_title="Data Analyst Agent", layout="wide", page_icon="📊")


def _password_gate() -> None:
    if not APP_PASSWORD:
        return
    if st.session_state.get("authed"):
        return
    st.title("📊 Data Analyst Agent")
    pw = st.text_input("Password", type="password", key="pw_input")
    if st.button("Entra"):
        if pw == APP_PASSWORD:
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("Password errata")
    st.stop()


_password_gate()


# ── Session state ────────────────────────────────────────────────────
DEFAULT_STATE: dict[str, Any] = {
    "campaigns_loaded": False,
    "all_campaigns": [],         # list[CampaignSummary]
    "selected_camp_key": "",     # "<account_slug>:<campaign_id>"
    "period_choice": "30",       # 7 / 14 / 30 / 60 / 90 / custom
    "custom_since": date.today() - timedelta(days=30),
    "custom_until": date.today(),
    "blocks": [
        "perf_meta",
        "lead_reali",
        "funnel",
        "roas",
        "breakdown",
        "confronto",
        "ai_reco",
    ],
    "result_payload": None,      # dict ultimo run
    "result_reco": "",           # raccomandazioni AI ultimo run
    "loaded_analysis_id": None,  # id archivio caricato (per badge UI)
    "_archive_action": None,     # dict {kind: 'load'|'delete', id: ...}
}
for k, v in DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Archivio ─────────────────────────────────────────────────────────
def _store() -> AnalysisStore | None:
    if "_analysis_store" not in st.session_state:
        try:
            st.session_state._analysis_store = AnalysisStore.from_env()
        except Exception:
            st.session_state._analysis_store = None
    return st.session_state._analysis_store


def _format_archive_ts(iso_string: str) -> str:
    try:
        return datetime.fromisoformat(iso_string.replace("Z", "+00:00")).strftime("%d/%m %H:%M")
    except Exception:
        return iso_string[:16]


def _render_archive_sidebar() -> None:
    store = _store()
    st.sidebar.divider()
    st.sidebar.header("📚 Archivio analisi")
    if store is None:
        st.sidebar.caption(
            "Archivio disabilitato: mancano `SUPABASE_URL` e `SUPABASE_SECRET_KEY`."
        )
        return
    try:
        rows = store.list_recent(limit=30)
    except Exception as e:
        st.sidebar.error(f"Errore archivio: {e}")
        return
    if not rows:
        st.sidebar.caption("_Nessuna analisi salvata ancora._")
        return
    if (loaded := st.session_state.get("loaded_analysis_id")):
        st.sidebar.caption(f"📌 Visualizzo `{loaded[:8]}…`")
    for row in rows:
        with st.sidebar.expander(f"{_format_archive_ts(row.created_at)} — {row.title[:60]}"):
            st.caption(f"id: `{row.id[:8]}…`")
            c1, c2 = st.columns(2)
            if c1.button("📥 Apri", key=f"open_{row.id}", use_container_width=True):
                st.session_state._archive_action = {"kind": "load", "id": row.id}
                st.rerun()
            if c2.button("🗑", key=f"del_{row.id}", use_container_width=True):
                st.session_state._archive_action = {"kind": "delete", "id": row.id}
                st.rerun()


def _apply_archive_action() -> None:
    action = st.session_state.pop("_archive_action", None)
    if not action:
        return
    store = _store()
    if store is None:
        return
    try:
        if action["kind"] == "load":
            row = store.get(action["id"])
            if row is None:
                st.warning("Analisi non trovata in archivio.")
                return
            payload = dict(row.payload)
            st.session_state.result_reco = payload.pop("ai_reco", "") or ""
            st.session_state.result_payload = payload
            st.session_state.loaded_analysis_id = row.id
        elif action["kind"] == "delete":
            store.delete(action["id"])
            if st.session_state.get("loaded_analysis_id") == action["id"]:
                st.session_state.loaded_analysis_id = None
                st.session_state.result_payload = None
                st.session_state.result_reco = ""
    except Exception as e:
        st.warning(f"Archivio: {e}")


# ── Loaders ──────────────────────────────────────────────────────────
def _meta_client_for(slug: str, accounts: list[MetaAccount]) -> MetaClient | None:
    for a in accounts:
        if a.slug == slug and a.is_configured:
            return MetaClient(
                access_token=a.access_token,
                ad_account_id=a.ad_account_id,
                account_slug=a.slug,
                account_name=a.name,
            )
    return None


@st.cache_data(ttl=300, show_spinner=False)
def _load_all_campaigns(account_slugs_blob: str) -> list[dict[str, Any]]:
    """Ritorna lista di CampaignSummary serializzate per cache."""
    out: list[dict[str, Any]] = []
    accounts = load_accounts()
    for acc in accounts:
        if not acc.is_configured:
            continue
        client = MetaClient(
            access_token=acc.access_token,
            ad_account_id=acc.ad_account_id,
            account_slug=acc.slug,
            account_name=acc.name,
        )
        try:
            for c in client.list_recent_campaigns():
                out.append(asdict(c))
        except MetaError as e:
            st.warning(f"Account {acc.slug}: errore Meta API → {e}")
    return out


def _period_range() -> tuple[str, str]:
    """Ritorna (since, until) YYYY-MM-DD dal session_state."""
    choice = st.session_state.period_choice
    if choice == "custom":
        s = st.session_state.custom_since
        u = st.session_state.custom_until
    else:
        days = int(choice)
        u = date.today()
        s = u - timedelta(days=days)
    return s.isoformat(), u.isoformat()


def _previous_period(since: str, until: str) -> tuple[str, str]:
    s = date.fromisoformat(since)
    u = date.fromisoformat(until)
    length = (u - s).days + 1
    new_u = s - timedelta(days=1)
    new_s = new_u - timedelta(days=length - 1)
    return new_s.isoformat(), new_u.isoformat()


def _delta_pct(curr: float, prev: float) -> float:
    if prev == 0:
        return 0.0 if curr == 0 else 100.0
    return (curr - prev) / prev * 100.0


# ── UI ───────────────────────────────────────────────────────────────
st.title("📊 Data Analyst Agent")
st.caption("Analisi campagne Meta Ads di Leone Master School: Meta + HubSpot + Postgres KPI.")

with st.sidebar:
    st.header("⚙️ Setup")
    accounts_loaded = [a for a in load_accounts() if a.is_configured]
    st.write(f"**Account Meta configurati:** {len(accounts_loaded)}")
    for a in accounts_loaded:
        st.write(f"• {a.name} (`{a.ad_account_id}`)")
    st.divider()
    st.write(f"**HubSpot:** {'✅' if HUBSPOT_TOKEN else '⚠️ token mancante'}")
    st.write(f"**Postgres:** {'✅' if PG_CFG.password else '⚠️ password mancante'}")
    st.write(f"**Claude API:** {'✅' if ANTHROPIC_KEY else '⚠️ key mancante'}")
    if st.button("🔄 Ricarica campagne (cache 5min)"):
        st.cache_data.clear()
        st.rerun()
    sidebar_project_picker()

_render_archive_sidebar()
_apply_archive_action()


# ── Step 1: campagna ─────────────────────────────────────────────────
st.subheader("1) Quale campagna analizzare?")

raw_camps = _load_all_campaigns(",".join(sorted(a.slug for a in accounts_loaded)))
campaigns: list[CampaignSummary] = [CampaignSummary(**c) for c in raw_camps]

if not campaigns:
    st.error("Nessuna campagna trovata. Controlla i token Meta nei Secrets/.env.")
    st.stop()

# Costruisco label "[ACCOUNT] nome (status)" e raggruppo per account
def _label(c: CampaignSummary) -> str:
    badge = "🟢" if c.effective_status == "ACTIVE" else "⏸️"
    return f"{badge} [{c.account_name}] {c.name}"


sorted_campaigns = sorted(
    campaigns,
    key=lambda c: (c.account_name, c.effective_status != "ACTIVE", c.name),
)
labels_to_key = {_label(c): f"{c.account_slug}:{c.id}" for c in sorted_campaigns}
keys_to_camp = {f"{c.account_slug}:{c.id}": c for c in sorted_campaigns}

selected_label = st.selectbox(
    "Campagna",
    options=list(labels_to_key.keys()),
    index=0,
    help=f"{len(campaigns)} campagne (attive + spente da <30gg).",
)
st.session_state.selected_camp_key = labels_to_key[selected_label]
selected_camp = keys_to_camp[st.session_state.selected_camp_key]


# ── Step 2: periodo ──────────────────────────────────────────────────
st.subheader("2) Periodo di analisi")
period_col, range_col = st.columns([1, 2])

with period_col:
    st.session_state.period_choice = st.radio(
        "Range",
        options=["7", "14", "30", "60", "90", "custom"],
        index=2,
        horizontal=True,
        format_func=lambda x: f"{x}gg" if x != "custom" else "Custom",
    )

with range_col:
    if st.session_state.period_choice == "custom":
        c1, c2 = st.columns(2)
        with c1:
            st.session_state.custom_since = st.date_input(
                "Da", value=st.session_state.custom_since
            )
        with c2:
            st.session_state.custom_until = st.date_input(
                "A", value=st.session_state.custom_until
            )
    since, until = _period_range()
    st.info(f"📅 Da **{since}** a **{until}**")


# ── Step 3: blocchi dati ─────────────────────────────────────────────
st.subheader("3) Quali dati ti interessano?")
BLOCK_OPTIONS = [
    ("perf_meta", "Performance Meta (spesa, CTR, CPM, CPL)"),
    ("lead_reali", "Lead reali (HubSpot per landing, Meta per Lead Ad)"),
    ("funnel", "Funnel post-lead (appuntamenti, chiamate, vendite)"),
    ("roas", "ROAS e revenue"),
    ("breakdown", "Breakdown per creative/ad"),
    ("confronto", "Confronto periodo precedente"),
    ("ai_reco", "Raccomandazioni AI (Claude)"),
]
cols = st.columns(2)
selected_blocks: list[str] = []
for idx, (key, label) in enumerate(BLOCK_OPTIONS):
    col = cols[idx % 2]
    default = key in st.session_state.blocks
    if col.checkbox(label, value=default, key=f"blk_{key}"):
        selected_blocks.append(key)
st.session_state.blocks = selected_blocks


# ── Analizza ─────────────────────────────────────────────────────────
run = st.button("📊 Analizza", type="primary", use_container_width=True)


# ── Esecuzione analisi ───────────────────────────────────────────────
def _run_analysis(camp: CampaignSummary, since: str, until: str, blocks: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "campagna": camp.name,
        "campaign_id": camp.id,
        "account": camp.account_name,
        "account_slug": camp.account_slug,
        "objective": camp.objective,
        "status": camp.effective_status,
        "period": {"since": since, "until": until},
    }

    meta_client = _meta_client_for(camp.account_slug, [a for a in load_accounts() if a.is_configured])
    if not meta_client:
        raise RuntimeError(f"Meta client non configurato per account {camp.account_slug}")

    # Tipo campagna + action_type "Risultati" (sempre, condiviso dai blocchi)
    meta_info = meta_client.get_campaign_meta_info(camp.id)
    camp_type = meta_info.campaign_type
    payload["tipo_campagna"] = camp_type
    payload["meta_result_action_type"] = meta_info.leads_action_type
    if meta_info.custom_conversion_id:
        payload["custom_conversion_id"] = meta_info.custom_conversion_id

    # Performance Meta + ROAS condividono insights
    insights = None
    if "perf_meta" in blocks or "roas" in blocks or "lead_reali" in blocks or "ai_reco" in blocks:
        insights = meta_client.get_campaign_insights(
            camp.id, since, until, leads_action_type=meta_info.leads_action_type
        )
        cpl_meta = (insights.spend / insights.leads_meta) if insights.leads_meta else 0.0
        cpa_offsite = (insights.spend / insights.conversions_offsite) if insights.conversions_offsite else 0.0
        payload["meta_insights"] = {
            "spend": round(insights.spend, 2),
            "impressions": insights.impressions,
            "clicks": insights.clicks,
            "ctr": round(insights.ctr, 2),
            "cpm": round(insights.cpm, 2),
            "cpc": round(insights.cpc, 2),
            "reach": insights.reach,
            "leads_meta": insights.leads_meta,
            "leads_action_type_usato": insights.leads_action_type,
            "conversions_offsite": insights.conversions_offsite,
            "cpl_meta": round(cpl_meta, 2),
            "cpa_offsite": round(cpa_offsite, 2),
        }

    # Lead reali
    if "lead_reali" in blocks:
        if camp_type == "lead_ad":
            payload["lead_reali"] = {
                "fonte": "Meta (leadgen.other)",
                "totale": insights.leads_meta if insights else 0,
                "cpl_reale": payload.get("meta_insights", {}).get("cpl_meta", 0),
            }
        else:
            mapping = find_form_for_campaign(camp.name)
            if not mapping:
                payload["lead_reali"] = {
                    "fonte": "HubSpot",
                    "warning": f"Form HubSpot non mappato per `{camp.name}` in data/campaigns_config.json",
                    "totale": None,
                }
            elif not HUBSPOT_TOKEN:
                payload["lead_reali"] = {
                    "fonte": "HubSpot",
                    "warning": "HUBSPOT_ACCESS_TOKEN non configurato",
                    "totale": None,
                }
            else:
                hs = HubSpotClient(HUBSPOT_TOKEN)
                try:
                    stats = hs.count_submissions(mapping.form_id, since, until, mapping.form_name)
                    spend = payload.get("meta_insights", {}).get("spend", 0)
                    cpl_total = (spend / stats.total) if stats.total else 0
                    cpl_unici = (spend / stats.unique_emails) if stats.unique_emails else 0
                    payload["lead_reali"] = {
                        "fonte": "HubSpot Forms API",
                        "form": mapping.form_name,
                        "form_id": mapping.form_id,
                        "totale_submission": stats.total,
                        "unique_emails": stats.unique_emails,
                        "cpl_reale_total": round(cpl_total, 2),
                        "cpl_reale_unici": round(cpl_unici, 2),
                        # alias backward-compat:
                        "cpl_reale": round(cpl_total, 2),
                    }
                except HubSpotError as e:
                    payload["lead_reali"] = {"fonte": "HubSpot", "errore": str(e)}

    # Funnel post-lead + ROAS dal DB
    funnel = None
    if "funnel" in blocks or "roas" in blocks or "ai_reco" in blocks:
        if not PG_CFG.password:
            payload["funnel_db"] = {"errore": "POSTGRES_PASSWORD non configurato"}
        else:
            try:
                funnel = get_funnel_metrics(PG_CFG, camp.name, since, until)
                payload["funnel_db"] = {
                    "matched_names": funnel.matched_names,
                    "lead": funnel.lead,
                    "unici": funnel.unici,
                    "risposte": funnel.risposte,
                    "app_set": funnel.app_set,
                    "app_proc": funnel.app_proc,
                    "app_conv": funnel.app_conv,
                    "boom_value": round(funnel.boom_value, 2),
                    "spesa_db": round(funnel.spesa_db, 2),
                    "roas": round(funnel.roas, 2),
                    "tasso_presa_appuntamento_pct": round(funnel.tasso_presa_appuntamento, 1),
                    "tasso_appuntamento_processato_pct": round(funnel.tasso_appuntamento_processato, 1),
                    "tasso_chiusura_pct": round(funnel.tasso_chiusura, 1),
                    "tasso_lead_to_sale_pct": round(funnel.tasso_conversione_lead_to_sale, 2),
                    "giorni_dati": funnel.days_with_data,
                }
            except Exception as e:
                payload["funnel_db"] = {"errore": f"DB: {e}"}

    # Breakdown ads
    if "breakdown" in blocks:
        try:
            rows = meta_client.get_ad_breakdown(
                camp.id, since, until, leads_action_type=meta_info.leads_action_type
            )
            payload["breakdown_ads"] = [
                {
                    "ad_id": r.ad_id,
                    "ad_name": r.ad_name,
                    "spend": round(r.spend, 2),
                    "impressions": r.impressions,
                    "clicks": r.clicks,
                    "ctr": round(r.ctr, 2),
                    "leads_meta": r.leads_meta,
                    "conversions_offsite": r.conversions_offsite,
                }
                for r in rows[:30]
            ]
        except MetaError as e:
            payload["breakdown_ads"] = {"errore": str(e)}

    # Confronto periodo precedente
    if "confronto" in blocks:
        prev_since, prev_until = _previous_period(since, until)
        try:
            prev_ins = meta_client.get_campaign_insights(
                camp.id, prev_since, prev_until, leads_action_type=meta_info.leads_action_type
            )
        except MetaError as e:
            payload["confronto"] = {"errore": str(e)}
        else:
            prev_funnel = None
            if PG_CFG.password:
                try:
                    prev_funnel = get_funnel_metrics(PG_CFG, camp.name, prev_since, prev_until)
                except Exception:
                    pass

            curr = payload.get("meta_insights", {})
            payload["confronto"] = {
                "periodo_precedente": {"since": prev_since, "until": prev_until},
                "spend": {
                    "curr": curr.get("spend", 0),
                    "prev": round(prev_ins.spend, 2),
                    "delta_pct": round(_delta_pct(curr.get("spend", 0), prev_ins.spend), 1),
                },
                "ctr": {
                    "curr": curr.get("ctr", 0),
                    "prev": round(prev_ins.ctr, 2),
                    "delta_pct": round(_delta_pct(curr.get("ctr", 0), prev_ins.ctr), 1),
                },
                "cpm": {
                    "curr": curr.get("cpm", 0),
                    "prev": round(prev_ins.cpm, 2),
                    "delta_pct": round(_delta_pct(curr.get("cpm", 0), prev_ins.cpm), 1),
                },
                "leads_meta": {
                    "curr": curr.get("leads_meta", 0),
                    "prev": prev_ins.leads_meta,
                    "delta_pct": round(_delta_pct(curr.get("leads_meta", 0), prev_ins.leads_meta), 1),
                },
                "roas_db": {
                    "curr": payload.get("funnel_db", {}).get("roas", 0) if isinstance(payload.get("funnel_db"), dict) else 0,
                    "prev": round(prev_funnel.roas, 2) if prev_funnel else None,
                },
            }

    return payload


# ── Render output ────────────────────────────────────────────────────
def _render_payload(p: dict[str, Any]) -> None:
    st.markdown(f"### Campagna: `{p['campagna']}`")
    cols = st.columns(4)
    cols[0].metric("Account", p["account"])
    cols[1].metric("Tipo", p.get("tipo_campagna", "?"))
    cols[2].metric("Status", p["status"])
    cols[3].metric("Objective", p["objective"])
    st.caption(f"📅 {p['period']['since']} → {p['period']['until']}")
    st.divider()

    if mi := p.get("meta_insights"):
        st.markdown("#### 📈 Performance Meta")
        c = st.columns(5)
        c[0].metric("Spesa", f"€ {mi['spend']:,.2f}")
        c[1].metric("Impression", f"{mi['impressions']:,}")
        c[2].metric("Click", f"{mi['clicks']:,}")
        c[3].metric("CTR", f"{mi['ctr']:.2f}%")
        c[4].metric("CPM", f"€ {mi['cpm']:.2f}")
        c2 = st.columns(5)
        c2[0].metric("CPC", f"€ {mi['cpc']:.2f}")
        c2[1].metric("Reach", f"{mi['reach']:,}")
        c2[2].metric("Lead (Meta)", f"{mi['leads_meta']:,}")
        c2[3].metric("Conv. offsite", f"{mi['conversions_offsite']:,}")
        c2[4].metric("CPL Meta", f"€ {mi['cpl_meta']:.2f}" if mi['cpl_meta'] else "—")
        st.caption(f"📌 Lead contati da Meta come `{mi.get('leads_action_type_usato', '?')}`")
        st.divider()

    if lr := p.get("lead_reali"):
        st.markdown("#### 👥 Lead reali")
        if "errore" in lr:
            st.error(f"Errore: {lr['errore']}")
        elif "warning" in lr:
            st.warning(lr["warning"])
        else:
            fonte = lr.get("fonte", "?")
            if "HubSpot" in fonte:
                cl = st.columns(5)
                cl[0].metric("Fonte", "HubSpot")
                cl[1].metric("Submission tot.", lr.get("totale_submission", 0))
                cl[2].metric("Lead unici", lr.get("unique_emails", 0))
                cl[3].metric("CPL su totale", f"€ {lr.get('cpl_reale_total', 0):.2f}" if lr.get('cpl_reale_total') else "—")
                cl[4].metric("CPL su unici", f"€ {lr.get('cpl_reale_unici', 0):.2f}" if lr.get('cpl_reale_unici') else "—")
            else:
                cl = st.columns(3)
                cl[0].metric("Fonte", fonte)
                cl[1].metric("Totale", lr.get("totale") or 0)
                cl[2].metric("CPL reale", f"€ {lr.get('cpl_reale', 0):.2f}")
            if frm := lr.get("form"):
                st.caption(f"Form: {frm} (id: `{lr.get('form_id')}`)")
        st.divider()

    if fd := p.get("funnel_db"):
        st.markdown("#### 🎯 Funnel post-lead (Postgres KPI)")
        if "errore" in fd:
            st.error(f"Errore: {fd['errore']}")
        else:
            f = st.columns(5)
            f[0].metric("Lead", fd["lead"])
            f[1].metric("Risposte", fd["risposte"])
            f[2].metric("App. set", fd["app_set"])
            f[3].metric("App. processati", fd["app_proc"])
            f[4].metric("Vendite", fd["app_conv"])
            f2 = st.columns(5)
            f2[0].metric("Tasso presa app.", f"{fd['tasso_presa_appuntamento_pct']:.1f}%")
            f2[1].metric("Tasso app. proc.", f"{fd['tasso_appuntamento_processato_pct']:.1f}%")
            f2[2].metric("Tasso chiusura", f"{fd['tasso_chiusura_pct']:.1f}%")
            f2[3].metric("Boom value", f"€ {fd['boom_value']:,.0f}")
            f2[4].metric("ROAS", f"{fd['roas']:.2f}x")
            if names := fd.get("matched_names"):
                if len(names) > 1:
                    st.caption(f"📌 Aggregato su {len(names)} nomi nel DB: {', '.join(names)}")
        st.divider()

    if bd := p.get("breakdown_ads"):
        st.markdown("#### 🎨 Breakdown per ad")
        if isinstance(bd, dict) and "errore" in bd:
            st.error(bd["errore"])
        elif bd:
            st.dataframe(bd, use_container_width=True, hide_index=True)
        st.divider()

    if cf := p.get("confronto"):
        st.markdown("#### 📉 Confronto periodo precedente")
        if "errore" in cf:
            st.error(cf["errore"])
        else:
            pp = cf["periodo_precedente"]
            st.caption(f"Periodo precedente: {pp['since']} → {pp['until']}")
            for metric in ("spend", "ctr", "cpm", "leads_meta"):
                m = cf[metric]
                delta = m["delta_pct"]
                arrow = "📈" if delta > 0 else ("📉" if delta < 0 else "➡️")
                st.write(f"{arrow} **{metric}**: {m['curr']} (prev {m['prev']}, Δ {delta:+.1f}%)")
            if cf.get("roas_db", {}).get("prev") is not None:
                rd = cf["roas_db"]
                st.write(f"📊 **ROAS DB**: {rd['curr']:.2f}x (prev {rd['prev']:.2f}x)")
        st.divider()


if run:
    if not selected_blocks:
        st.warning("Seleziona almeno un blocco dati da analizzare.")
    else:
        with st.spinner(f"Analizzo `{selected_camp.name}`..."):
            try:
                since, until = _period_range()
                payload = _run_analysis(selected_camp, since, until, selected_blocks)
                st.session_state.result_payload = payload

                # Raccomandazioni AI (se selezionate)
                if "ai_reco" in selected_blocks:
                    with st.spinner("Genero raccomandazioni AI..."):
                        reco = generate_recommendations(
                            api_key=ANTHROPIC_KEY,
                            payload=payload,
                            model=CLAUDE_MODEL,
                        )
                        st.session_state.result_reco = reco
                else:
                    st.session_state.result_reco = ""

                # Auto-save in archivio (non blocca se Supabase down)
                store = _store()
                if store is not None:
                    try:
                        saved = store.save_analysis(
                            payload=payload,
                            ai_reco=st.session_state.result_reco,
                            inputs={
                                "campaign_key": st.session_state.selected_camp_key,
                                "period_choice": st.session_state.period_choice,
                                "since": since,
                                "until": until,
                                "blocks": selected_blocks,
                            },
                        )
                        st.session_state.loaded_analysis_id = saved.id
                    except Exception as e:
                        st.warning(f"⚠️ Analisi fatta ma archivio non aggiornato: {e}")
            except Exception as e:
                st.error(f"Errore durante l'analisi: {e}")
                with st.expander("Traceback"):
                    st.code(traceback.format_exc())
                st.session_state.result_payload = None
                st.session_state.result_reco = ""


# ── Render persistente (sopravvive ai rerun) ─────────────────────────
if st.session_state.result_payload:
    st.divider()
    st.markdown("## 📋 Risultato analisi")
    _render_payload(st.session_state.result_payload)

    if st.session_state.result_reco:
        st.markdown("## 🧠 Raccomandazioni AI")
        st.markdown(st.session_state.result_reco)

    # Cross-app: salva analisi nel progetto orchestrator collegato
    if linked_project_id():
        save_to_project_button(
            agent_slug="analyst",
            output={
                **st.session_state.result_payload,
                "ai_reco": st.session_state.result_reco or "",
            },
            user_input={
                "campaign_id": st.session_state.result_payload.get("campaign_id"),
                "campaign_name": st.session_state.result_payload.get("campagna"),
            },
            label="🎯 Approva analisi per progetto",
            key_suffix="analyst",
        )

    with st.expander("🔍 Payload completo (JSON)"):
        st.json(st.session_state.result_payload)
