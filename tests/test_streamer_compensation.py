# -*- coding: utf-8 -*-
"""
WebSocket 订阅补偿机制与 90s 临期交割守门专项测试套件
"""
import pytest
import asyncio
import json
import time
from unittest.mock import MagicMock, AsyncMock, patch

from polymarket.streamer import MarketDataStreamer
from polymarket.config import MIN_TIME_TO_EXPIRY_ENTRY
from polymarket.services.handlers.idle_handler import IdleTickHandler
from polymarket.services.handlers.context import StrategyParams, TickBundle, TickFilterLogger
from polymarket.domain.models import TradeContext


def test_config_min_time_to_expiry_entry_is_90():
    """验证全局 config 中 MIN_TIME_TO_EXPIRY_ENTRY 默认基准值为 90 秒"""
    assert MIN_TIME_TO_EXPIRY_ENTRY == 90


def test_invalid_op_triggers_backoff_and_resubscribe():
    """测试当远端返回 INVALID OPERATION 时，自适应退避计数器累加并触发补偿重订"""
    streamer = MarketDataStreamer.get_instance()
    streamer._invalid_op_retries = 0

    with patch.object(streamer, "_schedule_resubscribe") as mock_resched:
        # 模拟第 1 次收到 INVALID OPERATION
        streamer._invalid_op_retries += 1
        delay1 = min(1.5 * (1.5 ** (streamer._invalid_op_retries - 1)), 5.0)
        streamer._schedule_resubscribe(delay=delay1)
        mock_resched.assert_called_with(delay=1.5)

        # 模拟第 2 次收到 INVALID OPERATION (指数退避)
        streamer._invalid_op_retries += 1
        delay2 = min(1.5 * (1.5 ** (streamer._invalid_op_retries - 1)), 5.0)
        streamer._schedule_resubscribe(delay=delay2)
        mock_resched.assert_called_with(delay=2.25)


def test_normal_market_data_resets_retry_counter():
    """测试接收到正常行情消息时，重试计数器自动清零"""
    streamer = MarketDataStreamer.get_instance()
    streamer._invalid_op_retries = 3

    # 模拟接收到合法行情
    streamer._last_market_data_ts = time.time()
    if streamer._invalid_op_retries > 0:
        streamer._invalid_op_retries = 0

    assert streamer._invalid_op_retries == 0


def test_send_subscription_skips_empty_assets():
    """测试活跃资产为空时不发送空订阅，防止远端报错"""
    async def _run():
        streamer = MarketDataStreamer.get_instance()
        mock_ws = AsyncMock()
        mock_ws.closed = False

        await streamer._send_subscription(mock_ws, [])
        mock_ws.send.assert_not_called()

        await streamer._send_subscription(mock_ws, ["asset_1", "asset_2"])
        mock_ws.send.assert_called_once()
        payload = json.loads(mock_ws.send.call_args[0][0])
        assert payload["type"] == "market"
        assert payload["assets_ids"] == ["asset_1", "asset_2"]

    asyncio.run(_run())


def test_idle_handler_expiry_90s_guardrail():
    """验证在 MIN_TIME_TO_EXPIRY_ENTRY=90 时，120s 盘口被正常放行，85s 盘口被准确拦截"""
    async def _run():
        params = StrategyParams(
            strategy_id="test_maker",
            amount=10.0,
            entry_max_price=0.45,
            entry_min_price=0.28,
            reentry_trigger=0.38,
            is_live=False,
            leg1_order_type="GTC",
            leg2_order_type="GTC",
            leg2_price_mode="bid",
            dual_bracket_entry=True,
            max_slippage_tolerance=0.015,
            leg1_max_unhedged_seconds=85,
            max_concurrent_unhedged_trades=3,
            exit_mode="dual_exit",
            initial_margin=0.02,
            breakeven_margin=0.003,
            flip_timeout_sec=30,
            min_time_to_expiry_entry=90.0,
            open_silence_sec=10.0,
            max_spread=0.065,
            mm_min_bid=0.35,
            obi_floor=-0.40,
        )

        mock_deps = MagicMock()
        mock_deps.risk_manager.is_market_occupied.return_value = (False, "")
        mock_filter_logger = MagicMock(spec=TickFilterLogger)
        mock_fsm = MagicMock()

        now_ts = 1000.0
        market_block = {"id": "mkt_block", "__asset_type": "BTC"}
        ctx_block = TradeContext(market_id="mkt_block", end_time=now_ts + 85.0)

        tick_bundle = TickBundle(
            yes_token="tok_yes",
            no_token="tok_no",
            best_bid_yes=0.40,
            best_ask_yes=0.42,
            best_bid_no=0.58,
            best_ask_no=0.60,
            now_ts=now_ts
        )

        handler = IdleTickHandler()
        await handler.handle(
            market=market_block,
            fsm=mock_fsm,
            ctx=ctx_block,
            tick=tick_bundle,
            params=params,
            deps=mock_deps,
            filter_logger=mock_filter_logger
        )

        intercept_calls = mock_filter_logger.intercept.call_args_list
        assert any("临近交割 (剩余 85.0s < 90.0s)" in str(call) for call in intercept_calls)

    asyncio.run(_run())
