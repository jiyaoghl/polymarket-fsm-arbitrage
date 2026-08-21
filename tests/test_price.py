import asyncio
from polymarket.client import get_client

async def test():
    client = get_client()
    try:
        res = await client.get_market_price_async('10465256653612705991685953300071241367002353463213121939852578204418036519721')
        print("Success:", res)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(test())
