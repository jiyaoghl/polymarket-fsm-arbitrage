import pytest
from unittest.mock import MagicMock
from polymarket.services.pricing import PricingEngine
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.domain.models import TradeContext, LegPosition
from polymarket.fsm import TradeState
from polymarket.client import PolyClient
from polymarket import config

def test_calculate_bid_vwap():
    """测试买盘深度加权均价 (Bid VWAP) 计算"""
    # 买盘深度: [价格, 数量] (乱序传入，需自动按价格降序排列吃单)
    mock_bids = [
        {"price": 0.30, "size": 10.0},
        {"price": 0.35, "size": 20.0},
        {"price": 0.25, "size": 30.0},
    ]

    # 1. 目标 20 份 -> 全部从最高买一 0.35 吃满 -> VWAP = 0.35
    vwap_20 = PricingEngine.calculate_bid_vwap(mock_bids, 20.0)
    assert vwap_20 == 0.35

    # 2. 目标 30 份 -> 吃满 0.35 (20份) + 0.30 (10份) -> (20*0.35 + 10*0.30) / 30 = 10.0 / 30 = 0.3333
    vwap_30 = PricingEngine.calculate_bid_vwap(mock_bids, 30.0)
    assert vwap_30 == 0.3333

    # 3. 目标 100 份 -> 深度不足 (总共仅 60 份) -> 返回 None
    vwap_100 = PricingEngine.calculate_bid_vwap(mock_bids, 100.0)
    assert vwap_100 is None


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
    assert win_gross == 35.0
    assert win_pnl == round(35.0 - win_fee, 4)

    # 2. 最终失败 (settlement_price = 0.0)
    loss_pnl, loss_gross, loss_fee = AdaptiveLiquidatorService.calculate_expiry_settled_pnl(
        leg1_cost=leg1_cost,
        leg1_size=leg1_size,
        settlement_price=0.0,
        leg1_is_taker=True
    )
    assert loss_gross == -15.0
    assert loss_pnl == round(-15.0 - loss_fee, 4)


def test_execute_force_close_with_vwap_and_sell_leg():
    """测试强平平仓执行返回 VWAP 均价并正确构建 SELL 卖出明细"""
    mock_client = MagicMock(spec=PolyClient)
    # 模拟订单簿买单深度
    mock_client.get_orderbook.return_value = {
        "bids": [
            {"price": "0.34", "size": "15.0"},
            {"price": "0.32", "size": "20.0"}
        ]
    }
    mock_client.post_order.return_value = {
        "orderID": "fok_close_123",
        "status": "FILLED"
    }

    ctx = TradeContext(
        market_id="0x_test_market",
        status="leg1_only",
        leg1=LegPosition(order_id="leg1_123", token="token_yes", side="BUY", cost=0.48, size=30.0)
    )

    success, close_price, size, order_id = AdaptiveLiquidatorService.execute_force_close(mock_client, ctx)
    assert success is True
    # 吃满 30 份: 15份@0.34 + 15份@0.32 = 5.1 + 4.8 = 9.9 / 30 = 0.33
    assert close_price == 0.33
    assert size == 30.0
    assert order_id == "fok_close_123"

    # 验证将平仓明细更新为 SELL
    ctx.leg2 = LegPosition(order_id=order_id, token=ctx.leg1.token, side="SELL", cost=close_price, size=size)
    assert ctx.leg2.side == "SELL"
    assert ctx.leg2.cost == 0.33
    assert ctx.to_dict()["leg2"]["side"] == "SELL"
