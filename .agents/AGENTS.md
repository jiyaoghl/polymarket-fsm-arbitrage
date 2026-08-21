# Polymarket 交易机器人开发规范 (Project Rules)

在为当前 Polymarket 交易项目（量化套利机器人）编写、修改或审查代码时，你必须严格遵守以下规则：

## 1. 资金与风险控制 (Capital & Risk First)
- **单边敞口保护**：在处理配对交易（如买 YES 买 NO 对冲）或类似逻辑时，必须引入“强平”或“限时撤单”的兜底机制（TTL）。坚决避免因为网络或波动原因导致资金长期锁定在单一方向（leg1_only）。**必须保留现有的 TTL 强平阈值（如 `LEG1_MAX_UNHEDGED_SECONDS = 90s`），到期后哪怕承担滑点也必须强制市价 FOK 平仓，严守资金红线**。
- **浮点数与金额计算**：涉及订单数量（amount）和价格（price）的计算需确保精度，防止微小滑点或负数额度导致请求被 Polymarket API 拒绝。
- **动态拦截禁止永久拉黑**：当某个市场因动态条件（如 K 线波动率过高、OBI 极端等）被临时拦截时，**严禁**将其加入 `processed_markets` 或写入 `db.mark_market_processed()`。动态行情是瞬变的，必须允许下一轮扫描时重新评估。只有市场生命周期正常结束（LOCKED/SETTLED）或硬性失败时才能标记为已处理。
  > **教训来源**：2026-08-20 发现 K 线单边拦截后把市场永久加入黑名单，导致行情恢复后仍然无法入场，机器人表现为“一直没有入场”。

## 2. 网络健壮性与代理兼容 (Network & Proxy Resilience)
- **强制超时与重试 (REST & WS)**：
  - 所有涉及 Polymarket REST API 或外部网络调用的 `requests.get/post` 必须显式设定 `timeout` 参数（如 5s ~ 10s）。必须对核心查询和下单接口包装 `Retry` 机制。
  - **WebSocket 连接必须具备指数退避 (Exponential Backoff) 机制**。在遇到 `HTTP 503` (限流或服务不可用) 或 `1006` 异常断开时，严禁使用固定的短休眠（如 `sleep(1)`）进行死循环重试，否则会导致服务器永久封禁。
- **无效订阅降噪**：对于 Polymarket 抛回的 `INVALID OPERATION` (通常由订阅不存在或未激活的市场引发)，仅做常规 `warning` 拦截，**无需**为了溯源其发生于哪笔交易而增加复杂的上下文记录。
- **代理敏感**：系统运行在国内环境，通常配置了 `HTTP_PROXY`，务必注意在使用 `websocket-client` 或 `requests` 时不要破坏代理设置与 SSL 证书的信任链。

## 3. 状态一致性与 Windows 并发 (State Integrity & Concurrency)
- **防锁死原子写入**：本地通过文件（如 JSON）保存风控状态或缓存时，**严禁**直接裸调用 `os.replace()`。由于项目运行在 Windows 上，可能遭遇多进程或防病毒软件导致的文件占用，必须捕获 `PermissionError (WinError 5)` 并使用带有短暂睡眠机制的重试循环来完成文件原子重命名。
- **状态分离**：交易记录优先使用 SQLite（如 `trading.db`）进行存取，避免超大型文本在内存中直接迭代。
- **SQLite 必须开启 WAL 模式**：`db.py` 的 `get_conn()` 必须在建立连接后执行 `PRAGMA journal_mode=WAL`，并设置 `timeout>=10`。这是多线程并发读写 SQLite 的基本前提，否则 `_fsm_timeout_daemon` 和 `_fsm_ws_listener` 同时写入时会触发 `database is locked`。
- **Dashboard 必须使用安全快照方法读取 active_trades**：`dashboard.py` 中遍历策略的活跃交易时，必须调用 `bot._get_all_active_trades()` 而非直接访问 `bot.active_trades.items()`。`base_strategy.py` 中已提供了带 `RLock` 保护的快照方法，跨模块读取不得绕过。
  > **教训来源**：2026-08-20 代码审计发现 `base_strategy.py` 已有完善的 `_trades_lock` 保护，但 `dashboard.py` 完全绕过了它直接裸读字典，存在 `RuntimeError: dictionary changed size during iteration` 风险。

## 4. 可观测性与日志 (Observability)
- **追踪上下文**：修改或增加核心交易逻辑时，必须保证 `logger` 具备充足的上下文信息，包括但不限于 `market_id`, `token_id`, `strategy_id`, 以及订单状态。
- **清晰排错**：捕获异常时，必须打印出触发异常的核心变量状态；网络异常记录应尽量包含 HTTP 响应码及错误原因。
- **防假死透传机制**：底层的“静默过滤”或风控拦截（如价差过大、深度不足）必须透传并推送到前端 WebSocket 流（记录为 `filter_reason`）。坚决避免底层被限流拦截但前端无任何输出导致的“服务假死焦虑”。
- **风控拦截日志必须携带量化数据**：将拦截原因推送到 `risk_logger` 时，必须附带具体的数值（如振幅百分比、净变动百分比、OBI 值），而非笼统的“单边行情”描述。
  > **教训来源**：2026-08-20 用户反馈“风险拦截一直为空但没有入场”，根因是拦截日志信息过于笼统，且未成功推送到 Dashboard（因 `self.strategy_name` 拼写错误导致线程崩溃）。

## 5. 类型与中文规范
- **Python 类型提示**：新加入或修改的函数必须附带标准的 `typing` 类型提示 (Type Hints)。
- **中文原生意图**：在输出思考、注释或提交记录时，应坚守“中文主谓宾结构 + 英文术语”的 Native Architect 规则，维持可读性。

## 6. Dev-Prod 分离与云端流水线规范 (Cloud-Native Workflow)
- **本地断网开发假设**：AI Agent 应当知晓用户的本地 Windows 环境连接 Polymarket 主网极度不稳定。因此，**严禁**在本地开发时要求用户“直接跑一下连网脚本看看效果”。
- **闭环调试协议**：所有的增量开发、策略参数调整，都应在本地仅执行“静态检查”与“离线代码推理”。修改完毕后，必须提示用户通过 `git commit & push` 提交，依赖于 VPS 上输出的 `trading.db` 交易记录和云端日志来提供反馈，做离线数据回溯优化。
- **跨平台路径安全**：生产环境已迁至 Ubuntu 24.04 (Python 3.12)，开发环境为 Windows (Python 3.10+)。所有涉及目录拼接、读取文件等 I/O 操作，**必须**使用 `pathlib` 或是严格的 `os.path.join`，绝不允许硬编码 `\` 或 `/` 路径符。在云端启动项目的统一入口需保持 `PYTHONPATH=src python3 -m apps.dashboard`。

## 7. 策略模型与做市基准 (Strategy & Execution)
- **放弃纯双边 Taker (No Taker-Taker)**：由于 Python 应用层架构与普通 RPC 存在毫秒至秒级的天然延迟，去尝试抢夺纯盘口双边吃单（Taker-Taker）无异于在竞争最卷的赛道送手续费。在默认配置中应剔除或放弃 `taker_taker` 策略。
- **全面拥抱 Taker-Maker 与长尾市场**：将算力倾注于 `Taker-Maker`（吃一挂二）或 `Maker-Maker` 模型，通过承担极短暂的单边库存风险（由 90s TTL 兜底）去赚取流动性溢价 (Spread)。优先针对流动性较弱但利润空间厚的**长尾事件市场** (Long-tail markets) 进行打击，避开与大机构争夺大选等超级热门盘口。
- **OBI 极端防爆盾 (Spoofing Defense)**：在作为 Taker 准备吃单前，必须提取 Orderbook 深网计算 OBI (订单簿不平衡度)。当检测到 OBI < -0.8 等极端单边压迫时，需主动拦截入场，严防大户撤销支撑盘造成的诱多做市陷阱。
- **规避一分钱互卷与降维防守 (Anti-Pennying War)**：在 Maker 动态跟单 (Pegging) 逻辑中，严禁单纯使用 `best_bid + 0.001` 来无脑争夺买一位置，这会引发 API 撤单风暴并必然遭遇 503 限流。必须采用以下组合机制：
  1. **随机装死迟滞**：被对方反超时，随机等待 1.5~3.5 秒过滤对手高频假动作。
  2. **阶梯式跃迁**：装死期满若必须追单，直接使用 0.002~0.004 的阶梯式跳跃反卷，而非慢吞吞加 0.001。

## 8. 守护线程与异常防护 (Daemon Thread Safety)
- **所有后台守护线程必须有顶层异常守护**：任何 `while True` 循环的守护线程（如 `_fsm_timeout_daemon`），其循环体内部必须用 `try...except Exception` 包裹。捕获异常后必须：(1) 以 `logger.critical()` 记录完整堆栈；(2) 通过 `risk_logger.push_risk_event(level="critical")` 推送到 Dashboard；(3) `continue` 保证线程存活。**绝不允许守护线程因为单个市场的异常而静默死亡**，否则所有 TTL 强平机制将全部失效。
  > **教训来源**：2026-08-20 发现 `_fsm_timeout_daemon` 完全没有 `try...except`，如果任何一行抛出未预期异常，整个止损守护线程会静默退出，所有 LEG1_ONLY 仓位将永远不会被超时平仓。
- **跨模块属性引用必须自查**：在新增代码中引用 `self.xxx` 属性时，必须确认该属性确实在 `__init__` 或父类中定义过。尤其注意 `self.strategy_id`（正确）vs `self.strategy_name`（不存在）这类易混淆的命名。重构后应全局搜索旧属性名确认无残留引用。
  > **教训来源**：2026-08-20 新增 `risk_logger.push_risk_event(strategy=self.strategy_name)` 时误用了不存在的属性 `strategy_name`（正确应为 `strategy_id`），导致风控推送线程崩溃，所有市场进入 FAILED 状态。
- **中文路径文件操作安全**：在 PowerShell 中使用 `Get-Content` 或 `Set-Content` 操作含中文路径的文件时，**绝对路径会因编码问题导致 `PathNotFound` 错误**。必须使用相对路径（如 `.\src\polymarket\strategy_fsm.py`）并显式指定 `-Encoding UTF8`。**严禁**使用 `Get-Content | Set-Content` 的管道模式做全文替换后再回写——一旦路径解析失败，整个文件会被删空。应优先使用 IDE 的 `replace_file_content` 工具进行精确编辑。
  > **教训来源**：2026-08-20 使用 PowerShell 绝对路径操作中文目录下的文件时路径解析失败，导致 `strategy_fsm.py` 被意外删除（748 行代码丢失），需要紧急 `git revert` 回滚。

## 9. 模拟盘仿真诚实性 (Paper Trading Fidelity)
- **EV 计算必须扣除手续费**：利润公式必须包含 Taker/Maker 手续费的扣除项。根据 `leg1_order_type` 和 `leg2_order_type` 动态匹配费率（FOK → Taker 费率，GTC → Maker 费率）。配置项为 `TAKER_FEE_RATE` 和 `MAKER_FEE_RATE`。
- **模拟成交不得 100% 成功**：`_confirm_order_filled` 的模拟分支必须引入基于 `SIM_BASE_FILL_RATE` 的随机概率判定，模拟真实市场中 FOK 订单被拒绝的场景。
- **模拟下单必须包含延迟与滑点**：模拟模式的 `post_order` / `post_order_async` 必须包含 `SIM_LATENCY_MIN_MS ~ SIM_LATENCY_MAX_MS` 的随机延迟，以及 `0 ~ SIM_SLIPPAGE_MAX` 的随机价格滑点（买单向上，卖单向下）。
  > **教训来源**：2026-08-20 模拟盘审计发现 Paper Trading 存在三层严重失真（100% 成交率、零手续费、零滑点），导致模拟盘显示 +$1.28 收益的交易组合，在扣除手续费后实际亏损。

## 10. 实盘执行与跨模块 Client 动态绑定 (Live Trading & Auto-Redeem)
- **跨策略调度器必须动态绑定实盘 Client**：调度管理器（如 `StrategyManager`）中的全局辅助线程（如 `_loop_redeem_closed_markets` 自动结算守护线程），其客户端必须根据策略列表是否存在 `is_live=True` 动态绑定真实实盘签名 Client。严禁在类初始化中硬编码 `is_live=False`，否则实盘盈利后资金将永远无法触发链上自动赎回。
  > **教训来源**：2026-08-21 发现 `StrategyManager.redeem_client` 硬编码为 `is_live=False`，导致实盘策略结束后后台自动结算只走模拟分支，无法向 Polymarket 合约发送真实 `/redeem` 请求。
- **强平与二腿对冲必须严格按首腿实际成交份数对齐 (Shares Alignment)**：在二腿对冲下单及 90s TTL 超时强平时，严禁直接使用静态配置的 `self.order_amount`。必须严格读取首腿实际成交持仓 `leg1_size = float(leg1.get("size") or self.order_amount)`，将二腿下单数量严格对齐首腿已持仓份数，消除因滑点或部分成交导致的单边风险敞口残留。
  > **教训来源**：2026-08-21 审计发现 TTL 强平时二腿直接按 `self.order_amount` 下单，若首腿部分成交或滑点，两腿份数不对等会导致单边风险敞口泄漏。

## 11. CLOB 协议与参数安全防御 (CLOB Guardrails & Precision)
- **价格与数量强制安全钳制**：底层所有 `post_order` 与 `post_order_async` 必须对 `price` 执行强制区间钳制 `safe_price = round(min(max(float(price), 0.001), 0.999), 4)`，对 `amount` 执行严格 `>0` 校验。杜绝浮点数微偏差导致 Polymarket CLOB 撮合引擎抛出 `HTTP 400 Invalid Price`。
- **最小下单门槛与份数规则 (Min Order Size >= 5.0 Shares)**：Polymarket CLOB 撮合引擎的底层最小下单单位是 **5.0 Shares（份数）** 而非纯 USDC。当使用 3U~5U 极小额测试时，必须配合低入场价策略（`entry_max_price <= 0.45`），保证折算的 Token 份数（如 $3.0 / 0.45 \approx 6.67$ 份）严格大于 5.0 门槛。
- **抵押资产规范 (pUSD/USDC.e)**：Polymarket CLOB V2 的链上官方抵押代币为 `pUSD` (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) 与 `Bridged USDC.e` (`0x2791...4174`)。资产查询与鉴权看板中必须包含 `pUSD` 维度的展示。

## 12. RPC 容错与子目录路径规范 (RPC Fallback & Script Paths)
- **多 RPC 节点自动降级轮询 (Multi-RPC Fallback)**：所有链上交互与资产查询脚本（如 `check.py`, `test_auth.py`）严禁仅依赖单一 RPC。必须构建候选列表（`[RPC_URL, polygon-rpc.com, publicnode.com, 1rpc.io]`）并在遇到 403 (如 Alchemy key disabled) 或超时时自动无缝降级重试。
- **脚本子目录导入路径标准**：任何位于 `scripts/` 目录下的独立执行脚本，严禁使用 `os.path.join(os.path.dirname(__file__), "src")`（会拼错为 `scripts/src`）。必须统一使用：
  ```python
  PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))
  ```

