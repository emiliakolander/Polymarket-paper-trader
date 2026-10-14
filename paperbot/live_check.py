"""LIVE connectivity check against the real public Polymarket APIs (read-only).

  python -m paperbot.live_check [--out DIR]

Calls every endpoint the bot uses, validates the responses with the bot's own
parsers, and prints a report clearly labelled LIVE MARKET DATA. It never writes to
the database and never calls anything outside the read-only allowlist.
Exit code 0 = all endpoints reachable and parseable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from paperbot import arbitrage
from paperbot.config import load_config
from paperbot.orderbook import BookError, parse_book
from paperbot.polymarket import ApiError, PolymarketClient
from paperbot.scanner import parse_market


def run_check(client: PolymarketClient, cfg, now: datetime) -> dict:
    report = {"label": "LIVE MARKET DATA", "checked_at": now.isoformat(), "steps": [], "ok": True}

    def step(name, fn):
        diagnostic = "diagnostic" in name
        t0 = time.monotonic()
        try:
            detail = fn()
            report["steps"].append({"step": name, "ok": True, "ms": round((time.monotonic() - t0) * 1000),
                                    "detail": detail})
            return detail
        except (ApiError, BookError, KeyError, ValueError, TypeError) as exc:
            if not diagnostic:
                report["ok"] = False
            report["steps"].append({"step": name, "ok": False, "ms": round((time.monotonic() - t0) * 1000),
                                    "error": f"{type(exc).__name__}: {exc}"})
            return None

    raws = step("GET gamma /markets (50 most active)", lambda: client.list_markets(limit=50, offset=0))
    if not raws:
        report["ok"] = False
        return report
    parsed, rejected = [], {}
    for r in raws:
        m, why = parse_market(r, cfg)
        if m is None:
            rejected[why] = rejected.get(why, 0) + 1
        else:
            parsed.append(m)
    report["markets"] = {"received": len(raws), "binary_yes_no": len(parsed), "rejected": rejected,
                         "fee_categories": sorted({m.fee_category for m in parsed})}
    if not parsed:
        report["ok"] = False
        return report
    sample = sorted(parsed, key=lambda m: m.liquidity, reverse=True)[:5]
    tokens = [t for m in sample for t in (m.yes_token, m.no_token)]
    raw_books = step("POST clob /books (10 tokens)", lambda: {k: True for k in client.get_books(tokens)})
    books_raw = client.get_books(tokens) if raw_books else {}
    books = []
    for m in sample:
        try:
            m.yes_book = parse_book(books_raw[m.yes_token], m.yes_token, fetched_at=now)
            m.no_book = parse_book(books_raw[m.no_token], m.no_token, fetched_at=now)
        except (KeyError, BookError) as exc:
            report["ok"] = False
            books.append({"market_id": m.market_id, "error": str(exc)})
            continue
        ev = arbitrage.evaluate(m, cfg.arbitrage, now, exec_cfg=cfg.execution)
        books.append({
            "market_id": m.market_id, "question": m.question[:90], "fee_category": m.fee_category,
            "yes_best_bid": m.yes_book.best_bid, "yes_best_ask": m.yes_book.best_ask,
            "no_best_bid": m.no_book.best_bid, "no_best_ask": m.no_book.best_ask,
            "yes_levels": [len(m.yes_book.bids), len(m.yes_book.asks)],
            "min_order_size": m.yes_book.min_order_size, "tick_size": m.yes_book.tick_size,
            "server_timestamp_present": m.yes_book.server_time is not None,
            "best_ask_sum": ev.best_ask_sum, "arbitrage_verdict": ev.reasons or ["OK"],
        })
    report["books"] = books
    if sample:
        tok = sample[0].yes_token
        step("GET clob /book (single token)", lambda: {
            "levels": [len(b.bids), len(b.asks)], "hash": b.book_hash}
            if (b := parse_book(client.get_book(tok), tok, fetched_at=now)) else None)
        step("GET gamma /markets/{id}", lambda: {"id": client.get_market(sample[0].market_id).get("id")})
    # ---- diagnostics: which fields carry category / fee information in LIVE data
    keys: dict[str, int] = {}
    for r in raws:
        for k in r:
            keys[k] = keys.get(k, 0) + 1
    interesting = sorted(k for k in keys if any(w in k.lower() for w in ("categ", "tag", "fee", "event")))
    report["diagnostics"] = {
        "market_keys": sorted(keys),
        "category_fee_keys": {k: keys[k] for k in interesting},
        "sample_category_fee_values": [{k: (r.get(k) if not isinstance(r.get(k), list) else
                                            [(x.get("label") or x.get("slug") or x.get("title"))
                                             if isinstance(x, dict) else x for x in r.get(k)][:6])
                                        for k in interesting} for r in raws[:3]],
    }
    ev = step("GET gamma /events (5, diagnostic)", lambda: client.list_events(limit=5))
    if ev:
        report["diagnostics"]["events_sample"] = [
            {"title": e.get("title"), "category": e.get("category"),
             "tags": [t.get("label") for t in (e.get("tags") or []) if isinstance(t, dict)][:8],
             "n_markets": len(e.get("markets") or []),
             "market_ids": [m.get("id") for m in (e.get("markets") or [])][:3]} for e in ev]
    if sample:
        step("GET clob /fee-rate (diagnostic)", lambda: client.get_fee_rate(sample[0].yes_token))
    report["api_stats"] = client.stats.as_dict()
    return report


def render(r: dict) -> str:
    lines = [f"# {r['label']} — connectivity check: {'PASSED' if r['ok'] else 'FAILED'}", "",
             f"Checked at {r['checked_at']} (read-only; nothing written, nothing traded).", "",
             "| Step | Result | Time | Detail |", "|---|---|---|---|"]
    for s in r["steps"]:
        lines.append(f"| {s['step']} | {'ok' if s['ok'] else '**FAIL**'} | {s['ms']} ms | "
                     f"{json.dumps(s.get('detail') if s['ok'] else s.get('error'), default=str)[:160]} |")
    if r.get("markets"):
        lines += ["", f"Markets: {r['markets']}"]
    if r.get("diagnostics"):
        lines += ["", "DIAGNOSTICS " + json.dumps(r["diagnostics"], default=str)[:6000]]
    for b in r.get("books", []):
        lines.append(f"- {json.dumps(b, default=str)}")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paperbot.live_check")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    cfg = load_config()
    r = run_check(PolymarketClient(cfg.api), cfg, datetime.now(timezone.utc))
    text = render(r)
    print(text)
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "live_check.md").write_text(text)
        (Path(args.out) / "live_check.json").write_text(json.dumps(r, indent=2, default=str))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
