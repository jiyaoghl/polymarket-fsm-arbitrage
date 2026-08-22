import threading
from typing import Dict, Any, Optional
from polymarket.logger import logger
from polymarket.client import PolyClient

class RiskManager:
    """
    中央风控与资金拦截器。
    以单例模式运行或挂载在全局 Manager 下。负责：
    1. 获取真实的链上 USDC 余额。
    2. 对所有的发单行为进行额度“预扣”，防止并发发单导致余额不足而遭遇 API 拒单。
    3. 管理挂单死锁的超时回收。
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(RiskManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, max_allowed_exposure: float = 0.0):
        # 避免多次初始化
        if hasattr(self, '_initialized') and self._initialized:
            return
            
        self.lock = threading.Lock()
        self.max_exposure = max_allowed_exposure # 如果为 0，则需要通过 client 刷新
        self.used_exposure = 0.0
        
        # 记录特定订单锁定的额度
        self.locked_orders: Dict[str, float] = {}
        
        # 统计面板指标
        self.total_intercepted_count = 0
        self.total_intercepted_amount = 0.0
        self.adaptive_retry_success = 0
        self.adaptive_retry_failed = 0
        
        self.last_balance_refresh = 0.0
        self._initialized = True
        logger.info(f"[风控中心] RiskManager 初始化完毕，初始硬性敞口上限: {self.max_exposure}")

    def refresh_balance_from_chain(self, client: PolyClient, min_interval: float = 30.0) -> bool:
        """调用链上或 API 获取真实 USDC 余额，重置上限（支持时间间隔节流）。"""
        import time
        now = time.time()
        with self.lock:
            if now - self.last_balance_refresh < min_interval:
                return False
            self.last_balance_refresh = now

        try:
            bal_info = client.get_balance()
            if bal_info and 'usdc' in bal_info:
                with self.lock:
                    real_usdc = float(bal_info['usdc'])
                    self.max_exposure = max(real_usdc * 0.95, 0.0)  # 保留 5% 缓冲
                    logger.info(f"[风控中心] 从链上刷新 USDC 余额成功。设置安全敞口上限为: ${self.max_exposure:.2f}")
                    return True
        except Exception as e:
            logger.error(f"[风控中心] 获取余额失败: {e}")
        return False

    def acquire_trade_lock(self, strategy_id: str, market_id: str, amount: float) -> bool:
        """
        申请预扣资金锁。
        在发单 (Taker 或 Maker) 前调用。
        """
        with self.lock:
            # 兼容：如果 max_exposure 被配置为 0，且没有链上环境，我们做假定放行，或者给予虚拟上限
            if self.max_exposure <= 0.0:
                self.max_exposure = 999999.0
                
            if self.used_exposure + amount <= self.max_exposure:
                self.used_exposure += amount
                lock_key = f"{strategy_id}_{market_id}"
                # 累加同一个市场的敞口
                self.locked_orders[lock_key] = self.locked_orders.get(lock_key, 0.0) + amount
                logger.info(f"[风控中心] {lock_key} 成功申请额度 ${amount:.2f}。当前总使用: ${self.used_exposure:.2f}/${self.max_exposure:.2f}")
                return True
            else:
                self.total_intercepted_count += 1
                self.total_intercepted_amount += amount
                logger.warning(f"[风控中心] 拒绝申请！{strategy_id} 申请 ${amount:.2f}，导致总敞口超限 (已用 {self.used_exposure:.2f} / 上限 {self.max_exposure:.2f})")
                return False

    def release_trade_lock(self, strategy_id: str, market_id: str, amount: float):
        """
        释放预扣资金。
        在订单成交、被拒、撤单时按金额释放。
        """
        with self.lock:
            lock_key = f"{strategy_id}_{market_id}"
            current_locked = self.locked_orders.get(lock_key, 0.0)
            
            # 防止过度释放
            release_amt = min(amount, current_locked)
            
            if release_amt > 0:
                self.used_exposure = max(0.0, self.used_exposure - release_amt)
                self.locked_orders[lock_key] -= release_amt
                if self.locked_orders[lock_key] <= 0:
                    del self.locked_orders[lock_key]
                logger.info(f"[风控中心] {lock_key} 释放额度 ${release_amt:.2f}。当前总使用: ${self.used_exposure:.2f}/${self.max_exposure:.2f}")

    def release_market_lock(self, strategy_id: str, market_id: str):
        """
        显式清空指定策略在该市场占用的全部预扣额度。
        在状态机流转到终态 (SETTLED / FAILED / LOCKED) 或市场结算后无条件调用。
        """
        with self.lock:
            lock_key = f"{strategy_id}_{market_id}"
            current_locked = self.locked_orders.pop(lock_key, 0.0)
            if current_locked > 0:
                self.used_exposure = max(0.0, self.used_exposure - current_locked)
                logger.info(f"[风控中心] [全量清锁] {lock_key} 释放全部锁定额度 ${current_locked:.2f}。当前总使用: ${self.used_exposure:.2f}/${self.max_exposure:.2f}")

    def record_adaptive_retry(self, success: bool):
        """记录微重试的成功或失败次数"""
        with self.lock:
            if success:
                self.adaptive_retry_success += 1
            else:
                self.adaptive_retry_failed += 1

    def get_status(self) -> Dict[str, Any]:
        """返回风控总览数据，供 Dashboard 展示"""
        with self.lock:
            return {
                "max_exposure": self.max_exposure,
                "used_exposure": self.used_exposure,
                "utilization": (self.used_exposure / self.max_exposure) * 100 if self.max_exposure > 0 else 0,
                "locked_orders": self.locked_orders.copy(),
                "total_intercepted_count": self.total_intercepted_count,
                "total_intercepted_amount": self.total_intercepted_amount,
                "adaptive_retry_success": self.adaptive_retry_success,
                "adaptive_retry_failed": self.adaptive_retry_failed
            }
