"""Read-only client for Polymarket's PUBLIC market-data APIs.

Only an explicit allowlist of endpoints can be called. None of them can place,
cancel or sign orders: there is no authentication code in this module and no
route for it. Any attempt to call another path raises ForbiddenEndpoint.

Endpoints used (docs.polymarket.com):
  GET  gamma-api /markets          market metadata (paginated)
  GET  gamma-api /markets/{id}     one market (used for settlement)
  GET  clob      /book?token_id=   full order book for one outcome token
  POST clob      /books            order books for many tokens (read-only, body = token ids)
  GET  gamma-api /events           events with category tags (read-only)
  GET  clob      /fee-rate?token_id= public taker fee rate for a token (read-only)
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from paperbot.config import ApiConfig


class ApiError(RuntimeError):
    """Network failure, bad status, or malformed response."""


class RateLimited(ApiError):
    """HTTP 429 persisted after all retries."""


class ForbiddenEndpoint(RuntimeError):
    """Attempt to call an endpoint outside the read-only allowlist."""


_ALLOWED: list[tuple[str, str, re.Pattern]] = [
    ("GET", "gamma", re.compile(r"^/markets$")),
    ("GET", "gamma", re.compile(r"^/markets/[0-9]+$")),
    ("GET", "gamma", re.compile(r"^/events$")),
    ("GET", "clob", re.compile(r"^/fee-rate$")),
    ("GET", "clob", re.compile(r"^/book$")),
    ("POST", "clob", re.compile(r"^/books$")),
]

_RETRY_STATUSES = {429, 500, 502, 503, 504}
BOOKS_BATCH = 100


@dataclass
class ApiStats:
    requests: int = 0
    errors: int = 0
    rate_limited: int = 0
    retries: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        return {"requests": self.requests, "errors": self.errors,
                "rate_limited": self.rate_limited, "retries": self.retries}


class PolymarketClient:
    def __init__(
        self,
        cfg: ApiConfig,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self._bases = {"gamma": cfg.gamma_base.rstrip("/"), "clob": cfg.clob_base.rstrip("/")}
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": cfg.user_agent, "Accept": "application/json"})
        self._sleep = sleep
        self._clock = clock
        self._last_request = -1e9
        self.stats = ApiStats()

    # ------------------------------------------------------------------ core
    def _check_allowed(self, method: str, api: str, path: str) -> None:
        for m, a, pattern in _ALLOWED:
            if m == method and a == api and pattern.match(path):
                return
        raise ForbiddenEndpoint(f"{method} {api}{path} is not an allowed read-only endpoint")

    def _throttle(self) -> None:
        wait = self.cfg.min_interval_s - (self._clock() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _request(self, method: str, api: str, path: str, *, params=None, json_body=None) -> Any:
        self._check_allowed(method, api, path)
        url = self._bases[api] + path
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            if attempt:
                self.stats.retries += 1
            self._throttle()
            self.stats.requests += 1
            try:
                resp = self._session.request(
                    method, url, params=params, json=json_body, timeout=self.cfg.timeout_s
                )
            except requests.RequestException as exc:
                last_exc = exc
                self._sleep(min(2 ** attempt, self.cfg.max_backoff_s))
                continue

            if resp.status_code in _RETRY_STATUSES:
                if resp.status_code == 429:
                    self.stats.rate_limited += 1
                last_exc = (RateLimited if resp.status_code == 429 else ApiError)(
                    f"HTTP {resp.status_code} from {method} {path}"
                )
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                self.stats.errors += 1
                raise ApiError(f"HTTP {resp.status_code} from {method} {path}")
            try:
                return resp.json()
            except ValueError as exc:
                self.stats.errors += 1
                raise ApiError(f"Invalid JSON from {method} {path}") from exc

        self.stats.errors += 1
        if isinstance(last_exc, ApiError):
            raise last_exc
        raise ApiError(f"{method} {path} failed after retries: {last_exc}") from last_exc

    def _retry_delay(self, resp: requests.Response, attempt: int) -> float:
        ra = resp.headers.get("Retry-After") if resp.headers else None
        if ra:
            try:
                return min(max(float(ra), 0.0), self.cfg.max_backoff_s)
            except ValueError:
                pass
        return min(2 ** attempt, self.cfg.max_backoff_s)

    # ------------------------------------------------------------ endpoints
    def list_markets(self, *, limit: int, offset: int) -> list[dict]:
        data = self._request(
            "GET", "gamma", "/markets",
            params={"active": "true", "closed": "false", "order": "volume24hr",
                    "ascending": "false", "limit": limit, "offset": offset,
                    "include_tag": "true"},
        )
        if not isinstance(data, list):
            raise ApiError("Gamma /markets did not return a list")
        return [m for m in data if isinstance(m, dict)]

    def get_market(self, market_id: str) -> dict:
        if not str(market_id).isdigit():
            raise ApiError(f"Invalid market id {market_id!r}")
        data = self._request("GET", "gamma", f"/markets/{market_id}")
        if not isinstance(data, dict):
            raise ApiError("Gamma /markets/{id} did not return an object")
        return data

    def list_events(self, *, limit: int, offset: int = 0) -> list[dict]:
        data = self._request("GET", "gamma", "/events",
                             params={"active": "true", "closed": "false", "order": "volume24hr",
                                     "ascending": "false", "limit": limit, "offset": offset})
        if not isinstance(data, list):
            raise ApiError("Gamma /events did not return a list")
        return [e for e in data if isinstance(e, dict)]

    def get_fee_rate(self, token_id: str):
        return self._request("GET", "clob", "/fee-rate", params={"token_id": token_id})

    def get_book(self, token_id: str) -> dict:
        data = self._request("GET", "clob", "/book", params={"token_id": token_id})
        if not isinstance(data, dict):
            raise ApiError("CLOB /book did not return an object")
        return data

    def get_books(self, token_ids: list[str]) -> dict[str, dict]:
        """Returns {token_id: raw_book}. Tokens missing from the response are omitted."""
        out: dict[str, dict] = {}
        for i in range(0, len(token_ids), BOOKS_BATCH):
            chunk = token_ids[i:i + BOOKS_BATCH]
            data = self._request("POST", "clob", "/books",
                                 json_body=[{"token_id": t} for t in chunk])
            if not isinstance(data, list):
                raise ApiError("CLOB /books did not return a list")
            for raw in data:
                if isinstance(raw, dict) and raw.get("asset_id"):
                    out[str(raw["asset_id"])] = raw
        return out
