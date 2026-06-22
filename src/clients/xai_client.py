"""
xai_client.py -- historical name; this is now the OpenRouter client wrapper.

The direct xAI dependency was removed when the project moved to a single
OpenRouter API key. The class kept the ``XAIClient`` name for compatibility,
but every call routes through src/clients/openrouter_client.py.

This file holds:
 1. The ``TradingDecision`` and ``DailyUsageTracker`` dataclasses, used
    across the codebase.
 2. The ``XAIClient`` wrapper, which adds a persistent daily-cost tracker
    on top of the OpenRouter client.
"""

import asyncio
import os
import pickle
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.config.settings import settings
from src.utils.logging_setup import TradingLoggerMixin


# ---------------------------------------------------------------------------
# Shared dataclasses (imported by openrouter_client, agents, etc.)
# ---------------------------------------------------------------------------

@dataclass
class TradingDecision:
    """Represents an AI trading decision."""
    action: str           # "buy", "sell", "hold"
    side: str             # "yes", "no"
    confidence: float     # 0.0 to 1.0
    limit_price: Optional[int] = None   # limit price in cents
    reasoning: Optional[str] = None


@dataclass
class DailyUsageTracker:
    """Track daily AI usage and costs."""
    date: str
    total_cost: float = 0.0
    request_count: int = 0
    daily_limit: float = 10.0  # Default $10/day (override via DAILY_AI_COST_LIMIT env var)
    is_exhausted: bool = False
    last_exhausted_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# XAIClient -- cost-tracking wrapper (delegates completions to OpenRouter)
# ---------------------------------------------------------------------------

class XAIClient(TradingLoggerMixin):
    """
    Compatibility shim that replaces the old xAI SDK client.

    All completions are forwarded to OpenRouter.  The daily-cost-tracking
    interface (daily_tracker, _check_daily_limits, _update_daily_cost) is
    preserved so that existing callers (beast_mode_bot) continue to work
    without modification.
    """

    def __init__(self, api_key: Optional[str] = None, db_manager=None):
        # api_key is accepted but ignored (no longer used for xAI direct calls)
        self.db_manager = db_manager

        # Model config -- read from settings so they stay in sync
        self.primary_model = settings.trading.primary_model
        self.fallback_model = settings.trading.fallback_model
        self.temperature = settings.trading.ai_temperature
        self.max_tokens = settings.trading.ai_max_tokens

        # Cost tracking (aggregate across all delegated calls)
        self.total_cost = 0.0
        self.request_count = 0

        # Daily usage tracking (persisted to disk)
        self.usage_file = "logs/daily_ai_usage.pkl"
        self.daily_tracker = self._load_daily_tracker()

        # Lazy OpenRouter client (initialised on first LLM call)
        self._openrouter_client = None

        self.logger.info(
            "XAIClient (OpenRouter delegate) initialized",
            primary_model=self.primary_model,
            daily_limit=self.daily_tracker.daily_limit,
            today_cost=self.daily_tracker.total_cost,
            today_requests=self.daily_tracker.request_count,
        )

    # ------------------------------------------------------------------
    # Daily cost tracking
    # ------------------------------------------------------------------

    def _load_daily_tracker(self) -> DailyUsageTracker:
        """Load or create daily usage tracker."""
        today = datetime.now().strftime("%Y-%m-%d")
        daily_limit = getattr(settings.trading, "daily_ai_cost_limit", 10.0)
        os.makedirs("logs", exist_ok=True)

        try:
            if os.path.exists(self.usage_file):
                with open(self.usage_file, "rb") as f:
                    tracker = pickle.load(f)
                if tracker.date != today:
                    tracker = DailyUsageTracker(date=today, daily_limit=daily_limit)
                else:
                    tracker.daily_limit = daily_limit
                    if tracker.is_exhausted and tracker.total_cost < daily_limit:
                        tracker.is_exhausted = False
                return tracker
        except Exception as e:
            self.logger.warning(f"Failed to load daily tracker: {e}")

        return DailyUsageTracker(date=today, daily_limit=daily_limit)

    def _save_daily_tracker(self):
        """Save daily usage tracker to disk."""
        try:
            os.makedirs("logs", exist_ok=True)
            with open(self.usage_file, "wb") as f:
                pickle.dump(self.daily_tracker, f)
        except Exception as e:
            self.logger.error(f"Failed to save daily tracker: {e}")

    def _update_daily_cost(self, cost: float):
        """Update daily cost tracking."""
        self.daily_tracker.total_cost += cost
        self.daily_tracker.request_count += 1
        self._save_daily_tracker()

        if self.daily_tracker.total_cost >= self.daily_tracker.daily_limit:
            self.daily_tracker.is_exhausted = True
            self.daily_tracker.last_exhausted_time = datetime.now()
            self._save_daily_tracker()
            self.logger.warning(
                "Daily AI cost limit reached -- trading paused until tomorrow.",
                daily_cost=self.daily_tracker.total_cost,
                daily_limit=self.daily_tracker.daily_limit,
                requests_today=self.daily_tracker.request_count,
            )

    async def _check_daily_limits(self) -> bool:
        """
        Returns True if we can proceed, False if daily limit reached.
        Called by beast_mode_bot before each trading cycle.
        """
        self.daily_tracker = self._load_daily_tracker()

        if self.daily_tracker.is_exhausted:
            now = datetime.now()
            if self.daily_tracker.date != now.strftime("%Y-%m-%d"):
                self.daily_tracker = DailyUsageTracker(
                    date=now.strftime("%Y-%m-%d"),
                    daily_limit=self.daily_tracker.daily_limit,
                )
                self._save_daily_tracker()
                self.logger.info("New day -- daily AI limits reset")
                return True

            self.logger.info(
                "Daily AI limit reached -- request skipped",
                daily_cost=self.daily_tracker.total_cost,
                daily_limit=self.daily_tracker.daily_limit,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # LLM provider delegation (Gemini direct or OpenRouter fallback)
    # ------------------------------------------------------------------

    def _get_openrouter_client(self):
        """Lazy-init OpenRouter client."""
        if self._openrouter_client is None:
            try:
                from src.clients.openrouter_client import OpenRouterClient
                self._openrouter_client = OpenRouterClient(db_manager=self.db_manager)
                self.logger.info("OpenRouter client initialised (via XAIClient shim)")
            except Exception as e:
                self.logger.error(f"Failed to init OpenRouter client: {e}")
        return self._openrouter_client

    def _get_gemini_client(self):
        """Lazy-init Gemini client (preferred when GEMINI_API_KEY is set)."""
        if not hasattr(self, "_gemini_client"):
            self._gemini_client = None
        if self._gemini_client is None:
            gemini_key = os.getenv("GEMINI_API_KEY", "")
            if gemini_key:
                try:
                    from src.clients.gemini_client import GeminiClient
                    self._gemini_client = GeminiClient(api_key=gemini_key, db_manager=self.db_manager)
                    self.logger.info("Gemini client initialised (direct API, no OpenRouter)")
                except Exception as e:
                    self.logger.error(f"Failed to init Gemini client: {e}")
        return self._gemini_client

    def _get_deepseek_client(self):
        """Lazy-init a direct DeepSeek client (OpenAI-compatible endpoint).

        Reuses OpenRouterClient pointed at DeepSeek's base URL so we get the
        same prompt/parse/retry logic without a separate class.
        """
        if not hasattr(self, "_deepseek_client"):
            self._deepseek_client = None
        if self._deepseek_client is None:
            ds_key = os.getenv("DEEPSEEK_API_KEY", "")
            if ds_key:
                try:
                    from src.clients.openrouter_client import OpenRouterClient
                    self._deepseek_client = OpenRouterClient(
                        api_key=ds_key,
                        default_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                        db_manager=self.db_manager,
                    )
                    self.logger.info("DeepSeek client initialised (direct API, fallback tier 2)")
                except Exception as e:
                    self.logger.error(f"Failed to init DeepSeek client: {e}")
        return self._deepseek_client

    def _get_provider_chain(self):
        """Ordered (name, client, model) failover chain.

        Tier 1: Gemini direct (free)  ->  Tier 2: DeepSeek direct  ->
        Tier 3: OpenRouter.  Tiers with no configured key/client are skipped.
        If every tier fails on a request we ABSTAIN (return None) instead of
        trading on garbage -- this is the fix for "trades on fallback data".
        """
        chain = []

        gemini = self._get_gemini_client()
        if gemini is not None and getattr(gemini, "_model", None) is not None and gemini._client is not None:
            chain.append(("gemini", gemini, self.primary_model))

        deepseek = self._get_deepseek_client()
        if deepseek is not None:
            chain.append(("deepseek", deepseek, os.getenv("DEEPSEEK_MODEL", "deepseek-chat")))

        openrouter = self._get_openrouter_client()
        if openrouter is not None:
            chain.append(("openrouter", openrouter, self.fallback_model))

        return chain

    def _get_llm_client(self):
        """Back-compat: return the first available provider in the chain."""
        chain = self._get_provider_chain()
        return chain[0][1] if chain else None

    async def get_completion(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        strategy: str = "unknown",
        query_type: str = "completion",
        market_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Get a completion via OpenRouter.  Delegates to OpenRouterClient and
        updates the local daily cost tracker so that beast_mode_bot's limit
        checks stay accurate.
        """
        if not await self._check_daily_limits():
            return None

        chain = self._get_provider_chain()
        if not chain:
            self.logger.error("No LLM providers available for completion -- abstaining.")
            return None

        for name, client, model in chain:
            try:
                result = await client.get_completion(
                    prompt=prompt,
                    model=model,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=temperature if temperature is not None else self.temperature,
                    strategy=strategy,
                    query_type=query_type,
                    market_id=market_id,
                )
            except Exception as e:
                self.logger.warning(f"Provider '{name}' completion failed ({e}); trying next tier.")
                continue

            if result is not None:
                cost = getattr(client, "_last_request_cost", 0.0)
                self.total_cost += cost
                self.request_count += 1
                self._update_daily_cost(cost)
                return result

            self.logger.warning(f"Provider '{name}' returned no completion; trying next tier.")

        self.logger.error("All LLM providers failed for completion -- abstaining (no trade).")
        return None

    async def get_trading_decision(
        self,
        market_data: Dict,
        portfolio_data: Dict,
        news_summary: str = "",
    ) -> Optional[TradingDecision]:
        """Get a trading decision (Gemini direct if available, else OpenRouter)."""
        if not await self._check_daily_limits():
            return None

        chain = self._get_provider_chain()
        if not chain:
            self.logger.error("No LLM providers available for decision -- abstaining (no trade).")
            return None

        for name, client, model in chain:
            try:
                decision = await client.get_trading_decision(
                    market_data=market_data,
                    portfolio_data=portfolio_data,
                    news_summary=news_summary,
                    model=model,
                )
            except Exception as e:
                self.logger.warning(f"Provider '{name}' decision failed ({e}); trying next tier.")
                continue

            if decision is not None:
                cost = getattr(client, "_last_request_cost", 0.0)
                self.total_cost += cost
                self.request_count += 1
                self._update_daily_cost(cost)
                self.logger.info(f"Decision served by provider '{name}'.")
                return decision

            self.logger.warning(f"Provider '{name}' returned no decision; trying next tier.")

        # Every provider failed -> ABSTAIN. decide.py treats None as SKIP, so
        # the bot will NOT place a trade on missing/garbage AI output.
        self.logger.error("All LLM providers failed for decision -- abstaining (no trade).")
        return None

    async def search(self, query: str, max_length: int = 300) -> str:
        """
        Search stub -- returns an empty fallback string.
        Live search via xAI has been removed; integrate a web-search tool
        if news context is needed.
        """
        self.logger.debug("XAIClient.search called -- returning empty fallback (xAI search removed)")
        return f"[Search unavailable: xAI search dependency removed. Query: {query[:80]}]"

    async def close(self) -> None:
        """Clean up resources."""
        if self._openrouter_client:
            await self._openrouter_client.close()
        self.logger.info(
            "XAIClient closed",
            total_estimated_cost=self.total_cost,
            total_requests=self.request_count,
        )
