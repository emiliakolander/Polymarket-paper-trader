# paperbot — Polymarket research & paper-trading system

**Paper trading only.** It starts with $1,000 of virtual money. There are no wallets,
private keys or order routes. It reads only public market data.

## What it does every cycle

```
safety checks → run lock → crash repair → settle resolved markets
 → value positions at real bids → stop-losses → scan markets + order books
 → YES/NO arbitrage scan → (optional, paid) Claude research → risk engine
 → realistic paper execution → equity snapshot → report + dashboard
```

| Component | File | Notes |
|---|---|---|
| Read-only Polymarket client | `paperbot/polymarket.py` | Endpoint allowlist, throttle, retries, 429 `Retry-After` |
| Market scanner | `paperbot/scanner.py` | Gamma `/markets` → binary YES/NO only → CLOB `/books` |
| Order book + depth walking | `paperbot/orderbook.py` | Validates books (crossed or garbage levels rejected) |
| Fees | `paperbot/fees.py` | Official taker formula, category rates, conservative default |
| Arbitrage scanner | `paperbot/arbitrage.py` | Executable asks only, depth, participation, fees, leg-risk unwind |
| Research agent | `paperbot/research.py` | Claude + web search, cited evidence, never shown the price |
| AI budget | `paperbot/budget.py` | Hard monthly cap ($10), persisted, worst-case pre-authorisation |
| Risk engine | `paperbot/risk.py` | Pure, deterministic, lists every violated rule |
| Paper execution | `paperbot/execution.py` | Latency, fresh book, stale/partial/failed fills, min size, fees |
| Portfolio | `paperbot/portfolio.py` | Liquidation value at bids, settlement on resolution |
| Database | `paperbot/db.py` | SQLite WAL, ledger-based cash, atomic fills, run lock |
| Report / dashboard | `paperbot/report.py`, `paperbot/dashboard.py` | Markdown + JSON, static HTML |
| Orchestration / CLI | `paperbot/engine.py`, `paperbot/__main__.py` | One idempotent cycle, or a loop |

### How profit is (and is not) computed

- Entries walk the **ask** side of the live order book, and exits/valuations walk the
  **bid** side. Taker fees are charged per level. Midpoints are never used.
- Only a fraction (`depth_participation`, default 50%) of displayed size is assumed fillable.
- Orders wait `latency_s`, then re-fetch the book. They fail if it is stale (`max_quote_age_s`),
  if price moved past the limit, if liquidity is below the minimum order size, or by a
  simulated rejection (2%).
- Equity = cash + liquidation value of positions. This is conservative: an arbitrage pair is
  shown below its $1 payout until the market resolves.
- An arbitrage is reported as a *theoretical* edge with explicit risk notes. It is never "risk-free".

### Risk limits (config/paperbot.yaml → `risk`)

| Rule | Default |
|---|---|
| Max cost of a new position | 5% of equity ($50 at start) |
| Max total exposure | 50% of equity |
| Max per market | 8% of equity |
| Max open markets | 10 |
| Min cash reserve | 20% of equity |
| Max spread (research trades) | $0.05 |
| Max quote age | 30 s |
| Daily loss limit | 3% → no new entries for the rest of the UTC day |
| Max drawdown | 15% → halted until `python -m paperbot resume` |
| Max trades per day | 10 |
| Stop-loss (research positions) | −35% of cost on liquidation value |

## Running locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes --only-binary=:all: -r requirements-paperbot.txt
export PAPER_MODE=true
python -m paperbot run --report-dir out/report --site-dir out/site   # one cycle
python -m paperbot status
open out/site/index.html
python -m pytest -c pytest-paperbot.ini                               # tests, offline
```

Research (paid) is off by default. To enable it for one process, set `ANTHROPIC_API_KEY`
and pass `--enable-research`.

## Scheduled cloud job (GitHub Actions)

`.github/workflows/paperbot-scheduled.yml` runs hourly. It does **nothing** until the repository
variable `PAPERBOT_ENABLED` is `true`. State is stored on the `paperbot-data` branch as a single
commit that is rewritten each run. Each run's dashboard and report are uploaded as the
artifact `paperbot-dashboard`.

| Setting (repo → Settings → Secrets and variables → Actions) | Purpose |
|---|---|
| Variable `PAPERBOT_ENABLED` = `true` | turns on the hourly free job |
| Variable `PAPERBOT_RESEARCH` = `true` | turns on the paid Claude research |
| Secret `ANTHROPIC_API_KEY` | needed only for research |

Always-on later: `Dockerfile.paperbot` runs `python -m paperbot loop` on any small VM or
container host. Mount `/app/data` as a volume.

## Cost estimate

| Item | Estimate |
|---|---|
| Polymarket data | $0 (public endpoints) |
| GitHub Actions | $0. Hourly × ~1–2 min ≈ 720–1,440 min/month, within the 2,000 free minutes of a private repo (unlimited for public repos) |
| Claude research, Opus 5.5 ($4/$20 per M tokens, $0.01/search) | worst case $0.42/call, typical ~$0.15–0.25. At ≤2 calls/day the **$10 cap stops it** around 40–60 calls/month |
| Alternative: Sonnet 5.5 ($2/$10) | worst case ~$0.23/call, typical ~$0.08–0.12 |
| Alternative: Haiku 5.5 ($0.10/$0.50) | worst case ~$0.04/call (lower forecasting quality) |

The model is set in `research.model`. Choosing between them is an owner decision.

### Monthly AI cap mechanics

Before each Claude call the bot computes a worst-case cost: max output tokens, 25k input
tokens per web search, plus search fees. It refuses the call unless `spent_this_month + worst_case ≤ cap`.
Spend is recorded from actual usage after every call (including failed or invalid answers)
in the `api_costs` table, so the cap survives restarts. When the cap is reached, research
stops until the next UTC month. Everything else in the bot is free and keeps running.
As a provider-side backstop, also set a $10 monthly spend limit in the Anthropic Console.

Server-side refusal fallbacks are deliberately **not** enabled. A fallback could switch to a
model with different pricing in the middle of a call, which would break the cap arithmetic.
A refused request is recorded and skipped instead.

## Limitations / unverified

- Live API behaviour was **not** exercised from the build container (Polymarket hosts were
  blocked by its network policy). All tests use recorded-shape fixtures. The first real
  run is the live verification.
- Hourly scheduling will rarely catch real arbitrage. Those gaps are usually closed within
  seconds by faster bots. Expect mostly "near misses" and rejections. That is a realistic result, not a bug.
- Fee categories are inferred from Gamma category/tag labels. Unknown categories get the highest rate.
- Maker (limit-order resting) strategies are not simulated. Every order is a taker.
- Multi-outcome (neg-risk event) arbitrage across markets is not implemented.

## Database tables

`market_snapshots`, `book_snapshots` (pruned after 14 days), `signals` (approved/rejected with
reasons), `research` (evidence, raw answer, cost), `orders` (every attempt incl. failures),
`fills`, `positions`, `ledger` (all cash movements), `api_costs`, `equity_snapshots`, `runs`,
`events` (errors, halts, stop-losses).
