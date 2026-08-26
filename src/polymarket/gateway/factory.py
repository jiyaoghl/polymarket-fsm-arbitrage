from typing import Optional
from polymarket.gateway.base import ITradingGateway
from polymarket.gateway.paper import PaperTradingGateway
from polymarket.gateway.live import LiveClobV2Gateway
from polymarket.config import CLOB_HOST, GAMMA_HOST

class GatewayFactory:
    """
    交易网关工厂 (Gateway Factory)。
    根据实盘/模拟标志动态构造对应的高可用网关实例。
    """

    @staticmethod
    def create_gateway(
        is_live: bool = False,
        host: str = CLOB_HOST,
        gamma_host: str = GAMMA_HOST,
        private_key: Optional[str] = None,
        warm_up: bool = True,
        initial_balance: Optional[float] = None,
        **kwargs
    ) -> ITradingGateway:
        """创建交易网关实例"""
        if is_live:
            return LiveClobV2Gateway(
                host=host,
                gamma_host=gamma_host,
                private_key=private_key,
                warm_up=warm_up
            )
        else:
            return PaperTradingGateway(
                host=host,
                gamma_host=gamma_host,
                initial_balance=initial_balance
            )
