"""Backward-compatibility shim.

The implementation moved to the src/strategies/portfolio/ package:
models, optimizer, immediate, runner. This module re-exports the public
surface so existing imports -- in this repo and in forks -- keep working:

    from src.strategies.portfolio_optimization import AdvancedPortfolioOptimizer

New code should import from src.strategies.portfolio instead.
"""

from src.strategies.portfolio import (  # noqa: F401
    AdvancedPortfolioOptimizer,
    MarketOpportunity,
    PortfolioAllocation,
    _calculate_simple_kelly,
    _evaluate_immediate_trade,
    _get_fast_ai_prediction,
    create_market_opportunities_from_markets,
    run_portfolio_optimization,
)

__all__ = [
    "MarketOpportunity",
    "PortfolioAllocation",
    "AdvancedPortfolioOptimizer",
    "create_market_opportunities_from_markets",
    "run_portfolio_optimization",
]
