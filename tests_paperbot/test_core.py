from __future__ import annotations

from datetime import timedelta

import pytest
import requests

from paperbot.config import ConfigError, load_config
from paperbot.fees import classify_category, fee_rate_for, taker_fee
from paperbot.orderbook import BookError, Level, parse_book, walk
from paperbot.polymarket import ApiError, ForbiddenEndpoint, PolymarketClient, RateLimited
from paperbot.safety import (
    SafetyError,
    assert_no_wallet_credentials,
    assert_paper_mode,
    find_forbidden_credentials,
)

from .conftest import NOW, FakeResponse, FakeSession, raw_book


# ------------------------------------------------------------------ config
def test_default_config_is_paper_and_research_off():
    cfg = load_config()
    assert cfg.starting_capital_usd == 1000.0
    assert cfg.research.enabled is False
    assert cfg.budget.monthly_ai_cap_usd == 10.0


def test_unknown_config_key_rejected():
    with pytest.raises(ConfigError):
        load_config(overrides={"risk": {"max_postion_pct": 0.5}})  # typo


def test_out_of_range_fraction_rejected():
    with pytest.raises(ConfigError):
        load_config(overrides={"risk": {"max_drawdown_pct": 1.5}})


def test_non_https_base_rejected():
    with pytest.raises(ConfigError):
        load_config(overrides={"api": {"clob_base": "http://clob.polymarket.com"}})


# ------------------------------------------------------------------ safety
@pytest.mark.parametrize("value", ["", "false", "0", "live", "TRUE-ish"])
def test_paper_mode_required(value):
    with pytest.raises(SafetyError):
        assert_paper_mode({"PAPER_MODE": value})


def test_paper_mode_true_ok():
    assert_paper_mode({"PAPER_MODE": "true"})
    assert_paper_mode({"PAPER_MODE": "TRUE"})


@pytest.mark.parametrize("name", [
    "POLYMARKET_PRIVATE_KEY", "PRIVATE_KEY", "WALLET_SEED", "POLY_API_KEY",
    "CLOB_API_SECRET", "MNEMONIC", "PK", "FUNDER_ADDRESS",
])
def test_wallet_credentials_refused(name):
    with pytest.raises(SafetyError) as e:
        assert_no_wallet_credentials({name: "x", "PAPER_MODE": "true"})
    assert "x" not in str(e.value).replace(name, "")  # value never echoed


def test_hex_private_key_value_detected_under_any_name():
    env = {"SOMETHING": "0x" + "ab" * 32}
    assert find_forbidden_credentials(env) == ["SOMETHING"]


def test_anthropic_key_allowed():
    assert_no_wallet_credentials({"ANTHROPIC_API_KEY": "sk-ant-xyz", "PAPER_MODE": "true"})


# ------------------------------------------------------------------ client
def _client(session, retries=2):
    cfg = load_config(overrides={"api": {"min_interval_s": 0, "max_retries": retries}})
    sleeps = []
    return PolymarketClient(cfg.api, session=session, sleep=sleeps.append), sleeps


@pytest.mark.parametrize("method,api,path", [
    ("POST", "clob", "/order"),
    ("DELETE", "clob", "/order"),
    ("POST", "clob", "/orders"),
    ("GET", "clob", "/auth/api-key"),
    ("POST", "clob", "/auth/api-key"),
    ("GET", "gamma", "/markets/../order"),
])
def test_trading_endpoints_are_forbidden(method, api, path):
    client, _ = _client(FakeSession())
    with pytest.raises(ForbiddenEndpoint):
        client._request(method, api, path)


def test_no_trading_code_paths_in_package():
    """Static check: nothing in paperbot references order-placement or signing APIs."""
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parent.parent / "paperbot"
    banned = re.compile(r"py_clob_client|post_order|create_order|sign_order|eth_account|web3|private_key\s*=")
    for f in src.glob("*.py"):
        assert not banned.search(f.read_text()), f"banned trading reference in {f.name}"


def test_retry_on_429_honours_retry_after_then_succeeds():
    s = FakeSession()
    responses = iter([FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(200, [])])
    s.route("GET", "/markets", lambda p, j: next(responses))
    client, sleeps = _client(s)
    assert client.list_markets(limit=10, offset=0) == []
    assert 3.0 in sleeps
    assert client.stats.rate_limited == 1


def test_persistent_429_raises_rate_limited():
    s = FakeSession()
    s.route("GET", "/markets", lambda p, j: FakeResponse(429))
    client, _ = _client(s, retries=2)
    with pytest.raises(RateLimited):
        client.list_markets(limit=10, offset=0)
    assert len(s.calls) == 3


def test_connection_error_retried_then_raises():
    s = FakeSession()
    s.route("GET", "/book", lambda p, j: requests.ConnectionError("boom"))
    client, _ = _client(s, retries=1)
    with pytest.raises(ApiError):
        client.get_book("t")


def test_invalid_json_raises_api_error():
    s = FakeSession()
    s.route("GET", "/book", lambda p, j: FakeResponse(200, text="<html>oops"))
    client, _ = _client(s)
    with pytest.raises(ApiError):
        client.get_book("t")


def test_wrong_shape_raises_api_error():
    s = FakeSession()
    s.route("GET", "/markets", lambda p, j: FakeResponse(200, {"error": "x"}))
    client, _ = _client(s)
    with pytest.raises(ApiError):
        client.list_markets(limit=1, offset=0)


def test_4xx_not_retried():
    s = FakeSession()
    s.route("GET", "/book", lambda p, j: FakeResponse(400, {"error": "bad"}))
    client, _ = _client(s)
    with pytest.raises(ApiError):
        client.get_book("t")
    assert len(s.calls) == 1


def test_get_books_batches_and_maps_by_asset():
    s = FakeSession()
    s.route("POST", "/books", lambda p, j: FakeResponse(
        200, [raw_book(x["token_id"], [], []) for x in j]))
    client, _ = _client(s)
    ids = [f"t{i}" for i in range(150)]
    books = client.get_books(ids)
    assert set(books) == set(ids)
    assert len(s.calls) == 2  # 100 + 50


def test_market_id_must_be_numeric():
    client, _ = _client(FakeSession())
    with pytest.raises(ApiError):
        client.get_market("1/../order")


# ---------------------------------------------------------------- orderbook
def test_parse_book_sorts_and_validates():
    raw = raw_book("T", bids=[(0.40, 10), (0.45, 5)], asks=[(0.55, 7), (0.50, 3)])
    b = parse_book(raw, "T", fetched_at=NOW)
    assert b.best_bid == 0.45 and b.best_ask == 0.50
    assert [l.price for l in b.asks] == [0.50, 0.55]
    assert b.min_order_size == 5


def test_parse_book_drops_garbage_levels():
    raw = raw_book("T", bids=[(0.4, 10)], asks=[(0.5, 3)])
    raw["asks"] += [{"price": "abc", "size": "1"}, {"price": "1.5", "size": "1"},
                    {"price": "0.6", "size": "-2"}, {"price": "NaN", "size": "1"}, "junk"]
    b = parse_book(raw, "T")
    assert len(b.asks) == 1 and b.dropped_levels == 5


def test_parse_book_rejects_crossed_and_malformed():
    with pytest.raises(BookError):
        parse_book(raw_book("T", bids=[(0.6, 1)], asks=[(0.5, 1)]), "T")
    with pytest.raises(BookError):
        parse_book({"asset_id": "T"}, "T")
    with pytest.raises(BookError):
        parse_book([], "T")
    with pytest.raises(BookError):
        parse_book(raw_book("OTHER", [], []), "T")


def test_empty_book_is_valid_but_has_no_prices():
    b = parse_book(raw_book("T", [], []), "T")
    assert b.best_ask is None and b.mid is None and b.spread is None
    assert walk(b.asks, max_shares=10).shares == 0


def test_book_age_uses_server_timestamp():
    ts = int((NOW - timedelta(seconds=45)).timestamp() * 1000)
    b = parse_book(raw_book("T", [], [], ts_ms=ts), "T", fetched_at=NOW)
    assert 44.9 < b.age_seconds(NOW) < 45.1


def test_walk_buy_multiple_levels_vwap_and_limit():
    asks = [Level(0.50, 10), Level(0.52, 10), Level(0.60, 100)]
    r = walk(asks, max_shares=15, limit_price=0.55)
    assert r.shares == pytest.approx(15)
    assert r.vwap == pytest.approx((0.50 * 10 + 0.52 * 5) / 15)
    r2 = walk(asks, max_shares=50, limit_price=0.55)
    assert r2.shares == pytest.approx(20)  # partial: limit stops at 0.52


def test_walk_participation_and_budget_include_fees():
    asks = [Level(0.50, 100)]
    r = walk(asks, max_notional=10.0, participation=0.5, fee_rate=0.04)
    per_share = 0.50 + 0.04 * 0.5 * 0.5
    assert r.shares == pytest.approx(10.0 / per_share)
    assert r.notional + r.fees == pytest.approx(10.0)
    r2 = walk(asks, max_shares=1000, participation=0.5)
    assert r2.shares == pytest.approx(50)


def test_walk_sell_respects_limit():
    bids = [Level(0.48, 5), Level(0.40, 50)]
    r = walk(bids, max_shares=20, limit_price=0.45, side="sell")
    assert r.shares == pytest.approx(5)


# ---------------------------------------------------------------------- fees
def test_taker_fee_formula_matches_docs():
    # 100 shares at 0.50 with rate 0.04 -> 100 * 0.04 * 0.25 = $1.00
    assert taker_fee(100, 0.50, 0.04) == pytest.approx(1.00)
    assert taker_fee(100, 0.90, 0.07) == pytest.approx(100 * 0.07 * 0.9 * 0.1)
    assert taker_fee(0, 0.5, 0.07) == 0
    assert taker_fee(10, 0.5, 0.0) == 0


def test_fee_category_mapping_and_conservative_default():
    cfg = load_config()
    assert fee_rate_for(["Politics"], cfg.fees) == (0.04, "politics")
    assert fee_rate_for(["Crypto"], cfg.fees) == (0.07, "crypto")
    assert fee_rate_for(["Geopolitics"], cfg.fees) == (0.0, "geopolitics")
    assert fee_rate_for(["NBA"], cfg.fees)[1] == "sports"
    assert fee_rate_for([], cfg.fees) == (0.07, "unknown")
    assert fee_rate_for(["Weird Stuff"], cfg.fees) == (0.07, "unknown")
    assert classify_category(["US-current-affairs", "Elections"]) == "politics"


# ---- fee data as returned by LIVE Gamma (shape observed on 2026-10-14)
def test_market_fee_schedule_from_live_shape_is_authoritative():
    from paperbot.fees import market_fee
    cfg = load_config()
    raw = {"feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.25},
           "feeType": "economics_fees", "feesEnabled": True,
           "tags": ["Recurring", "US CPI", "Fed", "fomc", "Trump", "Economy"]}
    assert market_fee(raw, raw["tags"], cfg.fees) == (0.05, "economics", "market_fee_schedule")
    assert market_fee({**raw, "feesEnabled": False}, [], cfg.fees)[0::2] == (0.0, "fees_disabled")
    no_sched = {"feeType": "sports_fees_v3", "tags": ["Sports"]}
    assert market_fee(no_sched, ["Sports"], cfg.fees) == (0.05, "sports", "category_table")
    assert market_fee({}, [], cfg.fees) == (0.07, "unknown", "conservative_default")
    # exponent < 1 cannot be bounded by the exponent-1 formula -> market is skipped
    assert market_fee({"feeSchedule": {"rate": 0.04, "exponent": 0.5}}, [], cfg.fees)[2] == \
        "unsupported_fee_schedule"
    from paperbot.scanner import parse_market
    from .conftest import raw_market
    m, why = parse_market(raw_market(feeSchedule={"rate": 0.04, "exponent": 0.5}), cfg)
    assert m is None and why == "UNSUPPORTED_FEE_SCHEDULE"
    m, _ = parse_market(raw_market(feeSchedule={"rate": 0.04, "exponent": 2}), cfg)
    assert m.fee_rate == 0.04 and m.fee_source == "market_fee_schedule"  # exponent>1: overstated, safe
