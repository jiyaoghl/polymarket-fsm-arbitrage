# Polymarket FSM Arbitrage Bot 🚀

基于 **Finite State Machine (有限状态机)** 架构的 Polymarket BTC 5 分钟 Up/Down 预测市场高频对冲套利机器人。该系统具备全异步并发、中央风控拦截、微秒级滑点控制以及先进的挂单重试策略，致力于在极短时间窗口内无损榨取预测市场的对冲差价 (EV)。

---

## 🌟 核心特性与架构 (v3.0)

本项目抛弃了传统的“定时轮询”模式，底层由高性能事件驱动与状态机接管：

### 1. 动态套利双引擎 (Dynamic Hedging)
- **波动率护盾 (Volatility Shield)**：在准备发单时探测 BTC 盘口波动，若 `Ask-Bid Spread > 0.05`，判断为极端单边行情，坚决拒发首腿。
- **动态盈亏平衡价**：首腿成交后，彻底废弃死板的触发价格。引擎将基于首腿实际成交价 (Cost) 动态计算二腿的最高可接受价格 `(1.0 - leg1_cost - 0.01)`。极大地提升了对冲闭环的成单率！
- **挂单深度跟随器 (Pegged Maker)**：如果在 `maker` 策略下对冲腿迟迟无法成交（超过 15 秒），触发急速撤单，并随着盘口的推移自动贴近买一价重新挂单，咬死盘口。
- **滑点微调重试**：针对 Polymarket FOK 拒单率高的问题，加入了毫秒级的滑点重试机制 (Max Slippage Tolerance)。

### 2. 状态机驱动 (TradeFSM)
- 所有订单状态严格沿 `IDLE -> PENDING_LEG1 -> LEG1_ONLY -> PENDING_LEG2 -> LOCKED / SETTLED` 单向流转，杜绝了并发错乱和重复发单。

### 3. 中央风控拦截器 (RiskManager)
- 以单例模式全局守护资金池。所有 FSM 在发出买单前必须向 `RiskManager` 申请额度预扣。
- 若总计未对冲敞口超出了配置的百分比 (例如 15%)，将遭遇“硬拒绝”，彻底告别爆仓。

### 4. 量化战绩看板 (Metrics Dashboard V2)
- 极具现代科技感的 Web 看板（运行在 `:8888` 端口）。
- 实时展示 **系统防御战绩**：风控自动拦截笔数、挽回的潜在亏损资金、自适应重试成功率以及实时动态资金利用水位条。

---

## 🛠️ 安装与部署

### 1. 环境准备
需要 Python 3.11+ 环境。
```bash
pip install -r requirements.txt
```

### 2. 配置环境
```bash
# 复制环境变量模板
cp configs/.env.example .env
```
编辑 `.env` 文件，填入你的 Polygon 钱包私钥及 Polymarket CLOB API Key。**请绝对确保 `.env` 不会被提交至版本库**。

**关键策略配置项 (`config.py`):**
| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `MAX_SLIPPAGE_TOLERANCE` | 最大滑点容忍度 | `0.015` |
| `ORDER_AMOUNT` | 单笔交易头寸 (USDC) | `10.0` |
| `BTC_CHOP_MAX_AMPLITUDE` | 波动率熔断阈值 | `0.15` |
| `MAX_CONCURRENT_UNHEDGED_TRADES`| 全局最大允许裸单数 | `3` |

### 3. 运行实盘大盘
```bash
# 在后台启动 Web 服务和交易内核
python -m apps.dashboard
```
然后在浏览器中打开 `http://127.0.0.1:8888`，即可欣赏全自动量化机器人的猎杀时刻。

---

## 📊 策略组合说明

系统当前内置并同时运行五大子策略（通过 `StrategyManager` 并发驱动）：
1. `taker_taker_aggressive`: 双边急速 FOK 吃单，首腿只管上车，速度最快，但承担较高的 Taker 手续费。
2. `taker_maker_aggressive`: 首腿急速吃单，二腿在计算好盈亏平衡线后，立刻以 `GTC` Maker 的形式顶上盘口，赚取手续费差。
3. `taker_taker_conservative`: 保守版双边吃单，对滑点要求极其苛刻。
4. `taker_maker_conservative`: 保守挂单策略，胜率高但偶尔会面临未成交超时。
5. `maker_maker_standard`: 全程 Maker（当前作为实验性策略运行），对冲极快。

---

## 🗺️ 架构演进与重构路线图 (Roadmap)

当前系统使用 **“每策略/每市场 -> 独立 WebSocket 线程”** 的解耦架构，适合研发期的逻辑隔离和稳定性测试。但在未来的极高频实盘（千万级交易量）下，Python GIL 会由于海量 JSON 解析产生严重的 CPU 锁争抢（导致慢消费 1013 Slow Consumer 断连）。

**[长期重构任务] 迁移至“全异步单例多路复用 (Multiplexing) 架构”：**
- **统一数据总线 (Event Bus)**：全局只维护 **1** 个 `MarketDataStreamer` WebSocket 连接，彻底解决 Polymarket IP 频率限制。
- **一次解析，多次消费**：`json.loads` 和 `_parse_ws_prices_full` 每帧数据只执行一次，随后通过 `asyncio.Queue` 或钩子发布给底层 8 个以上的策略实例。
- **消除 GIL 阻塞**：废弃 `threading.Thread` 的多事件循环模型，使整个系统运行在单线程纯异步环境下，性能上限可提升 10 倍以上。

---

## ⚠️ 免责声明 (Disclaimer)

本项目仅作为算法交易的研究与学习交流使用。Polymarket 和 Crypto 预测市场风险极高，任何交易策略都无法保证 100% 胜率，且可能因 API 宕机、极端单边波动导致严重亏损。使用本代码造成的任何资金损失均由使用者本人承担。