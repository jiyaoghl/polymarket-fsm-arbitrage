import json
from py_clob_client_v2 import ClobClient, OrderArgs, Side, CreateOrderOptions

pk = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)

order_args = OrderArgs(
    price=0.45,
    size=10.0,
    side=Side.BUY,
    token_id="123456789"
)
options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
signed_order = client.builder.build_order(order_args, options=options, version=2)
print("Signed Order V2 dict keys:", vars(signed_order))
print("Dict format:", signed_order.dict() if hasattr(signed_order, "dict") else "no dict()")
