"""Configuration: config/paperbot.yaml -> typed, validated dataclasses.

Unknown keys are rejected so a typo cannot silently disable a limit.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "paperbot.yaml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ApiConfig:
    gamma_base: str = "https://gamma-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    timeout_s: float = 10
    min_interval_s: float = 0.25
    max_retries: int = 3
    max_backoff_s: float = 30
    user_agent: str = "paperbot/0.1 (paper-trading research; read-only)"


@dataclass(frozen=True)
class ScannerConfig:
    max_markets: int = 400
    page_size: int = 100
    min_liquidity_usd: float = 5000
    min_volume_24h_usd: float = 1000
    max_books_per_run: int = 60
    snapshot_levels: int = 5
    snapshot_retention_days: float = 14


@dataclass(frozen=True)
class FeeConfig:
    category_rates: dict = field(default_factory=lambda: {"other": 0.05})
    unknown_category_rate: float = 0.07


@dataclass(frozen=True)
class ArbitrageConfig:
    min_net_edge_pct: float = 0.01
    min_net_profit_usd: float = 0.10
    target_pair_cost_usd: float = 10.0
    max_one_leg_loss_usd: float = 3.0
    depth_participation: float = 0.5
    slippage_buffer: float = 0.003
    max_days_to_resolution: float = 90


@dataclass(frozen=True)
class ResearchConfig:
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "low"
    max_tokens: int = 4000
    max_web_searches: int = 3
    min_days_to_resolution: float = 7
    max_markets_per_run: int = 1
    max_calls_per_day: int = 2
    recheck_hours: float = 72
    min_edge: float = 0.05
    min_confidence: float = 0.6
    kelly_fraction: float = 0.25
    target_order_usd: float = 3.0
    min_evidence_items: int = 2
    max_days_to_resolution: float = 120
    prompt_version: str = "v2-trackrecord"
    blocked_categories: tuple = ()


@dataclass(frozen=True)
class BudgetConfig:
    monthly_ai_cap_usd: float = 10.0
    web_search_usd_per_search: float = 0.01
    est_input_tokens_per_search: int = 25000
    est_prompt_tokens: int = 3000
    pricing_usd_per_mtok: dict = field(default_factory=dict)
    unknown_model_pricing: tuple = (10.0, 50.0)
    infra_cost_usd_per_month: float = 0.0


@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float = 0.05
    max_total_exposure_pct: float = 0.50
    max_per_market_pct: float = 0.08
    max_open_positions: int = 10
    min_trade_usd: float = 1.0
    min_cash_reserve_pct: float = 0.20
    max_spread: float = 0.05
    max_quote_age_s: float = 30
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    max_trades_per_day: int = 10
    min_hours_to_resolution: float = 2
    stop_loss_pct: float = 0.35
    take_profit_pct: float = 0.0          # 0 = disabled (hold to resolution)


@dataclass(frozen=True)
class ExecutionConfig:
    latency_s: float = 1.0
    latency_jitter_s: float = 0.5
    verify_delay_s: float = 2.0
    verify_samples: int = 2
    limit_slippage_pct: float = 0.02
    depth_participation: float = 0.5
    random_reject_rate: float = 0.02
    unwind_unhedged: bool = True
    min_marketable_notional_usd: float = 1.0
    share_decimals: int = 2
    default_tick_size: float = 0.01


@dataclass(frozen=True)
class LearningConfig:
    enabled: bool = True
    llm_postmortems: bool = True          # only used when research is enabled (paid)
    max_postmortems_per_day: int = 2
    resolve_checks_per_run: int = 20
    lessons_in_prompt: int = 5
    proposal_interval_days: float = 7
    min_train_samples: int = 15
    min_test_samples: int = 20
    max_test_samples: int = 80
    improvement_margin: float = 0.02      # per $1 staked, out-of-sample
    auto_apply: bool = True               # bounded strategy filters only, never risk limits
    min_edge_bounds: tuple = (0.03, 0.20)
    min_confidence_bounds: tuple = (0.50, 0.90)
    # Position reviews
    fair_value_margin: float = 0.01       # sell when bid-after-fees >= our probability - margin
    review_research_hours: float = 72     # re-research held positions this often (paid)
    max_reviews_per_run: int = 1


@dataclass(frozen=True)
class Config:
    starting_capital_usd: float = 1000.0
    database_path: str = "data/paperbot.sqlite"
    api: ApiConfig = field(default_factory=ApiConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)


_SECTIONS = {
    "api": ApiConfig,
    "scanner": ScannerConfig,
    "fees": FeeConfig,
    "arbitrage": ArbitrageConfig,
    "research": ResearchConfig,
    "budget": BudgetConfig,
    "risk": RiskConfig,
    "execution": ExecutionConfig,
    "learning": LearningConfig,
}

# Fractions that must lie in [0, 1].
_FRACTIONS = {
    ("arbitrage", "depth_participation"), ("arbitrage", "min_net_edge_pct"),
    ("research", "min_edge"), ("research", "min_confidence"), ("research", "kelly_fraction"),
    ("risk", "max_position_pct"), ("risk", "max_total_exposure_pct"),
    ("risk", "max_per_market_pct"), ("risk", "min_cash_reserve_pct"), ("risk", "max_spread"),
    ("risk", "daily_loss_limit_pct"), ("risk", "max_drawdown_pct"), ("risk", "stop_loss_pct"),
    ("risk", "take_profit_pct"),
    ("execution", "limit_slippage_pct"), ("execution", "depth_participation"),
    ("execution", "random_reject_rate"), ("fees", "unknown_category_rate"),
}


def _build(cls: type, data: dict, section: str) -> Any:
    allowed = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Unknown key(s) in '{section}': {sorted(unknown)}")
    kwargs = dict(data)
    for key in ("unknown_model_pricing", "min_edge_bounds", "min_confidence_bounds",
                "blocked_categories"):
        if key in kwargs and isinstance(kwargs[key], list):
            kwargs[key] = tuple(kwargs[key])
    return cls(**kwargs)


def _validate(cfg: Config) -> None:
    if not (cfg.starting_capital_usd > 0 and math.isfinite(cfg.starting_capital_usd)):
        raise ConfigError("starting_capital_usd must be a positive number")
    for section, name in _FRACTIONS:
        value = getattr(getattr(cfg, section), name)
        if not isinstance(value, (int, float)) or not (0 <= value <= 1):
            raise ConfigError(f"{section}.{name} must be between 0 and 1 (got {value!r})")
    for cat, rate in cfg.fees.category_rates.items():
        if not (0 <= float(rate) <= 1):
            raise ConfigError(f"fees.category_rates.{cat} must be between 0 and 1")
    if cfg.execution.verify_samples < 1:
        raise ConfigError("execution.verify_samples must be >= 1 (fills must be verified)")
    if not (0 <= cfg.execution.share_decimals <= 6):
        raise ConfigError("execution.share_decimals must be between 0 and 6")
    for name in ("min_edge_bounds", "min_confidence_bounds"):
        lo, hi = getattr(cfg.learning, name)
        if not (0 <= lo <= hi <= 1):
            raise ConfigError(f"learning.{name} must be 0 <= low <= high <= 1")
    if cfg.budget.monthly_ai_cap_usd < 0:
        raise ConfigError("budget.monthly_ai_cap_usd must be >= 0")
    for url in (cfg.api.gamma_base, cfg.api.clob_base):
        if not url.startswith("https://"):
            raise ConfigError(f"API base URL must use https: {url}")


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    """Load and validate configuration. `overrides` is merged per section (for tests)."""
    raw: dict = {}
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    elif path is not None:
        raise ConfigError(f"Config file not found: {p}")
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value

    top_allowed = {f.name for f in dataclasses.fields(Config)}
    unknown = set(raw) - top_allowed
    if unknown:
        raise ConfigError(f"Unknown top-level key(s): {sorted(unknown)}")

    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _SECTIONS:
            kwargs[key] = _build(_SECTIONS[key], value or {}, key)
        else:
            kwargs[key] = value
    cfg = Config(**kwargs)
    _validate(cfg)
    return cfg
