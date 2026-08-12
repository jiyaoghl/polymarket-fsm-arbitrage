## 仓库结构（标准化后）

```
.
├─ src/polymarket/          # 主代码包（src 布局，唯一包根）
│  ├─ apps/                 # 真实入口实现（对外推荐使用扁平入口 `python -m apps.*`）
│  ├─ backtest/             # 回测引擎与数据源
│  ├─ tools/                # 工具：ai_refactor 等
│  ├─ paths.py              # 统一路径约定（configs/tmp/data/logs）
│  ├─ config.py             # 环境变量与运行参数（支持 DOTENV_PATH）
│  ├─ client.py             # Gamma/CLOB 客户端
│  ├─ db.py                 # SQLite 队列与持仓台账
│  ├─ strategy.py           # 策略核心（WS + 风控逻辑）
│  ├─ trade_state.py        # 每日回撤状态存储（默认 tmp/state.json）
│  ├─ notifier.py           # Telegram/Discord/SMTP 通知
│  └─ logger.py             # 日志（默认 logs/ 下轮转）
├─ configs/                 # 配置与模板（.env.example/strategies.json）
├─ scripts/                 # 运维脚本（start/stop）
├─ data/                    # 数据输出（回测结果）
├─ logs/                    # 日志输出目录（运行时生成）
├─ tmp/                     # 运行期状态/缓存/锁文件（halt、db、cache）
├─ docker/                  # 容器构建文件
├─ tests/                   # 单元测试（通过 conftest.py 指向 src）
├─ docs/                    # 文档与设计方案
├─ structure_diff.json      # 迁移映射（原路径 -> 新路径）
├─ refactor_report.md       # 重构报告
└─ .aiconfig/               # AI 重构历史与 manifest
```

## 依赖边界与约定

- `polymarket.paths` 是**默认路径唯一真相源**；禁止在业务代码中散落 `halt/`、`backtest_out/` 等硬编码相对路径。
- 对外运行入口采用扁平风格：`python -m apps.*`；`src/apps/*` 仅桥接到 `polymarket.apps.*`，真实实现仍在包内，便于测试与复用。
- `.env` 默认在仓库根；也支持 `configs/.env` 或通过 `DOTENV_PATH` 显式指定。

## 变更记录

- 2026-03-19：迁移到 `src/` 布局；引入 `configs/ scripts/ data/ logs/ tmp/ docker/` 标准层级；批量改写 import 与路径默认值；新增 `ai_refactor` CLI 骨架与 `.aiconfig/manifest.yaml`。

