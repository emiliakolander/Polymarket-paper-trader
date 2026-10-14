"""YES/NO complement scanner and paired, verified paper execution.

Idea: in one binary market exactly one of YES / NO pays $1.00 at resolution.
If you can BUY one YES share plus one NO share for less than $1.00 in total,
after taker fees, holding the pair to resolution returns more than it cost.
That is a *theoretical* edge, NOT a risk-free profit. Real risks:

- Leg risk: the second leg may fill partially, fail, or be unverifiable. You
  are then left with a one-sided bet; selling it back costs money.
- The book can move between scan and execution (latency, competitors).
- Resolution risk: disputes, delays, ambiguous or 50/50 resolutions.
- Capital is locked until the market resolves (opportunity cost).
- Fees can change. Displayed depth may vanish (we only assume a fraction).

An opportunity is only acted on if BOTH legs are realistically executable:
same share count on each leg (rounded down to valid size), each leg at or
above the market's minimum order size and minimum order value, enough depth
on both ladders, and a bounded loss in the one-leg-failure scenario.
We only use executable ASK prices from the live book, never midpoints.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from paperbot.config import ArbitrageConfig, ExecutionConfig
from paperbot.db import MAIN, Database
from paperbot.execution import FillResult, PaperExecutor
from paperbot.fees import fee_per_share
from paperbot.orderbook import round_down, walk
from paperbot.portfolio import exit_conditions, liquidation_value
from paperbot.scanner import Market

RISK_NOTES = [
    "Not risk-free: second leg may fail or be unverifiable (leg risk).",
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
    shares: float = 0.0             # per leg
    cost_usd: float = 0.0           # notional + fees for both legs
    fees_usd: float = 0.0
    payout_usd: float = 0.0         # shares * $1 at resolution
    net_usd: float = 0.0
    edge_pct: float = 0.0
    yes_limit: Optional[float] = None
    no_limit: Optional[float] = None
    yes_cost: float = 0.0
    no_cost: float = 0.0
    best_ask_sum: Optional[float] = None
    quote_age_s: float = 0.0
    one_leg_loss: dict = field(default_factory=dict)   # {"YES_first": $, "NO_first": $}
    min_order_size: float = 0.0
    hypothetical_net_at_top: Optional[float] = None    # top-of-book only, before depth/size rules

    def details(self) -> dict:
        return {
            "shares_per_leg": round(self.shares, 4), "cost_usd": round(self.cost_usd, 4),
            "yes_leg_cost": round(self.yes_cost, 4), "no_leg_cost": round(self.no_cost, 4),
            "fees_usd": round(self.fees_usd, 4), "payout_at_resolution_usd": round(self.payout_usd, 4),
            "net_if_both_legs_fill_usd": round(self.net_usd, 4), "edge_pct": round(self.edge_pct, 5),
            "hypothetical_net_at_top_of_book_usd": self.hypothetical_net_at_top,
            "yes_limit": self.yes_limit, "no_limit": self.no_limit,
            "best_ask_sum": self.best_ask_sum, "fee_rate": self.market.fee_rate,
            "fee_category": self.market.fee_category, "quote_age_s": round(self.quote_age_s, 2),
            "market_min_order_size": self.min_order_size,
            "one_leg_failure_loss_usd": {k: round(v, 4) for k, v in self.one_leg_loss.items()},
            "risk_notes": RISK_NOTES,
        }


def evaluate(market: Market, cfg: ArbitrageConfig, now: datetime,
             budget_usd: Optional[float] = None,
             exec_cfg: Optional[ExecutionConfig] = None) -> ArbEvaluation:
    exec_cfg = exec_cfg or ExecutionConfig()
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
    ya0, na0 = yb.asks[0], nb.asks[0]
    ev.best_ask_sum = ya0.price + na0.price
    first_unit = ev.best_ask_sum + fee_per_share(ya0.price, rate) + fee_per_share(na0.price, rate)
    ev.hypothetical_net_at_top = round((1 - first_unit) * min(ya0.size, na0.size), 4)
    threshold = 1.0 - cfg.slippage_buffer
    if first_unit >= threshold:
        ev.reasons.append("NO_EDGE_AFTER_FEES")
        return ev

    budget = cfg.target_pair_cost_usd if budget_usd is None else min(cfg.target_pair_cost_usd,
                                                                     budget_usd)
    ya = [[l.price, l.size * cfg.depth_participation] for l in yb.asks]
    na = [[l.price, l.size * cfg.depth_participation] for l in nb.asks]
    i = j = 0
    shares = cost = 0.0
    while i < len(ya) and j < len(na):
        py, pn = ya[i][0], na[j][0]
        unit = py + pn + fee_per_share(py, rate) + fee_per_share(pn, rate)
        if unit >= threshold:
            break
        take = min(ya[i][1], na[j][1], max(0.0, (budget - cost) / unit))
        if take <= 1e-9:
            break
        shares += take
        cost += take * unit
        ya[i][1] -= take
        na[j][1] -= take
        if ya[i][1] <= 1e-9:
            i += 1
        if na[j][1] <= 1e-9:
            j += 1

    # Same, valid share count on both legs; exact per-leg cost by walking each ladder.
    q = round_down(shares, exec_cfg.share_decimals)
    wy = walk(yb.asks, max_shares=q, participation=cfg.depth_participation, fee_rate=rate)
    wn = walk(nb.asks, max_shares=q, participation=cfg.depth_participation, fee_rate=rate)
    ev.shares = q
    ev.yes_cost, ev.no_cost = wy.notional + wy.fees, wn.notional + wn.fees
    ev.cost_usd = ev.yes_cost + ev.no_cost
    ev.fees_usd = wy.fees + wn.fees
    ev.yes_limit, ev.no_limit = wy.worst_price, wn.worst_price
    ev.payout_usd = q
    ev.net_usd = q - ev.cost_usd
    ev.edge_pct = ev.net_usd / ev.cost_usd if ev.cost_usd > 0 else 0.0

    ev.min_order_size = max(yb.min_order_size, nb.min_order_size, market.order_min_size, 0.0)
    if q <= 0 or wy.shares < q - 1e-9 or wn.shares < q - 1e-9:
        ev.reasons.append("INSUFFICIENT_DEPTH")
    if q < ev.min_order_size - 1e-9:
        ev.reasons.append("BELOW_MARKET_MINIMUM")
    elif min(wy.notional, wn.notional) < exec_cfg.min_marketable_notional_usd - 1e-9:
        ev.reasons.append("BELOW_MARKET_MINIMUM")

    # One-leg failure scenarios: leg 1 fills, leg 2 fails, leg 1 is sold back at bids.
    if q > 0:
        ev.one_leg_loss = {
            "YES_first": ev.yes_cost - liquidation_value(yb, q, rate),
            "NO_first": ev.no_cost - liquidation_value(nb, q, rate),
        }
        if max(ev.one_leg_loss.values()) > cfg.max_one_leg_loss_usd + 1e-9:
            ev.reasons.append("ONE_LEG_RISK_TOO_HIGH")

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
    note: str = ""

    @property
    def status(self) -> str:
        if not self.legs or not self.legs[0].ok:
            if self.legs and self.legs[0].uncertain_shares > 0:
                return "LEG1_UNVERIFIED"
            return "FAILED"
        if self.unwind is not None:
            return "LEG_RISK_UNWOUND" if self.orphan_shares <= 1e-6 else "LEG_RISK_OPEN"
        return "HEDGED" if self.orphan_shares <= 1e-6 else "LEG_RISK_OPEN"


def execute(executor: PaperExecutor, ev: ArbEvaluation, run_id: str, signal_id: str,
            unwind_unhedged: bool) -> ArbExecution:
    """Buy the thinner leg first, then the other leg for the CONFIRMED quantity only.

    - Leg 1 unverifiable/failed -> leg 2 is not sent.
    - Leg 2 short (failed, partial, or unverifiable) -> the excess leg-1 shares are
      an unhedged bet; optionally sold back into the bids (a realised loss).
    """
    m = ev.market
    yes = (m.yes_token, "YES", ev.yes_limit, m.yes_book)
    no = (m.no_token, "NO", ev.no_limit, m.no_book)
    depth = lambda leg: sum(l.size for l in leg[3].asks if l.price <= leg[2] + 1e-12)
    first, second = (yes, no) if depth(yes) <= depth(no) else (no, yes)
    end = m.end_date.isoformat() if m.end_date else None
    cond = exit_conditions("arbitrage", None, end)
    cond["min_order_size"] = ev.min_order_size
    info = {"condition_id": m.condition_id, "question": m.question, "strategy": "arbitrage",
            "end_date": end, "fee_rate": m.fee_rate, "exit_conditions": cond}
    decision_at = executor.clock()

    def buy(leg, shares):
        return executor.place(run_id=run_id, signal_id=signal_id, market_id=m.market_id,
                              token_id=leg[0], outcome=leg[1], side="BUY", shares=shares,
                              limit_price=leg[2], signal_price=leg[3].asks[0].price,
                              fee_rate=m.fee_rate, position_info=info, decision_book=leg[3],
                              min_order_size=m.order_min_size, tick_size=m.tick_size,
                              strategy="arbitrage", decision_at=decision_at)

    r1 = buy(first, ev.shares)
    if not r1.ok:
        return ArbExecution(legs=[r1], hedged_shares=0.0, orphan_shares=0.0,
                            note="leg 1 not confirmed; leg 2 not sent")
    target = round_down(r1.shares, executor.cfg.share_decimals)
    legs = [r1]
    hedged = 0.0
    note = ""
    if target + 1e-9 < ev.min_order_size:
        note = f"confirmed leg 1 ({target:g}) below market minimum; cannot hedge"
    else:
        r2 = buy(second, target)
        legs.append(r2)
        hedged = r2.shares
        if not r2.ok:
            note = f"leg 2 {r2.status}/{r2.verification}: one-leg failure"
    orphan = r1.shares - hedged
    result = ArbExecution(legs=legs, hedged_shares=hedged, orphan_shares=orphan, note=note)
    if orphan > 1e-6 and unwind_unhedged:
        result.unwind = executor.place(
            run_id=run_id, signal_id=signal_id, market_id=m.market_id, token_id=first[0],
            outcome=first[1], side="SELL", shares=orphan, limit_price=0.0,
            signal_price=first[3].bids[0].price if first[3].bids else 0.0,
            fee_rate=m.fee_rate, reason="unwind unhedged arbitrage leg",
            min_order_size=m.order_min_size, tick_size=m.tick_size, strategy="arbitrage",
        )
        result.orphan_shares = orphan - result.unwind.shares
    return result


def unhedged_excess(db: Database) -> list[dict]:
    """MAIN arbitrage markets where YES and NO share counts differ."""
    by_market: dict[str, dict] = defaultdict(dict)
    for p in db.open_positions(MAIN):
        if p["strategy"] == "arbitrage":
            by_market[p["market_id"]][p["outcome"]] = dict(p)
    out = []
    for market_id, legs in by_market.items():
        yes, no = legs.get("YES"), legs.get("NO")
        ys, ns = (yes or {}).get("shares", 0.0), (no or {}).get("shares", 0.0)
        if abs(ys - ns) <= 1e-6:
            continue
        heavy = yes if ys > ns else no
        out.append({"market_id": market_id, "position": heavy, "excess": abs(ys - ns)})
    return out


def repair_unhedged(executor: PaperExecutor, db: Database, run_id: str) -> list[dict]:
    """Try to sell unhedged arbitrage excess left over from earlier runs (verified)."""
    import json
    done = []
    for item in unhedged_excess(db):
        p = item["position"]
        cond = json.loads(p.get("exit_conditions_json") or "{}")
        min_size = float(cond.get("min_order_size") or 0.0)
        excess = round_down(item["excess"], executor.cfg.share_decimals)
        if excess <= 0 or excess + 1e-9 < min_size:
            done.append({"market_id": item["market_id"], "status": "BELOW_MIN_HELD",
                         "excess": item["excess"]})
            continue
        r = executor.place(run_id=run_id, signal_id=None, market_id=p["market_id"],
                           token_id=p["token_id"], outcome=p["outcome"], side="SELL",
                           shares=excess, limit_price=0.0, signal_price=0.0,
                           fee_rate=p["fee_rate"] or 0.0, reason="repair unhedged arbitrage leg",
                           min_order_size=min_size, strategy="arbitrage")
        done.append({"market_id": item["market_id"], "status": r.status,
                     "verification": r.verification, "sold": r.shares, "excess": item["excess"]})
    return done
