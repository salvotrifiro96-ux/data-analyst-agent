# Data Analyst Agent — Leone Master School

Streamlit pubblico per analisi performance campagne Meta Ads del team Leone.
Aggrega dati da Meta API, HubSpot Forms, Postgres KPI (`daily_kpi_campaign`)
e produce raccomandazioni operative con Claude.

Deploy target: `data-analyst-agent.streamlit.app`

## Flow utente

1. Selezione campagna da dropdown (ACTIVE + PAUSED da <30gg, su tutti gli account configurati).
2. Selezione periodo (default ultimi 30gg, range 7/14/30/60/90/custom).
3. Multi-select dei blocchi dati da estrarre:
   - **Performance Meta** — spesa, impressioni, click, CTR, CPM, CPC, reach, CPL Meta
   - **Lead reali** — auto-routing: Lead Ad → Meta `leadgen.other`; Landing → HubSpot Forms API
   - **Funnel post-lead** — risposte, app. set, app. processati, vendite, tassi di conversione (da Postgres)
   - **ROAS e revenue** — `boom_value / spesa` da `daily_kpi_campaign`
   - **Breakdown per creative/ad** — performance singoli ads (referral `imgN`/`vidN`)
   - **Confronto periodo precedente** — delta % su spend/CTR/CPM/leads
   - **Raccomandazioni AI** — Claude analizza il payload e suggerisce scaling/pause/test

## Sorgenti dati

| Dato | Sorgente | Note |
|---|---|---|
| Spesa / CTR / CPM / Lead Meta | Meta `/{camp_id}/insights` | per periodo |
| Tipo campagna (Lead Ad vs Landing) | Meta `adset.destination_type` | `ON_AD`/`INSTANT_FORM` → Lead Ad, `WEBSITE` → Landing |
| Lead reali (Lead Ad) | Meta `actions[leadgen.other]` | da insights |
| Lead reali (Landing) | HubSpot Forms API | mapping in `data/campaigns_config.json` |
| Funnel post-lead | Postgres `daily_kpi_campaign` | match per substring nome campagna |
| ROAS / revenue | Postgres `boom_value / spesa` | stessa tabella |
| Deal singoli | Postgres `daily_kpi_camp_mm` | dettaglio per boom_id |
| Raccomandazioni | Claude API (anthropic) | modello configurabile |

## Setup locale

```bash
cd /Users/salvotrifiro/leone-agents/data-analyst-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# compila i valori in .env
streamlit run app.py
```

## Deploy Streamlit Cloud

1. Repo GitHub: spingere questa cartella su un repo dedicato.
2. Nei **Secrets** del deploy mettere in formato TOML:

```toml
APP_PASSWORD = ""               # vuoto per pubblico
ANTHROPIC_API_KEY = "sk-ant-..."
CLAUDE_MODEL = "claude-sonnet-4-6"

POSTGRES_HOST = "217.154.117.118"
POSTGRES_PORT = "5432"
POSTGRES_DB = "db_kpi"
POSTGRES_USER = "ummeister"
POSTGRES_PASSWORD = "..."

HUBSPOT_ACCESS_TOKEN = "..."

META_SWAT_ACCESS_TOKEN = "..."
META_SWAT_AD_ACCOUNT_ID = "act_191279579779492"
META_SWAT_NAME = "MBE Swat"

META_PATATINO_ACCESS_TOKEN = "..."
META_PATATINO_AD_ACCOUNT_ID = "act_900331255794779"
META_PATATINO_NAME = "Patatino"

META_LRES_ACCESS_TOKEN = "..."
META_LRES_AD_ACCOUNT_ID = "act_2176405965804688"
META_LRES_NAME = "LRES"
```

3. `streamlit run app.py` come main file.

## Mappare nuove campagne landing → form HubSpot

Editare `data/campaigns_config.json` e aggiungere un blocco:

```json
{
  "match": "<substring lowercase nel nome campagna Meta>",
  "form_id": "<HubSpot form GUID>",
  "form_name": "<nome leggibile>",
  "is_optin": true
}
```

Per Lead Ad non serve mapping: il conteggio arriva da Meta `leadgen.other`.

## Tests

```bash
pytest -q
```

## Architettura moduli

```
agent/
  accounts.py        # loader Meta accounts (SWAT/PATATINO/LRES/...)
  meta_api.py        # MetaClient: list_campaigns, insights, breakdown, detect_type
  hubspot_api.py     # HubSpotClient: count_submissions per form_id+date_range
  db.py              # Postgres: get_funnel_metrics, get_deals
  config.py          # mapping campagna → form HubSpot
  recommendations.py # Claude API per raccomandazioni operative
data/
  campaigns_config.json
app.py               # Streamlit UI
```
