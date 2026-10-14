# Repository audit — 2026-10-14

Scope: the whole repository as of commit `3e0420a`, before `paperbot/` was added.
Method: read the source, installed dependencies from PyPI (wheels only, no
source-package build scripts executed), ran the existing test suite offline,
and compared market-data and fee handling against the current Polymarket docs.

## Summary

The existing bot (`src/`, `main.py`, `dashboard.py`) is a news + LLM sentiment
paper trader. It contains **no wallet, private-key or order-submission code**
(good), but its simulated P&L is not realistic enough for this experiment.
So a new, separate package `paperbot/` was built. The legacy code was left
unchanged apart from removing one unused, risky dependency.

## Findings

| # | Severity | Finding | Evidence | Action |
|---|---|---|---|---|
| 1 | High | Trading fees assumed to be 0%. Polymarket now charges taker fees: `fee = shares × feeRate × p × (1−p)`, with the rate depending on category (crypto 0.07 … geopolitics 0). | `src/paper_trader.py` docstring, `polymarket.trading_fee_pct: 0.0` | `paperbot/fees.py` implements the official formula. Unknown category → highest rate. |
| 2 | High | Positions valued at **midpoint** (`/midpoint`) and entries at Gamma `outcomePrices`. No order-book depth, flat 0.5% slippage. This overstates P&L. | `src/orchestrator.py:587,799`, `src/clob_client.py` | `paperbot` walks real `/book` levels for every entry, exit and valuation. |
| 3 | High | Backtester assumes **every entry at 0.50**, so its results are not meaningful. | `src/backtester.py:441` | Not used by paperbot. Documented. |
| 4 | Medium | LLM spend cap is **daily and in-memory**, so it resets on every restart. Not a hard cap. | `src/llm_client.py:283` | `paperbot/budget.py`: monthly cap persisted in SQLite, worst-case pre-authorisation. |
| 5 | Medium | `requirements.txt` pins `py-clob-client` (the real-money order-signing SDK), which is **never imported**. Its pinned 0.20.0 exists only as a source package. | `grep -r py_clob_client` → only requirements | Removed from `requirements.txt`. |
| 6 | Low | `telethon==1.36.0` and `feedparser`'s dependency `sgmllib3k` exist only as source packages, so installing them runs setup scripts. Installing wheels-only fails. | pip `--only-binary` run | Documented. paperbot does not need them. |
| 7 | Low | Slippage guard compares against an old DB snapshot, not a fresh book. | `src/paper_trader.py:_get_current_token_price` | paperbot re-fetches the book after a simulated latency. |
| 8 | Info | Docs and config disagree: CLAUDE.md says 15% max position / 3 positions; `settings.yaml` has 5% / 15. | `CLAUDE.md`, `config/settings.yaml` | Documented. |
| 9 | Info | Telegram integration logs in as a **user account**; `data/*.session` grants full account access. It is git-ignored. | `src/telegram_client.py` | Not used by paperbot. Keep those files private. |
| 10 | Info | External network calls in legacy code: Gamma, CLOB `/midpoint`, NewsAPI, GDELT, Reddit, RSS feeds, Telegram (MTProto), Discord webhooks, Anthropic, local Ollama. | source | paperbot calls only Gamma + CLOB read endpoints, plus Anthropic if research is enabled. |

## Tests (legacy)

`pytest`: **283 passed, 9 failed**. All 9 failures are `ModuleNotFoundError: feedparser`
(the source-only dependency was not installed). The tests use mocks, with no live API calls.

## Comparison with official Polymarket docs (checked 2026-10-14)

Done via web search, because docs.polymarket.com was not reachable from the
build container (blocked by its network policy).

- Fees: taker-only, `C × feeRate × p × (1−p)`. Category rates from the maker-rebates
  table: crypto 0.07, sports 0.05, finance 0.04, politics 0.04, economics 0.05, culture 0.05,
  tech 0.04, other 0.05, geopolitics 0. Third-party sources disagree on sports (0.03 vs 0.05);
  paperbot uses the official table's 0.05, the more conservative value.
- Order book: public `GET https://clob.polymarket.com/book?token_id=…` returns `market`,
  `asset_id`, `bids`, `asks`, `tick_size`, `min_order_size`, `neg_risk`, `hash`. Batch
  `POST /books` takes up to 500 token ids. No authentication needed.
- CLOB V2 (April 2026) changed order signing and collateral. That does not affect read-only market data.
- **Not verified live:** exact rate limits, and whether `/book` includes a `timestamp` field.
  paperbot handles both cases (it backs off on 429, and falls back to fetch time when no server timestamp is present).

## paperbot security properties

- `PAPER_MODE=true` is required at startup and is re-checked before every simulated order.
- The process refuses to run if any environment variable looks like a wallet key or trading
  credential (names like `PRIVATE_KEY`, `POLY_*`, `MNEMONIC`, or any 64-hex-character value).
- The HTTP client can only call an allowlist of read-only endpoints. Any other path raises `ForbiddenEndpoint`.
- A test fails the build if the package ever references order-placement or signing APIs.
- All API text shown in the dashboard is HTML-escaped. The SQLite DB uses parameterised queries only.
- Dependencies are pinned with hashes and installed wheels-only in CI.
