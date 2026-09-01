# Polymarket 项目持续优化方案 (Optimization Playbook)

> 面向后续 Agent / 开发者的执行手册。优化目标不是堆功能，而是：**提高双腿锁定率、压低单腿强平亏损、让实盘规模与资金匹配、保持工程可迭代**。  
> **最后更新**：2026-08-31

---

## 0. 数据源铁律：只分析 VPS，禁止本地策略统计

> [!CAUTION]
> 本地开发机延迟巨大，成交、盘口、LEG1 转化、PnL 均无参考意义。任何策略结论必须以 VPS 远程数据为唯一准绳。

| 允许行为 (VPS 为准) | 严厉禁止 (本地失真) |
| :--- | :--- |
| `python scripts/vps_ops.py status` | 读本地 `data/trading.db` 做策略统计与盈亏评估 |
| `python scripts/vps_ops.py logs` / `analyze` | 读本地 `logs/trade*.log` 归因盈亏或计算转化率 |
| VPS Dashboard: `/api/status`, `/api/metrics`, `/api/diagnostics`, `/api/logs/tail` | 用本地 Paper / 本地 live 的延迟、滑点、成交当作调参依据 |
| 从 VPS 拉取的 `vps-logs/`、远程 `trading.db` 快照 | 把 `scratch/` 里对本地库/本地日志的分析结果写进基线 |
| 本地运行 `pytest`、静态检查、改配置后通过 `vps_ops.py release` 发布 | 在本地跑一轮策略再「看看效果好不好」 |

### 统一运维入口（`VPS_HOST`，默认 `http://161.120.171.236:8888`）:
```bash
python scripts/vps_ops.py status      # 远程大盘、仓位、各策略盈亏、延迟快照
python scripts/vps_ops.py logs -n 80  # 远程实时日志
python scripts/vps_ops.py analyze     # 远程 diagnostics：LOCKED / 强平 / 净 EV
```
- **本地定位**：改代码、改 `configs/strategies.json`、跑单元测试、`vps_ops.py release` 推送到 VPS。
- **基线表与复盘**：调参对错一律用上述 VPS 命令刷新后再写。
- **离线分析原则**：若必须离线深挖，先从 VPS 同步日志/DB 到 `vps-logs/`，分析脚本只读该目录，严禁混入本地 `logs/` 与 `data/trading.db`。

---

## 1. 当前基线 (Current Baseline)

> **来源约定**：VPS `/api/diagnostics` + `/api/status`，非本地归档。调参前必须重跑 `analyze` 刷新。

| 指标维度 | 上次 VPS 快照 (调参前请重拉) |
| :--- | :--- |
| **LOCKED (双腿套利锁定)** | `taker_maker_standard` / `aggressive` 各约 7 笔 |
| **LEG1_ONLY (单腿暴露)** | 31 / 34 笔，远高于 LOCKED |
| **强平 / 止损触发** | 44 / 47 次 |
| **归档 PnL** | 合计约 -$58.7，均值约 -$0.90 |
| **实盘余额** | 约 $0.14，敞口上限约 $0.13（以 VPS 链上刷新为准） |
| **实盘策略** | 仅 `taker_maker_conservative` (`amount=3.0`) 为 `is_live=true` |
| **风控拦截** | 敞口超限约 71 次（3U 首腿 vs 0.14U 余额） |

> **核心结论**：主亏损路径是 **首腿成交 → 二腿未锁 → 强平**；实盘因资金不足（$0.14 余额 vs 3U 策略）被风控挡死。**核心任务是先修转化率与去噪，再谈加仓与扩容。**

---

## 2. 优化北极星 (North Star)

按优先级固定，未经 VPS 数据验证不得跳级：

1. **🥇 LEG1_ONLY → LOCKED 转化率（核心核心）**
2. **🥈 强平单笔亏损与强平次数**
3. **🥉 扣费后净 EV / 滚动 PnL（按策略、按市场）**
4. **🏅 未对冲时长、孤儿单防御、额度归还一致性**
5. **🎖️ 工程健康度（重复模块清理、依赖收敛、测试覆盖主亏损路径）**

### 看板必须包含的指标（全部来自 VPS metrics/diagnostics）：
- LOCKED 率、LEG1_ONLY 率、强平次数与原因分布；
- 按策略滚动 PnL；
- 未对冲持有时间 P50 / P95；
- 链上真实余额 vs 内部风控额度；
- API 429 / 签名失败 / WS 断线率；
- VPS 侧下单 / 撤单 / WS 网络延迟（严禁用本机延迟代替）。

> 🚫 **红线**：禁止只看开仓次数或“感觉有机会”；禁止用本地延迟解释“为什么没锁上二腿”。

---

## 3. 优化推进节奏 (Cadence)

| 周期 | 核心动作 | 数据源准绳 |
| :--- | :--- | :--- |
| **每个 5min 盘后** | 扫描异常：`LEG1_ONLY`、强平、孤儿单、额度未归还 | `vps_ops.py status` / `logs` |
| **每日** | 按策略汇总转化率与 PnL；对照参数是否被热更覆盖 | `vps_ops.py analyze` |
| **每周** | 1～2 个高杠杆参数实验（改配置 → release 到 VPS Paper/live） | 实验效果只看 VPS |
| **每双周** | 复盘：成功路径 vs 亏损路径；用 VPS 真实数据更新基线表 | VPS `/api/diagnostics` |
| **每季度** | 清理技术债、依赖、收敛 scratch 脚本（scratch 仅允许读 `vps-logs/`） | 生产仓库代码审计 |

- **基本原则**：小步演进、随时可回滚、先 VPS Paper 验证后再上 VPS Live、一次只调整 2～3 个参数。
- **本地 PaperGateway 仅用于单元测试与回放正确性验证，严禁用于策略表现评估。**

---

## 4. 分阶段实施计划 (Phased Roadmap)

### 阶段 A（本周）— 止损与去噪
> **目标**：停止无效实盘噪声，把主亏损路径转化为 VPS 清晰可观测指标。

- [x] **资金与策略匹配**：以 VPS 链上余额为准。`taker_maker_conservative.amount=3.0` 与 `~$0.14` 余额不匹配时：暂时置 `is_live: false` 转为 VPS Paper 演练，避免高频刷屏敞口拦截。
- [x] **确认 VPS 单实例**：检查远程日志，确保策略管理器与网关为单实例运行，排查多进程残留。
- [x] **产品化观测（挂在 VPS Dashboard）**：LOCKED / LEG1_ONLY / 强平原因 / 按策略 PnL 必须稳定被 `vps_ops.py analyze` 捕获。
- [x] **拦截日志分级**：敞口超限日志降低频次，避免淹没 EV / 深度等关键业务拦截。

### 阶段 B（1～2 周）— 提高双腿锁定率
> **目标**：在不盲目增加开仓频率的前提下，大幅提高 LOCKED 转化率。所有对照实验必须在 VPS 上运行。

| 调优杠杆点 | 调整方向 | 机制说明 |
| :--- | :---: | :--- |
| **对侧买盘深度 / OBI** | 偏严 | 现有约 $\ge 20$ 份，高波动时动态提高，杜绝二腿无流动性承接 |
| **Anti-Pennying 冷却与加价** | 动态排队 | 冷却 3s、加价 0.002～0.004；上限严格受净利差 $\ge 0.2\%$ 约束 |
| **flip_timeout / 撤单时限** | 与自适应 TTL 对齐 | 避免挂单拖延成单边持仓后再触发强平 |
| **entry_max_price** | 严格保守 | 0.40 / 0.42 / 0.44 对照；入场门槛越宽，LEG1_ONLY 暴露越高 |
| **开盘静默 15s / 1m 动量飞刀** | 保持或偏严 | 从源头杜绝接飞刀，绝不为了开仓频率而放宽安全门槛 |

### 阶段 C（持续）— 工程健康度
- [x] **消除 `src/` 顶层与 `src/polymarket/` 重复模块**（已完成：顶层改为纯转发，新代码统一 `from polymarket.X import ...`）；
- [ ] **统一依赖管理**：以 `pyproject.toml` 为单一源，对齐 `requirements.txt`；
- [ ] **收敛 `scratch/` 脚本**：仅允许分析 `vps-logs/` 或远程 API，删除直接读本地 `trading.db` 与 `logs/` 的旧脚本；
- [ ] **拆分 `apps/dashboard.py`**：将 API 路由、WS 成交流与前端渲染拆分解耦；
- [ ] **测试针对性覆盖主亏损路径**：补齐“首腿成交 $\rightarrow$ 二腿挂单延迟/失败”及“强平 + 全量撤单 + 孤儿单防御”的单元测试。

---

## 5. 风控与资金闭环准则

- 余额、敞口、自动 Redeem、额度归还一律以 **VPS 链上刷新 + VPS DB** 为唯一准绳；
- 单市场 / 单策略设立未对冲时长与名义敞口硬上限，超限触发报警；
- 实盘加仓前提：仅当阶段 B 在 VPS 上连续达标，且 VPS 真实余额 $\ge \text{amount} \times 5\sim 10$ 笔并发缓冲时方可开仓。

---

## 6. 明确不做清单 (Explicit Non-Goals)

1. ❌ **禁止在本地做策略统计、延迟评估与盈亏归因**；
2. ❌ **严禁为了“看起来能多开仓”而放宽入场四重守门**；
3. ❌ **严禁在 VPS 上 LEG1_ONLY 未明显下降前开启更多激进策略或加大 amount**；
4. ❌ **严禁把双边做市 (Maker-Maker) 作为当前主攻方向**；
5. ❌ **严禁在未统一 import 路径前进行不可控的大范围破坏性重构**；
6. ❌ **严禁把本地回测美化价格或本地 Paper 延迟当作上线依据**。

---

## 7. Agent 执行铁律与约定

1. **先读本手册与 `configs/strategies.json`**：需要量化数据时一律先执行 `vps_ops.py status / analyze`，严禁打开本地 `trading.db` 或 `logs/` 作为分析依据；
2. **一次变更只服务一个北极星指标**：说明中写明 VPS 对照窗口与预期指标；
3. **策略参数只在 `configs/strategies.json` 中调整**，严禁在 handler 代码中硬编码魔法数；
4. **实盘改动必须随时可关闭**，效果验证统一在 VPS Paper / Live 环境；
5. **每次有效 VPS 实验或复盘后，更新基线表与日期**；
6. **所有代码提交必须使用中文 Commit Message，并通过 `python scripts/vps_ops.py release "<中文提交>"` 发布**；
7. **编写分析脚本时，数据源固定为 VPS 同步的 `vps-logs/` 或远程 API，文件名/注释标明 `source=vps`**。
