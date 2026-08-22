import json
import inspect
from py_clob_client_v2 import ClobClient, OrderArgs, Side, OrderType

pk = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
client = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)

print("ClobClient methods:", [m for m in dir(client) if not m.startswith("_")])
order_args = OrderArgs(
    price=0.45,
    size=10.0,
    side=Side.BUY,
    token_id="123456789"
)
try:
    print("Inspecting create_order...")
    print(inspect.getsource(client.create_order))
except Exception as e:
    print("Error inspecting create_order:", e)
