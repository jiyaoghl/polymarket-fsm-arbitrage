# Polymarket FSM Arbitrage Bot 🚀

基于 **Finite State Machine (有限状态机)** 驱动的 Polymarket BTC 5 分钟 Up/Down 预测市场高频对称套利量化系统。全异步并发多路复用总线、毫秒级 OrderBook VWAP 深度预估、双腿套利锁利数学拦截器与智能 Maker 动态盯盘追单（Order Pegging），在极短时间窗口内无损榨取预测市场的确定性对冲差价 (EV)。

---

## 🏛️ 系统全局逻辑架构 (System Architecture)

```mermaid
flowchart TD
    subgraph Market_Data [外部市场数据源]
        WS[Polymarket CLOB WebSocket]
        GAMMA[Gamma HTTP API]
        BINANCE[Binance 1m KLine API]
    end

    subgraph Data_Bus [统一数据总线与调度]
        STREAMER[MarketDataStreamer 单例多路复用总线]
        DISCOVER[5min 滚动市场定位器]
        CHOP_FILTER[BTC 单边/震荡波动率过滤器]
    end

    WS --> STREAMER
    GAMMA --> DISCOVER
    BINANCE --> CHOP_FILTER

    subgraph Strategy_Matrix [多策略状态机矩阵 (8 组 FSM)]
        S1[Taker + Taker 保守/标准/激进]
        S2[Taker + Maker 保守/标准/激进]
        S3[Maker + Maker 保守/标准]
    end

    STREAMER -->|无拷贝分发价格/深度 Bundle| Strategy_Matrix
    DISCOVER -->|派发新 5min 盘口| Strategy_Matrix
    CHOP_FILTER -->|单边行情熔断拦截| Strategy_Matrix

    subgraph Execution_Engine [执行层与深度撮合引擎]
        VWAP_EST[全量订单簿 VWAP 深度加权预估]
        EV_CHECK[双腿净 EV 数学拦截器 Net Margin >= 1%]
        MAKER_PEG[智能 Maker 动态钉盘追单 Order Pegging]
        ADAPTIVE_FOK[自适应滑点微重试 FOK]
    end

    Strategy_Matrix --> Execution_Engine

    subgraph Risk_Defense [中央风控与防御系统]
        RISK_GUARD[RiskGuard 独立风控守卫]
        CIRCUIT[三级熔断机制: 黄牌 / 橙牌 / 红牌]
        TTL_SL[单腿敞口超时强平 TTL StopLoss]
        AUTO_REDEEM[到期市场自动结算 Redeem]
    end

    Execution_Engine --> Risk_Defense

    subgraph Storage_UI [持久化与可视化看板]
        DB[(SQLite trading.db)]
        DASHBOARD[FastAPI 实时 WebSocket 仪表盘 :8888]
        VPS_CLI[vps.sh 一键运维管理系统]
    end

    Risk_Defense --> DB
    Strategy_Matrix --> DB
    DB --> DASHBOARD
    DASHBOARD --> VPS_CLI
```

---

## 🌟 核心量化机制与技术亮点

### 1. 状态机驱动的订单生命周期 (TradeFSM)
所有交易在状态机内严格按单向事件驱动流转，彻底告别旧版阻塞式轮询：
```
[IDLE 监听] 
   │
   ├─► 深度校验通过 ─► [PENDING_LEG1] ─► 成交 ─► [LEG1_ONLY 单腿敞口]
   │                                                    │
   │   ┌────────────────────────────────────────────────┘
   │   ├─► 满足对冲阈值 (EV >= 1%) ─► [PENDING_LEG2] ─► 成交 ─► [LOCKED 锁仓套利]
   │   │                                  │
   │   │                                  ├─► 盘口上移 ─► [Maker 动态钉盘撤改单]
   │   │                                  └─► 超时/未成交 ─► 智能降级/吃单强平
   │   │
   │   └─► 超过 TTL 时间 (默认 90s) ─► 动态滑点市价平仓 ─► [FAILED 止损退出]
   │
   └─► 盘口到期 ─► [SETTLED 自动结算 Redeem]
```

### 2. 全量订单簿 VWAP 深度撮合（防滑点击穿）
* **现状痛点**：Polymarket 5min 盘口深度较薄，只看 Best Ask 极易吃穿深度导致买入均价飙升，产生“锁亏”。
* **升级解法**：在发单前扫描全量 Ask 深度，计算投入 `ORDER_AMOUNT` 的**实际成交均价 (VWAP)**。
* **数学拦截器**：执行 `Total_Cost / Hedged_Shares < 0.99` 检验，确保双腿对冲后至少拥有 1% 的确定性净收益，从数学层面 100% 杜绝负 EV 交易。

### 3. 智能 Maker 动态钉盘与追单算法 (Active Order Pegging)
* 对于 Maker 策略，自动将挂单设置在 `min(Best_Bid + 0.001, dynamic_reentry_max)`，抢占买一档并赚取 Spread。
* 处于挂单等待期间，系统以 1.0s 冷却节流实时监听盘口，一旦被他人压价立即自动撤单并以最新最优价重挂，大幅提升被动成交率。

### 4. 统一异步多路复用数据总线 (MarketDataStreamer)
* 全局单条 WebSocket 接入 Polymarket CLOB，一次解析、零拷贝分发给全部 8 套策略的 `asyncio.Queue`，大幅消除 Python GIL 锁争抢与网络重连开销。

### 5. 三级资金熔断与多层风控 (Risk Defense)
* **外部行情护盾**：Binance 1m K 线振幅 `> 0.15%` 判定为单边大行情，主动拒绝首腿开仓。
* **黄牌 (亏损≥10%)**：暂停新市场发现。
* **橙牌 (亏损≥20%)**：停止所有新开仓，平掉未对冲单腿。
* **红牌 (亏损≥30%)**：市价全仓清仓并触发 HALT 安全制动。

---

## 📊 策略矩阵说明 (`configs/strategies.json`)

系统内置 8 组异构策略并行运行，覆盖不同行情风格：

| 策略 ID | 策略模式 | 首腿入场 | 二腿补仓 | 核心特性 |
| :--- | :--- | :--- | :--- | :--- |
| `taker_taker_conservative` | 吃单 + 吃单 | ≤ 0.45 | < 0.35 | 保守型，追求高确定性低入场价 |
| `taker_taker_standard` | 吃单 + 吃单 | ≤ 0.50 | < 0.42 | 标准型，兼顾成单率与对冲空间 |
| `taker_taker_aggressive` | 吃单 + 吃单 | ≤ 0.55 | < 0.45 | 激进型，高频入场快速锁仓 |
| `taker_maker_conservative` | 吃单 + 挂单 | ≤ 0.45 | 智能钉盘 | 首腿吃单，二腿以买一价 Pegging 挂单赚 Spread |
| `taker_maker_standard` | 吃单 + 挂单 | ≤ 0.50 | 智能钉盘 | 平衡型 Maker 策略，带超时自动降级 |
| `taker_maker_aggressive` | 吃单 + 挂单 | ≤ 0.55 | 智能钉盘 | 激进型 Maker，全速争抢二腿挂单 |
| `maker_maker_conservative` | 挂单 + 挂单 | ≤ 0.45 | 智能钉盘 | 双边 Maker，极低手续费但需更高流动性 |
| `maker_maker_standard` | 挂单 + 挂单 | ≤ 0.50 | 智能钉盘 | 标准双边做市套利策略 |

---

## 🛠️ 安装与快速上手

### 1. 本地/开发环境启动
```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp configs/.env.example .env
# 编辑 .env 填入私钥与 API 配置

# 3. 启动 Dashboard
PYTHONPATH=src python3 -m apps.dashboard
```

### 2. VPS 云端一键运维 (Ubuntu 22.04 / 24.04 推荐)
仓库自带开箱即用的自动化管理脚本 `vps.sh`：

```bash
# 一键初始化环境并后台启动
bash vps.sh

# 常用运维指令
bash vps.sh update     # 拉取 GitHub 最新代码、更新依赖并平滑重启
bash vps.sh restart    # 重启当前服务
bash vps.sh stop       # 安全停止后台服务
bash vps.sh status     # 查看服务运行状态与 PID
bash vps.sh logs       # 查看实时运行日志 (等同于 tail -f logs/nohup.log)
```

启动成功后，在浏览器中访问 `http://<你的IP>:8888` 即可进入实时量化大盘。

---

## ⚙️ 关键配置项详解 (`.env` / `config.py`)

| 变量名 | 类型 | 默认值 | 描述 |
| :--- | :--- | :--- | :--- |
| `ORDER_AMOUNT` | float | `10.0` | 单笔交易头寸基础金额 (USDC) |
| `MAX_SLIPPAGE_TOLERANCE` | float | `0.015` | 最大允许 VWAP 滑点比例 (1.5%) |
| `BTC_CHOP_MAX_AMPLITUDE` | float | `0.15` | Binance 10m K线振幅熔断阈值 (0.15%) |
| `LEG1_MAX_UNHEDGED_SECONDS`| int | `90` | 首腿最大未对冲单腿持有时间 (秒)，超时触发止损 |
| `MAX_CONCURRENT_UNHEDGED_TRADES`| int | `3` | 全账户最大允许同时存在的单腿敞口数 |
| `INITIAL_CAPITAL` | float | `100.0` | 初始资金底数 (用于回撤与熔断计算) |
| `MIN_CASH_RESERVE_PCT` | float | `0.20` | 最低现金保留比例 (20%) |

---

## ⚠️ 免责声明 (Disclaimer)

本项目仅供算法交易、量化对冲及区块链高频交易的技术研究与学习交流。预测市场价格波动剧烈，任何策略均存在单边行情被套、网络延迟及流动性缺失等风险。使用本系统造成的任何实际盈亏均由使用者本人承担。