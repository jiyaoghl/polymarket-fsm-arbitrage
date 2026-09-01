# Polymarket 项目持续优化方案 (Optimization Playbook)

> 面向后续 Agent / 开发者的核心工程与量化执行手册。优化目标不是堆微观功能，而是：**提高双腿锁定率 (LEG1 → LOCKED)、压低单腿强平亏损、让实盘规模与资金匹配、彻底消除技术债并保持工程长期可迭代**。  
> **最后更新**：2026-09-01 (已落地阶段 A 观测增强与阶段 C 依赖统一，进入阶段 B 策略配置化与工程解耦)

---

## 0. 数据源铁律：只分析 VPS，严禁本地策略统计

> [!CAUTION]
> 本地开发机网络延迟巨大，撮合成交、盘口深度、LEG1 转化率、PnL 均无实战参考价值。**任何关于胜率、转化率、PnL、未对冲时长的归因分析必须通过 VPS 统一入口获取**。

| 允许行为 (VPS 为唯一定量真理源) | 严厉禁止 (本地失真与误导) |
| :--- | :--- |
| `python scripts/vps_ops.py status` | 读本地 `data/trading.db` 做策略统计与盈亏评估 |
| `python scripts/vps_ops.py logs` / `analyze` | 读本地 `logs/trade*.log` 归因盈亏或计算转化率 |
| VPS Dashboard: `/api/status`, `/api/metrics`, `/api/diagnostics`, `/api/logs/tail` | 用本地 Paper / 本地 live 的延迟、滑点、成交当作调参依据 |
| 从 VPS 拉取的 `vps-logs/`、远程 `trading.db` 快照 | 把 `scratch/` 里对本地库/本地日志的分析结果写进基线 |
| 本地运行 `pytest`、静态检查、改配置后通过 `vps_ops.py release` 发布 | 在本地跑一轮策略再「看看效果好不好」 |

### 统一运维与诊断入口（`VPS_HOST` 动态适配）:
```bash
python scripts/vps_ops.py status      # 远程大盘、活跃仓位、各策略盈亏、延迟快照
python scripts/vps_ops.py logs -n 50  # 远程实时业务与风控日志
python scripts/vps_ops.py analyze     # 北极星转化率卡、分策略盈亏、各出场路径透视表
python scripts/vps_ops.py release "<中文提交>"  # 自动化全量测试 -> 提交 -> 推送 -> VPS秒级热更
```
- **离线深挖规范**：若必须离线深挖日志，先从 VPS 同步到 `vps-logs/`，分析脚本只读该目录，严禁混入本地 `logs/` 与 `data/trading.db`。

---

## 1. 架构定位与系统评价

### 1.1 系统定位
针对 Polymarket CLOB V2 的 **5 分钟 Up/Down** 高频预测市场，执行 **Taker-Maker / Maker-Maker 净 EV 驱动中性套利 + 动态自适应强平** 的高频全自动交易系统。

### 1.2 架构分层全景与成熟度评价

| 层级 | 核心实现 | 工程成熟度与亮点 |
| :--- | :--- | :--- |
| **统一事件循环** | `AsyncRuntime` + `BoundedDropOldestQueue` + `MarketTaskSupervisor` | 避免每市场短命 loop，背压丢旧保新，避免事件堆积引发延迟 |
| **微观盘口网格** | `OrderbookMemoryGrid` 本地内存快照 + VWAP 穿透加权 | 强平与定价不打 REST，杜绝 429 限流，执行延迟压缩至毫秒级 |
| **状态机与调度** | `TradeFSM` + Handler 职责分离 (`Idle`/`PendingBoth`/`Leg1Only`/`PendingLeg2`) | 状态拓扑清晰，生命周期流转明确 |
| **纯数学定价层** | `PricingEngine` (VWAP / OBI / 净 EV / 自适应 flip 幂律定价) | 无任何 I/O 阻塞，纯函数设计，可测性极强 |
| **交易网关抽象** | `ITradingGateway` + `PaperGateway` / `LiveGateway` + 原生 EIP-712 codec | 模拟/实盘零感知无缝切换，高保真度费率与滑点模拟 |
| **资金与风控** | 双资金池预扣 (`RiskManager`)、单市场排他锁、动态 TTL 强平、K 线动量防爆盾 | 资金生命周期闭环，杜绝超额敞口 |
| **运维与交付** | VPS Dashboard、`vps_ops.py` 自动化发布流水线 | 免登录秒级热更新，产品化意识强 |

### 1.3 核心指标与规模简表
- **系统代码量**：约 1.5 万行 Python；
- **自动化测试用例**：**174 项单元测试 100% 绿灯覆盖**；
- **架构评分**：
  - **架构设计**：8.5 / 10（FSM 状态机、领域模型、双网关抽象清晰）；
  - **执行与延迟意识**：8.0 / 10（本地 Grid、直通二腿、HTTP/2 连接池、背压队列到位）；
  - **策略可验证性**：7.5 / 10（已实现转化率指标卡与出场路径归因，硬编码待完全配置化）；
  - **工程整洁度**：7.0 / 10（依赖已统一，待清理 notifier 重复实现与拆分 `dashboard.py`）；
  - **运维与纪律**：9.0 / 10（Playbook 铁律与 VPS 单一真理源已全面执行）。

---

## 2. 核心问题与技术债诊断 (按优先级)

### 2.1 策略层：LEG1 → LOCKED 转化率是唯一北极星
- **现状**：历史主亏损路径集中在 **首腿成交 → 二腿未锁（排队被超/价差拉开） → 触发强平**。
- **痛点**：入场四重守门（15s 静默、价差、OBI、对侧深度）已显著降低失血，但二腿侧仍需细粒度归因（区分是“流动性蒸发”、“追单超调”还是“网络/签名延迟”）。

### 2.2 参数配置化：消除散落在 Handler 中的硬编码
- **痛点**：部分关键守门门槛硬编码在 Handler 代码中（例如开盘静默 `285.0s`、买卖价差 `> 0.05`、做市买一 `≥ 0.38`、OBI `< -0.40`、动态深度基准 `20.0` 份等）。
- **优化方向**：将全部硬编码提取至 `StrategyParams` 与 `configs/strategies.json`，Handler 仅消费配置参数，便于线上 A/B 实验与按资产热更。

### 2.3 工程技术债清单

| 技术债项 | 当前现状 | 优化行动计划 | 优先级 |
| :--- | :--- | :--- | :---: |
| **通知多份重复实现** | `src/notifier.py`、`polymarket/notifier.py`、`services/notifier.py` 三处并存 (~1400 行) | 合并为 `polymarket.services.notifier` 统一单例，顶层作为轻量别名 | P1 |
| **`manager.py` 代码重复** | `_get_traded_market_ids`、`_loop_redeem_closed_markets` 存在重复定义 | 删除冗余重复定义，统一收敛至对应服务模块 | P1 |
| **`dashboard.py` 单文件过大** | 单文件 ~1600 行，FastAPI 路由、前端 HTML/JS 与诊断逻辑强耦合 | 拆分为 `apps/dashboard/` 模块（`routes/`、`static/`、`service/`） | P2 |
| **同步 HTTP 阻塞异步路径** | `kline_analyzer` 中使用同步 `requests` 在线程内拉取 K 线 | 改造为 `aiohttp` 异步非阻塞拉取或后台独立协程刷新内存缓存 | P2 |
| **FSM 钩子异常隔离** | `transition_to` 中 Hook 报错仅打日志，状态已切可能导致动作未做 | 状态流转与副作用事务化，Hook 异常显式转入 `FAILED` 或保护态 | P2 |

---

## 3. 优化北极星 (North Star)

按优先级固定，未经 VPS 数据验证不得跳级：

1. **🥇 LEG1_ONLY → LOCKED 转化率（北极星之首）**
2. **🥈 压低单笔强平亏损与强平频次**
3. **🥉 扣除双方真实手续费后的净 EV / 滚动 PnL**
4. **🏅 未对冲时长时序分布 (P50/P95)、额度归还 100% 闭环**
5. **🎖️ 工程健康度（消除重复代码、单一依赖源、全量测试绿灯）**

---

## 4. 分阶段实施计划 (Phased Roadmap)

### 阶段 A（已完成 ✅）— 止损、去噪与产品化观测
- [x] **资金与策略匹配**：VPS 链上余额 $0.14，`taker_maker_conservative` 置为 `is_live: false` 作为 Paper 演练模式，彻底消除 3U 敞口超限刷屏；
- [x] **产品化量化观测**：
  - `MetricsEngine` 新增 `poly_unhedged_duration_seconds` (未对冲时长直方图)、`poly_dual_exit_sells_total`、`poly_expiry_resolved_total`；
  - VPS `/api/diagnostics` 结构化输出 `conversion_summary`（LOCKED 率、做 T 胜率、分策略盈亏与出场路由明细）；
  - `vps_ops.py analyze` 现代化升级为一键直观北极星指标卡与出场路径拆解。

### 阶段 B（当前重点 · 1～2 周）— 提高双腿锁定率与参数完全配置化
> **目标**：在 VPS Paper 环境开展对照实验，大幅提高 LOCKED 转化率与做 T 脱手率。

| 调优杠杆点 | 调整方向 | 机制说明与落地要求 |
| :--- | :---: | :--- |
| **硬编码阈值完全配置化** | 提取至配置 | 将 `open_silence_sec`, `max_spread`, `mm_min_bid`, `obi_floor`, `base_opp_depth` 等抽离至 `StrategyParams` / `strategies.json` |
| **对侧买盘动态深度壁垒** | 波动率联动 | 20.0~50.0 份动态浮动（已上线初步算法，待按资产/时段配置化） |
| **价差自适应 Anti-Pennying** | 宽/窄价差动态响应 | 宽价差 1.5s 极速抢单，窄价差 3.0s 防卷迟滞；阶梯跃迁 0.002~0.005，受净 EV 保护 |
| **flip_timeout / 自适应 TTL 对齐** | 与波动率联动 | 避免在做 T 让价期与强平 TTL 之间产生逻辑打架 |
| **二腿挂单失败细粒度归因** | 区分错误类型 | 在日志与指标中精准记录“排队被超”、“流动性穿透”、“网络 503”与“签名失败” |

### 阶段 C（持续推进 · 1～2 周）— 工程健康度与架构解耦
- [x] **统一依赖管理**：以 `pyproject.toml` 为单一依赖源，补齐 `aiohttp`, `py-clob-client`, `brotli`, `discord.py`, `web3`；
- [x] **主亏损路径测试补齐**：新增 `tests/test_loss_path_defenses.py`，覆盖二腿下单网络异常容错与强平撤单异常隔离（174 项单测全绿）；
- [ ] **合并清理 Notifier**：废除三套分散实现，统一合并至 `polymarket.services.notifier`；
- [ ] **清理 `manager.py` 重复代码**：移除重复定义的 `_get_traded_market_ids` 与 `_loop_redeem_closed_markets`；
- [ ] **拆分 `apps/dashboard.py`**：将 1600 行巨型单文件解耦为路由层、服务层与静态资源；
- [ ] **K 线拉取异步非阻塞化**：将 `kline_analyzer.py` 的同步 `requests` 改造为 `aiohttp` 异步缓存，杜绝主事件循环卡顿。

---

## 5. 明确不做清单 (Explicit Non-Goals)

1. ❌ **严禁在本地开发机进行策略盈亏统计、胜率归因或延迟评估**；
2. ❌ **严禁为了提高开仓频率而随意放宽入场四重守门（15s 静默/价差/深度/OBI）**；
3. ❌ **严禁在 VPS 上 LEG1_ONLY 转化率未明显改善前加大 `amount` 或开更多激进实盘**；
4. ❌ **严禁在资金规模与单边保护成熟前将双边做市 (Maker-Maker) 作为主攻方向**；
5. ❌ **严禁进行缺乏测试保障的大范围破坏性 import 重构**；
6. ❌ **严禁把本地理想化撮合回测作为实盘上线的唯一依据**。

---

## 6. Agent 执行铁律与协作准则

1. **先查 VPS，后做决策**：需要数据时一律先跑 `python scripts/vps_ops.py status / logs / analyze`，严禁打开本地 `trading.db` 或 `logs/` 做推论；
2. **一次变更只服务一个核心目标**：每次改动清晰说明预期影响的北极星指标；
3. **策略调参只改 `configs/strategies.json`**：严禁在代码中写死魔法数；
4. **全量测试通过方可发布**：每次代码修改后必须运行 `pytest -s tests/` 确保 174+ 项测试 100% 绿灯；
5. **规范化流水线交付**：代码提交必须使用中文 Commit Message，并统一通过 `python scripts/vps_ops.py release "<中文提交>"` 触发 VPS 秒级热更新；
6. **调参或复盘后及时同步基线**：在 `OPTIMIZATION_PLAYBOOK.md` 中更新最新日期与量化成果。
