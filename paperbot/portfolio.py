"""Portfolio valuation, settlement and risk state.

Valuation rule: an open position is worth what we could SELL it for right
now, walking the real bid side of the book, minus taker fees. Shares the
bids cannot absorb are valued at 0. This is conservative by design: it
never uses midpoints or last-trade prices. (An arbitrage pair is therefore
shown below its $1 resolution value until it actually settles.)
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from paperbot.db import Database, iso
from paperbot.orderbook import BookError, OrderBook, parse_book, walk
from paperbot.polymarket import ApiError, PolymarketClient
from paperbot.risk import PortfolioState


def liquidation_value(book: OrderBook, shares: float, fee_rate: float) -> float:
    w = walk(book.bids, max_shares=shares, side="sell", fee_rate=fee_rate)
    return max(0.0, w.notional - w.fees)


def _json_list(value) -> Optional[list]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            v = json.loads(value)
            return v if isinstance(v, list) else None
        except ValueError:
            return None
    return None


def resolution_prices(market: dict) -> Optional[dict[str, float]]:
    """Return {token_id: payout_per_share} if the market is definitively resolved."""
    if market.get("closed") is not True:
        return None
    status = market.get("umaResolutionStatus")
    if status is not None and str(status).lower() != "resolved":
        return None
    tokens = _json_list(market.get("clobTokenIds"))
    prices = _json_list(market.get("outcomePrices"))
    if not tokens or not prices or len(tokens) != len(prices):
        return None
    try:
        p = [float(x) for x in prices]
    except (TypeError, ValueError):
        return None
    if any(x < 0 or x > 1 for x in p) or abs(sum(p) - 1.0) > 0.01:
        return None
    # Only accept clean outcomes: a winner at 1/0, or an explicit 50/50.
    if not (all(x in (0.0, 1.0) for x in p) or all(abs(x - 0.5) < 1e-9 for x in p)):
        return None
    return {str(t): x for t, x in zip(tokens, p)}


class Portfolio:
    def __init__(self, db: Database, client: PolymarketClient, starting_capital: float) -> None:
        self.db = db
        self.client = client
        self.starting_capital = starting_capital

    # ---------------------------------------------------------- settlement
    def settle_resolved(self, now: datetime) -> list[dict]:
        settled = []
        markets = {r["market_id"] for r in self.db.open_positions()}
        for market_id in sorted(markets):
            try:
                market = self.client.get_market(market_id)
            except ApiError as exc:
                self.db.log_event(now, "WARN", "SETTLEMENT_CHECK_FAILED", f"{market_id}: {exc}")
                continue
            prices = resolution_prices(market)
            if prices is None:
                continue
            for pos in self.db.q("SELECT * FROM positions WHERE market_id=? AND status='OPEN'",
                                 (market_id,)):
                price = prices.get(pos["token_id"])
                if price is None:
                    self.db.log_event(now, "ERROR", "SETTLEMENT_TOKEN_MISSING",
                                      f"token {pos['token_id']} not in resolved market {market_id}")
                    continue
                payout = self.db.settle_position(pos["token_id"], price, now,
                                                 f"resolved at {price:g}")
                settled.append({"token_id": pos["token_id"], "market_id": market_id,
                                "price": price, "payout": payout,
                                "pnl": payout - pos["cost_basis"]})
        return settled

    # ------------------------------------------------------------- marking
    def mark_all(self, now: datetime, books: Optional[dict[str, OrderBook]] = None) -> dict:
        """Value every open position at its liquidation value. Returns summary."""
        positions = self.db.open_positions()
        books = dict(books or {})
        missing = [p["token_id"] for p in positions if p["token_id"] not in books]
        if missing:
            try:
                raw = self.client.get_books(missing)
                for tok, rb in raw.items():
                    try:
                        books[tok] = parse_book(rb, tok, fetched_at=now)
                    except BookError:
                        pass
            except ApiError as exc:
                self.db.log_event(now, "WARN", "MARK_FETCH_FAILED", str(exc))
        total, unmarked = 0.0, 0
        values = {}
        for p in positions:
            book = books.get(p["token_id"])
            if book is None:
                unmarked += 1
                value = p["mark_value"] if p["mark_value"] is not None else 0.0
            else:
                value = liquidation_value(book, p["shares"], p["fee_rate"] or 0.0)
                self.db.set_mark(p["token_id"], value, now)
            values[p["token_id"]] = value
            total += value
        return {"positions_value": total, "unmarked": unmarked, "values": values}

    # --------------------------------------------------------------- state
    def day_start_equity(self, now: datetime) -> float:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        eq = self.db.equity_at_or_before(midnight)
        if eq is None:
            row = self.db.q1("SELECT equity FROM equity_snapshots WHERE ts>=? ORDER BY ts LIMIT 1",
                             (iso(midnight),))
            eq = float(row["equity"]) if row else self.starting_capital
        return eq

    def equity(self, positions_value: float) -> float:
        return self.db.cash() + positions_value

    def state(self, now: datetime, positions_value: float, market_id: Optional[str] = None,
              paper_mode: bool = True) -> PortfolioState:
        open_pos = self.db.open_positions()
        equity = self.equity(positions_value)
        peak = max(float(self.db.get_meta("peak_equity", str(self.starting_capital))), equity)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return PortfolioState(
            equity=equity,
            cash=self.db.cash(),
            peak_equity=peak,
            day_start_equity=self.day_start_equity(now),
            open_positions=len({p["market_id"] for p in open_pos}),
            open_markets=frozenset(p["market_id"] for p in open_pos),
            exposure_usd=sum(p["cost_basis"] for p in open_pos),
            market_exposure_usd=sum(p["cost_basis"] for p in open_pos
                                    if market_id is not None and p["market_id"] == market_id),
            trades_today=self.db.trades_since(midnight),
            halted=self.db.get_meta("halted", "0") == "1",
            paper_mode=paper_mode,
        )

    def snapshot(self, now: datetime, run_id: Optional[str], mark: dict) -> dict:
        positions_value = mark["positions_value"]
        cash = self.db.cash()
        equity = cash + positions_value
        peak = max(float(self.db.get_meta("peak_equity", str(self.starting_capital))), equity)
        self.db.set_meta("peak_equity", peak)
        open_basis = sum(p["cost_basis"] for p in self.db.open_positions())
        v = dict(
            cash=cash, positions_value=positions_value, equity=equity, peak_equity=peak,
            drawdown=(peak - equity) / peak if peak > 0 else 0.0,
            realized_pnl=self.db.realized_pnl(), unrealized_pnl=positions_value - open_basis,
            fees_total=self.db.total_fees(), ai_cost_total=self.db.ai_spend_total(),
            unmarked_positions=mark["unmarked"],
        )
        self.db.insert_equity_snapshot(now, run_id, **v)
        return v


def hours_until(end: Optional[datetime], now: datetime) -> Optional[float]:
    if end is None:
        return None
    return (end - now).total_seconds() / 3600

