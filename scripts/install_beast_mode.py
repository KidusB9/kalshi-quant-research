#!/usr/bin/env python3
"""
Beast Mode Installation Script 🚀

This script installs dependencies and validates the Beast Mode trading system.

Usage:
    python install_beast_mode.py
"""

import subprocess
import sys
import os
from pathlib import Path

def run_command(command, description):
    """Run a command and handle errors."""
    print(f"🔄 {description}...")
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        print(f"✅ {description} completed!")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed: {e.stderr}")
        return False

def main():
    print("🚀 BEAST MODE INSTALLATION SCRIPT 🚀")
    print("=" * 50)
    
    # Check Python version
    if sys.version_info < (3, 8):
        print(f"❌ Python 3.8+ required. Current: {sys.version}")
        return False
    
    print(f"✅ Python version: {sys.version.split()[0]}")
    
    # Install requirements
    if not run_command("pip install -e .", "Installing dependencies"):
        print("❌ Failed to install dependencies. Try manually: pip install scipy pandas numpy")
        return False
    
    # Test imports
    print("\n🧪 Testing Beast Mode components...")
    
    test_commands = [
        ("python -c 'import numpy; print(\"✅ NumPy:\", numpy.__version__)'", "NumPy"),
        ("python -c 'import scipy; print(\"✅ SciPy:\", scipy.__version__)'", "SciPy"),
        ("python -c 'import pandas; print(\"✅ Pandas:\", pandas.__version__)'", "Pandas"),
    ]
    
    for command, name in test_commands:
        if not run_command(command, f"Testing {name}"):
            return False
    
    # Test Beast Mode imports
    print("\n🚀 Testing Beast Mode system...")
    
    beast_test = """
try:
    from src.strategies.unified_trading_system import UnifiedAdvancedTradingSystem, TradingSystemConfig
    from src.jobs.trade import run_trading_job
    from src.strategies.portfolio_optimization import AdvancedPortfolioOptimizer
    from src.strategies.market_making import AdvancedMarketMaker
    print('✅ All Beast Mode components imported successfully!')
    print('🚀 BEAST MODE READY FOR DEPLOYMENT!')
except Exception as e:
    print(f'❌ Beast Mode import error: {e}')
    import traceback
    traceback.print_exc()
"""
    
    if not run_command(f'python -c "{beast_test}"', "Beast Mode system validation"):
        return False
    
    # Success message
    print("\n" + "🚀" * 20)
    print("BEAST MODE INSTALLATION COMPLETE!")
    print("🚀" * 20)
    
    print("\n📚 NEXT STEPS:")
    print("1. Run the dashboard: python beast_mode_dashboard.py --summary")
    print("2. Monitor costs: python cost_monitor.py")
    print("3. View performance: python beast_mode_dashboard.py")
    print("4. Run trading job: python -m src.main")
    print("\n✨ Welcome to Beast Mode Trading! ✨")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 