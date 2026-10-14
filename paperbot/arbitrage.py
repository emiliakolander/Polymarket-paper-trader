"""YES/NO complement scanner and paired paper execution.

Idea: in one binary market exactly one of YES / NO pays $1.00 at resolution.
If you can BUY one YES share plus one NO share for less than $1.00 in total,
after taker fees, then holding the pair to resolution returns more than it
cost. That is a *theoretical* edge, NOT a risk-free profit. Real risks:

- Leg risk: the second leg may fill partially or not at all. You are then left
  holding a one-sided bet (we try to unwind it, which costs money).
- The book can move between scan and execution (latency, competitors).
- Resolution risk: disputes, delays, ambiguous or 50/50 resolutions.
- Capital is locked until the market resolves (opportunity cost).
- Fees can change. Displayed depth may vanish (we only assume a fraction).

We only use executable ASK prices from the live book, never midpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from paperbot.config import ArbitrageConfig
from paperbot.execution import FillResult, PaperExecutor
from paperbot.fees import fee_per_share
from paperbot.scanner import Market

RISK_NOTES = [
    "Not risk-free: second leg may not fill (leg risk).",
    "Order book can move before execution; displayed depth may vanish.",
    "Resolution can be disputed, delayed, or resolve 50/50.",
    "Capital is locked until resolution.",
    "Simulated fills; real fills may differ.",
]


@dataclass
class ArbEvaluation:
    market: Market
    ok: bool
    reasons: list[str] = field(default_factory=list)
    shares: float = 0.0
    cost_usd: float = 0.0           # notional + fees for both legs
    fees_usd: float = 0.0
    payout_usd: float = 0.0         # shares * $1 at resolution
    net_usd: float = 0.0
    edge_pct: float = 0.0
    yes_limit: Optional[float] = None
    no_limit: Optional[float] = None
    best_ask_sum: Optional[float] = None
    quote_age_s: float = 0.0

    def details(self) -> dict:
        return {
            "shares": round(self.shares, 4), "cost_usd": round(self.cost_usd, 4),
            "fees_usd": round(self.fees_usd, 4), "payout_at_resolution_usd": round(self.payout_usd, 4),
            "net_if_both_legs_fill_usd": round(self.net_usd, 4), "edge_pct": round(self.edge_pct, 5),
            "yes_limit": self.yes_limit, "no_limit": self.no_limit,
            "best_ask_sum": self.best_ask_sum, "fee_rate": self.market.fee_rate,
            "fee_category": self.market.fee_category, "quote_age_s": round(self.quote_age_s, 2),
            "risk_notes": RISK_NOTES,
        }


def evaluate(market: Market, cfg: ArbitrageConfig, now: datetime,
             budget_usd: Optional[float] = None) -> ArbEvaluation:
    ev = ArbEvaluation(market=market, ok=False)
    yb, nb = market.yes_book, market.no_book
    if yb is None or nb is None:
        ev.reasons.append("NO_BOOK")
        return ev
    ev.quote_age_s = max(yb.age_seconds(now), nb.age_seconds(now))
    if not yb.asks or not nb.asks:
        ev.reasons.append("EMPTY_BOOK")
        return ev
    rate = market.fee_rate
    ev.best_ask_sum = yb.asks[0].price + nb.asks[0].price
    threshold = 1.0 - cfg.slippage_buffer
    first_unit = (ev.best_ask_sum + fee_per_share(yb.asks[0].price, rate)
                  + fee_per_share(nb.asks[0].price, rate))
    if first_unit >= threshold:
        ev.reasons.append("NO_EDGE_AFTER_FEES")
        return ev

    budget = cfg.max_trade_usd if budget_usd is None else min(cfg.max_trade_usd, budget_usd)
    ya = [[l.price, l.size * cfg.depth_participation] for l in yb.asks]
    na = [[l.price, l.size * cfg.depth_participation] for l in nb.asks]
    i = j = 0
    shares = cost = fees = 0.0
    while i < len(ya) and j < len(na):
        py, pn = ya[i][0], na[j][0]
        fy, fn = fee_per_share(py, rate), fee_per_share(pn, rate)
        unit = py + pn + fy + fn
        if unit >= threshold:
            break
        take = min(ya[i][1], na[j][1])
        if budget is not None:
            take = min(take, (budget - cost) / unit)
        if take <= 1e-9:
            break
        shares += take
        cost += take * unit
        fees += take * (fy + fn)
        ev.yes_limit, ev.no_limit = py, pn
        ya[i][1] -= take
        na[j][1] -= take
        if ya[i][1] <= 1e-9:
            i += 1
        if na[j][1] <= 1e-9:
            j += 1

    ev.shares, ev.cost_usd, ev.fees_usd = shares, cost, fees
    ev.payout_usd = shares
    ev.net_usd = shares - cost
    ev.edge_pct = ev.net_usd / cost if cost > 0 else 0.0

    min_size = max(yb.min_order_size, nb.min_order_size, 1e-6)
    if shares < min_size:
        ev.reasons.append("INSUFFICIENT_DEPTH")
    if ev.net_usd < cfg.min_net_profit_usd:
        ev.reasons.append("PROFIT_BELOW_MINIMUM")
    if ev.edge_pct < cfg.min_net_edge_pct:
        ev.reasons.append("EDGE_BELOW_MINIMUM")
    ev.ok = not ev.reasons
    return ev


@dataclass
class ArbExecution:
    legs: list[FillResult]
    hedged_shares: float
    orphan_shares: float
    unwind: Optional[FillResult] = None

    @property
    def status(self) -> str:
        if not self.legs or not self.legs[0].ok:
            return "FAILED"
        if self.unwind is not None:
            return "LEG_RISK_UNWOUND" if self.orphan_shares <= 1e-6 else "LEG_RISK_OPEN"
        return "HEDGED" if self.orphan_shares <= 1e-6 else "LEG_RISK_OPEN"


def execute(executor: PaperExecutor, ev: ArbEvaluation, run_id: str, signal_id: str,
            unwind_unhedged: bool) -> ArbExecution:
    """Buy the thinner leg first, then the other for the filled quantity.

    If the second leg comes up short, the excess first-leg shares are an
    unhedged bet; optionally sell them back into the bids (at a loss).
    """
    m = ev.market
    yes = (m.yes_token, "YES", ev.yes_limit, m.yes_book)
    no = (m.no_token, "NO", ev.no_limit, m.no_book)
    depth = lambda leg: sum(l.size for l in leg[3].asks if l.price <= leg[2] + 1e-12)
    first, second = (yes, no) if depth(yes) <= depth(no) else (no, yes)
    info = {"condition_id": m.condition_id, "question": m.question, "strategy": "arbitrage",
            "end_date": m.end_date.isoformat() if m.end_date else None, "fee_rate": m.fee_rate}

    def buy(leg, shares):
        return executor.place(run_id=run_id, signal_id=signal_id, market_id=m.market_id,
                              token_id=leg[0], outcome=leg[1], side="BUY", shares=shares,
                              limit_price=leg[2], signal_price=leg[3].asks[0].price,
                              fee_rate=m.fee_rate, position_info=info)

    r1 = buy(first, ev.shares)
    if not r1.ok:
        return ArbExecution(legs=[r1], hedged_shares=0.0, orphan_shares=0.0)
    r2 = buy(second, r1.shares)
    hedged = r2.shares if r2.ok else 0.0
    orphan = r1.shares - hedged
    result = ArbExecution(legs=[r1, r2], hedged_shares=hedged, orphan_shares=orphan)
    if orphan > 1e-6 and unwind_unhedged:
        result.unwind = executor.place(
            run_id=run_id, signal_id=signal_id, market_id=m.market_id, token_id=first[0],
            outcome=first[1], side="SELL", shares=orphan, limit_price=0.01,
            signal_price=first[3].bids[0].price if first[3].bids else 0.0,
            fee_rate=m.fee_rate, reason="unwind unhedged arbitrage leg",
        )
        if result.unwind.ok:
            result.orphan_shares = orphan - result.unwind.shares
    return result
