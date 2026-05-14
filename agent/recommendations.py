"""Genera raccomandazioni operative via Claude API.

Input: dict con tutti i dati raccolti (insights Meta, lead, funnel, ROAS,
breakdown, confronto periodo).
Output: testo markdown strutturato con scaling/pause/test/note.
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """\
Sei il Data Analyst di Leone Master School. Analizzi performance di campagne \
Meta Ads (Facebook + Instagram) e produci raccomandazioni operative concrete \
per il media buyer.

Stile:
- Italiano, diretto, niente fronzoli.
- Numeri prima dei pareri.
- Output in markdown, max 400 parole.
- Sezioni: ## Cosa dicono i numeri / ## Cosa fare adesso / ## Cosa testare / ## Allarmi.

Regole di giudizio operativo (Leone playbook):
1. ROAS = boom_value / spesa. Scalare solo se ROAS >= 3x stabile.
2. CPL Meta vs CPL HubSpot: se divergono >30%, il pixel sta mentendo, fidarsi di HubSpot.
3. Spegnere ad/campagne con CPL > 5x mediana e zero conversioni dopo 50+ lead.
4. Mai scalare oltre +20% al giorno.
5. Tasso presa appuntamento sano: 25-40% (app_set/risposte). Sotto 20% = problema messaggi a lead.
6. Tasso chiusura sano: 15-25% (app_conv/app_proc). Sotto 10% = problema vendite/qualita` lead.
7. Se delta vs periodo prec. > -30% su una metrica chiave, mettere un ALLARME.
8. Suggerire test (creative refresh) quando CTR scende sotto 0.8% e/o frequency > 3.

Non inventarti dati. Se un dato manca, dillo.
"""


def build_user_prompt(payload: dict[str, Any]) -> str:
    """Serializza il payload come testo strutturato per Claude."""
    return (
        "Ecco i dati raccolti per la campagna selezionata. Producimi le "
        "raccomandazioni seguendo il formato richiesto.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str, ensure_ascii=False)}\n```"
    )


def generate_recommendations(
    api_key: str,
    payload: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1500,
) -> str:
    """Chiama Claude con il payload e ritorna il testo della risposta."""
    if not api_key:
        return "_ANTHROPIC_API_KEY non configurato: raccomandazioni AI disabilitate._"

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(payload)}],
    )
    parts = []
    for block in msg.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "\n".join(parts).strip() or "_Nessuna risposta generata._"
