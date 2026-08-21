import asyncio
from polymarket.client import get_client

async def test():
    client = get_client(is_live=True)
    try:
        # 使用当前市场活跃的 token_id，刚才日志里的YES: 95095123239255359112549008251309792668599379500309065634863428398684004882226
        res = await client.get_market_price_async('95095123239255359112549008251309792668599379500309065634863428398684004882226')
        print("Live Success:", res)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(test())
