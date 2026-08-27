import pytest
import time
from polymarket.services.pricing import PricingEngine
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.domain.models import TradeContext, LegPosition

def test_decayed_margin_calculation():
    """测试时间衰减利润率计算"""
    # 0s 处应为初始利润 2.5%
    m0 = PricingEngine.calculate_decayed_margin(0.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert m0 == 0.025

    # 15s 处按幂律平滑衰减 (保持较高利润)
    m15 = PricingEngine.calculate_decayed_margin(15.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert round(m15, 4) == round(0.025 - ((0.5 ** 1.8) * 0.023), 4)

    # 30s 处应衰减至保本门槛 0.2%
    m30 = PricingEngine.calculate_decayed_margin(30.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert m30 == 0.002

    # 45s (超时) 仍应锁定在保本门槛 0.2%
    m45 = PricingEngine.calculate_decayed_margin(45.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert m45 == 0.002


def test_flip_sell_price_calculation():
    """测试同向做 T 限价卖出价格计算"""
    leg1_cost = 0.420
    # 初始时刻卖价应高于成本 + 手续费
    p0 = PricingEngine.calculate_flip_sell_price(leg1_cost, elapsed_seconds=0.0, initial_margin=0.025, decay_duration=30.0)
    assert p0 > leg1_cost
    assert p0 > 0.44

    # 随着时间推移，卖价单调下调至保本卖价
    p30 = PricingEngine.calculate_flip_sell_price(leg1_cost, elapsed_seconds=30.0, initial_margin=0.025, min_margin=0.002, decay_duration=30.0)
    assert p30 < p0
    # 扣除手续费后保本卖价仍略高于买入纯成本
    assert p30 >= leg1_cost


def test_hedged_pair_price_calculation():
    """测试反向配对买入价格计算"""
    leg1_cost = 0.420
    pair_p = PricingEngine.calculate_hedged_pair_price(leg1_cost, elapsed_seconds=0.0, initial_margin=0.025, min_margin=0.002)
    # 两腿成本之和必须小于等于 1.0 - 利润
    assert pair_p + leg1_cost < 1.0


def test_trade_context_smart_exit_serialization():
    """测试 TradeContext 中 exit_mode 序列化与还原"""
    ctx = TradeContext(
        market_id="0x_test_exit",
        exit_mode="smart_flip",
        exit_stage="flip_active"
    )
    d = ctx.to_dict()
    assert d["exit_mode"] == "smart_flip"
    assert d["exit_stage"] == "flip_active"

    restored = TradeContext.from_dict(d)
    assert restored.exit_mode == "smart_flip"
    assert restored.exit_stage == "flip_active"


def test_smart_flip_realized_pnl_calculation():
    """测试做 T 卖出成功后的 Realized PnL 核算"""
    leg1_cost = 0.420
    leg1_size = 20.0
    sell_price = 0.450

    realized_pnl, gross_pnl, fee = AdaptiveLiquidatorService.calculate_realized_pnl(
        leg1_cost=leg1_cost, leg1_size=leg1_size,
        close_price=sell_price, leg1_is_taker=True, close_is_taker=False
    )

    # 收入 = 20 * 0.45 = 9.0，成本 = 20 * 0.42 = 8.4，毛利 = 0.60
    assert round(gross_pnl, 2) == 0.60
    assert fee > 0  # 扣除了开仓 Taker 手续费
    assert realized_pnl > 0  # 净利润锁定为正


def test_dual_exit_serialization():
    """测试 dual_exit 模式与 dual_orders 序列化与还原"""
    ctx = TradeContext(
        market_id="0x_test_dual",
        exit_mode="dual_exit",
        exit_stage="dual_active",
        dual_orders=[
            {"token_id": "tok_yes", "price": 0.45, "size": 20.0, "side": "SELL", "orderID": "ord_1"},
            {"token_id": "tok_no", "price": 0.54, "size": 20.0, "side": "BUY", "orderID": "ord_2"}
        ]
    )
    d = ctx.to_dict()
    assert d["exit_mode"] == "dual_exit"
    assert len(d["dual_orders"]) == 2

    restored = TradeContext.from_dict(d)
    assert restored.exit_mode == "dual_exit"
    assert len(restored.dual_orders) == 2
    assert restored.dual_orders[0]["side"] == "SELL"
    assert restored.dual_orders[1]["side"] == "BUY"
