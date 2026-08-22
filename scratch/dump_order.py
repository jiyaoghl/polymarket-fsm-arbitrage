import os
import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, Side, OrderType

# Initialize a dummy client
pk = "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)

# Create an order
order_args = OrderArgs(
    price=0.4,
    size=10,
    side=Side.BUY,
    token_id="102936"
)
order = client.create_order(order_args)
print(json.dumps(order, indent=2))
