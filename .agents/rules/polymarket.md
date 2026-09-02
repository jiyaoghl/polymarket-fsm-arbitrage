# Polymarket 交易机器人开发与架构核心规范 (Project Rules)

本文档是 Polymarket 量化套利机器人系统的核心工程规范。在编写、修改、重构或审查本项目的任何代码时，必须严格遵守以下 8 大工程铁律：

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
- **全面聚焦 Taker-Maker 净 EV 套利**：
  - 核心算力与资金倾注于 `Taker-Maker`（吃一挂二）全盘口净 EV 驱动套利（VPS 实盘验证具备 100% 胜率），彻底放弃纯双边吃单（Taker-Taker）。
  - 双边做市（Maker-Maker）必须具备极严苛的盘口成熟度守门（双边买一 $\ge 0.35$），防止在 5min 盘口单边行情中接飞刀失血。
- **买盘 VWAP 订单簿深度加权平仓**：
  - 单边持仓强平市价 FOK 平仓时，必须调用 `PricingEngine.calculate_bid_vwap` 穿透买盘深度逐档加权核算均价，杜绝仅按买一价估算导致的流动性穿透亏损。
- **防恶意插针与一分钱互卷 (Anti-Pennying)**：
  - Maker 挂单跟单严禁无脑 `+0.001` 互卷。必须结合“随机装死迟滞 (1.5~3.5s)”与“阶梯式跃迁 (0.002~0.004)”反卷。
  - 入场前提取 Orderbook 深网计算 OBI，遇到极端单边压迫主动拦截入场。

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
- **真实损益闭环与额度归还**：
  - 强平、做 T 或锁仓结算后必须精确核算扣除双方真实手续费后的净损益 `realized_pnl` / `profit_usdc`，严禁在账本中记录 `$0.0000` 造成失真；赎回后必须无条件流转 `SETTLED` 并 100% 归还风控预扣额度。

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
  - 本地严禁跑主网连网脚本，依赖本地 135+ 项全量单元测试与离线推理。
  - 代码上线统一使用敏捷流水线 `python scripts/vps_ops.py release "feat: 中文提交说明"`，自动完成【回归测试 -> 中文 Commit -> Push -> 远程调用 VPS POST /api/ops/update 免登录秒级热更】。
- **模拟盘高保真度 (Paper Fidelity)**：
  - 模拟模式必须包含真实的 Taker/Maker 手续费扣除、基于 `SIM_BASE_FILL_RATE` 的非 100% 成交判定、以及随机网络延迟与滑点模拟。
