import warnings

warnings.warn(
    "src.polymarket.backtest 属于初代静态回测原型，缺乏 2026 抛物线费率与真实订单簿深度。"
    "生产回测与参数寻优请迁移使用 src.polymarket.services.backtest 及 scripts/calibrate_params.py。",
    DeprecationWarning,
    stacklevel=2,
)

from .runner import run_backtest  # noqa: F401
