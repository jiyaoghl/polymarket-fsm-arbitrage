# Polymarket 策略模板与参数调优指南 (Strategy Template Guide)

## 1. 策略模型概览

系统中所有策略均在 `configs/strategies.json` 中配置，系统支持同时并发运行多个不同参数或模型的策略实例。

```
┌────────────────────────────────────────────────────────────────────────┐
│                       Polymarket 策略三大模型                          │
├──────────────────────┬─────────────────────────┬───────────────────────┤
│   Taker-Maker (吃挂)  │   Maker-Maker (双边挂)   │   Taker-Taker (双吃)  │
├──────────────────────┼─────────────────────────┼───────────────────────┤
│ • 首腿 FOK 市价吃单   │ • 首腿 GTC 限价挂单      │ • 首腿 FOK 市价吃单   │
│ • 二腿 GTC 做市对冲   │ • 二腿 GTC 限价挂单      │ • 二腿 FOK 市价吃单   │
│ • 享受二腿流动性溢价  │ • 双边 0 手续费 (免佣)   │ • 零单边库存暴露      │
│ • 90s TTL 强平兜底   │ • 原子级双边并发挂单     │ • 易受网络延迟与滑点  │
│ ★ 推荐主力实盘使用   │ ★ 推荐主力实盘使用       │ ⚠️ 默认不推荐        │
└──────────────────────┴─────────────────────────┴───────────────────────┘
```

---

## 2. 策略配置参数字典 (Parameter Reference)

每个策略在 `configs/strategies.json` 中为一个独立的 JSON 对象，包含以下字段：

| 参数字段 (Key) | 数据类型 | 默认/推荐值 | 业务含义与作用说明 |
| :--- | :--- | :--- | :--- |
| `strategy_id` | `string` | `"taker_maker_standard"` | **策略唯一标识符**。用于日志追踪、风控锁仓及 SQLite 数据隔离。 |
| `name` | `string` | `"吃单+挂单 标准策略"` | **策略中文显示名称**。展示在 Web 可视化看板及通知面板中。 |
| `description` | `string` | `"主力均衡策略..."` | **策略描述**。简述该策略的定位与适用场景。 |
| `is_live` | `boolean` | `false` | **实盘开关**。`true`: 连接真实钱包发送链上签名订单；`false`: 本地高保真模拟盘演练。 |
| `amount` | `number` | `10.0` | **单笔订单基础金额 (USDC)**。如 `10.0` 代表每次开仓投入约 $10。系统会自动根据价格折算为 Shares（保证 $\ge 5.0$ 份）。 |
| `entry_max_price` | `number` | `0.50` | **首腿最大入场价**。若盘口最优卖价高于此值则放弃吃单（保留至少 $1.0 - 0.50 = 0.50$ 的对冲空间）。 |
| `entry_min_price` | `number` | `0.25` | **首腿最小入场价**。若盘口低于此值放弃吃单（防黑天鹅小概率事件归零）。 |
| `reentry_trigger` | `number` | `0.42` | **二腿反卷对冲触发阈值**。当二腿盘口价格低于此值时，触发动态阶梯跃迁抢回买一。 |
| `leg1_order_type` | `string` | `"FOK"` / `"GTC"` | **首腿订单类型**。`"FOK"`: 市价立即吃单，未满即撤；`"GTC"`: 限价挂单等待被吃。 |
| `leg2_order_type` | `string` | `"GTC"` | **二腿订单类型**。通常为 `"GTC"` 做市挂单，赚取流动性溢价。 |
| `exit_mode` | `string` | `"dual_exit"` / `"smart_flip"` | **二腿智能出场模式**。<br>• `"dual_exit"`: **OCO 双出口并发模式**（同时挂出做T卖单与对冲买单，任意一边成交立即撤销另一边）；<br>• `"smart_flip"`: **智能做T优先模式**（优先做T卖出，超时或下行无缝切对冲）；<br>• `"pair_only"`: **传统纯对冲模式**（仅挂出反向买单）。 |
| `initial_margin` | `number` | `0.025` | **初始做T期望毛利率**。例如 `0.025` 代表首选挂单追求 2.5% 利差高抛。 |
| `breakeven_margin` | `number` | `0.002` | **保本安全毛利率**。时间衰减后的最低保本毛利门槛 (0.2%)。 |
| `flip_timeout_sec` | `number` | `35` | **做T高抛最长等待时间 (秒)**。超时若买方未吃单，自动无缝切换至反向对冲买入。 |
| `dual_bracket_entry` | `boolean` | `false` / `true` | **双边并发双挂开关**。仅在 `maker_maker` 模式下设为 `true`，通过批量接口原子级同时挂出 YES 与 NO。 |
| `leg2_price_mode` | `string` | `"bid"` | **二腿定价参考**。`"bid"`: 参考买一价进行跟单做市；`"ask"`: 参考卖一价。 |
| `leg2_cancel_before_expiry`| `number` | `30` | **到期前强制撤单时间 (秒)**。剩余时间小于该值且二腿未成交时自动撤单。 |
| `leg2_fallback_to_maker` | `boolean` | `true` | **二腿做市保底开关**。若二腿无法直接撮合，自动降级为盘口做市挂单。 |

---

## 3. 实战标准模板推荐 (4 大经典策略组合)

### 模板 1：`taker_maker_conservative` (保守防守型 · 3U 实盘起步)
> **适用场景**：新账户实盘小资金试水、极小滑点、严控首腿成本（≤0.40）、双出口快速离场。
```jsonc
{
    "strategy_id": "taker_maker_conservative",        // [必填] 策略唯一ID
    "name": "吃单+挂单 保守型策略 (3U起步)",           // [必填] 策略中文名称
    "description": "首腿FOK市价吃单，二腿双出口并发退出。低入场价区间，严格防滑点与小额安全试水", // 策略描述
    "is_live": true,                                  // [必填] 实盘开关: true=实盘, false=模拟盘
    "is_active": true,                                // [必填] 策略激活状态
    "amount": 3.0,                                   // [必填] 单笔金额 (USDC)，折算份数保证 >= 5.0
    "entry_max_price": 0.40,                         // [必填] 首腿最大买价 (最高0.40，留足60%对冲空间)
    "entry_min_price": 0.30,                         // [必填] 首腿最小买价 (最低0.30，防极端事件)
    "reentry_trigger": 0.35,                         // [必填] 二腿反卷买一阈值
    "min_time_to_expiry_entry": 150.0,               // 临近交割禁止入场时间 (秒)
    "leg1_order_type": "FOK",                        // [必填] 首腿订单类型: FOK 市价吃单
    "leg2_order_type": "GTC",                        // [必填] 二腿订单类型: GTC 做市挂单
    "leg2_price_mode": "bid",                        // [必填] 二腿定价参考: bid 买一价
    "dual_bracket_entry": false,                     // 首腿是否双挂: false
    "exit_mode": "dual_exit",                        // [必填] 出场模式: dual_exit 双出口并发
    "initial_margin": 0.015,                         // [必填] 初始做T期望毛利: 1.5%
    "breakeven_margin": 0.002,                       // [必填] 保本安全最低毛利: 0.2%
    "flip_timeout_sec": 35,                          // [必填] 做T等待衰减周期: 35秒
    "leg2_cancel_before_expiry": 30,                 // [必填] 到期前强制撤单时间: 30秒
    "leg2_fallback_to_maker": true                   // [必填] 挂单取消后是否允许降级做市
}
```

---

### 模板 2：`taker_maker_standard` (均衡主力型 · 10U 日常套利)
> **适用场景**：主力均衡策略，兼顾开仓成交率与利差利润（≤0.42），适合 BTC / ETH / SOL 5min 盘口。
```jsonc
{
    "strategy_id": "taker_maker_standard",            // [必填] 策略唯一ID
    "name": "吃单+挂单 标准主力策略",                 // [必填] 策略中文名称
    "description": "主力均衡策略，平衡开仓频率与利差收益，适合日常自动化套利",
    "is_live": false,                                 // [必填] 实盘开关
    "is_active": true,                                // [必填] 策略激活状态
    "amount": 10.0,                                  // [必填] 单笔金额: $10.0 USDC
    "entry_max_price": 0.42,                         // [必填] 首腿最大买价: 0.42 (留足58%空间)
    "entry_min_price": 0.30,                         // [必填] 首腿最小买价: 0.30
    "reentry_trigger": 0.35,                         // [必填] 二腿反卷阈值: 0.35
    "min_time_to_expiry_entry": 150.0,               // 临近交割禁止入场时间: 150s
    "leg1_order_type": "FOK",                        // [必填] 首腿 FOK
    "leg2_order_type": "GTC",                        // [必填] 二腿 GTC
    "leg2_price_mode": "bid",                        // [必填] 二腿参考买一
    "dual_bracket_entry": false,                     // 首腿单边吃单
    "exit_mode": "dual_exit",                        // [必填] 出场模式: dual_exit 双出口并发
    "initial_margin": 0.015,                         // [必填] 初始期望毛利: 1.5%
    "breakeven_margin": 0.002,                       // [必填] 保本最低毛利: 0.2%
    "flip_timeout_sec": 35,                          // [必填] 衰减周期: 35秒
    "leg2_cancel_before_expiry": 30,                 // [必填] 到期前撤单: 30秒
    "leg2_fallback_to_maker": true                   // [必填] 挂单取消降级做市: true
}
```

---

### 模板 3：`maker_maker_conservative` (低频做市观察型 · 3U 极小额)
> **适用场景**：仅在双边买一 $\ge 0.38$ 成熟盘口下进行极低额度双边挂单观察，严防单边接飞刀。
```jsonc
{
    "strategy_id": "maker_maker_conservative",        // [必填] 策略唯一ID
    "name": "挂单+挂单 保守做市策略",                 // [必填] 策略中文名称
    "description": "极低额度双边限价做市观察，严苛流动性守门(买一>=0.38)，单腿成交立即转入OCO脱手",
    "is_live": false,                                 // [必填] 实盘开关
    "is_active": true,                                // [必填] 策略激活状态
    "amount": 3.0,                                   // [必填] 单笔金额: $3.0 USDC
    "dual_bracket_entry": true,                      // [必填] 开局双边原子并发双挂！
    "entry_max_price": 0.42,                         // [必填] 首腿最大买价: 0.42
    "entry_min_price": 0.30,                         // [必填] 首腿最小买价: 0.30
    "reentry_trigger": 0.35,                         // [必填] 二腿反卷阈值: 0.35
    "min_time_to_expiry_entry": 150.0,               // 临近交割禁止入场: 150s
    "leg1_order_type": "GTC",                        // [必填] 首腿 GTC 限价单 (0费率)
    "leg2_order_type": "GTC",                        // [必填] 二腿 GTC 限价单 (0费率)
    "leg2_price_mode": "bid",                        // [必填] 参考买一
    "exit_mode": "dual_exit",                        // [必填] 单边残留时激活 dual_exit 双出口脱身
    "initial_margin": 0.015,                         // [必填] 初始毛利: 1.5%
    "breakeven_margin": 0.002,                       // [必填] 保本毛利: 0.2%
    "flip_timeout_sec": 35,                          // [必填] 衰减周期: 35秒
    "leg2_cancel_before_expiry": 30,                 // [必填] 撤单时间: 30秒
    "leg2_fallback_to_maker": true                   // [必填] 降级做市: true
}
```

---

### 模板 4：`maker_maker_wide_spread` (宽价差高利润型 · 专打长尾/SOL)
> **适用场景**：专打价差较厚（3%~5%）的盘口，要求更高的利润空间才入场挂单。
```jsonc
{
    "strategy_id": "maker_maker_wide_spread",         // [必填] 策略唯一ID
    "name": "挂单+挂单 宽价差高利润策略",             // [必填] 策略中文名称
    "description": "专打流动性较薄但价差厚的盘口，互补定价锁定2.5%以上高毛利",
    "is_live": false,                                 // [必填] 实盘开关
    "amount": 10.0,                                  // [必填] 单笔金额: $10.0 USDC
    "dual_bracket_entry": true,                      // [必填] 开局双边并发双挂
    "entry_max_price": 0.48,                         // [必填] 首腿最大买价: 0.48
    "entry_min_price": 0.20,                         // [必填] 首腿最小买价: 0.20
    "reentry_trigger": 0.40,                         // [必填] 二腿反卷阈值: 0.40
    "min_time_to_expiry_entry": 45,                  // 临近交割禁止入场: 45s
    "leg1_order_type": "GTC",                        // [必填] 首腿 GTC
    "leg2_order_type": "GTC",                        // [必填] 二腿 GTC
    "leg2_price_mode": "bid",                        // [必填] 参考买一
    "exit_mode": "dual_exit",                        // [必填] 双出口脱身
    "initial_margin": 0.035,                         // [必填] 初始高毛利: 3.5%
    "breakeven_margin": 0.002,                       // [必填] 保本毛利: 0.2%
    "flip_timeout_sec": 35,                          // [必填] 衰减周期: 35秒
    "leg2_cancel_before_expiry": 30,                 // [必填] 撤单时间: 30秒
    "leg2_fallback_to_maker": true                   // [必填] 降级做市: true
}
```

---

## 4. 如何新增与启用自定义策略？

1. **编辑配置文件**：打开 [configs/strategies.json](file:///d:/生活/Trading/polymarket/configs/strategies.json)；
2. **复制一个模板**：在 JSON 数组中添加新的对象，并确保 **`strategy_id` 全局唯一**；
3. **微调参数**：根据资金体量调整 `amount`，根据风险偏好微调 `entry_max_price`；
4. **切换实盘/模拟**：
   * 测试阶段保持 `"is_live": false`；
   * 验证稳定后将目标策略改为 `"is_live": true`；
5. **重启 Dashboard**：保存文件后重新启动 Dashboard，系统会自动加载新策略并接入风控。

---

## 5. 看板指标与交易状态对照表 (Dashboard Legend)

| 状态标签 | 状态标识 | 含义与流转说明 | 对应二腿展示 |
| :--- | :---: | :--- | :--- |
| **`📡 监听中`** | `idle` | 策略正在监听盘口，进行 K 线单边波幅与价差评估，尚未开仓 | 显示 `--` |
| **`⏳ 发单中`** | `pending_*` | 首腿已买入，二腿双出口限价单已挂出，正在盘口撮合等待 | 显示 `🎯 挂卖0.535 / 挂买0.465` |
| **`🔒 已锁仓`** | `locked` | 反向对冲买单已成交，形成 YES+NO 双边全锁无风险套利 | 显示 `BUY 0.465×21.74` |
| **`✅ 已结算`** | `settled` | OCO 做 T 高抛卖单率先成交变现，净利润已入账 | 显示 `SELL 0.531×21.74 [做T]` |
| **`⚡ 已强平`** | `force_closed` | 单边敞口达 35s 动态 TTL 超时，强平引擎穿透订单簿 VWAP 市价止损 | 显示 `SELL 0.564×22.73` |

---

## 6. 实盘资金管理与常见风控拦截排查

### 6.1 拦截提示：`导致实盘总敞口超限 (已用 X / 上限 Y)`
* **触发根因**：
  * 系统按链上真实抵押品余额自动计算安全上限：$$\text{Max Live Exposure} = \text{Balance} \times 0.95$$
  * 若链上余额不足以覆盖单笔开仓金额（例如当前余额为 $0.14 USDC，安全上限为 $0.13，而策略配置为 `amount: 3.0`），风控中心将主动拦截。
* **排查与解决**：
  1. 向钱包地址存入足够的 `USDC.e` 或 `pUSD` 抵押品（实盘建议保持 $\ge \$5.00$）；
  2. 若仅做功能连通性验证，可微调 `amount` 至 $3.00，并确保 Token 份数 $\text{amount} / \text{price} \ge 5.0$（满足 Polymarket CLOB 最小下单门槛）。

