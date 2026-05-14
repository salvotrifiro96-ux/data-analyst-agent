"""Postgres client per il data-analyst (DB Leone db_kpi).

Espone:
  - get_funnel_metrics(campaign_name, since, until)
  - get_deals(campaign_name, since, until)

Match per nome campagna fatto con ILIKE substring per gestire i casi tipo
Meet & Greet Aste (2 nomi diversi nel DB) o piccole differenze di naming.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor


@dataclass(frozen=True)
class FunnelMetrics:
    """Aggregato su daily_kpi_campaign nel periodo."""

    lead: int = 0
    unici: int = 0
    risposte: int = 0
    app_set: int = 0
    app_proc: int = 0
    app_conv: int = 0
    boom_value: float = 0.0
    spesa_db: float = 0.0
    roas: float = 0.0
    days_with_data: int = 0
    matched_names: list[str] = field(default_factory=list)

    @property
    def tasso_risposta(self) -> float:
        return (self.risposte / self.unici * 100) if self.unici else 0.0

    @property
    def tasso_presa_appuntamento(self) -> float:
        return (self.app_set / self.risposte * 100) if self.risposte else 0.0

    @property
    def tasso_appuntamento_processato(self) -> float:
        return (self.app_proc / self.app_set * 100) if self.app_set else 0.0

    @property
    def tasso_chiusura(self) -> float:
        return (self.app_conv / self.app_proc * 100) if self.app_proc else 0.0

    @property
    def tasso_conversione_lead_to_sale(self) -> float:
        return (self.app_conv / self.unici * 100) if self.unici else 0.0


@dataclass(frozen=True)
class DealRow:
    campaign_name: str
    boom_id: Optional[str]
    importo: float
    fatturato_atteso: float
    chiusure: int
    giorni_conv_chiusura: Optional[int]
    data: str  # ISO date


class DBConfig:
    def __init__(self, host: str, port: int, dbname: str, user: str, password: str) -> None:
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password

    def connect(self):
        return psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            connect_timeout=15,
        )


def _build_match_clauses(campaign_name: str) -> tuple[str, tuple]:
    """Per il caso 'Meet & Greet Aste' (2 nomi) cerco con OR di ILIKE su due
    sotto-substring se rilevo il pattern. Altrimenti uso un singolo ILIKE."""
    name = campaign_name.lower()
    # Special case meet & greet aste: matcho su substring senza l'underscore
    if "meet" in name and "greet" in name and "aste" in name:
        return (
            "(camp ILIKE %s OR camp ILIKE %s)",
            ("%meet_greet_aste%", "%meet&greet_aste%"),
        )
    # Match esatto del nome (lowercase, _ tollerati come %)
    return ("camp ILIKE %s", (f"%{campaign_name}%",))


def get_funnel_metrics(cfg: DBConfig, campaign_name: str, since: str, until: str) -> FunnelMetrics:
    """Aggrega leads/risposte/appointments_*/boom_value_created/spesa nel range [since, until]."""
    where_match, params = _build_match_clauses(campaign_name)
    query = f"""
        SELECT
            camp,
            SUM(leads_gen) as leads_gen,
            SUM(leads_uni) as leads_uni,
            SUM(risposte) as risposte,
            SUM(appointments_set) as appointments_set,
            SUM(appointments_processed) as appointments_processed,
            SUM(appointments_converted) as appointments_converted,
            SUM(boom_value_created) as boom_value_created,
            SUM(spesa) as spesa,
            COUNT(DISTINCT kpi_date) as days_with_data
        FROM daily_kpi_campaign
        WHERE kpi_date >= %s AND kpi_date <= %s
            AND {where_match}
        GROUP BY camp
    """
    with cfg.connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (since, until, *params))
            rows = cur.fetchall()

    if not rows:
        return FunnelMetrics()

    lead = sum(int(r["leads_gen"] or 0) for r in rows)
    unici = sum(int(r["leads_uni"] or 0) for r in rows)
    risposte = sum(int(r["risposte"] or 0) for r in rows)
    app_set = sum(int(r["appointments_set"] or 0) for r in rows)
    app_proc = sum(int(r["appointments_processed"] or 0) for r in rows)
    app_conv = sum(int(r["appointments_converted"] or 0) for r in rows)
    boom = sum(float(r["boom_value_created"] or 0) for r in rows)
    spesa = sum(float(r["spesa"] or 0) for r in rows)
    days = max(int(r["days_with_data"] or 0) for r in rows)
    roas = (boom / spesa) if spesa > 0 else 0.0
    matched_names = [r["camp"] for r in rows if r.get("camp")]

    return FunnelMetrics(
        lead=lead,
        unici=unici,
        risposte=risposte,
        app_set=app_set,
        app_proc=app_proc,
        app_conv=app_conv,
        boom_value=boom,
        spesa_db=spesa,
        roas=roas,
        days_with_data=days,
        matched_names=matched_names,
    )


def get_deals(cfg: DBConfig, campaign_name: str, since: str, until: str) -> list[DealRow]:
    """Lista deal singoli da daily_kpi_camp_mm."""
    where_match, params = _build_match_clauses(campaign_name)
    query = f"""
        SELECT
            camp,
            boom_id,
            importo,
            fatturato_atteso,
            chiusure,
            giorni_conv_chiusura,
            kpi_date
        FROM daily_kpi_camp_mm
        WHERE kpi_date >= %s AND kpi_date <= %s
            AND {where_match}
            AND (chiusure > 0 OR importo > 0)
        ORDER BY kpi_date DESC, importo DESC
    """
    with cfg.connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (since, until, *params))
            rows = cur.fetchall()
    out: list[DealRow] = []
    for r in rows:
        out.append(
            DealRow(
                campaign_name=r["camp"] or "",
                boom_id=r.get("boom_id"),
                importo=float(r["importo"] or 0),
                fatturato_atteso=float(r["fatturato_atteso"] or 0),
                chiusure=int(r["chiusure"] or 0),
                giorni_conv_chiusura=r.get("giorni_conv_chiusura"),
                data=r["kpi_date"].strftime("%Y-%m-%d") if r["kpi_date"] else "",
            )
        )
    return out
