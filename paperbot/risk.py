"""Deterministic risk engine.

`RiskEngine.check()` is a pure function of (proposed trade, portfolio state,
config). No network, no randomness, no LLM. Every rule that fails is listed,
so a rejection is fully explainable. The caller may NOT override a rejection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from paperbot.config import RiskConfig


@dataclass(frozen=True)
class TradeProposal:
    kind: str                      # "arbitrage" | "research"
    market_id: str
    cost_usd: float                # total cash out incl. fees
    edge: float                    # net expected return / cost (arb) or prob edge (research)
    quote_age_s: float
    hours_to_resolution: Optional[float]
    spread: Optional[float] = None  # research only
    max_days_to_resolution: Optional[float] = None


@dataclass(frozen=True)
class PortfolioState:
    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    open_positions: int
    open_markets: frozenset
    exposure_usd: float            # sum of cost basis of open positions
    market_exposure_usd: float     # cost basis already in this market
    trades_today: int
    halted: bool = False
    paper_mode: bool = True


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)


class RiskEngine:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    def max_order_usd(self, state: PortfolioState) -> float:
        """Largest cost a new position may have right now (used for sizing)."""
        c = self.cfg
        if state.equity <= 0:
            return 0.0
        caps = [
            c.max_position_pct * state.equity,
            c.max_total_exposure_pct * state.equity - state.exposure_usd,
            c.max_per_market_pct * state.equity - state.market_exposure_usd,
            state.cash - c.min_cash_reserve_pct * state.equity,
        ]
        return max(0.0, min(caps))

    def portfolio_halt_reasons(self, state: PortfolioState) -> list[str]:
        c = self.cfg
        reasons = []
        if not state.paper_mode:
            reasons.append("NOT_PAPER_MODE")
        if state.halted:
            reasons.append("HALTED")
        if state.peak_equity > 0:
            dd = (state.peak_equity - state.equity) / state.peak_equity
            if dd >= c.max_drawdown_pct:
                reasons.append("MAX_DRAWDOWN")
        if state.day_start_equity > 0:
            day_loss = (state.day_start_equity - state.equity) / state.day_start_equity
            if day_loss >= c.daily_loss_limit_pct:
                reasons.append("DAILY_LOSS_LIMIT")
        if state.trades_today >= c.max_trades_per_day:
            reasons.append("MAX_TRADES_PER_DAY")
        return reasons

    def check(self, p: TradeProposal, state: PortfolioState) -> RiskDecision:
        c = self.cfg
        numbers = [p.cost_usd, p.edge, p.quote_age_s, state.equity, state.cash,
                   state.exposure_usd, state.market_exposure_usd]
        if p.hours_to_resolution is not None:
            numbers.append(p.hours_to_resolution)
        if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in numbers):
            return RiskDecision(False, ["INVALID_INPUT"])

        reasons = self.portfolio_halt_reasons(state)

        new_market = p.market_id not in state.open_markets
        if new_market and state.open_positions >= c.max_open_positions:
            reasons.append("MAX_OPEN_POSITIONS")
        if p.cost_usd < c.min_trade_usd:
            reasons.append("BELOW_MIN_TRADE")
        if state.equity <= 0 or p.cost_usd > c.max_position_pct * state.equity + 1e-9:
            reasons.append("POSITION_TOO_LARGE")
        if state.exposure_usd + p.cost_usd > c.max_total_exposure_pct * state.equity + 1e-9:
            reasons.append("TOTAL_EXPOSURE_LIMIT")
        if state.market_exposure_usd + p.cost_usd > c.max_per_market_pct * state.equity + 1e-9:
            reasons.append("MARKET_EXPOSURE_LIMIT")
        if state.cash - p.cost_usd < c.min_cash_reserve_pct * state.equity - 1e-9:
            reasons.append("CASH_RESERVE")
        if p.quote_age_s > c.max_quote_age_s:
            reasons.append("STALE_QUOTE")
        if p.edge <= 0:
            reasons.append("NO_POSITIVE_EDGE")
        if p.hours_to_resolution is None:
            reasons.append("UNKNOWN_RESOLUTION_DATE")
        else:
            if p.hours_to_resolution < c.min_hours_to_resolution:
                reasons.append("TOO_CLOSE_TO_RESOLUTION")
            if (p.max_days_to_resolution is not None
                    and p.hours_to_resolution > p.max_days_to_resolution * 24):
                reasons.append("TOO_FAR_FROM_RESOLUTION")
        if p.kind == "research":
            if p.spread is None:
                reasons.append("NO_TWO_SIDED_MARKET")
            elif p.spread > c.max_spread + 1e-12:
                reasons.append("SPREAD_TOO_WIDE")

        return RiskDecision(not reasons, reasons)

    def should_stop_loss(self, cost_basis: float, liquidation_value: Optional[float]) -> bool:
        if liquidation_value is None or cost_basis <= 0:
            return False
        return liquidation_value <= cost_basis * (1 - self.cfg.stop_loss_pct)
