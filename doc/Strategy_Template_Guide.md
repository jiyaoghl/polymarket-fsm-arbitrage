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
> **适用场景**：新账户实盘小资金试水、极小滑点、低入场价格保护。
```json
{
    "strategy_id": "taker_maker_conservative",
    "name": "吃单+挂单 保守型策略 (3U起步)",
    "description": "首腿FOK市价吃单，二腿GTC做市挂单对冲。严格防滑点与小额安全试水",
    "is_live": false,
    "amount": 3.0,
    "entry_max_price": 0.45,
    "entry_min_price": 0.30,
    "reentry_trigger": 0.35,
    "leg1_order_type": "FOK",
    "leg2_order_type": "GTC",
    "leg2_price_mode": "bid",
    "leg2_cancel_before_expiry": 30,
    "leg2_fallback_to_maker": true
}
```

---

### 模板 2：`taker_maker_standard` (均衡主力型 · 10U 日常套利)
> **适用场景**：主力均衡策略，兼顾开仓成交率与利差利润，适合 BTC / ETH / SOL 5min 盘口。
```json
{
    "strategy_id": "taker_maker_standard",
    "name": "吃单+挂单 标准型策略 (10U主力)",
    "description": "主力均衡策略，平衡开仓频率与利差收益，适合日常自动化套利",
    "is_live": false,
    "amount": 10.0,
    "entry_max_price": 0.50,
    "entry_min_price": 0.25,
    "reentry_trigger": 0.42,
    "leg1_order_type": "FOK",
    "leg2_order_type": "GTC",
    "leg2_price_mode": "bid",
    "leg2_cancel_before_expiry": 30,
    "leg2_fallback_to_maker": true
}
```

---

### 模板 3：`maker_maker_standard` (双边做市型 · 0 手续费完美套利)
> **适用场景**：通过批量接口同时原子级挂出 YES 与 NO 限价单，完全免手续费，两边成交直接锁定 1.5% 刚性净利。
```json
{
    "strategy_id": "maker_maker_standard",
    "name": "挂单+挂单 标准双挂策略 (Dual Bracket 10U)",
    "description": "双边限价做市主力策略，享受Polymarket Maker零手续费福利，零单边暴露完美套利",
    "is_live": false,
    "amount": 10.0,
    "dual_bracket_entry": true,
    "entry_max_price": 0.50,
    "entry_min_price": 0.25,
    "reentry_trigger": 0.42,
    "leg1_order_type": "GTC",
    "leg2_order_type": "GTC",
    "leg2_price_mode": "bid",
    "leg2_cancel_before_expiry": 30,
    "leg2_fallback_to_maker": true
}
```

---

### 模板 4：`maker_maker_wide_spread` (宽价差高利润型 · 专打长尾/SOL)
> **适用场景**：专打价差较厚（3%~5%）的盘口，要求更高的利润空间才入场挂单。
```json
{
    "strategy_id": "maker_maker_wide_spread",
    "name": "挂单+挂单 宽价差高利润策略 (Dual Bracket 10U)",
    "description": "专打流动性较薄但价差厚的盘口，互补定价锁定2.5%以上高毛利",
    "is_live": false,
    "amount": 10.0,
    "dual_bracket_entry": true,
    "entry_max_price": 0.48,
    "entry_min_price": 0.20,
    "reentry_trigger": 0.40,
    "leg1_order_type": "GTC",
    "leg2_order_type": "GTC",
    "leg2_price_mode": "bid",
    "leg2_cancel_before_expiry": 30,
    "leg2_fallback_to_maker": true
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
