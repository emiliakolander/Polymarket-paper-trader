"""Polymarket taker fees.

Official formula (docs.polymarket.com/trading/fees, checked 2026-10-14):

    fee = C * feeRate * p * (1 - p)

C = number of shares traded, p = share price, feeRate depends on the market
category. Makers pay no fee. Every simulated order here is a taker order
(we cross the spread), so the taker fee always applies.

When the category cannot be determined we apply the highest configured rate,
so simulated costs err on the expensive side.
"""

from __future__ import annotations

from paperbot.config import FeeConfig

# Keywords (lower-case) found in Gamma `category` / event tag labels -> fee category.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("geopolitics", ("geopolitic", "war", "ukraine", "israel", "middle east", "foreign policy")),
    ("crypto", ("crypto", "bitcoin", "ethereum", "solana", "btc", "eth")),
    ("sports", ("sport", "nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis",
                "ufc", "f1", "golf", "cricket", "esports")),
    ("politics", ("politic", "election", "trump", "congress", "senate", "president")),
    ("economics", ("economy", "economic", "fed", "inflation", "rates", "gdp", "cpi")),
    ("finance", ("finance", "stock", "equities", "ipo", "earnings", "commodities")),
    ("tech", ("tech", "ai", "science")),
    ("culture", ("culture", "pop", "entertainment", "music", "movies", "awards", "celebrit")),
]


def classify_category(labels: list[str]) -> str | None:
    """Map Gamma category / tag labels to a fee category, or None if unknown."""
    lowered = [l.strip().lower() for l in labels if isinstance(l, str) and l.strip()]
    for category, keywords in _CATEGORY_KEYWORDS:
        for label in lowered:
            if label == category or any(
                (k == label) or (len(k) > 3 and k in label) for k in keywords
            ):
                return category
    return None


def fee_rate_for(labels: list[str], cfg: FeeConfig) -> tuple[float, str]:
    """Returns (taker_fee_rate, category_used). Unknown -> conservative rate."""
    category = classify_category(labels)
    if category is not None and category in cfg.category_rates:
        return float(cfg.category_rates[category]), category
    return float(cfg.unknown_category_rate), "unknown"


def taker_fee(shares: float, price: float, rate: float) -> float:
    if shares <= 0 or rate <= 0:
        return 0.0
    if not (0.0 < price < 1.0):
        return 0.0
    return shares * rate * price * (1.0 - price)


def fee_per_share(price: float, rate: float) -> float:
    return taker_fee(1.0, price, rate)
