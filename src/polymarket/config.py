import os
from pathlib import Path

from dotenv import load_dotenv

from polymarket import paths


def _load_dotenv() -> None:
    # 优先级：显式 DOTENV_PATH > 仓库根 .env > configs/.env
    explicit = os.getenv("DOTENV_PATH")
    if explicit:
        load_dotenv(dotenv_path=explicit, override=False)
        return

    root_env = paths.repo_root() / ".env"
    if root_env.exists():
        load_dotenv(dotenv_path=str(root_env), override=False)
        return

    cfg_env = paths.configs_dir() / ".env"
    load_dotenv(dotenv_path=str(cfg_env), override=False)


_load_dotenv()

# Polymarket CLOB 配置
PK = os.getenv("POLX_PK")
API_KEY = os.getenv("POLX_API_KEY")
API_SECRET = os.getenv("POLX_API_SECRET")
API_PASSPHRASE = os.getenv("POLX_API_PASSPHRASE")

# 策略阈值
INITIAL_ENTRY_MAX_PRICE = float(os.getenv("ENTRY_MAX_PRICE", "0.50"))
INITIAL_ENTRY_MIN_PRICE = float(os.getenv("ENTRY_MIN_PRICE", "0.20"))
REENTRY_TRIGGER_PRICE = float(os.getenv("REENTRY_TRIGGER_PRICE", "0.40"))
STOP_LOSS_TIME_REMAINING = int(os.getenv("STOP_LOSS_TIME", "60"))
ORDER_AMOUNT = float(os.getenv("ORDER_AMOUNT", "10.0"))
MAX_SLIPPAGE_TOLERANCE = float(os.getenv("MAX_SLIPPAGE_TOLERANCE", "0.015"))  # 最大 VWAP 滑点容忍度 1.5%
LEG1_MAX_UNHEDGED_SECONDS = int(os.getenv("LEG1_MAX_UNHEDGED_SECONDS", "90"))  # 首腿单腿最大未对冲保持时间（秒）
MAX_CONCURRENT_UNHEDGED_TRADES = int(os.getenv("MAX_CONCURRENT_UNHEDGED_TRADES", "3"))  # 全账户最大允许未对冲单腿数

# 震荡过滤配置 (Choppy Market Filter)
BTC_CHOP_MAX_AMPLITUDE = float(os.getenv("BTC_CHOP_MAX_AMPLITUDE", "0.15"))
BTC_CHOP_MAX_NET_CHANGE = float(os.getenv("BTC_CHOP_MAX_NET_CHANGE", "0.10"))
# 风控配置（旧版，保留兼容）
DAILY_MAX_DRAWDOWN = float(os.getenv("DAILY_MAX_DRAWDOWN", "0.05"))  # 5% 最大回撤
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "100.0"))       # 小资金默认 100 USDC

# 网络配置
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
RPC_URL = os.getenv("RPC_URL", "https://polygon-rpc.com")

# CLOB API & V2 协议配置
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
EIP712_DOMAIN_VERSION = os.getenv("EIP712_DOMAIN_VERSION", "2")
EXCHANGE_CONTRACT_V2 = os.getenv("EXCHANGE_CONTRACT_V2", "0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_EXCHANGE_CONTRACT_V2 = os.getenv("NEG_RISK_EXCHANGE_CONTRACT_V2", "0xe2222d279d744050d28e00520010520000310F59")
COLLATERAL_TOKEN_NAME = os.getenv("COLLATERAL_TOKEN_NAME", "pUSD")

# 代理配置（用于访问 Polymarket API，格式如 http://127.0.0.1:7890）
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

# 通知配置 (可选)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ============================================================
# 以下为多策略系统新增配置项
# ============================================================

# EV 评分引擎
EV_MIN_SCORE = float(os.getenv("EV_MIN_SCORE", "0.02"))          # 最低 EV 评分阈值
EV_MIN_LIQUIDITY = float(os.getenv("EV_MIN_LIQUIDITY", "500"))   # 市场最低流动性（USDC）
EV_SCAN_INTERVAL = int(os.getenv("EV_SCAN_INTERVAL", "15"))      # EVEngine 扫描间隔（秒）
MARKET_SCAN_INTERVAL = int(os.getenv("MARKET_SCAN_INTERVAL", "30"))  # MarketScanner 扫描间隔（秒）

# 仓位风控（小资金专用）
MAX_SINGLE_MARKET_PCT = float(os.getenv("MAX_SINGLE_MARKET_PCT", "0.40"))  # 单市场最大仓位比例
MAX_OPEN_MARKETS = int(os.getenv("MAX_OPEN_MARKETS", "3"))                 # 同时最多持仓数
MIN_CASH_RESERVE_PCT = float(os.getenv("MIN_CASH_RESERVE_PCT", "0.20"))    # 最低现金保留比例
MAX_ORDER_USDC = float(os.getenv("MAX_ORDER_USDC", "30.0"))                # 单笔最大下单（USDC）
MIN_ORDER_USDC = float(os.getenv("MIN_ORDER_USDC", "1.0"))                 # 单笔最小下单（USDC）

# 三级熔断阈值（相对 INITIAL_CAPITAL 的亏损比例）
DRAWDOWN_YELLOW = float(os.getenv("DRAWDOWN_YELLOW", "0.10"))   # 黄牌：暂停新市场发现
DRAWDOWN_ORANGE = float(os.getenv("DRAWDOWN_ORANGE", "0.20"))   # 橙牌：停止所有新开仓
DRAWDOWN_RED = float(os.getenv("DRAWDOWN_RED", "0.30"))         # 红牌：全仓平仓并停止系统

# Telegram 确认超时（秒）
CONFIRM_TIMEOUT_SEC = int(os.getenv("CONFIRM_TIMEOUT_SEC", "60"))

# 数据库路径（默认放 tmp/，避免污染仓库根）
DB_PATH = os.getenv("DB_PATH", str(paths.tmp_dir() / "trading.db"))

# RiskGuard 检查间隔（秒）
RISK_CHECK_INTERVAL = int(os.getenv("RISK_CHECK_INTERVAL", "10"))
