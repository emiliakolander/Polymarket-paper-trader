"""Market scanner: Gamma market list -> validated binary markets -> real order books.

Gamma's bestBid/bestAsk are stored as *indicative* only (to pre-filter which
books to fetch). All trading decisions use the CLOB order books.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from paperbot.config import Config
from paperbot.db import Database, parse_iso
from paperbot.fees import fee_rate_for
from paperbot.orderbook import BookError, OrderBook, parse_book
from paperbot.polymarket import PolymarketClient


@dataclass
class Market:
    market_id: str
    condition_id: str
    question: str
    slug: str
    end_date: Optional[datetime]
    yes_token: str
    no_token: str
    liquidity: float
    volume_24h: float
    indicative_bid: Optional[float]
    indicative_ask: Optional[float]
    neg_risk: bool
    fee_category: str
    fee_rate: float
    labels: list[str] = field(default_factory=list)
    description: str = ""
    order_min_size: float = 0.0          # Gamma orderMinSize (fallback if the book lacks it)
    tick_size: Optional[float] = None    # Gamma orderPriceMinTickSize
    yes_book: Optional[OrderBook] = None
    no_book: Optional[OrderBook] = None


def _f(value) -> Optional[float]:
    try:
        x = float(value)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _list(value) -> Optional[list]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            v = json.loads(value)
            return v if isinstance(v, list) else None
        except ValueError:
            return None
    return None


def _labels(raw: dict) -> list[str]:
    labels = []
    if isinstance(raw.get("category"), str):
        labels.append(raw["category"])
    for ev in raw.get("events") or []:
        if not isinstance(ev, dict):
            continue
        if isinstance(ev.get("category"), str):
            labels.append(ev["category"])
        for tag in ev.get("tags") or []:
            if isinstance(tag, dict) and isinstance(tag.get("label"), str):
                labels.append(tag["label"])
    for tag in raw.get("tags") or []:
        if isinstance(tag, dict) and isinstance(tag.get("label"), str):
            labels.append(tag["label"])
    return labels


def parse_market(raw: dict, cfg: Config) -> tuple[Optional[Market], str]:
    """Returns (market, "") or (None, reason) for markets we cannot trade safely."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None, "MALFORMED"
    if raw.get("active") is not True or raw.get("closed") is True:
        return None, "NOT_ACTIVE"
    if raw.get("acceptingOrders") is False or raw.get("enableOrderBook") is False:
        return None, "NOT_ACCEPTING_ORDERS"
    outcomes = _list(raw.get("outcomes"))
    tokens = _list(raw.get("clobTokenIds"))
    # Contract equivalence: exactly one YES and one NO token of the SAME market
    # (same conditionId), so exactly one of them pays $1 at resolution.
    if not outcomes or not tokens or len(outcomes) != 2 or len(tokens) != 2:
        return None, "NOT_BINARY"
    if [str(o).strip().lower() for o in outcomes] != ["yes", "no"]:
        return None, "NOT_YES_NO"
    if not all(isinstance(t, (str, int)) and str(t) for t in tokens) or tokens[0] == tokens[1]:
        return None, "BAD_TOKENS"
    if not raw.get("conditionId"):
        return None, "NO_CONDITION_ID"
    labels = _labels(raw)
    rate, category = fee_rate_for(labels, cfg.fees)
    return Market(
        market_id=str(raw["id"]),
        condition_id=str(raw["conditionId"]),
        question=str(raw.get("question") or "")[:500],
        slug=str(raw.get("slug") or ""),
        end_date=parse_iso(raw.get("endDate")),
        yes_token=str(tokens[0]),
        no_token=str(tokens[1]),
        liquidity=_f(raw.get("liquidityNum", raw.get("liquidity"))) or 0.0,
        volume_24h=_f(raw.get("volume24hr")) or 0.0,
        indicative_bid=_f(raw.get("bestBid")),
        indicative_ask=_f(raw.get("bestAsk")),
        neg_risk=bool(raw.get("negRisk")),
        fee_category=category,
        fee_rate=rate,
        labels=labels,
        description=str(raw.get("description") or "")[:4000],
        order_min_size=_f(raw.get("orderMinSize")) or 0.0,
        tick_size=_f(raw.get("orderPriceMinTickSize")),
    ), ""


@dataclass
class ScanResult:
    markets: list[Market]                 # with books attached
    considered: int = 0
    rejected: dict = field(default_factory=dict)
    book_errors: int = 0


class MarketScanner:
    def __init__(self, cfg: Config, client: PolymarketClient, db: Database) -> None:
        self.cfg = cfg
        self.client = client
        self.db = db

    def fetch_markets(self) -> list[dict]:
        sc = self.cfg.scanner
        out: list[dict] = []
        offset = 0
        while len(out) < sc.max_markets:
            limit = min(sc.page_size, sc.max_markets - len(out))
            page = self.client.list_markets(limit=limit, offset=offset)
            out.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return out

    def scan(self, run_id: str, now: datetime) -> ScanResult:
        sc = self.cfg.scanner
        raws = self.fetch_markets()
        rejected: dict[str, int] = {}
        candidates: list[Market] = []
        for raw in raws:
            m, reason = parse_market(raw, self.cfg)
            if m is None:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            if m.end_date is not None and m.end_date <= now:
                rejected["PAST_END_DATE"] = rejected.get("PAST_END_DATE", 0) + 1
                continue
            if m.liquidity < sc.min_liquidity_usd or m.volume_24h < sc.min_volume_24h_usd:
                rejected["LOW_LIQUIDITY_OR_VOLUME"] = rejected.get("LOW_LIQUIDITY_OR_VOLUME", 0) + 1
                continue
            candidates.append(m)

        candidates.sort(key=lambda m: m.liquidity, reverse=True)
        selected = candidates[: sc.max_books_per_run]
        for m in selected:
            self.db.insert_market_snapshot(run_id, now, m)

        result = ScanResult(markets=[], considered=len(raws), rejected=rejected)
        if not selected:
            return result
        tokens = [t for m in selected for t in (m.yes_token, m.no_token)]
        raw_books = self.client.get_books(tokens)   # ApiError propagates to engine
        fetched = now
        for m in selected:
            try:
                m.yes_book = parse_book(raw_books[m.yes_token], m.yes_token, fetched_at=fetched)
                m.no_book = parse_book(raw_books[m.no_token], m.no_token, fetched_at=fetched)
            except (KeyError, BookError) as exc:
                result.book_errors += 1
                self.db.log_event(now, "WARN", "BAD_BOOK", f"market {m.market_id}: {exc}", run_id)
                continue
            self.db.insert_book_snapshot(run_id, now, m.market_id, "YES", m.yes_book, sc.snapshot_levels)
            self.db.insert_book_snapshot(run_id, now, m.market_id, "NO", m.no_book, sc.snapshot_levels)
            result.markets.append(m)
        return result



def load_market(cfg: Config, client: PolymarketClient, market_id: str,
                now: datetime) -> Optional[Market]:
    """Fetch one market with live books (used to re-review held positions)."""
    try:
        m, _ = parse_market(client.get_market(market_id), cfg)
        if m is None:
            return None
        raw = client.get_books([m.yes_token, m.no_token])
        m.yes_book = parse_book(raw[m.yes_token], m.yes_token, fetched_at=now)
        m.no_book = parse_book(raw[m.no_token], m.no_token, fetched_at=now)
        return m
    except Exception:
        return None
