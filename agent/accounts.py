"""Loader dei Meta business account dai secrets Streamlit / env.

Formato atteso (env o Streamlit secrets), per ogni account:

    META_<KEY>_NAME             = "..."   # opzionale, display name
    META_<KEY>_ACCESS_TOKEN     = "..."
    META_<KEY>_AD_ACCOUNT_ID    = "act_..."
    META_<KEY>_PAGE_ID          = "..."
    META_<KEY>_INSTAGRAM_USER_ID = "..."
    META_<KEY>_PIXEL_ID         = "..."   # opzionale

dove <KEY> e` lo slug uppercase del nome (es. SWAT, PATATINO, LRES).

L'app filtra automaticamente gli account che non hanno almeno
access_token + ad_account_id + page_id + instagram_user_id valorizzati.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

# Account configurati di default per Leone. Se ne servono altri, aggiungi
# qui lo slug e in .env / secrets le 5 var corrispondenti.
DEFAULT_ACCOUNT_SLUGS: tuple[str, ...] = (
    "SWAT",
    "PATATINO",
    "LRES",
    "GEN",
    "MEP",
    "ICMD",
)


@dataclass(frozen=True)
class MetaAccount:
    """Bundle di credenziali per un singolo Meta business account."""

    slug: str
    name: str
    access_token: str
    ad_account_id: str
    page_id: str
    instagram_user_id: str
    pixel_id: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(
            self.access_token
            and self.ad_account_id
            and self.page_id
            and self.instagram_user_id
        )


def _read(key: str) -> str:
    """Legge env > st.secrets > stringa vuota."""
    val = os.getenv(key, "")
    if val:
        return val.strip()
    try:
        import streamlit as st

        return str(st.secrets.get(key, "")).strip()
    except Exception:
        return ""


def load_account(slug: str) -> MetaAccount:
    s = slug.upper()
    name = _read(f"META_{s}_NAME") or slug.capitalize()
    return MetaAccount(
        slug=s,
        name=name,
        access_token=_read(f"META_{s}_ACCESS_TOKEN"),
        ad_account_id=_read(f"META_{s}_AD_ACCOUNT_ID"),
        page_id=_read(f"META_{s}_PAGE_ID"),
        instagram_user_id=_read(f"META_{s}_INSTAGRAM_USER_ID"),
        pixel_id=_read(f"META_{s}_PIXEL_ID"),
    )


def load_accounts(slugs: Iterable[str] = DEFAULT_ACCOUNT_SLUGS) -> list[MetaAccount]:
    return [load_account(s) for s in slugs]
