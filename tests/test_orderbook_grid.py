import time
import pytest
from unittest.mock import MagicMock

from polymarket.services.grid import OrderbookMemoryGrid, OrderbookSnapshot
from polymarket.services.liquidator import AdaptiveLiquidatorService
from polymarket.domain.models import TradeContext, LegPosition

def test_grid_book_snapshot_and_ladder():
    """测试全量 book 消息正确构建 L2 单调深度档位"""
    grid = OrderbookMemoryGrid.get_instance()
    
    book_data = {
        "event_type": "book",
        "asset_id": "tok_test_1",
        "bids": [
            {"price": "0.45", "size": "100.0"},
            {"price": "0.44", "size": "200.0"},
            {"price": "0.40", "size": "500.0"},
        ],
        "asks": [
            {"price": "0.47", "size": "150.0"},
            {"price": "0.48", "size": "250.0"},
            {"price": "0.50", "size": "600.0"},
        ]
    }
    
    updated = grid.update_from_ws(book_data)
    assert "tok_test_1" in updated
    
    snap = grid.get_snapshot("tok_test_1")
    assert snap is not None
    assert snap.best_bid == 0.45
    assert snap.best_ask == 0.47
    assert snap.spread == 0.02
    assert snap.mid_price == 0.46
    assert len(snap.bids) == 3
    assert len(snap.asks) == 3
    assert not snap.is_stale(max_age_seconds=10.0)


def test_grid_price_change_reconciliation():
    """测试 price_change 增量消息触发时，L2 档位动态校准器修剪脏档位防止倒挂"""
    grid = OrderbookMemoryGrid.get_instance()
    
    # 初始快照：bids=[0.45, 0.44], asks=[0.47, 0.48]
    initial_book = {
        "event_type": "book",
        "asset_id": "tok_recon_1",
        "bids": [{"price": "0.45", "size": "100.0"}, {"price": "0.44", "size": "100.0"}],
        "asks": [{"price": "0.47", "size": "100.0"}, {"price": "0.48", "size": "100.0"}]
    }
    grid.update_from_ws(initial_book)
    
    # 收到 price_change：买盘上移至 best_bid=0.48，此时必须清除原 asks 中 <= 0.48 的倒挂卖单
    price_change_data = {
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "tok_recon_1", "best_bid": "0.48", "best_ask": "0.50"}
        ]
    }
    grid.update_from_ws(price_change_data)
    
    snap = grid.get_snapshot("tok_recon_1")
    assert snap is not None
    assert snap.best_bid == 0.48
    assert snap.best_ask == 0.50
    # 所有卖单必须 > 0.48
    for p, s in snap.asks:
        assert p > 0.48
    # 所有买单必须 < 0.50
    for p, s in snap.bids:
        assert p < 0.50


def test_grid_stale_data_guard():
    """测试时效性防爆盾 (超过 10s 未更新主动返回 None)"""
    grid = OrderbookMemoryGrid.get_instance()
    
    old_snap = OrderbookSnapshot(
        token_id="tok_stale_1",
        best_bid=0.45,
        best_ask=0.47,
        bids=((0.45, 100.0),),
        asks=((0.47, 100.0),),
        last_update_ts=time.time() - 15.0  # 15 秒前的数据
    )
    grid._books["tok_stale_1"] = old_snap
    
    assert old_snap.is_stale(max_age_seconds=10.0) is True
    # 本地 VWAP 应主动拒绝陈旧数据
    vwap = grid.calculate_bid_vwap_local("tok_stale_1", target_shares=10.0, max_staleness=10.0)
    assert vwap is None


def test_grid_local_vwap_calculation():
    """测试基于本地 L2 深度 0 网络 I/O 穿透 VWAP 加权均价"""
    grid = OrderbookMemoryGrid.get_instance()
    
    book_data = {
        "event_type": "book",
        "asset_id": "tok_vwap_1",
        "bids": [
            {"price": "0.45", "size": "10.0"},   # 10 份 @ 0.45
            {"price": "0.40", "size": "10.0"},   # 10 份 @ 0.40
        ],
        "asks": [
            {"price": "0.50", "size": "10.0"},
            {"price": "0.55", "size": "10.0"},
        ]
    }
    grid.update_from_ws(book_data)
    
    # 卖出 15 份：前 10 份 @ 0.45, 后 5 份 @ 0.40 -> 总金额 4.5 + 2.0 = 6.5 / 15 = 0.4333
    bid_vwap = grid.calculate_bid_vwap_local("tok_vwap_1", target_shares=15.0)
    assert bid_vwap is not None
    assert round(bid_vwap, 4) == round(6.5 / 15.0, 4)
    
    # 买入 15 份：前 10 份 @ 0.50, 后 5 份 @ 0.55 -> 总金额 5.0 + 2.75 = 7.75 / 15 = 0.5167
    ask_vwap = grid.calculate_ask_vwap_local("tok_vwap_1", target_shares=15.0)
    assert ask_vwap is not None
    assert round(ask_vwap, 4) == round(7.75 / 15.0, 4)


def test_liquidator_using_local_grid():
    """测试强平引擎优先使用本地 OrderbookMemoryGrid 0 网络 I/O 平仓"""
    grid = OrderbookMemoryGrid.get_instance()
    
    # 写入最新深度
    token_id = "tok_liq_test"
    book_data = {
        "event_type": "book",
        "asset_id": token_id,
        "bids": [{"price": "0.42", "size": "100.0"}],
        "asks": [{"price": "0.48", "size": "100.0"}]
    }
    grid.update_from_ws(book_data)
    
    mock_client = MagicMock()
    # 模拟 post_order 成功
    mock_client.post_order.return_value = {
        "status": "FILLED",
        "orderID": "0x_close_order_123",
        "price": 0.42
    }
    
    ctx = TradeContext(
        market_id="m_liq",
        status="leg1_only",
        leg1=LegPosition(token=token_id, side="BUY", cost=0.45, size=20.0, order_id="ord_1")
    )
    
    success, close_price, size, order_id = AdaptiveLiquidatorService.execute_force_close(
        mock_client, ctx, strategy_id="test_strat"
    )
    
    assert success is True
    assert close_price == 0.42
    assert size == 20.0
    assert order_id == "0x_close_order_123"
    # mock_client.get_orderbook 不应该被调用（因为优先命中了本地网格）
    assert not mock_client.get_orderbook.called
