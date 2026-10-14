# Polymarket Paper Trading Bot — Claude Context

## What this project is

Autonomous paper trading bot for [Polymarket](https://polymarket.com) prediction markets. It ingests news, uses an LLM to infer true event probabilities, finds mispriced markets, and simulates trades under strict risk management. No real money involved.

---

## paperbot (new, preferred) vs legacy `src/`

`paperbot/` is a separate package (order-book execution, real fees, persistent AI budget).
Docs: `docs/PAPERBOT.md`, audit: `docs/AUDIT.md`. Tests: `python -m pytest -c pytest-paperbot.ini`
(requires `PAPER_MODE=true` only for CLI runs; tests set it themselves). Rules for paperbot:
never add order-placement/signing code or wallet handling; never value positions at midpoints;
never count UNCERTAIN (unverified) fills in MAIN-book performance — they belong in the SHADOW book;
every new limit goes in `config/paperbot.yaml` and `paperbot/risk.py` with a test.

## How to run (legacy bot)

```bash
# Activate venv first
venv\Scripts\activate

# Run the bot
python main.py

# Dashboard (separate terminal)
streamlit run dashboard.py

# Dev mode (hot-reload on file save)
python dev_runner.py

# Backtest
python scripts/run_backtest.py --mode current
```

---

## Architecture — pipeline order

```
NewsIngestor → SentimentAnalyzer → MarketScanner → DecisionEngine → RiskManager → PaperTrader
```

Orchestrated by APScheduler in `src/orchestrator.py`:
- Every **5 min** → full scan + trade cycle
- Every **15 min** → re-evaluate open positions
- Every **23:55** → generate daily Excel report + Discord summary

All inter-module data flows through **Pydantic models** defined in `src/models.py`. Never pass raw dicts between modules.

---

## Key files

| File | Role |
|---|---|
| `src/orchestrator.py` | Pipeline coordinator, scheduler, lifecycle |
| `src/models.py` | All shared Pydantic models — single source of truth |
| `src/config_loader.py` | Loads `config/settings.yaml` + `.env` into `BotConfig` |
| `src/decision_engine.py` | Trade sizing, entry logic, Kelly-based position sizing |
| `src/risk_manager.py` | Validates every trade; stop-loss, drawdown, time exits |
| `src/sentiment_analyzer.py` | LLM calls (Claude / Ollama), in-memory LRU cache |
| `src/llm_client.py` | Unified Anthropic/Ollama interface |
| `src/database.py` | SQLite wrapper (WAL mode, no ORM) |
| `dashboard.py` | Streamlit dashboard — reads same DB, writes `data/overrides.json` |
| `config/settings.yaml` | **All tuneable parameters** — change here, not in code |

---

## Configuration

All parameters live in `config/settings.yaml` — no code changes needed to tune the bot:
- LLM provider: `llm.provider: anthropic` or `ollama`
- Risk limits: `risk.*`
- News sources: `news.newsapi/gdelt/telegram` (each independently toggleable)
- Market filters: `market_filters.*`

Secrets go in `.env` only — never in the YAML.

---

## Critical conventions

- **Enum values are stored in SQLite as-is.** `BUY_YES`, `BUY_NO`, `WAIT`, `IMMEDIATE`, `HOURS`, `DAYS`, `UNKNOWN` are the DB-persisted strings. Do not rename them without a migration.
- **`data/overrides.json`** bridges dashboard ↔ bot at runtime. Dashboard writes it; orchestrator reads it each cycle.
- **`pause_on_drawdown: false`** in settings means the drawdown alert is monitoring-only — bot never auto-pauses unless set to `true`.
- **Telegram session tokens** (`data/*.session`) are sensitive — grant full account access. Always excluded from git.

---

## Risk management defaults (€150 bankroll)

| Rule | Value |
|---|---|
| Max position size | 15% of balance |
| Max open positions | 3 |
| Stop-loss | −20% of entry |
| Take-profit | +30% |
| Max drawdown | 30% → alert |
| Time exit tier 1 | 24h → tighten TP to +15% |
| Time exit tier 2 | 48h → close if any profit |
| Time exit tier 3 | 72h → unconditional close |

---

## Testing

```bash
pytest                    # full suite
pytest tests/test_risk_manager.py   # single module
```

Tests use mocks for external APIs. No live API calls in the test suite.

---

## What is excluded from git

`.env`, `data/` (databases, session tokens), `logs/`, `reports/`, `venv/`, `CONTEXT.md`, `.claude/`, `.mulch/`, `node_modules/`. See `.gitignore` for full list.

GitHub repo is public: `https://github.com/icabrera0/Polymarket-paper-trader`

---

## Mulch — structured expertise

<!-- mulch:start -->
## Project Expertise (Mulch)
<!-- mulch-onboard:v0.8.0 -->

This project uses [Mulch](https://github.com/jayminwest/mulch) v0.8.0 for structured expertise management.

**At the start of every session**, run:
```bash
ml prime
```

Injects project-specific conventions, patterns, decisions, failures, references, and guides into
your context. Run `ml prime --files src/foo.ts` before editing a file to load only records
relevant to that path (per-file framing, classification age, and confirmation scores included).

For monolith projects where dumping every record wastes context, set
`prime.default_mode: manifest` in `.mulch/mulch.config.yaml` (or pass `--manifest`) to emit a
quick reference + domain index. Agents then scope-load with `ml prime <domain>` or
`ml prime --files <path>`.

**Before completing your task**, record insights worth preserving — conventions discovered,
patterns applied, failures encountered, or decisions made:
```bash
ml record <domain> --type <convention|pattern|failure|decision|reference|guide> --description "..."
```

Evidence auto-populates from git (current commit + changed files). Link explicitly with
`--evidence-seeds <id>` / `--evidence-gh <id>` / `--evidence-linear <id>` / `--evidence-bead <id>`,
`--evidence-commit <sha>`, or `--relates-to <mx-id>`. Upserts of named records merge outcomes
instead of replacing them; validation failures print a copy-paste retry hint with missing fields
pre-filled.

Run `ml status` for domain health, `ml doctor` to check record integrity (add `--fix` to strip
broken file anchors), `ml --help` for the full command list. Write commands use file locking and
atomic writes, so multiple agents can record concurrently. Expertise survives `git worktree`
cleanup — `.mulch/` resolves to the main repo.

`ml prune` soft-archives stale records to `.mulch/archive/` instead of deleting them; pass
`--hard` for true deletion. Restore an archived record with `ml restore <id>`. Do not read
`.mulch/archive/` directly — those records are stale by definition. If you need historical
context, run `ml search --archived <query>`.

### Before You Finish

1. Discover what to record (shows changed files and suggests domains):
   ```bash
   ml learn
   ```
2. Store insights from this work session:
   ```bash
   ml record <domain> --type <convention|pattern|failure|decision|reference|guide> --description "..."
   ```
3. Validate and commit:
   ```bash
   ml sync
   ```
<!-- mulch:end -->
