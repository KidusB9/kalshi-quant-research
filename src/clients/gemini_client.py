"""
Direct Google Gemini client for trading decisions.

Bypasses OpenRouter entirely — hits the Gemini API directly using the
google-genai SDK.  Free tier: 15 RPM, 1500 RPD, 1M tokens/day.
No credits to manage, no middleman latency.
"""

import json
import os
import re
from typing import Any, Dict, Optional

from json_repair import repair_json

from src.clients.xai_client import TradingDecision
from src.config.settings import settings
from src.utils.logging_setup import TradingLoggerMixin

try:
    from google import genai
except ImportError:
    genai = None


class GeminiClient(TradingLoggerMixin):
    """Direct Gemini API client for market analysis."""

    def __init__(self, api_key: Optional[str] = None, db_manager: Any = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        self.db_manager = db_manager
        self.total_cost = 0.0
        self.request_count = 0
        self._last_request_cost = 0.0
        self._model_name = settings.trading.primary_model.replace("google/", "")

        if not self.api_key:
            self.logger.warning("GEMINI_API_KEY not set")
            self._client = None
            self._model = True
            return

        if genai is None:
            self.logger.error("google-genai not installed")
            self._client = None
            self._model = None
            return

        self._client = genai.Client(api_key=self.api_key)
        self._model = True
        self.logger.info(
            "Gemini client initialized (direct API, google-genai SDK)",
            model=self._model_name,
        )

    async def get_completion(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        strategy: str = "unknown",
        query_type: str = "completion",
        market_id: Optional[str] = None,
    ) -> Optional[str]:
        if self._client is None:
            return None

        try:
            response = await self._generate(prompt)
            self.request_count += 1
            self._last_request_cost = 0.0
            return response
        except Exception as e:
            self.logger.error(f"Gemini completion failed: {e}")
            return None

    async def get_trading_decision(
        self,
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        news_summary: str = "",
        model: Optional[str] = None,
    ) -> Optional[TradingDecision]:
        if self._client is None:
            return None

        prompt = self._build_trading_prompt(market_data, portfolio_data, news_summary)

        try:
            text = await self._generate(prompt)
            self.request_count += 1
            self._last_request_cost = 0.0

            if not text:
                return None

            return self._parse_trading_decision(text)

        except Exception as e:
            self.logger.error(f"Gemini trading decision failed: {e}")
            return None

    async def _generate(self, prompt: str) -> Optional[str]:
        import asyncio
        config = {
            "temperature": settings.trading.ai_temperature,
            "max_output_tokens": min(settings.trading.ai_max_tokens, 8192),
        }
        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model_name,
            contents=prompt,
            config=config,
        )
        if response and response.text:
            return response.text
        return None

    def _build_trading_prompt(
        self,
        market_data: Dict[str, Any],
        portfolio_data: Dict[str, Any],
        news_summary: str,
    ) -> str:
        title = market_data.get("title", "Unknown Market")
        if "yes_bid_dollars" in market_data:
            yes_price = (float(market_data.get("yes_bid_dollars", 0) or 0) + float(market_data.get("yes_ask_dollars", 0) or 0)) / 2
            no_price = (float(market_data.get("no_bid_dollars", 0) or 0) + float(market_data.get("no_ask_dollars", 0) or 0)) / 2
        else:
            yes_price = (market_data.get("yes_bid", 0) + market_data.get("yes_ask", 100)) / 2
            no_price = (market_data.get("no_bid", 0) + market_data.get("no_ask", 100)) / 2
        volume = int(float(market_data.get("volume_fp", 0) or market_data.get("volume", 0) or 0))
        days_to_expiry = market_data.get("days_to_expiry", "Unknown")
        rules = market_data.get("rules", "No specific rules provided")

        cash = portfolio_data.get("cash", portfolio_data.get("balance", 1000))
        max_trade_value = portfolio_data.get(
            "max_trade_value",
            cash * settings.trading.max_position_size_pct / 100,
        )

        truncated_news = (
            news_summary[:800] + "..." if len(news_summary) > 800 else news_summary
        )

        return f"""Analyze this prediction market and provide a trading decision.

Market: {title}
Rules: {rules}
YES price: {yes_price}c | NO price: {no_price}c | Volume: ${volume:,.0f}
Days to expiry: {days_to_expiry}

Available cash: ${cash:,.2f} | Max trade value: ${max_trade_value:,.2f}

News/Context:
{truncated_news}

Instructions:
- Estimate the true probability of the event.
- Only trade if your estimated edge (|your_probability - market_price/100|) exceeds 10%.
- Confidence must be >60% to recommend a trade.
- Consider: is the market already pricing this efficiently? If YES is at 90c, the market already thinks there's a 90% chance. You need to be VERY confident it's wrong to trade.
- Prefer NO-side bets on overpriced favorites — the expected value is often better.
- Return ONLY a JSON object in the following format (no markdown, no extra text):

{{"action": "BUY", "side": "YES", "limit_price": 55, "confidence": 0.72, "reasoning": "brief explanation"}}

If you do not recommend trading, use action "SKIP":

{{"action": "SKIP", "side": "YES", "limit_price": 0, "confidence": 0.40, "reasoning": "insufficient edge"}}
"""

    def _parse_trading_decision(self, response_text: str) -> Optional[TradingDecision]:
        try:
            json_match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                if json_match:
                    json_str = json_match.group(0)
                else:
                    return None

            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                repaired = repair_json(json_str)
                if repaired:
                    data = json.loads(repaired)
                else:
                    return None

            action = data.get("action", "SKIP").upper()
            if action in ("BUY_YES", "BUY_NO", "BUY"):
                action = "BUY"
            elif action in ("SELL",):
                action = "SELL"
            else:
                action = "SKIP"

            side = data.get("side", "YES").upper()
            confidence = float(data.get("confidence", 0.5))
            limit_price = int(data.get("limit_price", 50)) if data.get("limit_price") is not None else None

            return TradingDecision(
                action=action,
                side=side,
                confidence=confidence,
                limit_price=limit_price,
            )
        except Exception as e:
            self.logger.error(f"Error parsing Gemini trading decision: {e}")
            return None

    async def close(self) -> None:
        self.logger.info(
            "Gemini client closed",
            total_requests=self.request_count,
        )
