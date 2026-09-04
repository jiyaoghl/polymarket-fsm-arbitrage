import threading
import time
from typing import Dict, Any, Optional
from polymarket.logger import logger
from polymarket.client import PolyClient
from polymarket import config

class RiskManager:
    """
    中央风控与资金拦截器（支持模拟盘与实盘双资金池隔离）。
    以单例模式运行。负责：
    1. 实盘模式：获取真实链上/CLOB USDC 可用余额，设置 95% 安全敞口上限。
    2. 模拟盘模式：使用配置的默认资金池（默认 100.0 USDC），独立管理模拟资金敞口。
    3. 对所有策略的发单进行额度预扣与生命周期全闭环释放。
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
        
        # 模拟盘资金池（默认 100.0 USDC）
        paper_cap = getattr(config, "PAPER_INITIAL_CAPITAL", 100.0)
        self.paper_max_exposure = float(paper_cap)
        self.paper_used_exposure = 0.0
        
        # 实盘资金池（从链上刷新真实余额）
        self.live_max_exposure = float(max_allowed_exposure)
        self.live_used_exposure = 0.0
        
        # 记录特定订单/市场锁定的额度
        self.locked_orders: Dict[str, float] = {}
        # 记录锁定项对应的模式（True: 实盘, False: 模拟盘）
        self.locked_is_live: Dict[str, bool] = {}
        # 记录已被策略锁定的活跃市场 (market_id -> strategy_id)，防止多策略内部互踩
        self.active_market_occupants: Dict[str, str] = {}
        
        # 统计面板指标
        self.total_intercepted_count = 0
        self.total_intercepted_amount = 0.0
        self.adaptive_retry_success = 0
        self.adaptive_retry_failed = 0
        
        # 紧急熔断与暂停标志
        self.is_emergency_halted = False
        
        # 原生 Gas 代币 (POL) 水位状态
        self.pol_balance: float = 0.0
        self.is_gas_starved: bool = False

        self.last_balance_refresh = 0.0
        
        # 策略级熔断保护数据 (Strategy Circuit Breakers)
        self._strategy_daily_stats: Dict[str, Dict[str, Any]] = {}
        self._strategy_cooldown_until: Dict[str, float] = {}
        self._last_stats_reset_day = time.localtime().tm_mday
        
        self._initialized = True
        logger.info(
            f"[风控中心] RiskManager 初始化完毕 | 模拟资金池: ${self.paper_max_exposure:.2f} | "
            f"实盘初始敞口: ${self.live_max_exposure:.2f}"
        )

    def is_market_occupied(
        self, market_id: str, strategy_id: str, is_live: bool = False
    ) -> tuple[bool, Optional[str]]:
        """
        检查指定市场是否已被其他策略锁定/占用 (单市场排他锁)。
        - 模拟盘 (is_live=False)：默认允许各策略在同一市场并发演练 (PAPER_MARKET_LOCK_ENABLED=false 时直接返回未占用)。
        - 实盘 (is_live=True)：强制执行单市场跨策略排他锁，杜绝多策略重复占用真钱与内部踩踏。
        """
        with self.lock:
            # 模拟盘且未开启模拟锁时，直接放行
            if not is_live and not getattr(config, "PAPER_MARKET_LOCK_ENABLED", False):
                return False, None

            occupant = self.active_market_occupants.get(market_id)
            if occupant is not None and occupant != strategy_id:
                return True, occupant
            return False, None

    @property
    def max_exposure(self) -> float:
        """向后兼容属性：优先返回实盘上限，无实盘时返回模拟上限。"""
        return self.live_max_exposure if self.live_max_exposure > 0 else self.paper_max_exposure

    @max_exposure.setter
    def max_exposure(self, value: float) -> None:
        """向后兼容设置项"""
        self.live_max_exposure = float(value)

    @property
    def used_exposure(self) -> float:
        """向后兼容属性：优先返回实盘已用，无实盘时返回模拟已用。"""
        return self.live_used_exposure if self.live_max_exposure > 0 else self.paper_used_exposure

    @used_exposure.setter
    def used_exposure(self, value: float) -> None:
        """向后兼容设置项"""
        self.live_used_exposure = float(value)

    def refresh_balance_from_chain(self, client: PolyClient, min_interval: float = 30.0) -> bool:
        """
        调用链上或 API 获取真实 USDC 余额，重置上限（支持时间间隔节流）。
        - 实盘模式：从链上/CLOB 获取真实余额并设置 95% 安全敞口上限。
        - 模拟盘模式：重置为默认模拟资金池 100U。
        """
        now = time.time()
        with self.lock:
            if now - self.last_balance_refresh < min_interval:
                return False
            self.last_balance_refresh = now

        if not client.is_live:
            with self.lock:
                paper_cap = getattr(config, "PAPER_INITIAL_CAPITAL", 100.0)
                self.paper_max_exposure = float(paper_cap)
                logger.info(f"[风控中心] 模拟盘模式：使用默认资金池 ${self.paper_max_exposure:.2f} 作为安全敞口上限")
                return True

        # 实盘模式：查询链上真实资产
        try:
            bal_info = client.get_balance()
            if bal_info and 'usdc' in bal_info:
                with self.lock:
                    real_usdc = float(bal_info['usdc'])
                    self.live_max_exposure = max(real_usdc * 0.95, 0.0)  # 保留 5% 缓冲
                    
                    # 检查 Native POL 原生 Gas 资产水位
                    pol_amt = float(bal_info.get('pol') or bal_info.get('matic') or 0.0)
                    self.pol_balance = pol_amt
                    if pol_amt < 0.1 and pol_amt > 0.0:
                        self.is_gas_starved = True
                        logger.error(
                            f"[风控中心] 【严重Gas警报】链上 POL 余额极度匮乏 (${pol_amt:.4f} POL < 0.1 POL)，"
                            f"已触发实盘保护性熔断，禁止开启新仓以保全已有持仓交割 Gas！"
                        )
                    elif pol_amt < 0.5 and pol_amt > 0.0:
                        self.is_gas_starved = False
                        logger.warning(
                            f"[风控中心] 【Gas低水位预警】链上 POL 余额偏低 (${pol_amt:.4f} POL < 0.5 POL)，请尽快补充燃料。"
                        )
                    else:
                        self.is_gas_starved = False

                    logger.info(
                        f"[风控中心] 实盘模式：从链上刷新真实 USDC 余额 ${real_usdc:.2f} 成功，"
                        f"设置实盘安全敞口上限为: ${self.live_max_exposure:.2f} | POL 水位: {self.pol_balance:.4f}"
                    )
                    return True
        except Exception as e:
            logger.error(f"[风控中心] 实盘获取链上余额失败: {e}")
        return False

    def acquire_trade_lock(
        self, strategy_id: str, market_id: str, amount: float, is_live: bool = False
    ) -> bool:
        """
        申请预扣资金锁。
        根据 is_live 分别从实盘资金池或模拟盘资金池中扣除。
        """
        with self.lock:
            # 实盘 Gas 匮乏保护拦截
            if is_live and self.is_gas_starved:
                logger.error(
                    f"[风控拦截] 实盘当前处于 Gas 匮乏保护熔断期 (POL={self.pol_balance:.4f} < 0.1)，"
                    f"拒绝策略 {strategy_id} 新开仓，防止交割失效！"
                )
                self.total_intercepted_count += 1
                self.total_intercepted_amount += amount
                return False

            lock_key = f"{strategy_id}_{market_id}"
            
            if is_live:
                # 实盘校验
                if self.live_used_exposure + amount <= self.live_max_exposure:
                    self.live_used_exposure += amount
                    self.locked_orders[lock_key] = self.locked_orders.get(lock_key, 0.0) + amount
                    self.locked_is_live[lock_key] = True
                    self.active_market_occupants[market_id] = strategy_id
                    logger.info(
                        f"[风控中心] [实盘] {lock_key} 成功申请额度 ${amount:.2f}。"
                        f"当前实盘使用: ${self.live_used_exposure:.2f}/${self.live_max_exposure:.2f}"
                    )
                    return True
                else:
                    self.total_intercepted_count += 1
                    self.total_intercepted_amount += amount
                    logger.warning(
                        f"[风控中心] 拒绝实盘申请！{strategy_id} 申请 ${amount:.2f}，"
                        f"导致实盘总敞口超限 (已用 {self.live_used_exposure:.2f} / 上限 {self.live_max_exposure:.2f})"
                    )
                    return False
            else:
                # 模拟盘校验 (默认 100U)
                if self.paper_used_exposure + amount <= self.paper_max_exposure:
                    self.paper_used_exposure += amount
                    self.locked_orders[lock_key] = self.locked_orders.get(lock_key, 0.0) + amount
                    self.locked_is_live[lock_key] = False
                    if getattr(config, "PAPER_MARKET_LOCK_ENABLED", False):
                        self.active_market_occupants[market_id] = strategy_id
                    logger.info(
                        f"[风控中心] [模拟盘] {lock_key} 成功申请额度 ${amount:.2f}。"
                        f"当前模拟使用: ${self.paper_used_exposure:.2f}/${self.paper_max_exposure:.2f}"
                    )
                    return True
                else:
                    self.total_intercepted_count += 1
                    self.total_intercepted_amount += amount
                    logger.warning(
                        f"[风控中心] 拒绝模拟盘申请！{strategy_id} 申请 ${amount:.2f}，"
                        f"导致模拟资金池超限 (已用 {self.paper_used_exposure:.2f} / 上限 {self.paper_max_exposure:.2f})"
                    )
                    return False

    def release_trade_lock(
        self, strategy_id: str, market_id: str, amount: float, is_live: Optional[bool] = None
    ) -> None:
        """
        释放预扣资金。
        在订单成交、被拒、撤单时按金额释放。
        """
        with self.lock:
            lock_key = f"{strategy_id}_{market_id}"
            current_locked = self.locked_orders.get(lock_key, 0.0)
            
            # 若未显式传入 is_live，则从记录中获取
            if is_live is None:
                is_live = self.locked_is_live.get(lock_key, False)
            
            # 防止过度释放
            release_amt = min(amount, current_locked)
            
            if release_amt > 0:
                if is_live:
                    self.live_used_exposure = max(0.0, self.live_used_exposure - release_amt)
                    logger.info(
                        f"[风控中心] [实盘] {lock_key} 释放额度 ${release_amt:.2f}。"
                        f"当前实盘总使用: ${self.live_used_exposure:.2f}/${self.live_max_exposure:.2f}"
                    )
                else:
                    self.paper_used_exposure = max(0.0, self.paper_used_exposure - release_amt)
                    logger.info(
                        f"[风控中心] [模拟盘] {lock_key} 释放额度 ${release_amt:.2f}。"
                        f"当前模拟总使用: ${self.paper_used_exposure:.2f}/${self.paper_max_exposure:.2f}"
                    )
                
                self.locked_orders[lock_key] -= release_amt
                if self.locked_orders[lock_key] <= 0:
                    self.locked_orders.pop(lock_key, None)
                    self.locked_is_live.pop(lock_key, None)

    def release_market_lock(
        self, strategy_id: str, market_id: str, is_live: Optional[bool] = None
    ) -> None:
        """
        显式清空指定策略在该市场占用的全部预扣额度。
        在状态机流转到终态 (SETTLED / FAILED / LOCKED) 或市场结算后无条件调用。
        """
        with self.lock:
            lock_key = f"{strategy_id}_{market_id}"
            current_locked = self.locked_orders.pop(lock_key, 0.0)
            if is_live is None:
                is_live = self.locked_is_live.pop(lock_key, False)
            else:
                self.locked_is_live.pop(lock_key, None)

            # 释放单市场跨策略排他锁
            if self.active_market_occupants.get(market_id) == strategy_id:
                self.active_market_occupants.pop(market_id, None)

            if current_locked > 0:
                if is_live:
                    self.live_used_exposure = max(0.0, self.live_used_exposure - current_locked)
                    logger.info(
                        f"[风控中心] [实盘全量清锁] {lock_key} 释放全部额度 ${current_locked:.2f}。"
                        f"当前实盘使用: ${self.live_used_exposure:.2f}/${self.live_max_exposure:.2f}"
                    )
                else:
                    self.paper_used_exposure = max(0.0, self.paper_used_exposure - current_locked)
                    logger.info(
                        f"[风控中心] [模拟全量清锁] {lock_key} 释放全部额度 ${current_locked:.2f}。"
                        f"当前模拟使用: ${self.paper_used_exposure:.2f}/${self.paper_max_exposure:.2f}"
                    )

    def record_adaptive_retry(self, success: bool) -> None:
        """记录微重试的成功或失败次数"""
        with self.lock:
            if success:
                self.adaptive_retry_success += 1
            else:
                self.adaptive_retry_failed += 1

    def get_status(self) -> Dict[str, Any]:
        """返回风控总览数据，供 Dashboard 展示（包含模拟与实盘双池状态）"""
        with self.lock:
            live_util = (self.live_used_exposure / self.live_max_exposure * 100) if self.live_max_exposure > 0 else 0
            paper_util = (self.paper_used_exposure / self.paper_max_exposure * 100) if self.paper_max_exposure > 0 else 0
            
            return {
                "live_max_exposure": self.live_max_exposure,
                "live_used_exposure": self.live_used_exposure,
                "live_utilization": live_util,
                "paper_max_exposure": self.paper_max_exposure,
                "paper_used_exposure": self.paper_used_exposure,
                "paper_utilization": paper_util,
                # 向下兼容旧字段
                "max_exposure": self.live_max_exposure if self.live_max_exposure > 0 else self.paper_max_exposure,
                "used_exposure": self.live_used_exposure if self.live_max_exposure > 0 else self.paper_used_exposure,
                "utilization": live_util if self.live_max_exposure > 0 else paper_util,
                "locked_orders": self.locked_orders.copy(),
                "total_intercepted_count": self.total_intercepted_count,
                "total_intercepted_amount": self.total_intercepted_amount,
                "adaptive_retry_success": self.adaptive_retry_success,
                "adaptive_retry_failed": self.adaptive_retry_failed,
            }

    def _reset_daily_stats_if_needed(self):
        import time
        current_day = time.localtime().tm_mday
        if getattr(self, "_last_stats_reset_day", None) != current_day:
            self._strategy_daily_stats.clear()
            self._strategy_cooldown_until.clear()
            self._last_stats_reset_day = current_day

    def record_trade_result(self, strategy_id: str, pnl: float, is_force_closed: bool) -> None:
        with self.lock:
            self._reset_daily_stats_if_needed()
            if strategy_id not in self._strategy_daily_stats:
                self._strategy_daily_stats[strategy_id] = {'loss': 0.0, 'consecutive_fc': 0}
            stats = self._strategy_daily_stats[strategy_id]
            stats['loss'] -= pnl
            if is_force_closed:
                stats['consecutive_fc'] += 1
            else:
                stats['consecutive_fc'] = 0

    def is_strategy_allowed(self, strategy_id: str, circuit_breaker_config: dict) -> bool:
        import time
        from polymarket.logger import logger
        with self.lock:
            self._reset_daily_stats_if_needed()
            now = time.time()
            if strategy_id in self._strategy_cooldown_until:
                if now < self._strategy_cooldown_until[strategy_id]:
                    return False
                else:
                    del self._strategy_cooldown_until[strategy_id]
                    if strategy_id in self._strategy_daily_stats:
                        self._strategy_daily_stats[strategy_id] = {'loss': 0.0, 'consecutive_fc': 0}
            if not circuit_breaker_config:
                return True
            stats = self._strategy_daily_stats.get(strategy_id)
            if not stats:
                return True
            max_fc = circuit_breaker_config.get('max_consecutive_force_closes', 3)
            max_loss = circuit_breaker_config.get('max_daily_loss_usdc', 10.0)
            cooldown_min = circuit_breaker_config.get('cooldown_minutes', 120)
            if stats['consecutive_fc'] >= max_fc or stats['loss'] >= max_loss:
                self._strategy_cooldown_until[strategy_id] = now + (cooldown_min * 60)
                logger.warning(f'[{strategy_id}] 触发策略熔断! 连续强平: {stats["consecutive_fc"]}, 当日亏损: {stats["loss"]:.2f}。冷却 {cooldown_min} 分钟。')
                return False
            return True
