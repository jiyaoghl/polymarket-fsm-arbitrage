import abc
import asyncio
from typing import Dict, List, Optional, Any, Tuple

class ITradingGateway(abc.ABC):
    """
    统一交易网关抽象接口 (Unified Trading Gateway Interface)。
    规范所有交易网关适配器 (Live, Paper 等) 的公开契约。
    """

    @property
    @abc.abstractmethod
    def is_live(self) -> bool:
        """是否为实盘环境"""
        pass

    @property
    @abc.abstractmethod
    def host(self) -> str:
        """CLOB 主机地址"""
        pass

    @property
    @abc.abstractmethod
    def gamma_host(self) -> str:
        """Gamma 市场发现主机地址"""
        pass

    # ========= 核心下单接口 =========

    @abc.abstractmethod
    def post_order(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """单笔下单同步接口"""
        pass

    @abc.abstractmethod
    async def post_order_async(
        self,
        token_id: str,
        price: float,
        amount: float,
        side: str = "BUY",
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """单笔下单异步接口"""
        pass

    @abc.abstractmethod
    def post_batch_orders(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """批量下单同步接口 (原子并发)"""
        pass

    @abc.abstractmethod
    async def post_batch_orders_async(self, orders: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """批量下单异步接口"""
        pass

    # ========= 撤单接口 =========

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤单同步接口"""
        pass

    @abc.abstractmethod
    async def cancel_order_async(self, order_id: str) -> bool:
        """撤单异步接口"""
        pass

    # ========= 订单状态与等待 =========

    @abc.abstractmethod
    def get_order_status(self, order_id: str) -> Optional[str]:
        """查询订单状态"""
        pass

    @abc.abstractmethod
    def wait_for_order_fill(self, order_id: str, timeout: float = 10.0) -> bool:
        """同步等待订单成交"""
        pass

    # ========= 资产与持仓接口 =========

    @abc.abstractmethod
    def get_balance(self) -> Dict[str, float]:
        """获取账户抵押资产余额 (USDC / pUSD)"""
        pass

    @abc.abstractmethod
    def get_position(self, token_id: str) -> Dict[str, Any]:
        """获取指定 Token 的持仓数据"""
        pass

    @abc.abstractmethod
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """获取当前所有活跃挂单"""
        pass

    # ========= 行情与盘口接口 =========

    @abc.abstractmethod
    def get_market_price(self, token_id: str) -> Dict[str, float]:
        """获取指定 Token 的买一卖一价格"""
        pass

    @abc.abstractmethod
    def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """获取指定 Token 的深度订单簿"""
        pass

    # ========= 结算与核对接口 =========

    @abc.abstractmethod
    def redeem(self, market_id: str) -> Dict[str, Any]:
        """已到期/已关闭市场合约结算领奖 (Redeem)"""
        pass

    @abc.abstractmethod
    async def redeem_async(self, market_id: str) -> Dict[str, Any]:
        """异步合约结算"""
        pass

    @abc.abstractmethod
    def check_user_trade_filled(self, token_id: str, max_age_seconds: float = 30.0) -> bool:
        """查证指定 Token 链上或 Data API 真实成交"""
        pass

    @abc.abstractmethod
    def list_closed_markets(self) -> List[Dict[str, Any]]:
        """获取可领奖结算的已结束市场列表"""
        pass

    @abc.abstractmethod
    def close(self) -> None:
        """安全释放网关资源与连接池"""
        pass
