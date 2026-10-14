"""Deterministic MOCK market data (used only with `--mock-market-data`).

Lets the full pipeline - including trades, positions and settlement - be exercised
on CI runners without depending on what real markets happen to look like. Every
database touched in mock mode is labelled data_source=MOCK and can never be used
for a LIVE run (and vice versa).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


def _market(mid, yes, no, category, end, question):
    return {
        "id": mid, "question": question, "conditionId": f"0xmock{mid}", "slug": f"mock-{mid}",
        "endDate": end.isoformat().replace("+00:00", "Z"),
        "outcomes": json.dumps(["Yes", "No"]), "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([yes, no]), "active": True, "closed": False,
        "acceptingOrders": True, "enableOrderBook": True, "liquidityNum": 50000,
        "volume24hr": 20000, "bestBid": 0.49, "bestAsk": 0.51, "negRisk": False,
        "category": category, "orderMinSize": 5, "description": "MOCK market for pipeline tests.",
    }


class MockSession:
    """Mimics requests.Session for the allowlisted Polymarket endpoints."""

    def __init__(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        end = (now + timedelta(days=30)).replace(microsecond=0)
        self.headers = {}
        self.markets = {
            # Fee-free category with a complement edge: 0.45 + 0.50 < 1.
            "900001": _market("900001", "mockY1", "mockN1", "Geopolitics", end,
                              "MOCK: Will the test event A happen?"),
            # Fairly priced market (no edge).
            "900002": _market("900002", "mockY2", "mockN2", "Politics", end,
                              "MOCK: Will the test event B happen?"),
        }
        self.books = {
            "mockY1": ([(0.40, 500)], [(0.45, 200)]), "mockN1": ([(0.45, 500)], [(0.50, 500)]),
            "mockY2": ([(0.48, 500)], [(0.50, 500)]), "mockN2": ([(0.48, 500)], [(0.52, 500)]),
        }

    def _book(self, tok):
        bids, asks = self.books[tok]
        return {"market": "0xmock", "asset_id": tok, "tick_size": "0.01", "min_order_size": "5",
                "neg_risk": False, "hash": f"mock-{tok}",
                "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
                "asks": [{"price": str(p), "size": str(s)} for p, s in asks]}

    def request(self, method, url, params=None, json=None, timeout=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        if method == "GET" and path == "/markets":
            return _Resp(200, list(self.markets.values()) if int(params.get("offset", 0)) == 0 else [])
        if method == "GET" and path.startswith("/markets/"):
            mid = path.rsplit("/", 1)[1]
            return _Resp(200, self.markets[mid]) if mid in self.markets else _Resp(404, {})
        if method == "GET" and path == "/book":
            tok = params["token_id"]
            return _Resp(200, self._book(tok)) if tok in self.books else _Resp(404, {})
        if method == "POST" and path == "/books":
            return _Resp(200, [self._book(x["token_id"]) for x in json if x["token_id"] in self.books])
        return _Resp(404, {"error": "not mocked"})
