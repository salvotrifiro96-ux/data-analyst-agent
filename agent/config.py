"""Loader del config campagne -> HubSpot form mapping.

Il file `data/campaigns_config.json` ha una lista di mapping {match, form_id, ...}.
La funzione `find_form_for_campaign(name)` ritorna il primo match per substring
case-insensitive contenuta nel nome campagna Meta. Se nessun match, ritorna None
e la UI mostrera` un warning all'utente.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "campaigns_config.json",
)


@dataclass(frozen=True)
class FormMapping:
    match: str
    form_id: str
    form_name: str
    is_optin: bool = True


def load_mappings(path: str = _CONFIG_PATH) -> list[FormMapping]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("mappings", [])
    return [
        FormMapping(
            match=m["match"].lower(),
            form_id=m["form_id"],
            form_name=m.get("form_name", ""),
            is_optin=bool(m.get("is_optin", True)),
        )
        for m in raw
    ]


def find_form_for_campaign(campaign_name: str, mappings: Optional[list[FormMapping]] = None) -> Optional[FormMapping]:
    """Ritorna il primo FormMapping il cui `match` e` substring del nome campagna."""
    if mappings is None:
        mappings = load_mappings()
    name = campaign_name.lower()
    for m in mappings:
        if m.match in name:
            return m
    return None
