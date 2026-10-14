"""SQLite persistence (stdlib sqlite3, WAL mode).

Design rules
- Cash is never stored as a mutable number. It is the SUM of the `ledger`
  table (deposit, buys, sells, fees, settlements), so it can always be audited.
- Every fill is written atomically: order status + fills + ledger + position
  change happen in ONE transaction. A crash leaves either all or nothing.
- Orders are first inserted as PENDING. If the process dies before the fill
  transaction commits, the next run marks them INTERRUPTED (no money moved).
- A run lock with expiry prevents two scheduled runs from overlapping.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1
EPS = 1e-9

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS run_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, errors_json TEXT NOT NULL DEFAULT '[]',
    api_stats_json TEXT NOT NULL DEFAULT '{}', summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT NOT NULL,
    market_id TEXT NOT NULL, condition_id TEXT, question TEXT, slug TEXT, end_date TEXT,
    fee_category TEXT, fee_rate REAL, liquidity REAL, volume_24h REAL,
    indicative_bid REAL, indicative_ask REAL, neg_risk INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ms_market ON market_snapshots(market_id, ts);

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT NOT NULL,
    market_id TEXT NOT NULL, token_id TEXT NOT NULL, outcome TEXT,
    best_bid REAL, best_ask REAL, spread REAL, bid_depth_usd REAL, ask_depth_usd REAL,
    server_time TEXT, book_hash TEXT, levels_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_bs_token ON book_snapshots(token_id, ts);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY, run_id TEXT, ts TEXT NOT NULL, kind TEXT NOT NULL,
    market_id TEXT NOT NULL, question TEXT, outcome TEXT,
    expected_net_usd REAL, edge REAL, cost_usd REAL,
    status TEXT NOT NULL, reasons_json TEXT NOT NULL DEFAULT '[]', details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, market_id TEXT NOT NULL,
    question TEXT, model TEXT, status TEXT NOT NULL, probability REAL, confidence REAL,
    summary TEXT, evidence_json TEXT, uncertainties_json TEXT, raw_text TEXT,
    cost_usd REAL NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_research_market ON research(market_id, ts);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY, signal_id TEXT, run_id TEXT,
    created_at TEXT NOT NULL, done_at TEXT,
    market_id TEXT NOT NULL, token_id TEXT NOT NULL, outcome TEXT, side TEXT NOT NULL,
    requested_shares REAL NOT NULL, limit_price REAL NOT NULL, signal_price REAL,
    status TEXT NOT NULL, filled_shares REAL NOT NULL DEFAULT 0, avg_price REAL,
    notional REAL NOT NULL DEFAULT 0, fee REAL NOT NULL DEFAULT 0,
    latency_ms REAL, book_hash TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL REFERENCES orders(order_id),
    ts TEXT NOT NULL, price REAL NOT NULL, shares REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY, token_id TEXT NOT NULL, market_id TEXT NOT NULL, condition_id TEXT,
    outcome TEXT, question TEXT, strategy TEXT, end_date TEXT, fee_rate REAL,
    shares REAL NOT NULL DEFAULT 0, cost_basis REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0, fees_paid REAL NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL, closed_at TEXT, status TEXT NOT NULL,
    settle_price REAL, mark_value REAL, mark_ts TEXT, close_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_position
    ON positions(token_id) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
    amount REAL NOT NULL, order_id TEXT, token_id TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS api_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, provider TEXT NOT NULL,
    model TEXT, purpose TEXT, market_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL, request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_costs_ts ON api_costs(ts);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT,
    cash REAL NOT NULL, positions_value REAL NOT NULL, equity REAL NOT NULL,
    peak_equity REAL NOT NULL, drawdown REAL NOT NULL,
    realized_pnl REAL NOT NULL, unrealized_pnl REAL NOT NULL,
    fees_total REAL NOT NULL, ai_cost_total REAL NOT NULL, unmarked_positions INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, run_id TEXT,
    level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple | dict = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    # --------------------------------------------------------------- meta
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def initialize(self, starting_capital: float, now: datetime) -> None:
        """Idempotent: deposits the virtual starting capital only once."""
        with self.tx():
            if self.get_meta("schema_version") is None:
                self.set_meta("schema_version", SCHEMA_VERSION)
            if not self.q1("SELECT 1 FROM ledger WHERE kind='DEPOSIT'"):
                self.conn.execute(
                    "INSERT INTO ledger(ts,kind,amount,note) VALUES(?,?,?,?)",
                    (iso(now), "DEPOSIT", float(starting_capital), "virtual starting capital"),
                )
                self.set_meta("starting_capital", starting_capital)
                self.set_meta("peak_equity", starting_capital)

    # ------------------------------------------------------------ locking
    def acquire_lock(self, run_id: str, now: datetime, ttl_s: float = 1800) -> bool:
        with self.tx():
            row = self.q1("SELECT run_id, expires_at FROM run_lock WHERE id=1")
            if row and (parse_iso(row["expires_at"]) or now) > now:
                return False
            self.conn.execute(
                "INSERT INTO run_lock(id,run_id,acquired_at,expires_at) VALUES(1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id, "
                "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                (run_id, iso(now), iso(now + timedelta(seconds=ttl_s))),
            )
            return True

    def release_lock(self, run_id: str) -> None:
        self.conn.execute("DELETE FROM run_lock WHERE id=1 AND run_id=?", (run_id,))

    def recover_interrupted_orders(self, now: datetime) -> int:
        """PENDING orders from a crashed run never moved money: mark them INTERRUPTED."""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE orders SET status='INTERRUPTED', done_at=?, "
                "reason='process stopped before fill was recorded' WHERE status='PENDING'",
                (iso(now),),
            )
            return cur.rowcount

    # --------------------------------------------------------------- runs
    def start_run(self, run_id: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO runs(run_id, started_at, status) VALUES(?,?,'RUNNING')",
            (run_id, iso(now)),
        )

    def finish_run(self, run_id: str, now: datetime, status: str, errors: list,
                   api_stats: dict, summary: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, errors_json=?, api_stats_json=?, "
            "summary_json=? WHERE run_id=?",
            (iso(now), status, json.dumps(errors), json.dumps(api_stats),
             json.dumps(summary), run_id),
        )

    def mark_stale_runs(self, now: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE runs SET status='CRASHED', finished_at=? WHERE status='RUNNING'", (iso(now),)
        )
        return cur.rowcount

    def log_event(self, now: datetime, level: str, kind: str, message: str,
                  run_id: Optional[str] = None) -> None:
        self.conn.execute(
            "INSERT INTO events(ts,run_id,level,kind,message) VALUES(?,?,?,?,?)",
            (iso(now), run_id, level, kind, message[:2000]),
        )

    # ---------------------------------------------------------- snapshots
    def prune_snapshots(self, before: datetime) -> int:
        """Delete raw market/book snapshots older than `before` (signals, orders,
        fills, ledger and equity history are never pruned)."""
        with self.tx():
            n = self.conn.execute("DELETE FROM book_snapshots WHERE ts<?", (iso(before),)).rowcount
            n += self.conn.execute("DELETE FROM market_snapshots WHERE ts<?", (iso(before),)).rowcount
        if n:
            self.conn.execute("VACUUM")
        return n

    def insert_market_snapshot(self, run_id: str, now: datetime, m: Any) -> None:
        self.conn.execute(
            "INSERT INTO market_snapshots(run_id,ts,market_id,condition_id,question,slug,end_date,"
            "fee_category,fee_rate,liquidity,volume_24h,indicative_bid,indicative_ask,neg_risk) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, iso(now), m.market_id, m.condition_id, m.question, m.slug,
             iso(m.end_date) if m.end_date else None, m.fee_category, m.fee_rate,
             m.liquidity, m.volume_24h, m.indicative_bid, m.indicative_ask, int(m.neg_risk)),
        )

    def insert_book_snapshot(self, run_id: str, now: datetime, market_id: str,
                             outcome: str, book: Any, levels: int) -> None:
        self.conn.execute(
            "INSERT INTO book_snapshots(run_id,ts,market_id,token_id,outcome,best_bid,best_ask,"
            "spread,bid_depth_usd,ask_depth_usd,server_time,book_hash,levels_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, iso(now), market_id, book.token_id, outcome, book.best_bid, book.best_ask,
             book.spread, book.depth_usd("bid"), book.depth_usd("ask"),
             iso(book.server_time) if book.server_time else None, book.book_hash,
             json.dumps(book.top_levels(levels))),
        )

    # ------------------------------------------------------------ signals
    def insert_signal(self, *, run_id: str, now: datetime, kind: str, market_id: str,
                      question: str, outcome: str, expected_net_usd: float, edge: float,
                      cost_usd: float, status: str, reasons: list[str], details: dict) -> str:
        sid = new_id()
        self.conn.execute(
            "INSERT INTO signals(signal_id,run_id,ts,kind,market_id,question,outcome,"
            "expected_net_usd,edge,cost_usd,status,reasons_json,details_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, run_id, iso(now), kind, market_id, question, outcome, expected_net_usd,
             edge, cost_usd, status, json.dumps(reasons), json.dumps(details, default=str)),
        )
        return sid

    def update_signal(self, signal_id: str, status: str, reasons: Optional[list] = None) -> None:
        if reasons is None:
            self.conn.execute("UPDATE signals SET status=? WHERE signal_id=?", (status, signal_id))
        else:
            self.conn.execute("UPDATE signals SET status=?, reasons_json=? WHERE signal_id=?",
                              (status, json.dumps(reasons), signal_id))

    # ------------------------------------------------------------- orders
    def record_pending_order(self, *, order_id: str, signal_id: Optional[str], run_id: str,
                     now: datetime, market_id: str, token_id: str, outcome: str, side: str,
                     requested_shares: float, limit_price: float, signal_price: float) -> None:
        self.conn.execute(
            "INSERT INTO orders(order_id,signal_id,run_id,created_at,market_id,token_id,outcome,"
            "side,requested_shares,limit_price,signal_price,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'PENDING')",
            (order_id, signal_id, run_id, iso(now), market_id, token_id, outcome, side,
             requested_shares, limit_price, signal_price),
        )

    def finalize_order(self, order_id: str, now: datetime, result: Any,
                       position: Optional[dict] = None) -> None:
        """Atomically record the outcome of an order and its accounting effects.

        `result` is an execution.FillResult. `position` carries static info needed
        to open a position on a BUY (market_id, outcome, question, ...).
        """
        order = self.q1("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if order is None:
            raise ValueError(f"unknown order {order_id}")
        if order["status"] != "PENDING":
            raise ValueError(f"order {order_id} already finalised ({order['status']})")
        with self.tx():
            self.conn.execute(
                "UPDATE orders SET status=?, done_at=?, filled_shares=?, avg_price=?, notional=?, "
                "fee=?, latency_ms=?, book_hash=?, reason=? WHERE order_id=?",
                (result.status, iso(now), result.shares, result.avg_price, result.notional,
                 result.fee, result.latency_ms, result.book_hash, result.reason, order_id),
            )
            if result.shares <= EPS:
                return
            for price, shares in result.fills:
                self.conn.execute("INSERT INTO fills(order_id,ts,price,shares) VALUES(?,?,?,?)",
                                  (order_id, iso(now), price, shares))
            token = order["token_id"]
            if order["side"] == "BUY":
                self._apply_buy(now, order_id, token, result, position or {}, order)
            else:
                self._apply_sell(now, order_id, token, result)

    def _ledger(self, now: datetime, kind: str, amount: float, order_id: Optional[str],
                token_id: Optional[str], note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO ledger(ts,kind,amount,order_id,token_id,note) VALUES(?,?,?,?,?,?)",
            (iso(now), kind, amount, order_id, token_id, note),
        )

    def _apply_buy(self, now, order_id, token, result, info, order) -> None:
        self._ledger(now, "BUY", -result.notional, order_id, token)
        if result.fee > 0:
            self._ledger(now, "FEE", -result.fee, order_id, token)
        pos = self.q1("SELECT * FROM positions WHERE token_id=? AND status='OPEN'", (token,))
        if pos is None:
            self.conn.execute(
                "INSERT INTO positions(position_id,token_id,market_id,condition_id,outcome,question,"
                "strategy,end_date,fee_rate,shares,cost_basis,fees_paid,opened_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
                (new_id(), token, order["market_id"], info.get("condition_id"), order["outcome"],
                 info.get("question"), info.get("strategy"), info.get("end_date"),
                 info.get("fee_rate"), result.shares, result.notional + result.fee,
                 result.fee, iso(now)),
            )
        else:
            self.conn.execute(
                "UPDATE positions SET shares=shares+?, cost_basis=cost_basis+?, fees_paid=fees_paid+? "
                "WHERE position_id=?",
                (result.shares, result.notional + result.fee, result.fee, pos["position_id"]),
            )

    def _apply_sell(self, now, order_id, token, result) -> None:
        pos = self.q1("SELECT * FROM positions WHERE token_id=? AND status='OPEN'", (token,))
        if pos is None or pos["shares"] + EPS < result.shares:
            raise ValueError(f"cannot sell {result.shares} of {token}: insufficient position")
        portion = pos["cost_basis"] * (result.shares / pos["shares"])
        realized = result.notional - result.fee - portion
        remaining = pos["shares"] - result.shares
        self._ledger(now, "SELL", result.notional, order_id, token)
        if result.fee > 0:
            self._ledger(now, "FEE", -result.fee, order_id, token)
        closed = remaining <= 1e-6
        self.conn.execute(
            "UPDATE positions SET shares=?, cost_basis=?, realized_pnl=realized_pnl+?, "
            "fees_paid=fees_paid+?, status=?, closed_at=?, close_reason=COALESCE(?, close_reason) "
            "WHERE position_id=?",
            (0.0 if closed else remaining, 0.0 if closed else pos["cost_basis"] - portion,
             realized, result.fee, "CLOSED" if closed else "OPEN",
             iso(now) if closed else None, result.reason if closed else None, pos["position_id"]),
        )

    def settle_position(self, token_id: str, price: float, now: datetime, note: str) -> float:
        """Resolution payout at `price` per share (1.0 win, 0.0 loss). Returns payout."""
        with self.tx():
            pos = self.q1("SELECT * FROM positions WHERE token_id=? AND status='OPEN'", (token_id,))
            if pos is None:
                return 0.0
            payout = pos["shares"] * price
            self._ledger(now, "SETTLEMENT", payout, None, token_id, note)
            self.conn.execute(
                "UPDATE positions SET shares=0, cost_basis=0, realized_pnl=realized_pnl+?, "
                "status='SETTLED', closed_at=?, settle_price=?, close_reason=?, mark_value=0 "
                "WHERE position_id=?",
                (payout - pos["cost_basis"], iso(now), price, note, pos["position_id"]),
            )
            return payout

    def set_mark(self, token_id: str, value: Optional[float], now: datetime) -> None:
        self.conn.execute(
            "UPDATE positions SET mark_value=?, mark_ts=? WHERE token_id=? AND status='OPEN'",
            (value, iso(now), token_id))

    # ----------------------------------------------------------- queries
    def cash(self) -> float:
        return float(self.q1("SELECT COALESCE(SUM(amount),0) AS c FROM ledger")["c"])

    def starting_capital(self) -> float:
        return float(self.get_meta("starting_capital", "0"))

    def open_positions(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM positions WHERE status='OPEN' ORDER BY opened_at")

    def total_fees(self) -> float:
        return -float(self.q1("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE kind='FEE'")["s"])

    def realized_pnl(self) -> float:
        return float(self.q1("SELECT COALESCE(SUM(realized_pnl),0) s FROM positions")["s"])

    def trades_since(self, since: datetime) -> int:
        return int(self.q1(
            "SELECT COUNT(DISTINCT signal_id) c FROM orders WHERE side='BUY' AND filled_shares>0 "
            "AND created_at>=?", (iso(since),))["c"])

    def equity_at_or_before(self, ts: datetime) -> Optional[float]:
        row = self.q1("SELECT equity FROM equity_snapshots WHERE ts<=? ORDER BY ts DESC LIMIT 1",
                      (iso(ts),))
        return float(row["equity"]) if row else None

    def last_research_ts(self, market_id: str) -> Optional[datetime]:
        row = self.q1("SELECT MAX(ts) t FROM research WHERE market_id=?", (market_id,))
        return parse_iso(row["t"]) if row else None

    # ------------------------------------------------------------- costs
    def record_api_cost(self, *, now: datetime, provider: str, model: str, purpose: str,
                        market_id: Optional[str], input_tokens: int, output_tokens: int,
                        cache_read_tokens: int, cache_write_tokens: int, web_searches: int,
                        cost_usd: float, status: str, request_id: Optional[str]) -> None:
        self.conn.execute(
            "INSERT INTO api_costs(ts,provider,model,purpose,market_id,input_tokens,output_tokens,"
            "cache_read_tokens,cache_write_tokens,web_searches,cost_usd,status,request_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(now), provider, model, purpose, market_id, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, web_searches, cost_usd, status, request_id),
        )

    def ai_spend_between(self, start: datetime, end: datetime) -> float:
        row = self.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM api_costs WHERE ts>=? AND ts<?",
                      (iso(start), iso(end)))
        return float(row["s"])

    def ai_calls_between(self, start: datetime, end: datetime) -> int:
        row = self.q1("SELECT COUNT(*) c FROM api_costs WHERE ts>=? AND ts<? AND provider='anthropic'",
                      (iso(start), iso(end)))
        return int(row["c"])

    def ai_spend_total(self) -> float:
        return float(self.q1("SELECT COALESCE(SUM(cost_usd),0) s FROM api_costs")["s"])

    # ------------------------------------------------------------ equity
    def insert_equity_snapshot(self, now: datetime, run_id: Optional[str], **v: float) -> None:
        self.conn.execute(
            "INSERT INTO equity_snapshots(ts,run_id,cash,positions_value,equity,peak_equity,drawdown,"
            "realized_pnl,unrealized_pnl,fees_total,ai_cost_total,unmarked_positions) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(now), run_id, v["cash"], v["positions_value"], v["equity"], v["peak_equity"],
             v["drawdown"], v["realized_pnl"], v["unrealized_pnl"], v["fees_total"],
             v["ai_cost_total"], int(v["unmarked_positions"])),
        )
