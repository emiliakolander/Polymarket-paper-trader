"""Shared fixtures. No test makes a real network call: HTTP goes through FakeSession."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from paperbot.config import load_config

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._text = text

    def json(self):
        if self._text is not None:
            return json.loads(self._text)
        return self._payload


class FakeSession:
    """Routes (method, path) -> handler(params, json) returning FakeResponse or raising."""

    def __init__(self):
        self.headers = {}
        self.routes = {}
        self.calls = []

    def route(self, method, path, handler):
        self.routes[(method, path)] = handler

    def request(self, method, url, params=None, json=None, timeout=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
        self.calls.append((method, path, params, json))
        handler = self.routes.get((method, path))
        if handler is None:
            return FakeResponse(404, {"error": "not found"})
        result = handler(params, json)
        if isinstance(result, Exception):
            raise result
        return result


def raw_book(token, bids, asks, ts_ms=None, min_size="5"):
    book = {
        "market": "0xcond",
        "asset_id": token,
        "bids": [{"price": str(p), "size": str(s)} for p, s in bids],
        "asks": [{"price": str(p), "size": str(s)} for p, s in asks],
        "tick_size": "0.01",
        "min_order_size": min_size,
        "neg_risk": False,
        "hash": f"h-{token}",
    }
    if ts_ms is not None:
        book["timestamp"] = str(ts_ms)
    return book


def raw_market(mid="101", yes="tokY", no="tokN", liquidity=20000, volume=5000,
               end=NOW + timedelta(days=30), category="Politics", **extra):
    m = {
        "id": mid,
        "question": f"Will event {mid} happen?",
        "conditionId": f"0xcond{mid}",
        "slug": f"event-{mid}",
        "endDate": end.isoformat().replace("+00:00", "Z"),
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps([yes, no]),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "liquidityNum": liquidity,
        "volume24hr": volume,
        "bestBid": 0.49,
        "bestAsk": 0.51,
        "negRisk": False,
        "category": category,
        "orderMinSize": 5,
    }
    m.update(extra)
    return m


@pytest.fixture
def cfg(tmp_path):
    return load_config(overrides={
        "database_path": str(tmp_path / "test.sqlite"),
        "api": {"min_interval_s": 0, "max_retries": 2},
        "execution": {"latency_s": 0, "random_reject_rate": 0.0},
    })


@pytest.fixture
def paper_env(monkeypatch):
    for k in list(__import__("os").environ):
        if "KEY" in k.upper() and k != "ANTHROPIC_API_KEY":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PAPER_MODE", "true")
