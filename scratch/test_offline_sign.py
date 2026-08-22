import sys
import os

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, CreateOrderOptions
    from py_clob_client.order_builder.constants import BUY, SELL

    pk = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk, signature_type=0)
    
    order_args = OrderArgs(
        price=0.45,
        size=10.0,
        side=BUY,
        token_id="123456789"
    )
    options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
    signed_order = client.builder.create_order(order_args, options=options)
    print("Signed order dict:", signed_order.dict() if hasattr(signed_order, "dict") else vars(signed_order))
    print("Success!")
except Exception as e:
    import traceback
    traceback.print_exc()
