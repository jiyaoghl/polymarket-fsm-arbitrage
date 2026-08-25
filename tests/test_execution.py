import pytest
from polymarket.services.execution import OrderExecutionService

def test_sanitize_order_params_normal():
    # 投入 $10.0 USDC，价格 0.40 -> 25.0 份
    price, shares = OrderExecutionService.sanitize_order_params(0.40, 10.0)
    assert price == 0.40
    assert shares == 25.0

def test_sanitize_order_params_min_shares_clamp():
    # 投入 $1.0 USDC，价格 0.50 -> 折算 2.0 份，强制钳制到 5.0 份
    price, shares = OrderExecutionService.sanitize_order_params(0.50, 1.0)
    assert price == 0.50
    assert shares == 5.0

def test_sanitize_order_params_price_clamp():
    # 极端价格边界钳制
    price, shares = OrderExecutionService.sanitize_order_params(0.00001, 10.0)
    assert price == 0.001
    price_high, _ = OrderExecutionService.sanitize_order_params(1.20, 10.0)
    assert price_high == 0.999
