> **New (2026-10): `paperbot/`** — a separate, order-book-based research & paper-trading
> system with realistic fills, Polymarket taker fees, a hard $10/month AI cap and a
> scheduled GitHub Actions job. Start with [docs/PAPERBOT.md](docs/PAPERBOT.md); audit
> findings for the legacy bot below are in [docs/AUDIT.md](docs/AUDIT.md).

# Polymarket Paper Trading Bot

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-v2-E92063?logo=pydantic&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-dashboard-FF4B4B?logo=streamlit&logoColor=white)
![Claude](https://img.shields.io/badge/Claude-API-D97757?logo=anthropic&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-active-brightgreen)

An autonomous paper trading bot for [Polymarket](https://polymarket.com) prediction markets. It ingests news from multiple sources in real time, uses an LLM (Claude or a local Ollama model) to infer the true probability of each market outcome, identifies mispriced markets, and simulates trades under strict risk management rules — all without touching real money.

> **Paper trading only.** No real funds are ever moved. All positions are virtual, backed by a configurable starting balance.

---

## How it works

The bot runs a continuous pipeline on a scheduler:

```
NewsAPI / GDELT / Telegram
         │
         ▼
    News Ingestor       ← deduplication, scoring, priority queue
         │
         ▼
  Sentiment Analyzer    ← LLM (Claude / Ollama): probability + edge + confidence
         │
         ▼
   Market Scanner       ← Polymarket Gamma API: filters by volume, spread, time
         │
         ▼
   Decision Engine      ← crosses news analysis with market price; sizes the trade
         │
         ▼
    Risk Manager        ← validates against configured limits (runs on every trade)
         │
         ▼
    Paper Trader        ← simulates execution with slippage and partial fills
         │
    ┌────┴─────┐
    ▼          ▼
 SQLite     Discord
    │
    ▼
Excel Report  ·  Streamlit Dashboard
```

Every 5 minutes the bot scans live markets, fetches fresh news, and makes decisions. Open positions are re-evaluated every 15 minutes. A daily Excel report and Discord summary are sent at 23:55.

---

## Features

- **Multi-source news ingestion** — NewsAPI, GDELT (no key required), and Telegram public channels via Telethon MTProto. Fuzzy deduplication prevents analyzing the same story twice.
- **3-agent LLM panel** — Three independent agents (Quant, Domain Expert, Adversarial) each estimate YES probability, then a Synthesis agent reconciles their views. Computes panel standard deviation, mispricing Z-score `(p_model − p_market) / σ`, and expected value `EV = p·b − (1−p)` for every signal.
- **LLM analysis monitor** — Run `python llm_monitor.py` in a second terminal to watch every agent call live: per-agent probability, edge, token usage, synthesis output, and post-mortem stream — all updated every 2 seconds via a Rich TUI.
- **Market scanning** — Polls the Polymarket Gamma + CLOB APIs; filters out low-volume, wide-spread, or near-expiry markets. Parallel processing for speed.
- **Kelly Criterion sizing** — Position size = `balance × min(f_kelly, 5%)` where `f_kelly = f_full × 0.25` (quarter Kelly). Negative-edge trades (`f_full ≤ 0`) are blocked automatically.
- **Value at Risk check** — Parametric VaR at 95% confidence (`1.645 × σ × size`). Rejects any trade whose 1-day VaR exceeds 5% of current balance.
- **Slippage guard** — Before executing, compares the current token price against the signal price. Aborts if drift exceeds 2% (configurable).
- **Kill switch** — Dashboard "Emergency Stop" button writes to `data/overrides.json`; the bot closes all open positions on its next cycle and halts new trades until deactivated.
- **Brier score tracking** — `predicted_prob` (model consensus) flows from analysis → decision → position → post-mortem. `actual_outcome` is inferred from exit price for resolved markets, enabling calibrated Brier score computation.
- **Sports in-play mode** — Optional separate sub-strategy for live sports markets with its own size and confidence limits.
- **Backtester** — Runs the full pipeline against already-resolved Polymarket markets. Two modes: `current` (today's news, fast calibration) and `replay` (historical GDELT news, realistic).
- **Streamlit dashboard** — Real-time view of balance curve, open positions, P&L per trade, LLM decision log, manual override controls, and emergency kill switch.
- **Excel reports** — Daily `.xlsx` with 5 sheets: Executive Summary, Detailed Trades, LLM Analyses, Decisions Log, Balance Curve.
- **Discord notifications** — Rich embeds for trade open/close, stop-loss trigger, drawdown alert, bot pause/resume, and daily summary.
- **Full test suite** — 292 pytest tests covering all major modules and integration fixtures.

---

## Tech stack

| Layer | Libraries |
|---|---|
| Data validation | Pydantic v2 |
| LLM | `anthropic` SDK, Ollama HTTP API |
| Scheduling | APScheduler |
| Database | SQLite (`sqlite3` stdlib, WAL mode) |
| News | `newsapi-python`, `gdeltdoc`, Telethon |
| Dashboard | Streamlit, Plotly |
| Reports | openpyxl, xlsxwriter |
| Notifications | `discord-webhook` |
| HTTP | `httpx`, `requests`, `tenacity` (retries) |
| Deduplication | `rapidfuzz` |
| Logging | Loguru |
| Testing | pytest, pytest-mock, pytest-asyncio |

---

## Quick start

**Prerequisites:** Python 3.10+, and at least one of: Anthropic API key, or [Ollama](https://ollama.com) running locally.

```bash
# 1. Clone and install
git clone https://github.com/icabrera0/Polymarket-paper-trader.git
cd Polymarket-paper-trader
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 2. Configure secrets
copy .env.example .env       # then fill in your keys

# 3. Review settings (balance, risk %, intervals, LLM provider, etc.)
#    config/settings.yaml — no code changes needed

# 4. Run the bot
python main.py

# 5. Open the dashboard (separate terminal)
streamlit run dashboard.py
```

### Environment variables (`.env`)

```
ANTHROPIC_API_KEY=sk-ant-...          # required if provider = anthropic
NEWSAPI_KEY=...                        # required if newsapi.enabled = true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
TELEGRAM_API_ID=...                    # required if telegram.enabled = true
TELEGRAM_API_HASH=...
TELEGRAM_PHONE=+34...
```

GDELT is always available with no credentials required.

---

## Risk management

Defaults calibrated for a **€150** starting bankroll — fully adjustable in `settings.yaml`:

| Parameter | Default | Description |
|---|---|---|
| Position sizing | Quarter Kelly | `f_kelly = f_full × 0.25`, hard cap at 5% of balance |
| Max position size cap | 5% of balance | Hard ceiling over Kelly output (~€7.50) |
| Max open positions | 15 | |
| Min trade size | €5 | Below this the trade is skipped |
| Min price edge | 4% | Minimum `\|p_model − p_market\|` to consider a trade |
| VaR limit | 5% of balance/day | Parametric 95% CI; rejects oversized trades |
| Slippage guard | 2% | Aborts trade if price drifted since signal |
| Stop loss | −20% of entry | Automatic close |
| Take profit | +30% | Evaluated on each cycle |
| Max drawdown | 30% | Bot auto-pauses (configurable) |
| Min 24h market volume | $10,000 | Liquidity filter |
| Max bid/ask spread | 5 cents | Efficiency filter |
| Time exit — tier 1 | 24 h | Tighten TP to +15% |
| Time exit — tier 2 | 48 h | Close immediately if profitable |
| Time exit — tier 3 | 72 h | Unconditional close |
| Kill switch | Dashboard button | Closes all positions immediately, halts new trades |

---

## Project structure

```
├── main.py                  # Entry point
├── dashboard.py             # Streamlit dashboard (includes kill switch)
├── llm_monitor.py           # Rich TUI — live view of every LLM agent call
├── dev_runner.py            # Hot-reload wrapper for development
├── config/
│   └── settings.yaml        # All tuneable parameters
├── src/
│   ├── orchestrator.py      # Pipeline coordinator + APScheduler
│   ├── market_scanner.py    # Gamma/CLOB API client + filters
│   ├── news_ingestor.py     # Multi-source ingestion + deduplication
│   ├── sentiment_analyzer.py# LLM analysis (Claude / Ollama)
│   ├── decision_engine.py   # Trade sizing + entry logic
│   ├── risk_manager.py      # Limits, stop-loss, drawdown
│   ├── paper_trader.py      # Simulated execution
│   ├── report_generator.py  # Excel reports
│   ├── notification_system.py# Discord webhooks
│   ├── backtester.py        # Historical simulation
│   ├── database.py          # SQLite persistence
│   ├── models.py            # Pydantic data models
│   ├── config_loader.py     # YAML + .env loader
│   ├── llm_client.py        # Unified Anthropic/Ollama interface
│   └── ...                  # API clients (Gamma, CLOB, NewsAPI, GDELT, Telegram)
├── tests/                   # pytest suite
├── scripts/                 # Utility scripts (backtest runner, balance manager, etc.)
└── requirements.txt
```

---

## Disclaimer

This project is for **educational and research purposes only**. Paper trading results do not predict real trading performance. Polymarket may be restricted in your jurisdiction. The author accepts no liability for any use of this code.
