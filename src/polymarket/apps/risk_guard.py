"""
风控守卫（独立进程）：每 RISK_CHECK_INTERVAL 秒执行一次完整检查。

职责：
  - 三级熔断（黄/橙/红），写 lock 文件制动其他进程
  - 单市场仓位监控
  - 到期市场自动 redeem
  - 回撤恢复时自动解除黄/橙锁

熔断级别：
  🟡 YELLOW (亏损≥10%): 暂停新市场发现，BTC 5min 可继续
  🟠 ORANGE (亏损≥20%): 停止所有新开仓，平未对冲单腿
  🔴 HALT   (亏损≥30%): 全仓平仓，写 HALT.lock，进程全部退出

启动方式：
  python risk_guard.py
"""
import time
import sys
from pathlib import Path

from polymarket.config import (
    INITIAL_CAPITAL, DB_PATH, RISK_CHECK_INTERVAL,
    DRAWDOWN_YELLOW, DRAWDOWN_ORANGE, DRAWDOWN_RED,
    MAX_SINGLE_MARKET_PCT, MAX_OPEN_MARKETS,
)
import polymarket.db as db
from polymarket.client import PolyClient
from polymarket.logger import logger
from polymarket import paths

try:
    from polymarket.notifier import Notifier
    notifier = Notifier()
except Exception:
    notifier = None

# Lock 文件路径
HALT_DIR = paths.halt_dir()
HALT_LOCK   = HALT_DIR / "HALT.lock"
ORANGE_LOCK = HALT_DIR / "ORANGE.lock"
YELLOW_LOCK = HALT_DIR / "YELLOW.lock"


def write_lock(level: str, reason: str = "") -> None:
    """写入熔断 lock 文件。"""
    HALT_DIR.mkdir(exist_ok=True)
    lock_map = {"HALT": HALT_LOCK, "ORANGE": ORANGE_LOCK, "YELLOW": YELLOW_LOCK}
    lock_path = lock_map.get(level)
    if lock_path and not lock_path.exists():
        lock_path.write_text(
            f"level={level}\nreason={reason}\ntime={time.strftime('%Y-%m-%d %Human:%M:%S')}\n",
            encoding="utf-8",
        )
        logger.warning(f"[RiskGuard] 写入 {level}.lock: {reason}")


def clear_lock(level: str) -> None:
    """清除熔断 lock 文件（恢复时使用，不清除 HALT）。"""
    if level == "HALT":
        return  # 红牌必须人工清除
    lock_map = {"ORANGE": ORANGE_LOCK, "YELLOW": YELLOW_LOCK}
    lock_path = lock_map.get(level)
    if lock_path and lock_path.exists():
        lock_path.unlink()
        logger.info(f"[RiskGuard] 清除 {level}.lock（回撤已恢复）")


def current_halt_level() -> str:
    """读取当前最高熔断级别。"""
    if HALT_LOCK.exists():   return "HALT"
    if ORANGE_LOCK.exists(): return "ORANGE"
    if YELLOW_LOCK.exists(): return "YELLOW"
    return ""


def send_alert(msg: str, urgent: bool = False) -> None:
    """发送告警通知。"""
    logger.warning(f"[RiskGuard] 告警: {msg}")
    if notifier:
        try:
            notifier.send_simple_alert(msg, urgent=urgent)
        except Exception as e:
            logger.error(f"[RiskGuard] 发送通知失败: {e}")


def force_close_all_positions(client: PolyClient) -> None:
    """
    红牌熔断：强制平仓所有未结算持仓（市价 SELL）。
    """
    positions = db.get_open_positions(path=DB_PATH)
    for pos in positions:
        market_id = pos["market_id"]
        side = pos.get("side", "")
        # 确定要卖出的 token
        if side == "YES":
            token_id = pos.get("yes_token", "")
        else:
            token_id = pos.get("no_token", "")

        if not token_id:
            continue

        logger.warning(f"[RiskGuard] 红牌平仓: {market_id} 卖出 {side}")
        try:
            client.post_order(
                token_id=token_id,
                price=0.01,          # 接受极端滑点
                amount=pos.get("size_usdc", 0),
                side="SELL",
                order_type="FOK",
            )
        except Exception as e:
            logger.error(f"[RiskGuard] 平仓失败 {market_id}: {e}")

        db.mark_position_settled(market_id, pnl_usdc=0.0, path=DB_PATH)


def close_unhedged_positions(client: PolyClient) -> None:
    """
    橙牌限流：平掉所有未对冲（status='leg1_only'）的单腿持仓。
    """
    positions = db.get_open_positions(path=DB_PATH)
    for pos in positions:
        if pos.get("status") != "leg1_only":
            continue

        market_id = pos["market_id"]
        side = pos.get("side", "")
        token_id = pos.get("yes_token") if side == "YES" else pos.get("no_token")
        if not token_id:
            continue

        logger.warning(f"[RiskGuard] 橙牌平未对冲仓: {market_id}")
        try:
            client.post_order(
                token_id=token_id,
                price=0.01,
                amount=pos.get("size_usdc", 0),
                side="SELL",
                order_type="FOK",
            )
        except Exception as e:
            logger.error(f"[RiskGuard] 橙牌平仓失败 {market_id}: {e}")

        db.mark_position_settled(market_id, pnl_usdc=0.0, path=DB_PATH)


def check_drawdown(client: PolyClient) -> None:
    """
    检查实时亏损并触发相应熔断级别。

    计算方式：亏损比例 = (INITIAL_CAPITAL - 当前余额) / INITIAL_CAPITAL
    注：当前余额从 CLOB API 查询（包含 pending）。
    """
    balance_info = client.get_balance()
    balance = balance_info.get("usdc", INITIAL_CAPITAL)
    loss = INITIAL_CAPITAL - balance
    loss_pct = loss / INITIAL_CAPITAL if INITIAL_CAPITAL > 0 else 0.0

    level = current_halt_level()

    if loss_pct >= DRAWDOWN_RED:
        if level != "HALT":
            logger.critical(f"[RiskGuard] 🔴 红牌熔断！亏损 {loss_pct*100:.1f}%，余额={balance:.2f}")
            write_lock("HALT", reason=f"亏损 {loss_pct*100:.1f}%")
            send_alert(f"🔴 红牌熔断！亏损 {loss_pct*100:.1f}%，所有持仓已市价平仓，系统停止。", urgent=True)
            force_close_all_positions(client)
            logger.critical("[RiskGuard] 已触发全仓平仓，进程退出")
            sys.exit(0)

    elif loss_pct >= DRAWDOWN_ORANGE:
        if level not in ("HALT", "ORANGE"):
            write_lock("ORANGE", reason=f"亏损 {loss_pct*100:.1f}%")
            send_alert(f"🟠 橙牌限流！亏损 {loss_pct*100:.1f}%，已停止新开仓。", urgent=True)
            close_unhedged_positions(client)
        elif level == "YELLOW":
            # 从黄牌升级为橙牌
            clear_lock("YELLOW")
            write_lock("ORANGE", reason=f"亏损升级 {loss_pct*100:.1f}%")

    elif loss_pct >= DRAWDOWN_YELLOW:
        if level == "":
            write_lock("YELLOW", reason=f"亏损 {loss_pct*100:.1f}%")
            send_alert(f"⚠️ 黄牌警告！亏损 {loss_pct*100:.1f}%，暂停发现新市场。")

    else:
        # 回撤恢复，自动解除黄/橙锁
        if level == "YELLOW":
            clear_lock("YELLOW")
            send_alert(f"✅ 回撤恢复 ({loss_pct*100:.1f}%)，黄牌已解除")
        elif level == "ORANGE":
            clear_lock("ORANGE")
            send_alert(f"✅ 回撤恢复 ({loss_pct*100:.1f}%)，橙牌已解除")


def check_and_redeem_expired(client: PolyClient) -> None:
    """自动结算到期未结算的持仓。"""
    expired = db.get_expired_unsettled(path=DB_PATH)
    for pos in expired:
        market_id = pos["market_id"]
        logger.info(f"[RiskGuard] 自动 redeem: {market_id}")
        try:
            result = client.redeem(market_id)
            pnl = float(result.get("payout", 0.0)) - pos.get("size_usdc", 0.0)
            db.mark_position_settled(market_id, pnl_usdc=pnl, path=DB_PATH)
            logger.info(f"[RiskGuard] redeem 成功: {market_id} pnl={pnl:.4f}")
        except Exception as e:
            logger.error(f"[RiskGuard] redeem 失败 {market_id}: {e}")


def print_status(client: PolyClient) -> None:
    """每轮结束时打印简要状态（便于观察）。"""
    balance_info = client.get_balance()
    balance = balance_info.get("usdc", 0.0)
    open_count = db.count_open_positions(path=DB_PATH)
    used_usdc = db.sum_open_positions_usdc(path=DB_PATH)
    level = current_halt_level() or "正常"
    loss_pct = (INITIAL_CAPITAL - balance) / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL > 0 else 0

    logger.info(
        f"[RiskGuard] 状态 | 余额={balance:.2f} USDC | "
        f"亏损={loss_pct:.1f}% | 持仓={open_count}/{MAX_OPEN_MARKETS} | "
        f"在用={used_usdc:.2f} | 熔断={level}"
    )


def main():
    """RiskGuard 主循环。最先启动，不依赖其他模块的初始化。"""
    HALT_DIR.mkdir(exist_ok=True)
    db.init_db(DB_PATH)
    logger.info(f"[RiskGuard] 启动，检查间隔={RISK_CHECK_INTERVAL}s，"
                f"初始资金={INITIAL_CAPITAL} USDC")

    # 使用模拟模式仅用于开发（实盘时 is_live=True）
    client = PolyClient(is_live=False)

    while True:
        try:
            check_drawdown(client)
            check_and_redeem_expired(client)
            print_status(client)
        except SystemExit:
            break
        except Exception as e:
            logger.exception(f"[RiskGuard] 主循环异常: {e}")

        time.sleep(RISK_CHECK_INTERVAL)

    logger.info("[RiskGuard] 进程退出")


if __name__ == "__main__":
    main()
