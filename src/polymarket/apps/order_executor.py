"""
订单执行器：监听 order_queue，执行 BTC 全自动交易与事件市场 Telegram 确认。

职责：
  - 每 2s 轮询 order_queue 中 pending 状态的订单
  - FAST  通道：直接调用 CLOB 下单（BTC 5min 套利逻辑）
  - CONFIRM 通道：发送 Telegram 确认按钮，等待用户点击后下单
  - 每次下单前检查 HALT/ORANGE/YELLOW lock
  - 下单成功后写入 positions 表

启动方式：
  python order_executor.py
"""
import time
import sys
import threading
import asyncio
import json
from pathlib import Path

from polymarket.config import (
    DB_PATH, CONFIRM_TIMEOUT_SEC,
    INITIAL_ENTRY_MAX_PRICE, REENTRY_TRIGGER_PRICE,
    STOP_LOSS_TIME_REMAINING, ORDER_AMOUNT,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
import polymarket.db as db
from polymarket.client import PolyClient, get_client
from polymarket.logger import logger
from polymarket import paths

try:
    from polymarket.notifier import Notifier
    notifier = Notifier()
except Exception:
    notifier = None

# Lock 目录
HALT_DIR    = paths.halt_dir()
HALT_LOCK   = HALT_DIR / "HALT.lock"
ORANGE_LOCK = HALT_DIR / "ORANGE.lock"
YELLOW_LOCK = HALT_DIR / "YELLOW.lock"

# 正在等待 Telegram 确认的 market_id 集合（防止重复推送）
_pending_confirm: set = set()
_confirm_lock = threading.Lock()


def check_halt_level() -> str:
    """返回当前最高熔断级别。"""
    if HALT_LOCK.exists():   return "HALT"
    if ORANGE_LOCK.exists(): return "ORANGE"
    if YELLOW_LOCK.exists(): return "YELLOW"
    return ""


def can_place_order(lane: str) -> bool:
    """
    根据熔断级别判断是否允许下单。

    HALT   → 所有下单拒绝
    ORANGE → 所有下单拒绝
    YELLOW → 仅允许 FAST（BTC 5min）
    ""（正常）→ 所有通道允许
    """
    level = check_halt_level()
    if level == "HALT":
        logger.critical("[OrderExecutor] HALT.lock 存在，拒绝下单，进程退出")
        sys.exit(0)
    if level == "ORANGE":
        logger.warning("[OrderExecutor] ORANGE.lock 存在，拒绝下单")
        return False
    if level == "YELLOW" and lane == "CONFIRM":
        logger.info("[OrderExecutor] YELLOW 状态，仅允许 BTC FAST 通道")
        return False
    return True


def execute_fast_lane(client: PolyClient, order: dict) -> bool:
    """
    FAST 通道执行（BTC 5min，直接下单）。

    复用现有 ArbitrageBot 的首腿逻辑：
      - 查询当前盘口最新价
      - 价格仍满足入场条件才下单
      - 下单成功后写 positions 表，并启动二腿 WS 监控

    Returns:
        True 表示成功触发下单（不代表最终成交）
    """
    market_id = order["market_id"]
    side      = order["side"]
    size_usdc = order["size_usdc"]

    # 从 market_candidates 读取 token 信息
    with db.get_conn(DB_PATH) as conn:
        row = conn.execute("""
            SELECT yes_token, no_token, expires_at FROM market_candidates
            WHERE market_id = ?
        """, (market_id,)).fetchone()

    if not row:
        logger.error(f"[OrderExecutor][FAST] 找不到 market_candidates: {market_id}")
        return False

    yes_token  = row["yes_token"]
    no_token   = row["no_token"]
    expires_at = row["expires_at"]
    token_id   = yes_token if side == "YES" else no_token

    # 重新获取当前价格（防止价格已变化）
    prices = client.get_market_price(token_id)
    if not prices:
        logger.warning(f"[OrderExecutor][FAST] 获取价格失败: {market_id}")
        return False

    entry_price = prices["ask"]
    if entry_price > INITIAL_ENTRY_MAX_PRICE:
        logger.info(
            f"[OrderExecutor][FAST] 价格已变化 ({entry_price:.4f} > {INITIAL_ENTRY_MAX_PRICE})，放弃"
        )
        return False

    # 执行首腿下单
    order_result = client.post_order(
        token_id=token_id,
        price=entry_price,
        amount=size_usdc,
        side="BUY",
        order_type="FOK",
    )
    if not order_result:
        logger.error(f"[OrderExecutor][FAST] 下单失败: {market_id}")
        return False

    logger.info(
        f"[OrderExecutor][FAST] 首腿成功 market={market_id} "
        f"side={side} price={entry_price:.4f} size={size_usdc:.2f} USDC"
    )

    # 写入 positions 表
    db.upsert_position({
        "market_id": market_id,
        "category":  "btc_5m",
        "side":      side,
        "yes_token": yes_token,
        "no_token":  no_token,
        "size_usdc": size_usdc,
        "entry_price": entry_price,
        "status":    "leg1_only",
        "expires_at": expires_at,
    }, path=DB_PATH)

    # 启动二腿 WS 监控（复用现有 ArbitrageBot 逻辑）
    _start_second_leg_monitor(client, market_id, side,
                              yes_token, no_token,
                              size_usdc, expires_at)
    return True


def _start_second_leg_monitor(client: PolyClient,
                              market_id: str, leg1_side: str,
                              yes_token: str, no_token: str,
                              size_usdc: float, expires_at: float) -> None:
    """
    在后台线程中运行二腿 WS 监控（复用 ArbitrageBot 的实现）。
    通过策略字典构造一个最小化的 ArbitrageBot 实例。
    """
    try:
        from polymarket.strategy_fsm import ArbitrageBotFSM as ArbitrageBot

        strategy_config = {
            "strategy_id": f"fast_{market_id[:8]}",
            "is_live":     client.is_live,
            "entry_max_price":  INITIAL_ENTRY_MAX_PRICE,
            "reentry_trigger":  REENTRY_TRIGGER_PRICE,
            "amount":           size_usdc,
        }
        bot = ArbitrageBot(strategy_config)

        # 手动注入已存在的首腿状态
        trade = {
            "market_id": market_id,
            "yes_token": yes_token,
            "no_token":  no_token,
            "end_time":  expires_at,
            "leg1": {
                "side":  leg1_side,
                "token": yes_token if leg1_side == "YES" else no_token,
                "cost":  INITIAL_ENTRY_MAX_PRICE,
                "size":  size_usdc,
            },
            "leg2":   None,
            "status": "leg1_only",
            "profit_usdc": 0.0,
            "created_at":  time.time(),
        }
        bot._set_trade(market_id, trade)

        # 在独立线程中启动二腿 WS 监控
        t = threading.Thread(
            target=lambda: asyncio.run(bot._ws_monitor_active_trade(market_id)),
            daemon=True,
            name=f"leg2_{market_id[:8]}",
        )
        t.start()
        logger.info(f"[OrderExecutor] 二腿 WS 监控已启动: {market_id}")

    except Exception as e:
        logger.exception(f"[OrderExecutor] 启动二腿监控失败: {e}")


def execute_confirm_lane(client: PolyClient, order: dict) -> None:
    """
    CONFIRM 通道：发送 Telegram 确认按钮，在子线程中等待回复。
    超时（CONFIRM_TIMEOUT_SEC）后自动标记为 expired。
    """
    market_id = order["market_id"]
    order_id  = order["id"]

    with _confirm_lock:
        if market_id in _pending_confirm:
            return  # 已推送，等待中
        _pending_confirm.add(market_id)

    def _wait_and_execute():
        try:
            result = "timeout"
            if notifier and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                try:
                    # 构造确认信息
                    with db.get_conn(DB_PATH) as conn:
                        c = conn.execute("""
                            SELECT question, ev_raw, volume
                            FROM market_candidates WHERE market_id=?
                        """, (market_id,)).fetchone()
                    question = c["question"] if c else market_id
                    ev_raw   = c["ev_raw"]   if c else 0.0
                    volume   = c["volume"]   if c else 0.0

                    market_info = {
                        "id":       market_id,
                        "question": question,
                        "ev_raw":   ev_raw,
                        "volume":   volume,
                        "side":     order["side"],
                        "score":    order.get("score", 0),
                    }
                    result = asyncio.run(notifier.send_confirm_request(
                        market_info=market_info,
                        size_usdc=order["size_usdc"],
                        timeout=CONFIRM_TIMEOUT_SEC,
                    ))
                except Exception as e:
                    logger.error(f"[OrderExecutor][CONFIRM] Telegram 通知失败: {e}")
                    result = "timeout"
            else:
                # 无 Telegram 配置，自动等待超时
                logger.warning(f"[OrderExecutor][CONFIRM] 无 Telegram 配置，{CONFIRM_TIMEOUT_SEC}s 后超时")
                time.sleep(CONFIRM_TIMEOUT_SEC)
                result = "timeout"

            if result == "confirmed":
                db.update_order_status(order_id, "confirmed", path=DB_PATH)
                # 立即执行下单
                if can_place_order("CONFIRM"):
                    _do_confirm_order(client, order)
            elif result == "rejected":
                db.update_order_status(order_id, "rejected", path=DB_PATH)
                logger.info(f"[OrderExecutor][CONFIRM] 用户拒绝: {market_id}")
            else:
                db.update_order_status(order_id, "expired", path=DB_PATH)
                logger.info(f"[OrderExecutor][CONFIRM] 已超时: {market_id}")

        finally:
            with _confirm_lock:
                _pending_confirm.discard(market_id)

    t = threading.Thread(target=_wait_and_execute, daemon=True,
                         name=f"confirm_{market_id[:8]}")
    t.start()


def _do_confirm_order(client: PolyClient, order: dict) -> None:
    """用户确认后执行实际下单并写入 positions。"""
    market_id = order["market_id"]
    side      = order["side"]
    size_usdc = order["size_usdc"]
    order_id  = order["id"]

    with db.get_conn(DB_PATH) as conn:
        row = conn.execute("""
            SELECT yes_token, no_token, expires_at, question, category
            FROM market_candidates WHERE market_id=?
        """, (market_id,)).fetchone()

    if not row:
        logger.error(f"[OrderExecutor][CONFIRM] 找不到 candidates: {market_id}")
        return

    yes_token  = row["yes_token"]
    no_token   = row["no_token"]
    expires_at = row["expires_at"]
    category   = row["category"] or "event"
    token_id   = yes_token if side == "YES" else no_token

    # 重新获取当前价格
    prices = client.get_market_price(token_id)
    entry_price = prices["ask"] if prices else order.get("entry_price", 0.5)

    result = client.post_order(
        token_id=token_id,
        price=entry_price,
        amount=size_usdc,
        side="BUY",
        order_type="GTC",
    )
    if not result:
        logger.error(f"[OrderExecutor][CONFIRM] 下单失败: {market_id}")
        db.update_order_status(order_id, "rejected", path=DB_PATH)
        return

    logger.info(
        f"[OrderExecutor][CONFIRM] 下单成功: {market_id} side={side} "
        f"price={entry_price:.4f} size={size_usdc:.2f}"
    )
    db.update_order_status(order_id, "done", path=DB_PATH)
    db.upsert_position({
        "market_id":   market_id,
        "category":    category,
        "side":        side,
        "yes_token":   yes_token,
        "no_token":    no_token,
        "size_usdc":   size_usdc,
        "entry_price": entry_price,
        "status":      "leg1_only",
        "expires_at":  expires_at,
    }, path=DB_PATH)


def process_pending_orders(client: PolyClient) -> None:
    """
    读取所有 pending 订单并处理。
    """
    orders = db.pop_pending_orders(path=DB_PATH)
    if not orders:
        return

    for order in orders:
        lane      = order.get("lane", "CONFIRM")
        market_id = order["market_id"]
        order_id  = order["id"]

        if not can_place_order(lane):
            continue

        if lane == "FAST":
            success = execute_fast_lane(client, order)
            status = "done" if success else "rejected"
            db.update_order_status(order_id, status, path=DB_PATH)

        elif lane == "CONFIRM":
            # 超时检查
            created_at = order.get("created_at", 0)
            if created_at and (time.time() - created_at) > CONFIRM_TIMEOUT_SEC:
                db.update_order_status(order_id, "expired", path=DB_PATH)
                logger.info(f"[OrderExecutor] CONFIRM 已过期: {market_id}")
                continue

            execute_confirm_lane(client, order)


def main():
    """OrderExecutor 主循环。"""
    HALT_DIR.mkdir(exist_ok=True)
    db.init_db(DB_PATH)
    logger.info("[OrderExecutor] 启动，轮询间隔=2s")

    client = get_client(is_live=False)

    while True:
        try:
            if HALT_LOCK.exists():
                logger.critical("[OrderExecutor] 检测到 HALT.lock，进程退出")
                sys.exit(0)
            process_pending_orders(client)
        except SystemExit:
            break
        except Exception as e:
            logger.exception(f"[OrderExecutor] 主循环异常: {e}")

        time.sleep(2)

    logger.info("[OrderExecutor] 进程退出")


if __name__ == "__main__":
    main()
