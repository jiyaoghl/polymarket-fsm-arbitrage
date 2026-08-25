import json
from typing import List, Optional, Dict, Any

from polymarket.logger import logger
from polymarket.config import DB_PATH
from polymarket.domain.models import TradeContext
from polymarket.fsm import TradeState
import polymarket.db as db_ops

class TradeRepository:
    """
    SQLite 交易仓储服务 (Repository Pattern)。
    统一管理 active_trades_cache、historical_trades 的冷热分离与崩溃热恢复。
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        db_ops.init_db(self.db_path)

    def save_active_trade(self, strategy_id: str, context: TradeContext):
        """持久化活跃交易状态快照（仅保存已开仓/已发生资金动作的活跃单）"""
        try:
            # IDLE 状态仅为盘口监听，不占用持久化活跃持仓表
            if context.status == TradeState.IDLE.value:
                return
            trade_json = json.dumps(context.to_dict())
            db_ops.upsert_trade_cache(context.market_id, strategy_id, trade_json, self.db_path)
        except Exception as e:
            logger.warning(f"[仓储服务：{strategy_id}] 保存 active_trades_cache 异常 ({context.market_id}): {e}")

    def archive_trade(self, strategy_id: str, context: TradeContext):
        """归档已终态的交易并清理活跃缓存"""
        try:
            trade_dict = context.to_dict()
            ev = float(trade_dict.get("profit_usdc", 0.0))
            st_type = trade_dict.get("settlement_type") or "HEDGED_LOCKED"
            trade_json = json.dumps(trade_dict)
            
            db_ops.archive_trade(context.market_id, strategy_id, trade_json, ev, self.db_path)
            db_ops.delete_trade_cache(context.market_id, strategy_id, self.db_path)
            
            label = "套利锁盈EV" if st_type == "HEDGED_LOCKED" else f"结算盈亏({st_type})"
            logger.info(f"[仓储服务：{strategy_id}] 成功归档终态交易: {context.market_id}, {label}: ${ev:.4f}")
        except Exception as e:
            logger.warning(f"[仓储服务：{strategy_id}] 归档交易异常 ({context.market_id}): {e}")

    def recover_unhedged_trades(self, strategy_id: str, is_live: bool) -> List[TradeContext]:
        """
        开机崩溃恢复：提取所有真正处于单边/未终态的持仓。
        模拟盘自动清空无意义历史缓存；实盘恢复未过期的单边敞口并清理 IDLE/过期废弃缓存。
        """
        recovered: List[TradeContext] = []
        try:
            caches = db_ops.get_all_trade_caches(strategy_id, self.db_path)
            
            # 模拟盘环境下，历史单边敞口无实物支撑，直接清空
            if not is_live:
                for cache in caches:
                    db_ops.delete_trade_cache(cache["market_id"], strategy_id, self.db_path)
                if caches:
                    logger.info(f"[仓储服务：{strategy_id}] (模拟盘) 已清理 {len(caches)} 个历史缓存。")
                return []

            import time
            now_ts = time.time()

            for cache in caches:
                market_id = cache["market_id"]
                try:
                    trade_data = json.loads(cache["trade_json"])
                    status_str = trade_data.get("status")
                    end_time = float(trade_data.get("end_time") or 0)

                    # 如果已是终态或纯 IDLE 状态，直接清理
                    if status_str in (TradeState.LOCKED.value, TradeState.SETTLED.value, TradeState.FAILED.value, TradeState.IDLE.value):
                        db_ops.delete_trade_cache(market_id, strategy_id, self.db_path)
                        continue

                    # 如果市场已经交割过期超过 300 秒，清理该死仓
                    if end_time > 0 and (now_ts - end_time > 300):
                        logger.warning(f"[仓储服务：{strategy_id}] 历史市场 {market_id} 已过期交割，清理其残余缓存。")
                        db_ops.delete_trade_cache(market_id, strategy_id, self.db_path)
                        continue

                    ctx = TradeContext.from_dict(trade_data)
                    recovered.append(ctx)
                    logger.info(f"[仓储服务：{strategy_id}] 从 DB 恢复未对冲敞口: {market_id}, 状态: {status_str}")
                except Exception as e:
                    logger.warning(f"[仓储服务：{strategy_id}] 解析恢复敞口异常 ({market_id}): {e}")


        except Exception as e:
            logger.warning(f"[仓储服务：{strategy_id}] 恢复历史敞口失败: {e}")

        return recovered
