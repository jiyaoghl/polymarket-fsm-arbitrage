# Polymarket 5 分钟套利机器人

## 项目概述

基于 Polymarket BTC 5 分钟 Up/Down 市场的方向中性微套利机器人，支持 WebSocket 实时监控、自动重连、风控管理和多渠道通知。

### 核心策略

- **首腿入场**：新盘出现 30 秒内，买入 ASK 较低的一边（≤ 0.50）
- **二腿补仓**：另一边 ASK < 0.40 时原子 batch 补仓，锁定正向 EV
- **尾盘止损**：剩余 ≤ 60s 且未补仓时止损卖出首腿
- **自动结算**：定期扫描已结束市场并执行 redeem

---

## 系统改进记录

### P0 - 核心功能

#### ✅ 对接真实 CLOB SDK 实现实盘下单
- 实现 HMAC-SHA256 签名认证
- 支持真实下单、批量下单、redeem 接口
- 添加账户余额查询、持仓查询功能

#### ✅ 集成风控模块到主流程
- 每日最大回撤限制（默认 5%）
- 触发回撤限制时自动暂停交易
- 支持 Telegram 风控告警

### P1 - 稳定性改进

#### ✅ WebSocket 重连机制
- 指数退避重连（1s, 2s, 4s, 8s, 16s, 30s 上限）
- 最大重连次数可配置（默认 5 次）
- 连接断开时自动回退到 REST 轮询

#### ✅ 完善异常处理与日志
- 彩色日志输出（控制台）
- 日志轮转（按时间/大小）
- 错误日志单独文件
- 详细的异常堆栈追踪

### P2 - 功能扩展

#### ✅ 配置管理优化
- 所有策略参数支持环境变量配置
- 添加风控配置项
- 支持通知渠道配置

#### ✅ 回测模块
- 基于历史数据模拟策略执行
- 计算 PnL、夏普比率、最大回撤等指标
- 导出交易记录到 CSV

#### ✅ 通知告警系统
- Telegram Bot 通知
- Discord Webhook 通知
- SMTP 邮件通知
- 统一告警接口

---

## 1. 环境准备

### 依赖安装

```bash
# Python 3.11+
pip install -r requirements.txt
```

### 配置文件

```bash
# 复制环境变量模板
cp configs/.env.example .env

# 编辑 .env 填入 Polymarket CLOB API 密钥
```

### .env 配置说明

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `POLX_PK` | 钱包私钥 | - |
| `POLX_API_KEY` | CLOB API Key | - |
| `POLX_API_SECRET` | API Secret | - |
| `POLX_API_PASSPHRASE` | API Passphrase | - |
| `ENTRY_MAX_PRICE` | 首腿最高入场价 | 0.50 |
| `REENTRY_TRIGGER_PRICE` | 二腿补仓触发价 | 0.40 |
| `STOP_LOSS_TIME` | 止损剩余时间 (秒) | 60 |
| `ORDER_AMOUNT` | 单笔交易金额 (USDC) | 10.0 |
| `DAILY_MAX_DRAWDOWN` | 每日最大回撤 | 0.05 (5%) |
| `INITIAL_CAPITAL` | 初始资金 | 1000.0 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | - |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | - |
| `DISCORD_WEBHOOK_URL` | Discord Webhook URL | - |

---

## 2. 运行方式

### 方式一：仅运行策略（无前端）

```bash
python -m apps.manager
```

日志输出到控制台和 `logs/trade.log` 文件。

### 方式二：运行策略 + Web Dashboard

```bash
python -m apps.dashboard
```

然后打开浏览器访问 `http://localhost:8888`

### 方式三：运行回测

```bash
python -m apps.run_backtest --days 3
```

### 方式四：Docker 运行

```bash
# 构建镜像
docker build -t polymarket-5m-bot .

# 运行容器
docker run --env-file .env polymarket-5m-bot
```

---

## 3. 策略配置

`configs/strategies.json` 中可配置多套策略：

```json
[
    {
        "strategy_id": "conservative_001",
        "name": "保守策略",
        "entry_max_price": 0.45,
        "reentry_trigger": 0.35,
        "amount": 5.0,
        "is_live": false
    },
    {
        "strategy_id": "standard_live",
        "name": "标准实盘",
        "entry_max_price": 0.50,
        "reentry_trigger": 0.40,
        "amount": 10.0,
        "is_live": true
    }
]
```

---

## 4. 风控与状态

### 每日回撤限制

- 默认每日最大回撤 5%
- 触发后自动暂停新市场发现
- 发送 Telegram 告警通知

### 状态存储

- 按天记录累计盈亏与最大回撤
- 存储于 `tmp/state.json` 文件（默认，可配置）
- 可用于生成日报/周报

---

## 5. 通知告警

### 支持的告警类型

| 类型 | 触发条件 | 通知渠道 |
|------|----------|----------|
| 交易通知 | 开仓/平仓 | Telegram, Discord |
| 盈利通知 | PnL > 0 | Telegram, Discord |
| 亏损通知 | PnL < 0 | Telegram, Discord |
| 风控告警 | 触发回撤限制 | Telegram, Discord (紧急) |
| 错误告警 | 系统异常 | Telegram, Discord (紧急) |
| 系统事件 | 启动/停止 | Telegram, Discord |

### 配置通知

```bash
# .env 中配置
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=-1001234567890
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## 6. 日志系统

### 日志文件

| 文件 | 说明 | 轮转策略 |
|------|------|----------|
| `trade.log` | 主日志文件 | 按天轮转，保留 7 天 |
| `trade_detailed.log` | 详细调试日志 | 按大小轮转，10MB/文件 |
| `trade_error.log` | 错误日志 | 按大小轮转，10MB/文件 |

### 日志级别

- `DEBUG`: 详细调试信息
- `INFO`: 一般信息
- `WARNING`: 警告信息
- `ERROR`: 错误信息
- `CRITICAL`: 严重错误

---

## 7. 回测功能

### 运行回测

```bash
python -m apps.run_backtest --days 3
```

也可以使用更完整的参数入口：

```bash
python -m apps.run_backtest --days 3 --mc-paths 500
```

### 回测数据口径（当前版本）

目前仓库内**尚未采集 L2 盘口快照**，因此回测使用“公开接口近似”模式：

- 从 Gamma 枚举 `btc-updown-5m-<ts>` 市场（按 5 分钟窗口）
- 使用市场的 `outcomePrices` 作为初始价格，并生成近似的 bid/ask 序列（随机游走 + 可控 spread）
- 到期结算采用 **隐含概率蒙特卡洛**：用到期前 YES 的 mid 近似概率，进行多路径随机结算估计期望 payout

这套口径适合：
- 验证回测框架/指标/参数敏感性
- 没有历史 L2 的情况下做近似评估

如果你后续补齐 L2 快照采集，只需要新增一个 L2 feed 即可升级为逐档撮合回测。

### 回测指标

- 总交易数
- 胜率
- 总盈亏
- 平均盈利/亏损
- 最大回撤
- 夏普比率
- 盈亏比
- 平均持仓时间

### 导出回测数据

回测结果自动导出到 `data/backtest_out/`：

- `backtest_out/trades.csv`: 按市场聚合的策略交易结果
- `backtest_out/fills.csv`: 订单级成交记录
- `backtest_out/equity.csv`: 资金曲线

---

## 8. 项目结构

```
src/
├── polymarket/        # 主包（真实实现）
├── apps/              # 扁平入口（桥接到 polymarket.apps.*）
├── tools/             # 扁平工具入口（桥接到 polymarket.tools.*）
├── client.py          # 扁平模块入口（桥接到 polymarket.client）
├── config.py          # 扁平模块入口（桥接到 polymarket.config）
├── db.py              # 扁平模块入口（桥接到 polymarket.db）
├── logger.py          # 扁平模块入口（桥接到 polymarket.logger）
├── notifier.py        # 扁平模块入口（桥接到 polymarket.notifier）
├── strategy.py        # 扁平模块入口（桥接到 polymarket.strategy）
└── trade_state.py     # 扁平模块入口（桥接到 polymarket.trade_state）
configs/               # 配置与模板（strategies.json/.env.example）
scripts/               # 启动/关停脚本
data/                  # 回测输出
logs/                  # 日志输出
tmp/                   # 运行期状态/缓存/锁文件
docker/                # Dockerfile
tests/                 # 单元测试
docs/                  # 文档与设计
```

---

## 9. 风险提示

⚠️ **重要提示**：

1. 本软件仅供学习研究，不构成投资建议
2. 加密货币交易存在高风险，可能导致本金损失
3. 实盘前请充分回测验证策略有效性
4. 请根据自身风险承受能力设置仓位

---

## 10. 许可证

MIT License