"""Portfolio optimization package.

Split from the former single-file portfolio_optimization.py (1,300+ lines):

- models.py     -- MarketOpportunity / PortfolioAllocation dataclasses
- optimizer.py  -- AdvancedPortfolioOptimizer (Kelly Criterion Extension pipeline)
- immediate.py  -- market -> opportunity conversion + immediate-trade evaluation
- runner.py     -- run_portfolio_optimization entry point

src/strategies/portfolio_optimization.py remains as a compatibility shim, so
existing imports (including in forks) keep working unchanged.
"""

from src.strategies.portfolio.models import MarketOpportunity, PortfolioAllocation
from src.strategies.portfolio.optimizer import AdvancedPortfolioOptimizer
from src.strategies.portfolio.immediate import (
    _calculate_simple_kelly,
    _evaluate_immediate_trade,
    _get_fast_ai_prediction,
    create_market_opportunities_from_markets,
)
from src.strategies.portfolio.runner import run_portfolio_optimization

__all__ = [
    "MarketOpportunity",
    "PortfolioAllocation",
    "AdvancedPortfolioOptimizer",
    "create_market_opportunities_from_markets",
    "run_portfolio_optimization",
]
