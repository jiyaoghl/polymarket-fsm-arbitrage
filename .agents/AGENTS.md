# Polymarket 交易机器人开发规范 (Project Rules)

在为当前 Polymarket 交易项目（量化套利机器人）编写、修改或审查代码时，你必须严格遵守以下规则：

## 1. 资金与风险控制 (Capital & Risk First)
- **单边敞口保护**：在处理配对交易（如买 YES 买 NO 对冲）或类似逻辑时，必须引入“强平”或“限时撤单”的兜底机制（TTL）。坚决避免因为网络或波动原因导致资金长期锁定在单一方向（leg1_only）。
- **浮点数与金额计算**：涉及订单数量（amount）和价格（price）的计算需确保精度，防止微小滑点或负数额度导致请求被 Polymarket API 拒绝。

## 2. 网络健壮性与代理兼容 (Network & Proxy Resilience)
- **强制超时与重试**：所有涉及 Polymarket REST API 或外部网络调用的 `requests.get/post` 必须显式设定 `timeout` 参数（如 5s ~ 10s）。必须对核心查询和下单接口包装 `Retry` 机制（使用指数退避处理 `401`, `429`, `Timeout` 等异常）。
- **代理敏感**：系统运行在国内环境，通常配置了 `HTTP_PROXY`，务必注意在使用 `websocket-client` 或 `requests` 时不要破坏代理设置与 SSL 证书的信任链。

## 3. 状态一致性与 Windows 并发 (State Integrity & Concurrency)
- **防锁死原子写入**：本地通过文件（如 JSON）保存风控状态或缓存时，**严禁**直接裸调用 `os.replace()`。由于项目运行在 Windows 上，可能遭遇多进程或防病毒软件导致的文件占用，必须捕获 `PermissionError (WinError 5)` 并使用带有短暂睡眠机制的重试循环来完成文件原子重命名。
- **状态分离**：交易记录优先使用 SQLite（如 `trading.db`）进行存取，避免超大型文本在内存中直接迭代。

## 4. 可观测性与日志 (Observability)
- **追踪上下文**：修改或增加核心交易逻辑时，必须保证 `logger` 具备充足的上下文信息，包括但不限于 `market_id`, `token_id`, `strategy_id`, 以及订单状态。
- **清晰排错**：捕获异常时，必须打印出触发异常的核心变量状态；网络异常记录应尽量包含 HTTP 响应码及错误原因。

## 5. 类型与中文规范
- **Python 类型提示**：新加入或修改的函数必须附带标准的 `typing` 类型提示 (Type Hints)。
- **中文原生意图**：在输出思考、注释或提交记录时，应坚守“中文主谓宾结构 + 英文术语”的 Native Architect 规则，维持可读性。
