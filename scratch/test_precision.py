from decimal import Decimal, ROUND_DOWN

price = 0.42
size = 7.14 # shares

d_price = Decimal(str(price))
d_size = Decimal(str(size))

# BUY: maker is USDC (max 2 decimals!), taker is Shares (max 4 decimals!)
maker_usdc = (d_size * d_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
taker_shares = d_size.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

raw_maker = int(maker_usdc * Decimal("1000000"))
raw_taker = int(taker_shares * Decimal("1000000"))

print(f"BUY order: makerAmount={raw_maker} ({maker_usdc} USDC, 2 decimals), takerAmount={raw_taker} ({taker_shares} Shares, 4 decimals)")

# SELL: maker is Shares (2 decimals), taker is USDC (2 decimals)
seller_shares = d_size.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
seller_usdc = (d_size * d_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
raw_maker_s = int(seller_shares * Decimal("1000000"))
raw_taker_s = int(seller_usdc * Decimal("1000000"))

print(f"SELL order: makerAmount={raw_maker_s} ({seller_shares} Shares), takerAmount={raw_taker_s} ({seller_usdc} USDC)")
