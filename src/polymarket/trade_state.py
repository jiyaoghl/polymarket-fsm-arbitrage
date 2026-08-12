import json
import os
import time
import threading
from typing import Dict, Any

from polymarket.logger import logger
from polymarket import paths


class TradeStateStore:
    """
    轻量级状态存储：
    - 按天记录累计盈亏与最大回撤
    - 可用于实现"每日最大回撤 5%→暂停机器人"的风控

    当前实现使用本地 JSON 文件，便于 Docker 映射卷进行持久化。
    
    改进：
    - 线程安全：使用 RLock 保护读写操作
    - 原子写入：使用临时文件 + rename 确保数据完整性
    """

    def __init__(self, path: str | None = None, initial_capital: float = 1000.0):
        if path is None:
            paths.tmp_dir().mkdir(parents=True, exist_ok=True)
            path = str(paths.tmp_dir() / "state.json")
        self.path = path
        self.initial_capital = initial_capital
        self._lock = threading.RLock()  # 线程安全锁
        self.state = self._load()

    def _today_key(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载状态文件失败: {e}")
            return {}

    def _save(self) -> None:
        """
        原子写入状态文件（包含 Windows 环境下的防占用重试机制）。
        
        使用临时文件 + rename 确保数据完整性：
        1. 先写入临时文件
        2. 成功后原子 rename 替换原文件
        """
        temp_path = f"{self.path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            
            # 原子操作：rename 在 POSIX 系统上是原子的
            # 在 Windows 环境下，如果有其他进程在读，os.replace 会触发 WinError 5
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    os.replace(temp_path, self.path)
                    break
                except PermissionError as pe:
                    if attempt < max_retries - 1:
                        time.sleep(0.1 * (2 ** attempt))
                    else:
                        raise pe
        except Exception as e:
            logger.error(f"保存状态文件失败: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def record_pnl(self, delta_usdc: float) -> None:
        """
        记录一笔盈亏。
        
        线程安全：使用锁保护读写操作。
        """
        with self._lock:
            key = self._today_key()
            day = self.state.get(
                key,
                {
                    "initial_capital": self.initial_capital,
                    "pnl": 0.0,
                    "max_drawdown": 0.0,
                },
            )
            day["pnl"] += delta_usdc
            equity = day["initial_capital"] + day["pnl"]
            drawdown = max(0.0, day["initial_capital"] - equity)
            day["max_drawdown"] = max(day["max_drawdown"], drawdown)
            self.state[key] = day
            self._save()

    def should_pause_for_drawdown(self, max_drawdown_pct: float = 0.05) -> bool:
        """
        判断是否触发每日最大回撤限制。
        
        线程安全：使用锁保护读操作。
        """
        with self._lock:
            key = self._today_key()
            day = self.state.get(key)
            if not day:
                return False
            dd = day.get("max_drawdown", 0.0)
            limit_amount = day["initial_capital"] * max_drawdown_pct
            return dd >= limit_amount

    def get_today_stats(self) -> Dict[str, Any]:
        """
        获取今日统计信息。
        
        Returns:
            包含 pnl, max_drawdown, equity 等信息的字典
        """
        with self._lock:
            key = self._today_key()
            day = self.state.get(key, {
                "initial_capital": self.initial_capital,
                "pnl": 0.0,
                "max_drawdown": 0.0,
            })
            equity = day["initial_capital"] + day["pnl"]
            return {
                "date": key,
                "initial_capital": day["initial_capital"],
                "pnl": day["pnl"],
                "equity": equity,
                "max_drawdown": day["max_drawdown"],
                "drawdown_pct": day["max_drawdown"] / day["initial_capital"] if day["initial_capital"] > 0 else 0.0,
            }

    def reset_today(self) -> None:
        """重置今日统计（用于测试或手动重置）。"""
        with self._lock:
            key = self._today_key()
            self.state[key] = {
                "initial_capital": self.initial_capital,
                "pnl": 0.0,
                "max_drawdown": 0.0,
            }
            self._save()