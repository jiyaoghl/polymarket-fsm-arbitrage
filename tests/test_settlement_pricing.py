import pytest
from unittest.mock import MagicMock
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.fsm import TradeState
from polymarket.client import PolyClient
from polymarket import config

def test_calculate_realized_pnl():
    """测试市价平仓已实现盈亏计算 (扣除双边手续费)"""
    leg1_cost = 0.25
    leg1_size = 40.0
    close_price = 0.08  # 止损价

    realized_pnl, gross_pnl, fee = AdaptiveLiquidatorService.calculate_realized_pnl(
        leg1_cost=leg1_cost,
        leg1_size=leg1_size,
        close_price=close_price,
        leg1_is_taker=True,
        close_is_taker=True
    )

    # 预期买入支出: 0.25 * 40 = 10.0 USDC
    # 预期卖出收入: 0.08 * 40 = 3.20 USDC
    # 毛亏损: 3.20 - 10.0 = -6.80 USDC
    assert gross_pnl == -6.80
    assert fee > 0
    assert realized_pnl < -6.80  # 扣费后实际亏损更大


def test_calculate_expiry_settled_pnl_win_and_loss():
    """测试二腿平仓失败直至到期时的最终交割结算价格与盈亏"""
    leg1_cost = 0.30
    leg1_size = 50.0

    # 1. 最终获胜 (settlement_price = 1.0)
    win_pnl, win_gross, win_fee = AdaptiveLiquidatorService.calculate_expiry_settled_pnl(
        leg1_cost=leg1_cost,
        leg1_size=leg1_size,
        settlement_price=1.0,
        leg1_is_taker=True
    )
    # 买入支出: 0.30 * 50 = 15.0 USDC
    # 到期交割收入: 1.0 * 50 = 50.0 USDC
    # 毛利润: 50.0 - 15.0 = 35.0 USDC
    assert win_gross == 35.0
    assert win_pnl == round(35.0 - win_fee, 4)

    # 2. 最终失败 (settlement_price = 0.0)
    loss_pnl, loss_gross, loss_fee = AdaptiveLiquidatorService.calculate_expiry_settled_pnl(
        leg1_cost=leg1_cost,
        leg1_size=leg1_size,
        settlement_price=0.0,
        leg1_is_taker=True
    )
    # 毛亏损: 0.0 - 15.0 = -15.0 USDC
    assert loss_gross == -15.0
    assert loss_pnl == round(-15.0 - loss_fee, 4)


def test_trade_context_settlement_fields_serialization():
    """测试 TradeContext 结算字段的完整序列化与反序列化"""
    ctx = TradeContext(
        market_id="0x_test_market",
        status="settled",
        profit_usdc=-6.85,
        realized_pnl=-6.85,
        settlement_price=0.08,
        settlement_type="FORCE_CLOSED"
    )

    data = ctx.to_dict()
    assert data["realized_pnl"] == -6.85
    assert data["settlement_price"] == 0.08
    assert data["settlement_type"] == "FORCE_CLOSED"

    restored = TradeContext.from_dict(data)
    assert restored.realized_pnl == -6.85
    assert restored.settlement_price == 0.08
    assert restored.settlement_type == "FORCE_CLOSED"
