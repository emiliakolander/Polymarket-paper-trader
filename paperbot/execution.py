"""Conservative, VERIFIED paper execution.

A simulated order is never assumed filled from the quote that triggered it.

 1. Order validity (as the real CLOB would require, conservatively):
    size rounded DOWN to `share_decimals`, limit price snapped to the market's
    tick size, size >= the market's minimum order size, and notional >=
    `min_marketable_notional_usd`. Too small -> FAILED_MIN_SIZE / FAILED_MIN_NOTIONAL.
    Orders are never enlarged to meet a minimum.
 2. The order is recorded as PENDING together with the liquidity seen at decision time.
 3. Latency: base + seeded random jitter. The order "arrives" later.
 4. Execution book: a FRESH /book snapshot at arrival. Fetch failure -> FAILED_API,
    invalid -> FAILED_BAD_BOOK, too old -> FAILED_STALE, simulated venue rejection
    -> FAILED_REJECTED. Walk real levels (IOC taker, limit price, participation
    factor) -> simulated fill; nothing within limit -> FAILED_NO_LIQUIDITY.
 5. Verification: `verify_samples` further snapshots, `verify_delay_s` apart. Only
    liquidity at or better than our limit that is still present in EVERY later
    sample counts as CONFIRMED. The confirmed quantity is priced at the WORST of
    the execution and verification books. Shares that cannot be confirmed (the
    liquidity vanished, or a verification snapshot failed) are UNCERTAIN.
 6. Accounting (db.finalize_order, atomic): CONFIRMED shares go to the MAIN book
    (the $1,000 virtual portfolio). UNCERTAIN shares of a BUY go to the SHADOW
    book (tracked, reported separately, never counted in performance). An
    uncertain SELL leaves the MAIN position open.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from paperbot.config import ExecutionConfig, RiskConfig
from paperbot.db import Database, iso, new_id
from paperbot.orderbook import (
    BookError, OrderBook, WalkResult, ceil_to_tick, depth_at_limit, floor_to_tick, parse_book,
    round_down, walk,
)
from paperbot.polymarket import ApiError, PolymarketClient
from paperbot.safety import assert_paper_mode

FILLED, PARTIAL = "FILLED", "PARTIAL"
FAILED_API, FAILED_BAD_BOOK, FAILED_STALE = "FAILED_API", "FAILED_BAD_BOOK", "FAILED_STALE"
FAILED_REJECTED, FAILED_NO_LIQUIDITY, FAILED_MIN_SIZE = (
    "FAILED_REJECTED", "FAILED_NO_LIQUIDITY", "FAILED_MIN_SIZE")
FAILED_MIN_NOTIONAL = "FAILED_MIN_NOTIONAL"
CONFIRMED, PARTIALLY_CONFIRMED, UNCERTAIN, NOT_APPLICABLE = (
    "CONFIRMED", "PARTIALLY_CONFIRMED", "UNCERTAIN", "NOT_APPLICABLE")


@dataclass
class FillResult:
    status: str
    shares: float = 0.0                 # CONFIRMED shares (MAIN book)
    notional: float = 0.0
    fee: float = 0.0
    avg_price: Optional[float] = None
    latency_ms: float = 0.0
    book_hash: str = ""
    reason: str = ""
    fills: list[tuple[float, float]] = field(default_factory=list)
    verification: str = NOT_APPLICABLE
    uncertain_shares: float = 0.0       # simulated but unverified (SHADOW book for BUYs)
    uncertain_notional: float = 0.0
    uncertain_fee: float = 0.0
    uncertain_fills: list[tuple[float, float]] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    timing: dict = field(default_factory=dict)
    order_id: str = ""

    @property
    def ok(self) -> bool:
        """True only if some shares were CONFIRMED."""
        return self.shares > 0

    @property
    def simulated_shares(self) -> float:
        return self.shares + self.uncertain_shares


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def observe(book: OrderBook, label: str, limit: float, side: str, participation: float,
            now: datetime) -> dict:
    """Liquidity snapshot relevant to one order, for the audit trail."""
    levels = book.asks if side == "BUY" else book.bids
    s = "buy" if side == "BUY" else "sell"
    raw = depth_at_limit(levels, limit, s)
    notional = sum(l.price * l.size for l in levels
                   if (l.price <= limit + 1e-12 if s == "buy" else l.price >= limit - 1e-12))
    return {
        "label": label, "fetched_at": iso(book.fetched_at),
        "server_time": iso(book.server_time) if book.server_time else None,
        "age_s": round(book.age_seconds(now), 3), "best_bid": book.best_bid,
        "best_ask": book.best_ask, "spread": book.spread,
        "shares_at_limit": round(raw, 4), "usable_shares": round(raw * participation, 4),
        "notional_at_limit": round(notional, 4), "min_order_size": book.min_order_size,
        "tick_size": book.tick_size, "hash": book.book_hash,
    }


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

    # ---------------------------------------------------------------- helpers
    def _rng(self, order_id: str, salt: int) -> random.Random:
        return random.Random(int(order_id[:12], 16) * 31 + salt)

    def _latency(self, order_id: str) -> float:
        jitter = self._rng(order_id, 1).uniform(0, self.cfg.latency_jitter_s) \
            if self.cfg.latency_jitter_s > 0 else 0.0
        return max(0.0, self.cfg.latency_s + jitter)

    def _rejected(self, order_id: str) -> bool:
        if self.cfg.random_reject_rate <= 0:
            return False
        return self._rng(order_id, 2).random() < self.cfg.random_reject_rate

    def _fetch(self, token_id: str) -> OrderBook:
        raw = self.client.get_book(token_id)
        return parse_book(raw, token_id, fetched_at=self.clock())

    def normalise(self, side: str, shares: float, limit_price: float,
                  tick: Optional[float]) -> tuple[float, float, float]:
        """Round size down and snap the limit to a valid tick. Returns (shares, limit, tick)."""
        t = tick or self.cfg.default_tick_size
        shares = round_down(shares, self.cfg.share_decimals)
        if side == "BUY":
            limit = min(floor_to_tick(limit_price, t), 1 - t)
        else:
            limit = max(ceil_to_tick(limit_price, t), t)
        return shares, round(limit, 10), t

    # ------------------------------------------------------------------ place
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
        decision_book: Optional[OrderBook] = None,
        min_order_size: float = 0.0,
        tick_size: Optional[float] = None,
        strategy: Optional[str] = None,
        decision_at: Optional[datetime] = None,
    ) -> FillResult:
        assert_paper_mode()  # defence in depth: re-checked on every order
        if side not in ("BUY", "SELL"):
            raise ValueError(f"bad side {side}")
        order_id = new_id()
        started = self.clock()
        tick = (decision_book.tick_size if decision_book and decision_book.tick_size else tick_size)
        shares, limit, tick = self.normalise(side, shares, limit_price, tick)
        obs = []
        if decision_book is not None:
            obs.append(observe(decision_book, "decision", limit, side,
                               self.cfg.depth_participation, started))
        self.db.record_pending_order(
            order_id=order_id, signal_id=signal_id, run_id=run_id, now=started,
            market_id=market_id, token_id=token_id, outcome=outcome, side=side,
            requested_shares=shares, limit_price=limit, signal_price=signal_price,
            strategy=strategy or (position_info or {}).get("strategy"),
            decision_at=decision_at or started, liquidity=obs,
        )
        min_size = max(min_order_size, decision_book.min_order_size if decision_book else 0.0)
        result = self._pretrade_check(side, shares, limit, min_size, decision_book)
        if result is None:
            result = self._simulate(order_id, token_id, side, shares, limit, fee_rate,
                                    started, min_size)
        result.order_id = order_id
        result.observations = obs + result.observations
        result.timing.setdefault("decision_at", iso(decision_at or started))
        result.timing.setdefault("order_created_at", iso(started))
        if reason and not result.reason:
            result.reason = reason
        elif reason:
            result.reason = f"{reason}: {result.reason}"
        self.db.finalize_order(order_id, self.clock(), result, position_info)
        return result

    def _pretrade_check(self, side, shares, limit, min_size, decision_book) -> Optional[FillResult]:
        if shares <= 0 or shares + 1e-9 < min_size:
            return FillResult(FAILED_MIN_SIZE,
                              reason=f"order {shares:g} shares < market minimum {min_size:g}")
        if side == "BUY":
            est = shares * limit
        else:
            bid = decision_book.best_bid if decision_book and decision_book.best_bid else None
            est = shares * bid if bid is not None else None
        if est is not None and est + 1e-9 < self.cfg.min_marketable_notional_usd:
            return FillResult(FAILED_MIN_NOTIONAL,
                              reason=f"order value ${est:.2f} < minimum "
                                     f"${self.cfg.min_marketable_notional_usd:.2f}")
        return None

    def _walk(self, book: OrderBook, side: str, shares: float, limit: float,
              fee_rate: float) -> WalkResult:
        levels = book.asks if side == "BUY" else book.bids
        return walk(levels, max_shares=shares, limit_price=limit,
                    side="buy" if side == "BUY" else "sell",
                    participation=self.cfg.depth_participation, fee_rate=fee_rate)

    def _simulate(self, order_id, token_id, side, shares, limit, fee_rate, started,
                  min_size) -> FillResult:
        latency_s = self._latency(order_id)
        if latency_s > 0:
            self.sleep(latency_s)
        arrival = started + timedelta(seconds=latency_s)
        timing = {"simulated_latency_s": round(latency_s, 3), "arrival_at": iso(arrival)}
        obs: list[dict] = []

        def fail(status, why, book=None):
            return FillResult(status, reason=why, latency_ms=latency_s * 1000, observations=obs,
                              timing=timing, book_hash=book.book_hash if book else "")

        try:
            book1 = self._fetch(token_id)
        except ApiError as exc:
            return fail(FAILED_API, f"execution book fetch failed: {exc}")
        except BookError as exc:
            return fail(FAILED_BAD_BOOK, f"invalid execution book: {exc}")
        now = self.clock()
        timing["execution_book_at"] = iso(book1.fetched_at)
        obs.append(observe(book1, "execution", limit, side, self.cfg.depth_participation, now))
        age = book1.age_seconds(now)
        if age > self.max_quote_age_s:
            return fail(FAILED_STALE, f"execution book {age:.0f}s old", book1)
        if self._rejected(order_id):
            return fail(FAILED_REJECTED, "simulated order rejection", book1)

        min_size = max(min_size, book1.min_order_size)
        w = self._walk(book1, side, shares, limit, fee_rate)
        sim_shares = round_down(w.shares, self.cfg.share_decimals)
        if sim_shares <= 0:
            return fail(FAILED_NO_LIQUIDITY, "no liquidity within limit price at arrival", book1)
        w = self._walk(book1, side, sim_shares, limit, fee_rate)
        if sim_shares + 1e-9 < min_size:
            return fail(FAILED_MIN_SIZE,
                        f"fillable {sim_shares:g} < market minimum {min_size:g}", book1)
        if w.notional + 1e-9 < self.cfg.min_marketable_notional_usd:
            return fail(FAILED_MIN_NOTIONAL, f"fill value ${w.notional:.2f} below minimum", book1)

        # ---- verification against subsequent market data
        verify_books: list[OrderBook] = []
        verify_error = ""
        timing["verification_at"] = []
        for k in range(self.cfg.verify_samples):
            if self.cfg.verify_delay_s > 0:
                self.sleep(self.cfg.verify_delay_s)
            try:
                b = self._fetch(token_id)
            except (ApiError, BookError) as exc:
                verify_error = f"verification snapshot {k + 1} unavailable: {exc}"
                break
            verify_books.append(b)
            timing["verification_at"].append(iso(b.fetched_at))
            obs.append(observe(b, f"verify_{k + 1}", limit, side, self.cfg.depth_participation,
                               self.clock()))

        s = "buy" if side == "BUY" else "sell"
        if verify_error:
            confirmed = 0.0
        else:
            supported = min(depth_at_limit(b.asks if side == "BUY" else b.bids, limit, s,
                                           self.cfg.depth_participation) for b in verify_books)
            confirmed = round_down(min(sim_shares, supported), self.cfg.share_decimals)
            if confirmed + 1e-9 < min_size:
                confirmed = 0.0

        result = FillResult(
            status=FILLED if sim_shares >= shares * (1 - 1e-9) else PARTIAL,
            latency_ms=latency_s * 1000, book_hash=book1.book_hash, observations=obs,
            timing=timing,
        )
        notes = []
        if result.status == PARTIAL:
            notes.append(f"partial: {sim_shares:g}/{shares:g} shares available at arrival")
        if confirmed > 0:
            # Conservative price: the worst of execution + verification books.
            walks = [self._walk(b, side, confirmed, limit, fee_rate) for b in [book1] + verify_books]
            walks = [x for x in walks if x.shares >= confirmed - 1e-9]
            if side == "BUY":
                best = max(walks, key=lambda x: x.notional + x.fees)
            else:
                best = min(walks, key=lambda x: x.notional - x.fees)
            if best.notional + 1e-9 < self.cfg.min_marketable_notional_usd:
                confirmed = 0.0
            else:
                result.shares, result.notional, result.fee = confirmed, best.notional, best.fees
                result.avg_price, result.fills = best.vwap, list(best.fills)
        uncertain = round_down(sim_shares - confirmed, self.cfg.share_decimals)
        if uncertain > 0:
            frac = uncertain / sim_shares
            result.uncertain_shares = uncertain
            result.uncertain_notional = w.notional * frac
            result.uncertain_fee = w.fees * frac
            result.uncertain_fills = [(w.vwap, uncertain)]
        if result.shares <= 0:
            result.verification = UNCERTAIN
            notes.append(verify_error or "liquidity did not persist in later snapshots; "
                                         "fill could not be verified")
        elif uncertain > 0:
            result.verification = PARTIALLY_CONFIRMED
            notes.append(f"{uncertain:g} shares unverified (liquidity shrank in later snapshots)")
        else:
            result.verification = CONFIRMED
        if side == "SELL" and uncertain > 0:
            notes.append("unverified sell portion NOT booked; position stays open")
        result.reason = "; ".join(notes)
        return result
