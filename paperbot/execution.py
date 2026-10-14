"""Realistic paper execution.

For every simulated order:
 1. The order is recorded as PENDING before anything else happens.
 2. We wait `latency_s` (the market can move while we "send" the order).
 3. We fetch a FRESH order book. Fetch failure -> FAILED_API.
 4. A book older than `max_quote_age_s` -> FAILED_STALE.
 5. A small, configurable share of orders is rejected outright (FAILED_REJECTED)
    to model exchange/infra rejections. Deterministic per order id.
 6. We walk the real levels as an immediate-or-cancel taker order, capped by
    our limit price and a participation factor (other traders compete for the
    same displayed liquidity). Whatever cannot fill is cancelled -> PARTIAL.
 7. Below the market's minimum order size -> FAILED_MIN_SIZE / FAILED_NO_LIQUIDITY.
 8. Taker fees are charged per level using the official formula.
 9. Result + accounting are committed atomically (db.finalize_order).
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from paperbot.config import ExecutionConfig, RiskConfig
from paperbot.db import Database, new_id
from paperbot.orderbook import BookError, OrderBook, parse_book, walk
from paperbot.polymarket import ApiError, PolymarketClient
from paperbot.safety import assert_paper_mode

FILLED, PARTIAL = "FILLED", "PARTIAL"
FAILED_API, FAILED_BAD_BOOK, FAILED_STALE = "FAILED_API", "FAILED_BAD_BOOK", "FAILED_STALE"
FAILED_REJECTED, FAILED_NO_LIQUIDITY, FAILED_MIN_SIZE = (
    "FAILED_REJECTED", "FAILED_NO_LIQUIDITY", "FAILED_MIN_SIZE")


@dataclass
class FillResult:
    status: str
    shares: float = 0.0
    notional: float = 0.0
    fee: float = 0.0
    avg_price: Optional[float] = None
    latency_ms: float = 0.0
    book_hash: str = ""
    reason: str = ""
    fills: list[tuple[float, float]] = field(default_factory=list)
    order_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (FILLED, PARTIAL) and self.shares > 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaperExecutor:
    def __init__(
        self,
        client: PolymarketClient,
        db: Database,
        exec_cfg: ExecutionConfig,
        risk_cfg: RiskConfig,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.db = db
        self.cfg = exec_cfg
        self.max_quote_age_s = risk_cfg.max_quote_age_s
        self.clock = clock
        self.sleep = sleep

    def _rejected(self, order_id: str) -> bool:
        if self.cfg.random_reject_rate <= 0:
            return False
        return random.Random(int(order_id[:12], 16)).random() < self.cfg.random_reject_rate

    def place(
        self,
        *,
        run_id: str,
        signal_id: Optional[str],
        market_id: str,
        token_id: str,
        outcome: str,
        side: str,
        shares: float,
        limit_price: float,
        signal_price: float,
        fee_rate: float,
        position_info: Optional[dict] = None,
        reason: str = "",
    ) -> FillResult:
        assert_paper_mode()  # defence in depth: re-checked on every order
        if side not in ("BUY", "SELL"):
            raise ValueError(f"bad side {side}")
        order_id = new_id()
        started = self.clock()
        self.db.record_pending_order(
            order_id=order_id, signal_id=signal_id, run_id=run_id, now=started,
            market_id=market_id, token_id=token_id, outcome=outcome, side=side,
            requested_shares=shares, limit_price=limit_price, signal_price=signal_price,
        )
        result = self._simulate(order_id, token_id, side, shares, limit_price, fee_rate, started)
        result.order_id = order_id
        if reason and not result.reason:
            result.reason = reason
        self.db.finalize_order(order_id, self.clock(), result, position_info)
        return result

    def _simulate(self, order_id, token_id, side, shares, limit_price, fee_rate, started) -> FillResult:
        if self.cfg.latency_s > 0:
            self.sleep(self.cfg.latency_s)
        try:
            raw = self.client.get_book(token_id)
            fetched = self.clock()
            book: OrderBook = parse_book(raw, token_id, fetched_at=fetched)
        except ApiError as exc:
            return FillResult(FAILED_API, reason=f"order book fetch failed: {exc}",
                              latency_ms=self._ms(started))
        except BookError as exc:
            return FillResult(FAILED_BAD_BOOK, reason=f"invalid order book: {exc}",
                              latency_ms=self._ms(started))

        latency = self._ms(started)
        age = book.age_seconds(self.clock())
        if age > self.max_quote_age_s:
            return FillResult(FAILED_STALE, reason=f"quote {age:.0f}s old", latency_ms=latency,
                              book_hash=book.book_hash)
        if self._rejected(order_id):
            return FillResult(FAILED_REJECTED, reason="simulated order rejection",
                              latency_ms=latency, book_hash=book.book_hash)

        levels = book.asks if side == "BUY" else book.bids
        w = walk(levels, max_shares=shares, limit_price=limit_price,
                 side="buy" if side == "BUY" else "sell",
                 participation=self.cfg.depth_participation, fee_rate=fee_rate)
        min_size = max(book.min_order_size, 1e-6)
        if w.shares <= 1e-9:
            return FillResult(FAILED_NO_LIQUIDITY, latency_ms=latency, book_hash=book.book_hash,
                              reason="no liquidity within limit price")
        if w.shares + 1e-9 < min_size:
            return FillResult(FAILED_MIN_SIZE, latency_ms=latency, book_hash=book.book_hash,
                              reason=f"fillable {w.shares:.2f} < min order size {min_size}")
        status = FILLED if w.shares >= shares * (1 - 1e-9) else PARTIAL
        return FillResult(
            status=status, shares=w.shares, notional=w.notional, fee=w.fees,
            avg_price=w.vwap, latency_ms=latency, book_hash=book.book_hash,
            fills=list(w.fills),
            reason="" if status == FILLED else f"partial: {w.shares:.2f}/{shares:.2f} shares",
        )

    def _ms(self, started: datetime) -> float:
        return max(0.0, (self.clock() - started).total_seconds() * 1000)
