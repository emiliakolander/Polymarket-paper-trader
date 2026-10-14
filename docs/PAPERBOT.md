# paperbot — Polymarket research & paper-trading system

**Paper trading only.** It starts with $1,000 of virtual money. There are no wallets,
private keys or order routes. It reads only public market data.

## What it does every cycle

```
safety checks → run lock → crash repair → settle resolved markets (MAIN + SHADOW)
 → value positions at real bids → exit conditions → repair unhedged arbitrage legs
 → scan markets + order books
 → YES/NO arbitrage scan → (optional, paid) Claude research → risk engine
 → verified paper execution → equity snapshot → report, daily report, dashboard
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
- Equity = cash + liquidation value of positions. This is conservative: an arbitrage pair is
  shown below its $1 payout until the market resolves.
- An arbitrage is reported as a *theoretical* edge with explicit risk notes. It is never "risk-free".

### Verified execution (a fill is never assumed from the first quote)

Every simulated order goes through `paperbot/execution.py`:

1. **Order validity, as the real CLOB would require (conservative):** the size is rounded
   *down* to 0.01 shares, and the limit price is snapped to the market's tick size (from
   the book, else Gamma `orderPriceMinTickSize`). The order must be at least the market's
   minimum order size (from the book, else Gamma `orderMinSize`) and worth at least $1.
   Orders are **never enlarged** to meet a minimum. A $3 order at 0.70 (4.28 shares, below
   a 5-share minimum) is rejected as `FAILED_MIN_SIZE`, and the matching research signal
   as `BELOW_MARKET_MINIMUM`.
2. **Latency:** 1.0 s plus 0–0.5 s of seeded jitter. The order "arrives" later than the decision.
3. **Execution book:** a fresh `/book` snapshot at arrival. Possible failures:
   `FAILED_API`, `FAILED_BAD_BOOK`, `FAILED_STALE`, `FAILED_REJECTED` (simulated 2%), and
   `FAILED_NO_LIQUIDITY` (nothing at or better than the limit). The fill is computed as an
   immediate-or-cancel taker order walking real levels: `FILLED` or `PARTIAL`.
4. **Verification against subsequent market data:** 2 more snapshots, 2 s apart. Only shares
   whose liquidity at or better than the limit is still present in **every** later snapshot
   are `CONFIRMED`. They are priced at the **worst** of the execution and verification books.
   - Liquidity that shrank → `PARTIALLY_CONFIRMED`.
   - Liquidity that vanished, or a verification snapshot that failed → `UNCERTAIN`.
5. **Two books:**
   - **MAIN** holds only confirmed shares. It is the $1,000 virtual portfolio, and every
     performance number uses it.
   - **SHADOW** holds unverified buy shares. They are tracked, marked and settled, but
     reported separately and **never counted**.
   - An unverified sell leaves the MAIN position open; it is retried on the next exit check.
6. **No phantom re-entry:** because we never really trade, the book keeps showing the
   liquidity we "took". The bot therefore never opens a second position in a market where
   it already holds one (`ALREADY_IN_MARKET`).

Every order row stores: decision time, the intended order (shares, limit, notional), the
liquidity seen at decision / execution / each verification sample (best bid and ask,
spread, shares and value at the limit, minimum size, tick, hash), status, verification
result, confirmed and unverified shares, average price, fees, timing (latency, timestamps),
and a link to the resulting position. Positions store their exit conditions (fixed at
entry), and every valuation is appended to `position_marks` (mark-to-market history).

### Arbitrage execution rules

- Both legs use the same share count after rounding, each at or above the market minimum
  size and the $1 minimum value, with enough depth on both ladders.
- **One-leg failure scenarios** are computed before trading: the loss if leg 1 fills, leg 2
  fails, and leg 1 is sold back at the live bids, for both leg orders. Above
  `max_one_leg_loss_usd` ($3) the opportunity is rejected (`ONE_LEG_RISK_TOO_HIGH`).
- Execution: the thinner leg goes first. Leg 2 is sent only for the *confirmed* leg-1 shares.
  - Leg 1 unverified → leg 2 is not sent (`LEG1_UNVERIFIED`).
  - Leg 2 failed, partial or unverified → the excess leg-1 shares are sold back
    (`LEG_RISK_UNWOUND`), or held if that sale fails (`LEG_RISK_OPEN`).
  - Every run retries selling unhedged excess (`unhedged_repair`) if it meets the minimum.

### Daily report (`report/daily/YYYY-MM-DD.md` + `.json`)

Written for today and yesterday on every run, or on demand with
`python -m paperbot daily-report --out DIR --date YYYY-MM-DD`. Sections:

1. **Hypothetical opportunities**: everything the scanners recorded, with theoretical net
   at quoted prices. Explicitly not achievable P&L.
2. **Conservatively simulated executions**: the full decision record of every order,
   including realized and mark-to-market P&L of the resulting position.
3. **Completed positions**: closed or settled that day. An arbitrage pair counts as one trade.
4. **Unresolved positions**: open at the end of that day, valued from the mark history.
   Unverified shadow positions are listed separately.
5. **Net performance after all costs**: equity change (includes fees and slippage) minus
   AI API costs and infrastructure costs, for the day and since start.

### Risk limits (config/paperbot.yaml → `risk`)

| Rule | Default |
|---|---|
| Intended size | research $3, arbitrage $10 per pair; rejected if below market minimums |
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
- Order-book "updates" are taken as repeated REST snapshots (execution + 2 verification
  samples per order, plus one scan per run). The CLOB WebSocket stream is not used: its
  message format could not be verified from the build container. Liquidity that appears
  and disappears *between* samples is not observed.
- The $1 minimum order value and 0.01-share rounding are conservative assumptions, not
  confirmed limits. The per-market minimum size and tick size come from live data.
- Our simulated orders do not affect the real book. Verification shows that the liquidity
  existed, not that nobody else took it first; the participation factor partly covers this.
- Maker (limit-order resting) strategies are not simulated. Every order is a taker.
- Multi-outcome (neg-risk event) arbitrage across markets is not implemented.

## Database tables

`market_snapshots`, `book_snapshots` (pruned after 14 days), `signals` (approved/rejected with
reasons), `research` (evidence, raw answer, cost), `orders` (every attempt incl. failures, with
liquidity observations, verification and timing), `fills` (MAIN/SHADOW), `positions`
(MAIN/SHADOW, exit conditions), `position_marks` (mark-to-market history), `ledger` (all cash
movements, MAIN/SHADOW), `api_costs`, `equity_snapshots`, `runs`, `events` (errors, halts,
exits). Older v1 databases are migrated in place on startup.
