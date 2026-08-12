"""
扁平入口包：以 `src/` 为模块根的运行入口。

约定用法：
  - python -m apps.manager
  - python -m apps.market_scanner
  - python -m apps.ev_engine
  - python -m apps.order_executor
  - python -m apps.risk_guard
  - python -m apps.run_backtest
  - python -m apps.run_backtest_compare

实现说明：
真实实现位于 `polymarket.apps.*`，这里提供兼容桥接，保持扁平入口风格。
"""

"""
扁平入口包：以 `src/` 为模块根的运行入口。

约定用法：
  - python -m apps.manager
  - python -m apps.dashboard
  - python -m apps.run_backtest
等。

实现说明：
目前仓库内部实现仍位于 `polymarket.apps.*`，这里提供兼容桥接，
以便你逐步把 `src/polymarket/*` 物理移动到 `src/*` 时不破坏调用方。
"""

