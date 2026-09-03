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
MIN_TIME_TO_EXPIRY_ENTRY = int(os.getenv("MIN_TIME_TO_EXPIRY_ENTRY", "45"))  # 临近交割禁止开仓阈值（秒）

# 手续费配置 (Polymarket 2026 官方微观规则: Fee = C * feeRate * p * (1 - p))
POLY_CRYPTO_FEE_RATE = float(os.getenv("POLY_CRYPTO_FEE_RATE", "0.07"))     # 加密货币 Taker 基准费率 7%
POLY_MAKER_REBATE_RATE = float(os.getenv("POLY_MAKER_REBATE_RATE", "0.20")) # 成交 Maker 享受 20% 手续费返还补贴
TAKER_FEE_RATE = POLY_CRYPTO_FEE_RATE   # 兼容旧字段别名 (7%)
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.00"))   # Maker 做市手续费率 0%

# 模拟盘仿真参数（仅 PAPER 模式生效）
SIM_BASE_FILL_RATE = float(os.getenv("SIM_BASE_FILL_RATE", "0.65"))       # 模拟 FOK 基础成交率 65%
SIM_LATENCY_MIN_MS = int(os.getenv("SIM_LATENCY_MIN_MS", "100"))          # 模拟最小网络延迟 ms
SIM_LATENCY_MAX_MS = int(os.getenv("SIM_LATENCY_MAX_MS", "300"))          # 模拟最大网络延迟 ms
SIM_SLIPPAGE_MAX = float(os.getenv("SIM_SLIPPAGE_MAX", "0.003"))          # 模拟最大滑点 0.3%
PAPER_MARKET_LOCK_ENABLED = os.getenv("PAPER_MARKET_LOCK_ENABLED", "false").lower() in ("true", "1", "yes")  # 模拟盘是否启用单市场跨策略排他锁 (默认 false 允许多策略并发演练)

# 支持的多加密资产池配置 (Multi-Asset 5min Markets)
SUPPORTED_ASSETS = [x.strip() for x in os.getenv("SUPPORTED_ASSETS", "BTC,ETH,SOL").split(",") if x.strip()]

CRYPTO_CHOP_MAX_AMPLITUDE = float(os.getenv("CRYPTO_CHOP_MAX_AMPLITUDE", "0.38"))  # 通用默认振幅阈值 0.38% (300轮标定最优)
CRYPTO_CHOP_MAX_NET_CHANGE = float(os.getenv("CRYPTO_CHOP_MAX_NET_CHANGE", "0.18")) # 通用默认净变动阈值 0.18% (300轮标定最优)

# 分资产独立阈值配置（百分比 %，基于 25.1 万帧真实 L2 快照 Optuna 贝叶斯寻优标定）
ASSET_CHOP_THRESHOLDS = {
    "BTC": {
        "max_amplitude": float(os.getenv("BTC_CHOP_MAX_AMPLITUDE", "0.36")),
        "max_net_change": float(os.getenv("BTC_CHOP_MAX_NET_CHANGE", "0.15")),
    },
    "ETH": {
        "max_amplitude": float(os.getenv("ETH_CHOP_MAX_AMPLITUDE", "0.42")),
        "max_net_change": float(os.getenv("ETH_CHOP_MAX_NET_CHANGE", "0.20")),
    },
    "SOL": {
        "max_amplitude": float(os.getenv("SOL_CHOP_MAX_AMPLITUDE", "0.48")),
        "max_net_change": float(os.getenv("SOL_CHOP_MAX_NET_CHANGE", "0.22")),
    },
    "DOGE": {
        "max_amplitude": float(os.getenv("DOGE_CHOP_MAX_AMPLITUDE", "0.60")),
        "max_net_change": float(os.getenv("DOGE_CHOP_MAX_NET_CHANGE", "0.30")),
    },
    "XRP": {
        "max_amplitude": float(os.getenv("XRP_CHOP_MAX_AMPLITUDE", "0.45")),
        "max_net_change": float(os.getenv("XRP_CHOP_MAX_NET_CHANGE", "0.22")),
    },
}
# 资金与风控配置
DAILY_MAX_DRAWDOWN = float(os.getenv("DAILY_MAX_DRAWDOWN", "0.05"))  # 5% 最大回撤
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "100.0"))       # 默认资金基准（100 USDC）
PAPER_INITIAL_CAPITAL = float(os.getenv("PAPER_INITIAL_CAPITAL", os.getenv("INITIAL_CAPITAL", "100.0"))) # 模拟盘默认资金池（100 USDC）

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
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "0"))  # 0: EOA, 1: POLY_PROXY, 2: POLY_GNOSIS_SAFE
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")        # 代理钱包出资地址（普通私钥留空）

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

# 数据库路径（固化保存在持久化 data/ 目录，避免被临时目录清理丢失）
DB_PATH = os.getenv("DB_PATH", str(paths.data_dir() / "trading.db"))

# RiskGuard 检查间隔（秒）
RISK_CHECK_INTERVAL = int(os.getenv("RISK_CHECK_INTERVAL", "10"))

# Discord Webhook 实时战报推送配置
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_ENABLED = os.getenv("DISCORD_ENABLED", "true").lower() in ("true", "1", "yes") and bool(DISCORD_WEBHOOK_URL)
DISCORD_MIN_SEVERITY = os.getenv("DISCORD_MIN_SEVERITY", "TRADE").upper()  # INFO | TRADE | WARNING | CRITICAL

# Discord Bot 交互式控制配置
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_ADMIN_IDS = [x.strip() for x in os.getenv("DISCORD_ADMIN_IDS", "").split(",") if x.strip()]
DISCORD_COMMAND_PREFIX = os.getenv("DISCORD_COMMAND_PREFIX", "!").strip() or "!"

# L2 盘口快照录包配置 (阶段 3: 真实 L2 录包与离线参数标定)
SNAPSHOT_ENABLED = os.getenv("SNAPSHOT_ENABLED", "true").lower() in ("true", "1")
SNAPSHOT_INTERVAL_SEC = float(os.getenv("SNAPSHOT_INTERVAL_SEC", "1.0"))       # 采样频率：1 帧/秒
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "7"))        # 保留最近 7 天
SNAPSHOT_DIR = str(paths.data_dir() / "snapshots")

# VPS 远程自动化免交互运维配置 (严禁上传 GitHub)
VPS_HOST = os.getenv("VPS_HOST", "161.120.187.156").strip()
VPS_SSH_USER = os.getenv("VPS_SSH_USER", "ubuntu").strip()
VPS_SSH_PASSWORD = os.getenv("VPS_SSH_PASSWORD", "").strip()
VPS_SSH_PORT = int(os.getenv("VPS_SSH_PORT", "22"))
VPS_REMOTE_DIR = os.getenv("VPS_REMOTE_DIR", "/home/ubuntu/polymarket-fsm-arbitrage").strip()

