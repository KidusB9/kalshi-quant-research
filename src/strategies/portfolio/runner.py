"""Entry point: run full portfolio optimization over eligible markets."""

from src.utils.database import DatabaseManager
from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.utils.logging_setup import get_trading_logger

from src.strategies.portfolio.models import PortfolioAllocation
from src.strategies.portfolio.optimizer import AdvancedPortfolioOptimizer
from src.strategies.portfolio.immediate import create_market_opportunities_from_markets


async def run_portfolio_optimization(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    xai_client: XAIClient
) -> PortfolioAllocation:
    """
    Main entry point for portfolio optimization.
    """
    logger = get_trading_logger("portfolio_optimization_main")
    
    try:
        # Initialize optimizer
        optimizer = AdvancedPortfolioOptimizer(db_manager, kalshi_client, xai_client)
        
        # Get markets
        markets = await db_manager.get_eligible_markets(
            volume_min=20000,  # Balanced volume for actual trading opportunities
            max_days_to_expiry=365  # Accept any timeline with dynamic exits
        )
        if not markets:
            logger.warning("No eligible markets for portfolio optimization")
            return optimizer._empty_allocation()
        
        # Convert to opportunities (no immediate trading in batch mode)
        opportunities = await create_market_opportunities_from_markets(
            markets, xai_client, kalshi_client, None, 0
        )
        
        if not opportunities:
            logger.warning("No valid opportunities for portfolio optimization")
            return optimizer._empty_allocation()
        
        logger.info(f"Running portfolio optimization on {len(opportunities)} opportunities")
        
        # Optimize portfolio
        allocation = await optimizer.optimize_portfolio(opportunities)
        
        logger.info(
            f"Portfolio optimization complete: "
            f"{len(allocation.allocations)} positions, "
            f"${allocation.total_capital_used:.0f} allocated"
        )
        
        return allocation
        
    except Exception as e:
        logger.error(f"Error in portfolio optimization: {e}")
        return AdvancedPortfolioOptimizer(db_manager, kalshi_client, xai_client)._empty_allocation() 
