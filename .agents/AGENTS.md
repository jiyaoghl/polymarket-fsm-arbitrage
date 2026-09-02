# Polymarket 交易机器人开发与架构核心规范 (Project Rules)

本文档是 Polymarket 量化套利机器人系统的核心工程规范。在编写、修改、重构或审查本项目的任何代码时，必须严格遵守以下 **12 大工程铁律**：

---

## 目录索引 (Quick Index)
- [0. 核心数据源铁律 (VPS Single Source of Truth)](#0-核心数据源铁律-vps-single-source-of-truth)
- [1. 最高交互与决策铁律 (High-Confidence Directive & Persona)](#1-最高交互与决策铁律-high-confidence-directive--persona)
- [2. 资金与风控生命周期闭环 (Capital & Risk First)](#2-资金与风控生命周期闭环-capital--risk-first)
- [3. 策略模型与微观执行 (Strategy & Execution Engine)](#3-策略模型与微观执行-strategy--execution-engine)
- [4. CLOB V2 协议与原生签名规范 (CLOB Guardrails & Native Signing)](#4-clob-v2-协议与原生签名规范-clob-guardrails--native-signing)
- [5. 极速事件驱动与网络高可用 (Event-Driven Streaming & Resilience)](#5-极速事件驱动与网络高可用-event-driven-streaming--resilience)
- [6. 链上智能合约自动结算 (On-Chain CTF Auto-Redeem)](#6-链上智能合约自动结算-on-chain-ctf-auto-redeem)
- [7. 领域模型与系统健壮性 (Domain Layering & Concurrency)](#7-领域模型与系统健壮性-domain-layering--concurrency)
- [8. Dev-Ops 敏捷闭环与仿真诚实性 (Agile Dev-Ops & Paper Fidelity)](#8-dev-ops-敏捷闭环与仿真诚实性-agile-dev-ops--paper-fidelity)
- [9. 2026 官方抛物线费率与 Maker 护城河 (Parabolic Dynamic Fee & Maker Edge)](#9-2026-官方抛物线费率与-maker-护城河-parabolic-dynamic-fee--maker-edge)
- [10. 真实 L2 快照录包与高保真沙盒标定 (L2 Snapshot Recording & Sandbox Calibration)](#10-真实-l2-快照录包与高保真沙盒标定-l2-snapshot-recording--sandbox-calibration)
- [11. 配置管理与单一真理源铁律 (Config Single Source of Truth)](#11-配置管理与单一真理源铁律-config-single-source-of-truth)

---

## 0. 核心数据源铁律 (VPS Single Source of Truth)
- **只分析 VPS，严禁本地策略统计**：
  - 本地开发环境网络延迟巨大，成交、盘口、LEG1 转化、盈亏计算均无实战参考价值。**任何关于胜率、转化率、PnL、未对冲时长的归因分析必须通过 VPS 统一入口（`python scripts/vps_ops.py status / logs / analyze` 或 VPS Dashboard API）获取**。
  - **严禁**直接读取本地 `data/trading.db` 或本地 `logs/` 进行策略盈亏与胜率统计；若需离线深挖，必须先将 VPS 数据同步到 `vps-logs/` 且只读该目录。
- **持续优化手册唯一优先级 ([`OPTIMIZATION_PLAYBOOK.md`](file:///d:/生活/Trading/polymarket/doc/OPTIMIZATION_PLAYBOOK.md))**：
  - 调参优化遵循北极星指标：`LEG1_ONLY -> LOCKED 转化率` > `压低强平亏损` > `扣费净 EV` > `未对冲时长与额度闭环` > `工程健康度`。

---

## 1. 最高交互与决策铁律 (High-Confidence Directive & Persona)
- **95% 置信度红线 (95% Confidence Threshold)**：
  - 在执行任何可能影响交易逻辑、风控阈值、状态机流转或涉及复杂系统重构的任务前，AI Agent 必须进行自我置信度评估。
  - **若对任务目标、边界条件或实施方案的把握未达到 95% 以上的绝对把握，严禁盲目行动或基于猜测编写代码**。必须立即向用户提出具体、有针对性的澄清问题，直到完全对齐。
- **信息缺失主动申报 (Missing Context Declaration)**：
  - 若在分析、排错或开发过程中发现上下文不足（例如：缺少关键日志切片、缺少特定端点的错误码、缺少环境配置或策略意图模糊），**必须明确、逐项告知用户当前缺少什么关键信息**，不得模棱两可或强行推导。
- **中文原生架构师意图**：
  - 坚持“中文主谓宾结构 + 英文术语”的混合思考与输出；代码新增注释全中文；Git commit 必须使用中文。

---

## 2. 资金与风控生命周期闭环 (Capital & Risk First)
- **单边敞口 TTL 强平红线**：
  - 配对套利逻辑必须保留 TTL 强平阈值（如 `LEG1_MAX_UNHEDGED_SECONDS = 90s`）。到期后哪怕承担滑点也必须强制市价 FOK 平仓，严守资金红线。
  - 强平与二腿下单必须严格按首腿实际成交份数对齐（`leg1_size = float(leg1.get("size") or self.order_amount)`），严禁使用静态配置导致敞口残留。
- **风控额度全生命周期闭环**：
  - 策略通过 `risk_manager.acquire_trade_lock()` 成功锁定额度后，必须保证在 `on_settled`、`on_failed` 以及自动赎回后**无条件释放额度**，避免小本金账户额度假死。
- **单市场跨策略排他锁 (Market Concurrency Lock)**：
  - 同一时间同一市场仅允许一个策略持有活跃仓位或挂单，开仓前通过 `is_market_occupied()` 预检，杜绝多策略内部抢单踩踏与本金双倍占用。
- **动态拦截禁止永久拉黑**：
  - 因 K 线波动率、价差或流动性等动态行情被临时拦截的市场，**严禁**写入 `processed_markets`。行情瞬变，必须允许下一轮扫描重新评估。

---

## 3. 策略模型与微观执行 (Strategy & Execution Engine)
- **Maker-Maker 零手续费做市与 Taker 净 EV 严格守门双引擎**：
  - **Maker-Maker (双边挂单)**：VPS 实盘验证具备 80%~100% 胜率与 0 手续费磨损，作为系统主盈利引擎，严格施加双边买一 $\ge 0.35$ 盘口成熟度守门。
  - **Taker-Maker (吃一挂二)**：首腿入场必须严格扣除双边真实手续费且净利差 $\text{Net EV} \ge \$0.005$，并施加 $(P_{\text{leg1}} + P_{\text{opp\_bid}}) \le 1.0$ 防倒挂保护；彻底放弃薄利润吃单。
  - **二腿追单保利天花板**：最高买入价动态钳制在 $P_{\text{max}} = 1.0 - \text{cost} - \text{fees} - \text{breakeven\_margin}$，严禁向上盲目让价。
- **做 T 优先于超时强平 (Smart Flip Priority)**：
  - 单边持仓建立后，优先坚守 OCO 保利高抛变现；超时强平时调用 `PricingEngine.calculate_bid_vwap` 穿透买盘深度逐档加权核算均价。
- **防恶意插针与一分钱互卷 (Anti-Pennying)**：
  - Maker 挂单跟单严禁无脑 `+0.001` 互卷。必须结合“价差自适应迟滞 (1.5~3.0s)”与“阶梯式跃迁 (0.002~0.005)”反卷。

---

## 4. CLOB V2 协议与原生签名规范 (CLOB Guardrails & Native Signing)
- **纯原生 EIP-712 签名体规范**：
  - V2 签名体必须严格定义为 `Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)`。
  - **严禁**在 EIP-712 计算中包含 `nonce`, `feeRateBps`, `taker` 等 V1 废弃字段。
  - Wire JSON 中 `salt` 与 `signatureType` 必须为 `int` 类型，`expiration` 必须保留 `"0"`，顶层带 `"deferExec": False` 与 `"postOnly": False`。
- **价格与份数安全钳制**：
  - 所有底层发单必须对价格执行 `safe_price = round(min(max(float(price), 0.001), 0.999), 4)` 钳制。
  - 撮合引擎硬性要求 `size >= 5.0 Shares`。按 USDC 金额发单时必须通过 `amount / safe_price` 折算份数并施加 `>= 5.0` 兜底保护。
- **幻象失败防御与免签 Data API 终极对账**：
  - 下单遇到 HTTP 400/401 必须从响应体提取 `orderID` 标记为 `UNCONFIRMED` 态；在超时退出前必须调用公共免签 Data API (`/trades?user=`) 对账真实成交，杜绝误判失败产生孤儿持仓。

---

## 5. 极速事件驱动与网络高可用 (Event-Driven Streaming & Resilience)
- **私有 WebSocket 极速成交流 (<5ms)**：
  - 首腿吃单后优先通过私有 WebSocket 监听 `ws/user` 成交事件，实现 `<5ms` 极速唤醒状态机并挂出二腿，避免频繁 REST 轮询导致 503 限流。
- **公共 WS 防抖合并订阅 (Debounce)**：
  - 多策略并发订阅时，必须引入 0.5s 防抖定时器合并 Payload，严禁高频发送单条订阅帧触发 Polymarket `INVALID OPERATION` 风控。
- **指数退避重试与超时保护**：
  - 所有外部 REST/RPC 请求必须显式设置 `timeout`（5s~10s）；WS 连接断开必须采用指数退避重连，严禁固定短 sleep 死循环重试。

---

## 6. 链上智能合约自动结算 (On-Chain CTF Auto-Redeem)
- **Polygon CTF 官方合约原生自动赎回**：
  - 自动结算废除向 CLOB 发送 REST 请求的失效路径，统一由 `OnChainRedeemer` 直接向 Polygon 主网 `ConditionalTokens` 官方合约（`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`）调用 `redeemPositions`。
- **多 RPC 节点故障转移与 35 Gwei 保底**：
  - 链上通信严禁依赖单一 RPC，必须构建候选列表（`[RPC_URL, polygon-rpc.com, 1rpc.io/matic, tenderly, ankr]`）并在遇到 403 或超时时自动无缝轮换；发交易施加 `max(int(gas_price * 1.25), 35_000_000_000)` 最低 Gas 保底防广播拦截。
- **三种出场路径真实损益闭环**：
  - 严格区分三种出场形态：`HEDGED_LOCKED`（双买锁仓）、`DUAL_EXIT_SELL_SETTLED`（买卖做 T 变现）、`FORCE_CLOSED`（超时强平）。严禁在 Web/Discord 视图层使用单一双买公式错误重算损益；赎回后必须无条件流转 `SETTLED` 并 100% 归还风控预扣额度。

---

## 7. 领域模型与系统健壮性 (Domain Layering & Concurrency)
- **`TradeContext` 单一真理源**：
  - 所有状态流转与订单明细必须通过 `TradeContext` 强类型领域模型承载，各 Service 保持 Stateless 纯无状态化；定价与数学计算保持为纯函数。
- **并发与 Windows 路径安全**：
  - SQLite 必须全局开启 `PRAGMA journal_mode=WAL`，设置 `timeout>=10`。
  - 本地状态文件原子写入必须捕获 `PermissionError (WinError 5)` 并重试。
  - 所有路径拼接必须使用 `pathlib` 或 `os.path.join`，严禁硬编码斜杠。
- **守护线程顶层异常防护**：
  - 所有 `while True` 后台守护线程循环体必须用 `try...except Exception` 严密包裹，捕获后记录日志并推送到风控中心，**严禁守护线程静默退出**。

---

## 8. Dev-Ops 敏捷闭环与仿真诚实性 (Agile Dev-Ops & Paper Fidelity)
- **本地断网开发与闭环发布流水线**：
  - 本地严禁跑主网连网脚本，依赖本地 **196 项全量单元测试** 与离线推理。
  - 代码上线统一使用敏捷流水线 `python scripts/vps_ops.py release "feat: 中文提交说明"`，自动完成【全量单测 -> 中文 Commit -> Push -> 远程调用 VPS POST /api/ops/update 免登录秒级热更】。
- **运维工具链常用指令**：
  - `python scripts/vps_ops.py status`: 实时查看 VPS 大盘、活跃仓位与分发延迟。
  - `python scripts/vps_ops.py logs -n 80`: 拉取 VPS 实时运行日志（支持 `-f` 持续跟踪）。
  - `python scripts/vps_ops.py analyze`: 获取最近 50 笔交易的北极星转化率、胜率与出场归因。
  - `python scripts/vps_ops.py clean-history`: 清空历史订单并重置大盘统计。
  - `python scripts/vps_ops.py sync-snapshots`: 从 VPS 拉取真实 L2 盘口快照到本地。
- **模拟盘高保真度 (Paper Fidelity)**：
  - 模拟模式必须包含真实的 Taker/Maker 手续费扣除、基于 `SIM_BASE_FILL_RATE` 的非 100% 成交判定、以及随机网络延迟与滑点模拟。
  - 挂买单（BUY Limit Order）必须严格以卖盘打穿（`best_ask <= buy_price`）作为模拟成交判定依据，严禁将对手盘买一抬升误判为成交。

---

## 9. 2026 官方抛物线费率与 Maker 护城河 (Parabolic Dynamic Fee & Maker Edge)
- **严格执行官方非线性对称抛物线公式**：
  - 手续费计算公式：$\text{Fee} = C \times \text{feeRate} \times p \times (1 - p)$，加密货币市场 $\text{feeRate} = 0.07$ (7%)。
  - **微观惩罚机理**：在 $p=0.50$ 时费率达到顶峰（每份 $\$0.0175$），在 $p=0.40$ 时每份 $\$0.0168$（占成本高达 **4.2%**）。**严禁使用任何过时的 1% 线性模型估算 Taker 净 EV**，必须全链路接入 `PricingEngine.calculate_parabolic_fee`。
- **Maker-Maker 零费率与 20% 返利作为绝对重仓主力**：
  - Maker 挂单享有 $0.0\%$ 零手续费并享受 **20% Maker 返利补贴**。所有增量本金与生产权重必须优先向 Maker-Maker 策略倾斜，Taker 系列仅作为严格控仓（≤3U~5U）的高门槛对照组运行。

---

## 10. 真实 L2 快照录包与高保真沙盒标定 (L2 Snapshot Recording & Sandbox Calibration)
- **零阻塞不可变内存网格快照录包 (`L2SnapshotRecorder`)**：
  - VPS 常驻后台守护线程以 1 帧/秒不可变只读访问 `OrderbookMemoryGrid`，每小时自动轮转生成 gzip 压缩快照，自动维护 7 天历史清理，磁盘异常绝不阻断主交易事件循环。
  - 快照通过 `python scripts/vps_ops.py sync-snapshots --days 7` 归档至 `vps-logs/snapshots/`，作为离线调参与回测唯一真实数据源。
- **多资产独立并发周期排他锁 (Multi-Asset 120s Concurrency Lock)**：
  - 离线回测沙盒（`scripts/calibrate_params.py`）必须为 BTC/ETH/SOL 分别维持独立的 120s 周期排他锁，杜绝在单资产密集采样帧中反复撮合虚增频次，确保与实盘多资产并发严格 1:1 对齐。
- **帕累托最优报表特征去重 (Signature Deduplication)**：
  - Optuna 贝叶斯寻优在连续参数空间收敛到最优点后，会在局部产生高密度微扰采样。**生成报告时必须按 `(entry_max, max_spread, initial_margin, obi_floor)` 等核心宏观特征进行分桶去重**，严禁微小扰动的单一参数霸占 Top 5 榜单，确保输出覆盖不同风控水位的真实帕累托前沿。

---

## 11. 配置管理与单一真理源铁律 (Config Single Source of Truth)
- **策略业务参数单一归一**：
  - 各策略的买入限价、价差、OBI 门槛、做市买一、目标利润等业务参数，必须且只能维护在 `configs/strategies.json` 中。
- **全局风控与分资产波动率环境对齐**：
  - 分资产波动率防爆盾（BTC 0.36%/0.15%, ETH 0.42%/0.20%, SOL 0.48%/0.22%）与费率环境变量统一维护在 `.env` 与 `.env.example` 中。
  - `src/polymarket/config.py` 中的代码默认值必须与 `.env.example` 保持 **100% 同步对齐**，严禁代码硬编码与外部配置产生脱节歧义。

---

## 12. Dual-Maker 双挂动态智能跟单与防抖铁律 (Dual-Bracket Re-peg & Pegging Guardrails)
- **双挂动态智能贴盘跟单 (Dual-Maker Re-peg)**：
  - 在 `PENDING_BOTH_LEGS` 挂单状态下，当盘口买一向上漂移反超当前挂单时，系统必须在自适应价差防抖冷却（宽价差 1.5s，紧凑价差 3.0s）后自动执行智能改单。
  - 改单必须严格遵循 **保利天花板底线**（$P_{\text{yes, new}} + P_{\text{no, new}} \le 1.0 - \text{initial\_margin}$），严禁向上盲目让利。
  - 施加 **阶梯跃迁保护**（仅在变动 $\ge 0.002$ 时触发，杜绝 0.001 互相踩踏）与 **卖一防穿透保护**（$P_{\text{new}} < \text{best\_ask}$），绝不转化为 Taker 吃单。
- **实盘撤单保底补单容错**：
  - 实盘批量改单失败时，必须无条件以原价格重新挂出保底订单，严禁留下单边裸奔敞口。
