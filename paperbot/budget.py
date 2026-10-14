"""Hard monthly AI spend cap.

- Spend is read from the persistent `api_costs` table (survives restarts).
- Before each Claude call we compute a WORST-CASE cost for that call
  (max output tokens, generous input estimate incl. web-search results,
  max searches). The call is refused unless spent + worst_case <= cap.
- Once the cap is reached the research agent shuts down for the rest of the
  calendar month (UTC). Nothing else in the bot costs money.

Residual risk: if a single call's real input exceeds our generous estimate,
actual spend can exceed the cap by that difference. Set a matching monthly
spend limit in the Anthropic Console as a second, provider-side stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from paperbot.config import BudgetConfig, ResearchConfig
from paperbot.db import Database


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def day_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


@dataclass
class BudgetStatus:
    cap_usd: float
    spent_month_usd: float
    calls_today: int
    worst_case_call_usd: float

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_month_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_month_usd + self.worst_case_call_usd > self.cap_usd


class AIBudget:
    def __init__(self, db: Database, cfg: BudgetConfig, research: ResearchConfig) -> None:
        self.db = db
        self.cfg = cfg
        self.research = research

    def pricing(self, model: str) -> tuple[float, float]:
        p = self.cfg.pricing_usd_per_mtok.get(model)
        if p is None:
            p = self.cfg.unknown_model_pricing
        return float(p[0]), float(p[1])

    def cost(self, model: str, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, cache_write_tokens: int = 0, web_searches: int = 0) -> float:
        pin, pout = self.pricing(model)
        return (
            input_tokens * pin
            + cache_write_tokens * pin * 1.25
            + cache_read_tokens * pin * 0.10
            + output_tokens * pout
        ) / 1_000_000 + web_searches * self.cfg.web_search_usd_per_search

    def worst_case_call(self, model: str | None = None) -> float:
        r = self.research
        input_est = self.cfg.est_prompt_tokens + r.max_web_searches * self.cfg.est_input_tokens_per_search
        return self.cost(model or r.model, input_est, r.max_tokens, web_searches=r.max_web_searches)

    def status(self, now: datetime) -> BudgetStatus:
        ms, me = month_bounds(now)
        ds, de = day_bounds(now)
        return BudgetStatus(
            cap_usd=self.cfg.monthly_ai_cap_usd,
            spent_month_usd=self.db.ai_spend_between(ms, me),
            calls_today=self.db.ai_calls_between(ds, de),
            worst_case_call_usd=self.worst_case_call(),
        )

    def authorize(self, now: datetime) -> tuple[bool, str]:
        st = self.status(now)
        if st.exhausted:
            return False, (f"AI_BUDGET_EXHAUSTED: spent ${st.spent_month_usd:.2f} of "
                           f"${st.cap_usd:.2f}; next call could cost up to "
                           f"${st.worst_case_call_usd:.2f}")
        if st.calls_today >= self.research.max_calls_per_day:
            return False, "AI_DAILY_CALL_LIMIT"
        return True, ""
