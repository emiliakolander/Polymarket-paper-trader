"""Order book parsing and depth walking.

All simulated prices come from walking actual order-book levels. Midpoints
are never used to compute profit; `mid` exists only for display.

The CLOB book response contains `bids` and `asks` as lists of
{"price": "0.52", "size": "120.5"}. We do not trust the ordering and sort
ourselves: bids high->low, asks low->high.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from paperbot.fees import taker_fee


class BookError(ValueError):
    pass


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[Level]
    asks: list[Level]
    fetched_at: datetime
    server_time: Optional[datetime] = None
    tick_size: Optional[float] = None
    min_order_size: float = 0.0
    book_hash: str = ""
    dropped_levels: int = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> Optional[float]:
        """Display only. Never use for P&L."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    def age_seconds(self, now: datetime) -> float:
        ref = self.server_time or self.fetched_at
        return max(0.0, (now - ref).total_seconds())

    def depth_usd(self, side: str, within: float = 0.02) -> float:
        """Notional available within `within` $ of the best price."""
        levels = self.asks if side == "ask" else self.bids
        if not levels:
            return 0.0
        best = levels[0].price
        return sum(l.price * l.size for l in levels if abs(l.price - best) <= within + 1e-12)

    def top_levels(self, n: int) -> dict:
        return {
            "bids": [[l.price, l.size] for l in self.bids[:n]],
            "asks": [[l.price, l.size] for l in self.asks[:n]],
        }


def _num(value) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise BookError(f"non-numeric value {value!r}")
    if not math.isfinite(x):
        raise BookError(f"non-finite value {value!r}")
    return x


def _parse_levels(raw_levels) -> tuple[list[Level], int]:
    if not isinstance(raw_levels, list):
        raise BookError("levels must be a list")
    levels: list[Level] = []
    dropped = 0
    for raw in raw_levels:
        try:
            if isinstance(raw, dict):
                price, size = _num(raw.get("price")), _num(raw.get("size"))
            elif isinstance(raw, (list, tuple)) and len(raw) == 2:
                price, size = _num(raw[0]), _num(raw[1])
            else:
                raise BookError("bad level")
        except BookError:
            dropped += 1
            continue
        if not (0.0 < price < 1.0) or size <= 0:
            dropped += 1
            continue
        levels.append(Level(price, size))
    return levels, dropped


def parse_book(raw: dict, token_id: str, fetched_at: Optional[datetime] = None) -> OrderBook:
    """Validate and normalise a raw CLOB book. Raises BookError on bad input.

    An empty side is allowed (it is real information); a crossed book is not.
    """
    if not isinstance(raw, dict):
        raise BookError("book is not an object")
    asset = raw.get("asset_id")
    if asset is not None and str(asset) != str(token_id):
        raise BookError(f"book asset_id {asset} != requested token {token_id}")
    if "bids" not in raw or "asks" not in raw:
        raise BookError("book missing bids/asks")
    bids, d1 = _parse_levels(raw["bids"])
    asks, d2 = _parse_levels(raw["asks"])
    bids.sort(key=lambda l: l.price, reverse=True)
    asks.sort(key=lambda l: l.price)
    if bids and asks and bids[0].price >= asks[0].price:
        raise BookError(f"crossed book: bid {bids[0].price} >= ask {asks[0].price}")

    server_time = None
    ts = raw.get("timestamp")
    if ts not in (None, ""):
        try:
            t = float(ts)
            server_time = datetime.fromtimestamp(t / 1000 if t > 1e11 else t, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            server_time = None

    def _opt(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return OrderBook(
        token_id=str(token_id),
        bids=bids,
        asks=asks,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        server_time=server_time,
        tick_size=_opt(raw.get("tick_size")),
        min_order_size=_opt(raw.get("min_order_size")) or 0.0,
        book_hash=str(raw.get("hash") or ""),
        dropped_levels=d1 + d2,
    )


@dataclass
class WalkResult:
    shares: float = 0.0
    notional: float = 0.0           # sum(price * shares), excluding fees
    fees: float = 0.0
    worst_price: Optional[float] = None
    levels_used: int = 0
    fills: list[tuple[float, float]] = field(default_factory=list)  # (price, shares)

    @property
    def vwap(self) -> Optional[float]:
        return self.notional / self.shares if self.shares > 0 else None


def walk(
    levels: list[Level],
    *,
    max_shares: Optional[float] = None,
    max_notional: Optional[float] = None,
    limit_price: Optional[float] = None,
    side: str = "buy",
    participation: float = 1.0,
    fee_rate: float = 0.0,
) -> WalkResult:
    """Consume levels best-first.

    side="buy" walks asks (limit = max price, budget includes fees);
    side="sell" walks bids (limit = min price). `participation` scales each
    level's displayed size to model competition for the same liquidity.
    """
    if participation <= 0:
        return WalkResult()
    res = WalkResult()
    for lvl in levels:
        if limit_price is not None:
            if side == "buy" and lvl.price > limit_price + 1e-12:
                break
            if side == "sell" and lvl.price < limit_price - 1e-12:
                break
        avail = lvl.size * participation
        take = avail
        if max_shares is not None:
            take = min(take, max_shares - res.shares)
        if max_notional is not None and side == "buy":
            per_share_cost = lvl.price + taker_fee(1.0, lvl.price, fee_rate)
            take = min(take, (max_notional - res.notional - res.fees) / per_share_cost)
        if take <= 1e-9:
            break
        res.shares += take
        res.notional += take * lvl.price
        res.fees += taker_fee(take, lvl.price, fee_rate)
        res.worst_price = lvl.price
        res.levels_used += 1
        res.fills.append((lvl.price, take))
    return res


# ------------------------------------------------------------ order validity
def round_down(x: float, decimals: int) -> float:
    """Round DOWN to `decimals` (order sizes may never be rounded up)."""
    if x <= 0:
        return 0.0
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def floor_to_tick(price: float, tick: float) -> float:
    """Largest valid price <= `price` (BUY limits)."""
    n = math.floor(price / tick + 1e-9)
    return round(n * tick, 10)


def ceil_to_tick(price: float, tick: float) -> float:
    """Smallest valid price >= `price` (SELL limits)."""
    n = math.ceil(price / tick - 1e-9)
    return round(n * tick, 10)


def depth_at_limit(levels: list[Level], limit_price: Optional[float], side: str,
                   participation: float = 1.0) -> float:
    """Shares available up to the limit price (buy: asks <= limit, sell: bids >= limit)."""
    total = 0.0
    for lvl in levels:
        if limit_price is not None:
            if side == "buy" and lvl.price > limit_price + 1e-12:
                break
            if side == "sell" and lvl.price < limit_price - 1e-12:
                break
        total += lvl.size * participation
    return total
