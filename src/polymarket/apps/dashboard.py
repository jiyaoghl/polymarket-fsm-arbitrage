import threading
import time
from typing import Any, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from polymarket.apps.manager import StrategyManager
from polymarket.client import PolyClient, get_client


# 全局共享的策略管理器与行情客户端
manager = StrategyManager()
# UI 获取价格应与主网一致，否则获取实际 token 的 book 会返回 404
price_client = get_client(is_live=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """FastAPI 生命周期管理：启动时初始化后台线程。"""
    t = threading.Thread(target=manager.run_all, daemon=True)
    t.start()
    yield


app = FastAPI(
    title="Polymarket 5min Symmetric Bot Dashboard",
    lifespan=lifespan
)


class LegModel(BaseModel):
    side: str
    token: str
    cost: float
    size: float


class TradeModel(BaseModel):
    market_id: str
    asset: str = ""
    status: str
    end_time: float
    leg1: LegModel | None = None
    leg2: LegModel | None = None
    leg1_dir: str = ""
    leg2_dir: str = ""
    profit_usdc: float
    gross_profit_usdc: float = 0.0
    fee_usdc: float = 0.0
    dynamic_ttl: float | None = None
    time_to_expiry: float
    strategy_id: str
    filter_reason: str | None = None
    settlement_type: str | None = None
    dual_orders: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []


class StrategyStatusModel(BaseModel):
    strategy_id: str
    name: str
    is_live: bool
    entry_max_price: float
    reentry_trigger: float
    amount: float
    strategy_total_pnl: float
    active_trades: List[TradeModel]


class DashboardStatusModel(BaseModel):
    server_time: float
    current_markets: List[Dict[str, Any]] = []
    strategies: List[StrategyStatusModel]
    risk_metrics: Dict[str, Any] = {}
    asset_status: Dict[str, dict] = {}
    risk_events: List[Dict[str, Any]] = []

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """优化的前端页面，支持实时价格、倒计时、策略触发通知。"""
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>5min Symmetric Bot Dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; }
    body { 
        font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; 
        background: radial-gradient(circle at 10% 20%, #1e1b4b 0%, #0f172a 40%, #020617 100%);
        background-attachment: fixed;
        color: #f8fafc; 
        margin: 0; padding: 0; min-height: 100vh; 
    }
    
    /* 通知样式 */
    #notifications { position: fixed; top: 16px; right: 16px; z-index: 1000; max-width: 400px; }
    .notification { 
        background: rgba(15, 23, 42, 0.8); backdrop-filter: blur(8px);
        border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; 
        border-left: 4px solid #3b82f6; animation: slideIn 0.3s ease; box-shadow: 0 8px 32px rgba(0,0,0,0.4); 
    }
    .notification.success { border-left-color: #10b981; }
    .notification.warning { border-left-color: #f59e0b; }
    .notification.error { border-left-color: #ef4444; }
    .notification .title { font-weight: 600; margin-bottom: 4px; }
    .notification .message { font-size: 12px; color: #94a3b8; }    /* Header */
    header { padding: 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.05); background: rgba(0,0,0,0.2); }
    .title-area { display: flex; align-items: center; gap: 12px; }
    h1 { margin: 0; font-size: 20px; font-weight: 700; background: linear-gradient(to right, #60a5fa, #c084fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
    .status-badge { padding: 4px 10px; border-radius: 20px; font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
    .status-running { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
    .header-right { text-align: right; display: flex; flex-direction: column; gap: 4px; align-items: flex-end; }
    .time-display { font-size: 32px; font-weight: 700; font-variant-numeric: tabular-nums; color: #34d399; text-shadow: 0 0 20px rgba(52, 211, 153, 0.4); line-height: 1; }
    .date-display { font-size: 11px; color: #94a3b8; }
    
    .btc-risk-badge { padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; display: inline-flex; align-items: center; gap: 6px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.3); }
    .btc-risk-badge.choppy { color: #34d399; border-color: rgba(52,211,153,0.3); }
    .btc-risk-badge.volatile { color: #f87171; border-color: rgba(248,113,113,0.3); animation: pulse 2s infinite; } .countdown { font-size: 36px; font-weight: 700; font-variant-numeric: tabular-nums; color: #34d399; text-shadow: 0 0 16px rgba(52,211,153,0.3); }
    .countdown.warning { color: #fbbf24; text-shadow: 0 0 16px rgba(251,191,36,0.3); }
    .countdown.danger { color: #f87171; text-shadow: 0 0 16px rgba(248,113,113,0.3); }
    .next-market { font-size: 12px; color: #94a3b8; }
    
    main { 
        padding: 24px 32px; display: grid; grid-template-columns: repeat(12, 1fr); gap: 24px; max-width: 1800px; margin: 0 auto;
    }
    @media (max-width: 1200px) { main { grid-template-columns: 1fr; } }
    
    section { 
        background: rgba(15, 23, 42, 0.4); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
        border-radius: 16px; border: 1px solid rgba(255, 255, 255, 0.08); padding: 20px; box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3); 
    }
    /* Grid 占位 */
    .section-market { grid-column: span 5; }
    .section-defense { grid-column: span 7; }
    .section-strategy { grid-column: span 12; }
    .section-terminal { grid-column: span 6; display: flex; flex-direction: column; }
    .section-risk { grid-column: span 6; display: flex; flex-direction: column; }
    
    @media (max-width: 1200px) {
        .section-market, .section-defense, .section-strategy, .section-terminal, .section-risk { grid-column: span 12; }
    }
    
    h2 { font-size: 16px; font-weight: 600; margin: 0 0 16px 0; display: flex; align-items: center; gap: 8px; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 12px; letter-spacing: 0.5px; }
    h2 .badge { font-size: 11px; padding: 2px 6px; border-radius: 4px; background: rgba(59, 130, 246, 0.2); color: #93c5fd; }
    
    /* 卡片共用 (Glassmorphism) */
    .glass-card {
        background: rgba(30, 41, 59, 0.4); border: 1px solid rgba(255,255,255,0.05); border-radius: 12px; padding: 12px;
        transition: transform 0.2s, box-shadow 0.2s;
    }
    .glass-card:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.5); border-color: rgba(255,255,255,0.1); }
    
    /* 价格卡片 */
    .price-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
    .price-card .label { font-size: 11px; color: #94a3b8; margin-bottom: 4px; text-transform: uppercase; font-weight: 500; }
    .price-card .price-row { display: flex; justify-content: space-between; align-items: center; }
    .price-card .side { font-weight: 700; font-size: 15px; }
    .price-card .side.YES { color: #34d399; }
    .price-card .side.NO { color: #f87171; }
    .price-card .prices { font-size: 12px; color: #cbd5e1; font-variant-numeric: tabular-nums; }
    .price-card .ask { color: #fbbf24; }
    .price-card .bid { color: #34d399; }
    .price-card .spread { font-size: 10px; color: #64748b; margin-top: 6px; }
    
    /* 闪烁更新动效 */
    .flash-update { animation: flashColor 0.5s ease-out; }
    @keyframes flashColor { 0% { background-color: rgba(255,255,255,0.2); } 100% { background-color: transparent; } }
    
    /* 市场信息 */
    .market-info { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; font-size: 12px; }
    .info-item { display: flex; flex-direction: column; gap: 4px; }
    .info-item .label { color: #64748b; font-size: 10px; font-weight: 500; text-transform: uppercase; }
    .info-item .value { font-variant-numeric: tabular-nums; color: #e2e8f0; }
    
    /* 策略卡片 */
    .strategy-card { margin-bottom: 16px; }
    .strategy-card.has-trades { border-color: rgba(59, 130, 246, 0.4); box-shadow: 0 0 15px rgba(59, 130, 246, 0.1); }
    .strategy-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .strategy-name { font-weight: 600; font-size: 14px; }
    .strategy-mode { font-size: 10px; padding: 2px 8px; border-radius: 4px; font-weight: 600; }
    .strategy-mode.live { background: rgba(16,185,129,0.2); color: #34d399; }
    .strategy-mode.paper { background: rgba(100,116,139,0.2); color: #94a3b8; }
    .strategy-params { font-size: 11px; color: #94a3b8; margin-bottom: 12px; display: flex; gap: 12px; flex-wrap: wrap; }
    
    /* 仓位表格 */
    table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12px; margin-top: 8px; }
    th, td { padding: 8px 12px; text-align: left; }
    th { color: #64748b; font-weight: 500; text-transform: uppercase; font-size: 10px; border-bottom: 1px solid rgba(255,255,255,0.05); }
    tr { transition: background 0.2s; }
    tr:hover td { background: rgba(255,255,255,0.02); }
    td { border-bottom: 1px solid rgba(255,255,255,0.02); color: #cbd5e1; }
    
    .pill { padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; display: inline-block; }
    .pill-open { background: rgba(15,118,110,0.2); color: #2dd4bf; }
    .pill-locked { background: rgba(29,78,216,0.2); color: #60a5fa; box-shadow: 0 0 8px rgba(96,165,250,0.2); }
    .pill-stopped { background: rgba(185,28,28,0.2); color: #fca5a5; }
    .pill-leg1_only { background: rgba(245,158,11,0.2); color: #fbbf24; animation: pulse 2s infinite; }
    
    .no-trades { color: #64748b; font-size: 12px; font-style: italic; padding: 12px; text-align: center; background: rgba(0,0,0,0.2); border-radius: 8px; }
    
    /* 统计卡片 */
    .stats-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 20px; }
    @media (max-width: 900px) { .stats-row { grid-template-columns: repeat(3, 1fr); } }
    .stat-card .value { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; margin-bottom: 4px; }
    .stat-card .label { font-size: 11px; color: #94a3b8; text-transform: uppercase; font-weight: 500; }
    
    /* 进度条 */
    .progress-track { background: rgba(0,0,0,0.3); border-radius: 99px; height: 8px; margin-top: 16px; overflow: hidden; border: 1px solid rgba(255,255,255,0.05); }
    .progress-bar { background: linear-gradient(90deg, #3b82f6, #8b5cf6, #ec4899); height: 100%; width: 0%; transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1); }
    
    /* FSM 终端 */
    .terminal { 
        background: rgba(0, 0, 0, 0.5); border-radius: 8px; padding: 16px; 
        font-family: 'Courier New', Courier, monospace; font-size: 11px; 
        flex: 1; min-height: 400px; max-height: 600px; overflow-y: auto; color: #a3a8b4; 
        border: 1px solid rgba(255,255,255,0.05); box-shadow: inset 0 2px 10px rgba(0,0,0,0.5);
    }
    .terminal::-webkit-scrollbar { width: 6px; }
    .terminal::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
    
    .terminal .line { margin-bottom: 6px; line-height: 1.5; display: flex; gap: 8px; }
    .terminal .time { color: #64748b; white-space: nowrap; }
    .terminal .market { color: #a78bfa; white-space: nowrap; }
    .terminal .state { padding: 0 4px; border-radius: 3px; font-weight: bold; white-space: nowrap; font-size: 10px; }
    .state-idle { color: #94a3b8; }
    .state-pending { color: #fbbf24; background: rgba(180,83,9,0.3); }
    .state-leg1_only { color: #ef4444; background: rgba(185,28,28,0.3); animation: pulse 2s infinite; }
    .state-pending_leg2 { color: #fbbf24; background: rgba(180,83,9,0.3); }
    .state-locked { color: #60a5fa; background: rgba(29,78,216,0.3); }
    .state-settled { color: #34d399; background: rgba(4,120,87,0.3); }
    .state-failed { color: #f87171; background: rgba(153,27,27,0.3); }
    .terminal .msg { color: #e2e8f0; word-break: break-all; }
  </style>
</head>
<body>
  <div id="notifications"></div>
  
  <header>
      <div class="title-area">
        <h1>5min Symmetric Bot</h1>
        <span class="status-badge status-running">● 运行中</span>
      </div>
      <div class="header-right">
        <div class="time-display" id="countdown">00:00</div>
        <div class="next-market" id="next-market" style="font-size: 12px; color: #94a3b8; margin-bottom: 4px;">下一个市场</div>
        <div class="date-display" id="server-time">Server Time: --</div>
        <div id="asset-status-ui" style="display: flex; flex-direction: column; align-items: flex-end; gap: 6px; margin-top: 8px;"></div>
      </div>
    </header>
  
  <main>
    <!-- 第一行: 左4 右8 -->
    <section class="section-market">
      <h2>📊 当前市场 <span class="badge" id="market-badge">等待中</span></h2>
      <div id="markets-container" style="display: flex; flex-direction: column; gap: 20px;">
        <div style="color: #64748b; font-size: 12px; font-style: italic; padding: 12px; text-align: center; background: rgba(0,0,0,0.2); border-radius: 8px;">等待新市场发现...</div>
      </div>
      <div id="filter-reason-ui" style="margin-top: 16px; padding: 12px; background: rgba(245, 158, 11, 0.1); border-left: 3px solid #f59e0b; border-radius: 4px; color: #fbbf24; font-size: 12px; font-weight: 500; display: none;"></div>
    </section>
    
    <section class="section-defense" style="display: flex; flex-direction: column; justify-content: center;">
      <h2 style="font-size: 13px; margin-bottom: 16px; color: #64748b; font-weight: 600; text-transform: uppercase;">🛡️ 系统风控墙 (Risk Shield)</h2>
      <div style="display: flex; gap: 32px; margin-bottom: 16px;">
        <div>
          <div id="stat-intercept-count" style="color: #f87171; font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1;">0</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; text-transform: uppercase;">拦截笔数</div>
        </div>
        <div>
          <div id="stat-intercept-amt" style="color: #fbbf24; font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1;">$0.00</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; text-transform: uppercase;">挽回资金</div>
        </div>
        <div>
          <div id="stat-retry-win" style="color: #34d399; font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1;">0 / 0</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; text-transform: uppercase;">滑点微调</div>
        </div>
        <div>
          <div id="stat-exposure" style="color: #60a5fa; font-size: 28px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1;">0.0%</div>
          <div style="font-size: 11px; color: #64748b; margin-top: 6px; text-transform: uppercase;">实时敞口</div>
        </div>
      </div>
      
      <div class="progress-track" style="height: 4px; margin-top: auto;">
        <div class="progress-bar" id="exposure-bar" style="background: linear-gradient(90deg, #3b82f6, #60a5fa);"></div>
      </div>
      <div style="font-size: 10px; color: #64748b; text-align: right; margin-top: 6px; font-variant-numeric: tabular-nums;" id="exposure-text">0.00 / 0.00 USDC</div>
    </section>
    
    <!-- 第二行: 左7 右5 -->
    <section class="section-strategy">
      <h2>🎯 策略与持仓</h2>
      
      <div class="stats-row">
        <div class="glass-card stat-card">
          <div class="value" id="stat-strategies">0</div>
          <div class="label">策略数</div>
        </div>
        <div class="glass-card stat-card">
          <div class="value" id="stat-trades">0</div>
          <div class="label">活跃/历史单</div>
        </div>
        <div class="glass-card stat-card">
          <div class="value" id="stat-locked">0</div>
          <div class="label">成功锁仓</div>
        </div>
        <div class="glass-card stat-card">
          <div class="value" id="stat-ev" style="color: #34d399;">$0</div>
          <div class="label">净净收益 (Net EV)</div>
        </div>
        <div class="glass-card stat-card">
          <div class="value" id="stat-fee" style="color: #f87171;">$0</div>
          <div class="label">手续费磨损</div>
        </div>
        <div class="glass-card stat-card">
          <div class="value" id="stat-winrate" style="color: #60a5fa;">0%</div>
          <div class="label">费后胜率</div>
        </div>
      </div>
      
      <div id="strategies"></div>
    </section>
    
    <section class="section-terminal">
      <h2>📜 FSM 实况追踪流 (Live Trace)</h2>
      <div class="terminal" id="terminal">
        <div class="line" style="color: #64748b; font-style: italic;">等待 FSM 状态流转...</div>
      </div>
    </section>
    
    <section class="section-risk">
      <h2>🛡️ 风控拦截与诊断日志</h2>
      <div class="terminal" id="risk-terminal" style="background: rgba(15, 23, 42, 0.7); max-height: 400px;">
        <div class="line" style="color: #64748b; font-style: italic;">等待风控数据...</div>
      </div>
    </section>
  </main>
  
  <script>
    // 状态管理
    let lastTrades = {};
    let lastMarketsKey = null;
    let printedEvents = new Set();
    let isTerminalInitialized = false;
    let lastPrices = {};
    let currentMarketsData = [];
    
    // 通知系统
    function showNotification(title, message, type = 'info') {
      const container = document.getElementById('notifications');
      const notif = document.createElement('div');
      notif.className = `notification ${type}`;
      notif.innerHTML = `<div class="title">${title}</div><div class="message">${message}</div>`;
      container.appendChild(notif);
      setTimeout(() => notif.remove(), 5000);
    }
    
    // 状态标签
    function statusPill(status) {
      const s = (status || "").toLowerCase();
      if (s === "locked") return '<span class="pill pill-locked">🔒 已锁仓</span>';
      if (s === "settled") return '<span class="pill pill-locked" style="background: rgba(16,185,129,0.2); color: #34d399;">✅ 已结算</span>';
      if (s === "force_closed" || s === "stopped") return '<span class="pill pill-stopped">⚡ 已强平</span>';
      if (s === "leg1_only") return '<span class="pill pill-leg1_only">📈 首腿持仓</span>';
      if (s === "pending" || s === "pending_leg1" || s === "pending_leg2" || s === "pending_both") return '<span class="pill pill-leg1_only" style="background: rgba(59,130,246,0.2); color: #60a5fa;">⏳ 发单中</span>';
      if (s === "idle") return '<span class="pill pill-open" style="background: rgba(100,116,139,0.2); color: #94a3b8;">📡 监听中</span>';
      return '<span class="pill pill-open">' + (status || 'open') + '</span>';
    }
    
    // 格式化倒计时
    function formatCountdown(seconds) {
      if (seconds <= 0) return '00:00';
      const mins = Math.floor(seconds / 60);
      const secs = Math.floor(seconds % 60);
      return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    }
    
    // 追加日志到终端
    function appendTerminal(event) {
      const term = document.getElementById('terminal');
      if (!isTerminalInitialized) {
        term.innerHTML = '';
        isTerminalInitialized = true;
      }
      
      const isScrolledToBottom = term.scrollHeight - term.clientHeight <= term.scrollTop + 10;
      
      const line = document.createElement('div');
      line.className = 'line';
      
      const d = new Date(event.time * 1000);
      const timeStr = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}.${d.getMilliseconds().toString().padStart(3, '0')}`;
      const marketShort = event.market_id.slice(0, 10);
      
      line.innerHTML = `
          <span class="time">[${timeStr}]</span>
          <span class="market">[${marketShort}]</span>
          <span class="state state-${event.state}">${event.state.toUpperCase()}</span>
          <span class="msg">${event.msg}</span>
      `;
      term.appendChild(line);
      
      if (term.childNodes.length > 200) {
          term.removeChild(term.firstChild);
      }
      
      if (isScrolledToBottom) {
          term.scrollTop = term.scrollHeight;
      }
    }
    
    // 触发闪烁动画
    function triggerFlash(elId) {
        const el = document.getElementById(elId);
        if(!el) return;
        el.classList.remove('flash-update');
        void el.offsetWidth; // 触发重绘
        el.classList.add('flash-update');
    }

    // 计算下一个5分钟窗口
    function getNextWindow(serverTime) {
      const nextTs = (Math.floor(serverTime / 300) + 1) * 300;
      return nextTs - serverTime;
    }
    
    // 检测新交易并通知
    function checkNewTrades(strategies) {
      const currentTrades = {};
      let newTradeCount = 0;
      let lockedCount = 0;
      
      strategies.forEach(s => {
        s.active_trades.forEach(t => {
          const key = `${s.strategy_id}-${t.market_id}`;
          currentTrades[key] = t;
          
          // 检测新交易
          if (!lastTrades[key]) {
            newTradeCount++;
            const leg1Info = t.leg1 ? `${t.leg1.side} @ ${t.leg1.cost.toFixed(3)}` : '';
            showNotification(
              `🚀 新仓位: ${s.name}`,
              `Market: ${t.market_id.slice(0, 10)}... | ${leg1Info}`,
              'success'
            );
          }
          
          // 检测锁仓
          if (t.status === 'locked' && lastTrades[key] && lastTrades[key].status !== 'locked') {
            lockedCount++;
            showNotification(
              `🔒 锁仓成功: ${s.name}`,
              `EV: ${t.profit_usdc.toFixed(4)} USDC`,
              'success'
            );
          }
        });
      });
      
      lastTrades = currentTrades;
    }
    
    // 更新价格显示
    async function updatePrices() {
      try {
        if (!currentMarketsData || currentMarketsData.length === 0) return;
        const res = await fetch('/api/prices');
        const data = await res.json();
        const marketsPrices = data.markets || {};
        
        currentMarketsData.forEach(m => {
          const mPrice = marketsPrices[m.id];
          if (!mPrice) return;
          
          if (mPrice.yes) {
            const yesAsk = mPrice.yes.ask?.toFixed(4) || '--';
            const yesBid = mPrice.yes.bid?.toFixed(4) || '--';
            
            const yesKey = m.id + '-yes';
            if (lastPrices[yesKey] !== yesAsk + yesBid) {
                triggerFlash('yes-card-' + m.id);
                lastPrices[yesKey] = yesAsk + yesBid;
            }
            
            const elPrice = document.getElementById('yes-price-' + m.id);
            if(elPrice) elPrice.innerHTML = `<span class="ask">Ask: ${yesAsk}</span> <span style="margin:0 4px; color:#475569">|</span> <span class="bid">Bid: ${yesBid}</span>`;
            
            const spread = mPrice.yes.ask && mPrice.yes.bid ? ((mPrice.yes.ask - mPrice.yes.bid) * 100).toFixed(2) : '--';
            const elSpread = document.getElementById('yes-spread-' + m.id);
            if(elSpread) elSpread.textContent = `spread: ${spread}%`;
          }
          
          if (mPrice.no) {
            const noAsk = mPrice.no.ask?.toFixed(4) || '--';
            const noBid = mPrice.no.bid?.toFixed(4) || '--';
            
            const noKey = m.id + '-no';
            if (lastPrices[noKey] !== noAsk + noBid) {
                triggerFlash('no-card-' + m.id);
                lastPrices[noKey] = noAsk + noBid;
            }
            
            const elPrice = document.getElementById('no-price-' + m.id);
            if(elPrice) elPrice.innerHTML = `<span class="ask">Ask: ${noAsk}</span> <span style="margin:0 4px; color:#475569">|</span> <span class="bid">Bid: ${noBid}</span>`;
            
            const spread = mPrice.no.ask && mPrice.no.bid ? ((mPrice.no.ask - mPrice.no.bid) * 100).toFixed(2) : '--';
            const elSpread = document.getElementById('no-spread-' + m.id);
            if(elSpread) elSpread.textContent = `spread: ${spread}%`;
          }
        });
      } catch (e) {
        console.error('Price update error:', e);
      }
    }
    
    // 主刷新函数
    async function refresh() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        
        // 更新服务器时间
        document.getElementById('server-time').textContent = 
          'Server Time: ' + new Date(data.server_time * 1000).toLocaleString('zh-CN');
        
        // 更新倒计时
        const nextWindow = getNextWindow(data.server_time);
        const countdownEl = document.getElementById('countdown');
        countdownEl.textContent = formatCountdown(nextWindow);
        countdownEl.className = 'countdown';
        if (nextWindow < 60) countdownEl.classList.add('danger');
        else if (nextWindow < 120) countdownEl.classList.add('warning');
        
        // 下一个市场时间
        const nextTs = (Math.floor(data.server_time / 300) + 1) * 300;
        document.getElementById('next-market').textContent = 
          'Next Round: ' + new Date(nextTs * 1000).toLocaleTimeString('zh-CN');
        
        // 市场信息 (多品种并列渲染)
        const markets = data.current_markets || [];
        currentMarketsData = markets;
        
        const containerM = document.getElementById('markets-container');
        if (markets.length > 0) {
          document.getElementById('market-badge').textContent = '监控中 (' + markets.length + ')';
          
          const newMarketsKey = markets.map(m => m.id).join(',');
          
          // 如果市场发生了轮转或新增，才全量重建 DOM，防止破坏价格闪烁动画
          if (lastMarketsKey !== newMarketsKey) {
              let html = '';
              markets.forEach((m, idx) => {
                  html += `
                  <div class="market-block" id="market-block-${m.id}" style="padding-bottom: 16px; border-bottom: ${idx === markets.length - 1 ? 'none' : '1px dashed rgba(255,255,255,0.1)'}">
                    <div style="font-weight: 600; font-size: 13px; color: #a78bfa; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                      <span style="background: rgba(167, 139, 250, 0.2); padding: 2px 6px; border-radius: 4px; font-size: 10px;">${m.asset || 'N/A'}</span>
                      ${m.description || '--'}
                    </div>
                    <div class="price-grid">
                      <div class="glass-card price-card" id="yes-card-${m.id}">
                        <div class="label">YES Token</div>
                        <div class="price-row"><span class="side YES">YES</span><span class="prices" id="yes-price-${m.id}">--</span></div>
                        <div class="spread" id="yes-spread-${m.id}">spread: --</div>
                      </div>
                      <div class="glass-card price-card" id="no-card-${m.id}">
                        <div class="label">NO Token</div>
                        <div class="price-row"><span class="side NO">NO</span><span class="prices" id="no-price-${m.id}">--</span></div>
                        <div class="spread" id="no-spread-${m.id}">spread: --</div>
                      </div>
                    </div>
                    <div class="market-info">
                      <div class="info-item"><span class="label">Market ID</span><span class="value" style="font-size: 10px;">${m.id ? m.id.slice(0, 20) + '...' : '--'}</span></div>
                      <div class="info-item"><span class="label">剩余时间 / 到期</span><span class="value" id="m-ttl-${m.id}">--</span></div>
                    </div>
                  </div>`;
              });
              containerM.innerHTML = html;
              lastMarketsKey = newMarketsKey;
          }
          
          // 始终更新每个市场的倒计时
          markets.forEach(m => {
              if (m.end_time) {
                  const ttl = m.end_time - data.server_time;
                  const expiry = new Date(m.end_time * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
                  const ttlEl = document.getElementById(`m-ttl-${m.id}`);
                  if (ttlEl) {
                      ttlEl.innerHTML = `<strong style="color: ${ttl < 60 ? '#f87171' : (ttl < 120 ? '#fbbf24' : '#34d399')}; font-variant-numeric: tabular-nums;">${formatCountdown(ttl)}</strong> (至 ${expiry})`;
                  }
              }
          });
          
        } else {
          document.getElementById('market-badge').textContent = '等待中';
          containerM.innerHTML = '<div style="color: #64748b; font-size: 12px; font-style: italic; padding: 12px; text-align: center; background: rgba(0,0,0,0.2); border-radius: 8px;">等待新市场发现...</div>';
          lastMarketsKey = null;
        }
        
        // 检测新交易
        checkNewTrades(data.strategies);
        
        // 收集所有 events 并输出
        let allEvents = [];
        data.strategies.forEach(s => {
          s.active_trades.forEach(t => {
            if (t.events && t.events.length) {
              t.events.forEach(e => {
                const eventId = `${t.market_id}-${e.time}-${e.state}`;
                if (!printedEvents.has(eventId)) {
                  allEvents.push({...e, market_id: t.market_id});
                  printedEvents.add(eventId);
                }
              });
            }
          });
        });
        allEvents.sort((a, b) => a.time - b.time).forEach(appendTerminal);
        
        // 统计信息
        let totalTrades = 0, lockedTrades = 0, totalNetEV = 0, totalFee = 0, winCount = 0, closedCount = 0;
        let latestFilterReason = null;
        
        // 5. 更新多品种风控状态
        const assetStatusEl = document.getElementById('asset-status-ui');
        if (data.asset_status && assetStatusEl) {
          assetStatusEl.innerHTML = '';
          for (const [asset, bs] of Object.entries(data.asset_status)) {
            if (bs.timestamp > 0) {
              const isChoppy = bs.is_choppy;
              const text = isChoppy ? `🟢 ${asset} 震荡 (允许入场)` : `🔴 ${asset} 单边 (${bs.amplitude.toFixed(2)}%)`;
              assetStatusEl.innerHTML += `<div class="btc-risk-badge ${isChoppy ? 'choppy' : 'volatile'}">${text}</div>`;
            }
          }
        }
        data.strategies.forEach(s => {
          totalTrades += s.active_trades.length;
          s.active_trades.forEach(t => {
            if (t.status === 'locked') lockedTrades++;
            if (t.status === 'locked' || t.status === 'settled') {
                closedCount++;
                if ((t.profit_usdc || 0) > 0) winCount++;
            }
            totalNetEV += (t.profit_usdc || 0);
            totalFee += (t.fee_usdc || 0);
            if (t.filter_reason) {
                latestFilterReason = `[${s.name}] ${t.filter_reason}`;
            }
          });
        });
        
        const filterUi = document.getElementById('filter-reason-ui');
        if (latestFilterReason) {
            filterUi.style.display = 'block';
            filterUi.innerHTML = `🚦 <strong>监控受阻:</strong> ${latestFilterReason}`;
        } else {
            filterUi.style.display = 'none';
        }
        
        const winRate = closedCount > 0 ? ((winCount / closedCount) * 100).toFixed(1) + '%' : '--';
        document.getElementById('stat-strategies').textContent = data.strategies.length;
        document.getElementById('stat-trades').textContent = totalTrades;
        document.getElementById('stat-locked').textContent = lockedTrades;
        document.getElementById('stat-ev').textContent = `$${totalNetEV.toFixed(4)}`;
        document.getElementById('stat-fee').textContent = `-$${totalFee.toFixed(4)}`;
        document.getElementById('stat-winrate').textContent = winRate;
        
        // 更新系统指标卡片
        if (data.risk_metrics) {
            document.getElementById('stat-intercept-count').textContent = data.risk_metrics.total_intercepted_count || 0;
            document.getElementById('stat-intercept-amt').textContent = "$" + (data.risk_metrics.total_intercepted_amount || 0).toFixed(2);
            const r_suc = data.risk_metrics.adaptive_retry_success || 0;
            const r_fail = data.risk_metrics.adaptive_retry_failed || 0;
            document.getElementById('stat-retry-win').textContent = `${r_suc} / ${r_suc + r_fail}`;
            document.getElementById('stat-exposure').textContent = (data.risk_metrics.utilization || 0).toFixed(1) + "%";
            
            document.getElementById('exposure-bar').style.width = Math.min((data.risk_metrics.utilization || 0), 100) + "%";
            document.getElementById('exposure-text').textContent = (data.risk_metrics.used_exposure || 0).toFixed(2) + " / " + (data.risk_metrics.max_exposure || 0).toFixed(2) + " USDC";
        }
        
        // 策略列表
        const container = document.getElementById('strategies');
        container.innerHTML = '';
        
        data.strategies.forEach((s) => {
          const div = document.createElement('div');
          div.className = `glass-card strategy-card ${s.active_trades.length ? 'has-trades' : ''}`;
          
          let html = `
            <div class="strategy-header">
              <span class="strategy-name">${s.name}</span>
              <span class="strategy-mode ${s.is_live ? 'live' : 'paper'}">${s.is_live ? 'LIVE' : 'PAPER'}</span>
            </div>
            <div class="strategy-params">
              <span>入场价 ≤ ${s.entry_max_price.toFixed(3)}</span> 
              <span>补仓触发 < ${s.reentry_trigger.toFixed(3)}</span> 
              <span>仓位 $${s.amount.toFixed(2)}</span> 
              <strong style="color:${s.strategy_total_pnl > 0 ? '#34d399' : (s.strategy_total_pnl < 0 ? '#f87171' : '#cbd5e1')}">总盈亏: $${s.strategy_total_pnl.toFixed(4)}</strong>
            </div>
          `;
          
          if (!s.active_trades.length) {
            html += '<div class="no-trades">暂无活跃仓位</div>';
          } else {
            html += `<table><thead><tr>
              <th>Market</th><th>状态</th><th>首腿</th><th>二腿</th><th>TTL</th>
              <th style="text-align: right;">净EV (扣费后)</th>
            </tr></thead><tbody>`;
            
            s.active_trades.forEach((t) => {
              const leg1DirHTML = t.leg1_dir ? `<span style="font-size:10px; padding:2px 4px; border-radius:4px; background:${t.leg1_dir==='UP'?'rgba(52,211,153,0.2)':'rgba(248,113,113,0.2)'}; color:${t.leg1_dir==='UP'?'#34d399':'#f87171'}; margin-right:4px;">${t.leg1_dir}</span>` : '';
              const leg2DirHTML = t.leg2_dir ? `<span style="font-size:10px; padding:2px 4px; border-radius:4px; background:${t.leg2_dir==='UP'?'rgba(52,211,153,0.2)':'rgba(248,113,113,0.2)'}; color:${t.leg2_dir==='UP'?'#34d399':'#f87171'}; margin-right:4px;">${t.leg2_dir}</span>` : '';
              
              const leg1 = t.leg1 
                ? `${leg1DirHTML}${t.leg1.side} ${t.leg1.cost.toFixed(3)}×${t.leg1.size.toFixed(2)}`
                : '--';
              
              let leg2 = '--';
              if (t.leg2) {
                const isT = (t.settlement_type === 'DUAL_EXIT_SELL_SETTLED' || t.settlement_type === 'SMART_FLIP_SETTLED');
                const tBadge = isT ? ' <span style="font-size:9px; color:#34d399; background:rgba(52,211,153,0.15); padding:1px 3px; border-radius:3px;">做T</span>' : '';
                leg2 = `${leg2DirHTML}${t.leg2.side} ${t.leg2.cost.toFixed(3)}×${t.leg2.size.toFixed(2)}${tBadge}`;
              } else if (t.status === 'settled' && t.settlement_type === 'DUAL_EXIT_SELL_SETTLED') {
                let sPrice = '--';
                let sSize = t.leg1 ? t.leg1.size.toFixed(2) : '--';
                if (t.dual_orders && t.dual_orders.length) {
                  const sOrder = t.dual_orders.find(o => o.side === 'SELL');
                  if (sOrder && sOrder.price) sPrice = Number(sOrder.price).toFixed(3);
                }
                if (sPrice === '--' && t.leg1) {
                  sPrice = ((t.leg1.cost * t.leg1.size + (t.gross_profit_usdc || t.profit_usdc || 0)) / t.leg1.size).toFixed(3);
                }
                leg2 = `<span style="color:#f87171;">SELL ${sPrice}×${sSize}</span> <span style="font-size:9px; color:#34d399; background:rgba(52,211,153,0.15); padding:1px 3px; border-radius:3px;">做T</span>`;
              } else if (t.status === 'pending_leg2' && t.dual_orders && t.dual_orders.length >= 2) {
                const sOrder = t.dual_orders.find(o => o.side === 'SELL');
                const bOrder = t.dual_orders.find(o => o.side === 'BUY');
                const sPrice = sOrder ? Number(sOrder.price).toFixed(3) : '--';
                const bPrice = bOrder ? Number(bOrder.price).toFixed(3) : '--';
                leg2 = `<span style="color:#60a5fa; font-size:10px;" title="双出口挂单撮合中">🎯 挂卖${sPrice} / 挂买${bPrice}</span>`;
              }
                
              const timeStr = new Date(t.end_time * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute:'2-digit' });
              const assetTag = t.asset ? `<span style="background: rgba(167,139,250,0.2); color:#a78bfa; padding:2px 4px; border-radius:4px; margin-right:4px;">${t.asset}</span>` : '';
              
              let ttlDisplay = formatCountdown(t.time_to_expiry);
              if (t.status === 'leg1_only' && t.dynamic_ttl) {
                  ttlDisplay = `<span style="color:#fbbf24; font-weight:600;" title="动态自适应强平倒计时 (基准 ${t.dynamic_ttl.toFixed(0)}s)">⚡ ${formatCountdown(t.dynamic_ttl)}</span>`;
              }
              
              const grossEV = t.gross_profit_usdc !== undefined ? t.gross_profit_usdc : t.profit_usdc;
              const feeVal = t.fee_usdc || 0;
              const feeBadge = feeVal > 0 ? `<div style="font-size:9px; color:#64748b; font-weight:normal;" title="毛利 +$${grossEV.toFixed(4)} / 费 -$${feeVal.toFixed(4)}">费 -$${feeVal.toFixed(4)}</div>` : '';

              html += `<tr>
                <td style="font-size:10px; color:#94a3b8;" title="${t.market_id}">${assetTag}${timeStr}</td>
                <td>${statusPill(t.status)}</td>
                <td>${leg1}</td>
                <td>${leg2}</td>
                <td style="font-variant-numeric: tabular-nums;">${ttlDisplay}</td>
                <td style="text-align: right;">
                  <span style="color:${t.profit_usdc > 0 ? '#34d399' : (t.profit_usdc < 0 ? '#f87171' : '#94a3b8')}; font-weight: 600;">${t.profit_usdc >= 0 ? '+' : ''}${t.profit_usdc.toFixed(4)}</span>
                  ${feeBadge}
                </td>
              </tr>`;
            });
            
            html += '</tbody></table>';
          }
          
          div.innerHTML = html;
          container.appendChild(div);
        });
        
        // 渲染风控拦截日志
        const riskEvents = data.risk_events || [];
        const riskTerm = document.getElementById('risk-terminal');
        if (riskTerm) {
            if (riskEvents.length === 0) {
                riskTerm.innerHTML = '<div class="line" style="color: #64748b; font-style: italic;">目前暂无风控拦截日志</div>';
            } else {
                const isScrolledToBottom = riskTerm.scrollHeight - riskTerm.clientHeight <= riskTerm.scrollTop + 30;
                let riskHtml = '';
                riskEvents.forEach(e => {
                    const d = new Date(e.timestamp * 1000);
                    const ts = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}`;
                    let color = '#94a3b8';
                    if (e.level === 'error' || e.level === 'critical') color = '#ef4444';
                    else if (e.level === 'warning') color = '#f59e0b';
                    else if (e.level === 'info') color = '#38bdf8';
                    riskHtml += `<div class="line" style="color: ${color}">[${ts}] [${e.asset}] [${e.strategy}] ${e.reason}</div>`;
                });
                riskTerm.innerHTML = riskHtml;
                if (isScrolledToBottom || !riskTerm.dataset.hasUserScrolled) {
                    riskTerm.scrollTop = riskTerm.scrollHeight;
                    riskTerm.dataset.hasUserScrolled = "1";
                }
            }
        }
        
      } catch (e) {
        console.error('Refresh error:', e);
      }
    }

    // 启动
    refresh();
    updatePrices();
    setInterval(refresh, 2000);
    setInterval(updatePrices, 1000);  // 价格每秒更新
  </script>
</body>
</html>
    """


@app.get("/api/prices")
async def api_prices():
    """获取当前市场的实时价格。"""
    result = {"timestamp": time.time(), "markets": {}}
    for m in manager.current_markets:
        market_id = m.get("id")
        if not market_id:
            continue
        tokens = m.get("tokens", {})
        yes_token = tokens.get("YES")
        no_token = tokens.get("NO")
        
        m_result = {"yes": None, "no": None}
        try:
            if yes_token:
                prices = await price_client.get_market_price_async(yes_token)
                if prices:
                    m_result["yes"] = prices
        except Exception as e:
            m_result["error_yes"] = str(e)
            
        try:
            if no_token:
                prices = await price_client.get_market_price_async(no_token)
                if prices:
                    m_result["no"] = prices
        except Exception as e:
            m_result["error_no"] = str(e)
            
        result["markets"][market_id] = m_result
    
    return result


@app.get("/api/status", response_model=DashboardStatusModel)
def api_status() -> DashboardStatusModel:
    """返回当前市场、策略与持仓的快照，用于前端轮询。"""
    now = time.time()

    # 当前市场
    current_markets = []
    for m in manager.current_markets:
        current_markets.append({
            "id": m.get("id"),
            "description": m.get("description"),
            "asset": m.get("__asset_type"),
            "tokens": m.get("tokens"),
            "end_time": m.get("expiry"),
        })

    strategies: List[StrategyStatusModel] = []

    for bot in manager.bots:
        active_trades: List[TradeModel] = []
        # [P1 修复] 使用安全快照方法读取，避免跨线程遍历字典时被后台 FSM 修改导致 RuntimeError
        for market_id, trade in bot._get_all_active_trades().items():
            ttl = trade.get("end_time", 0) - now
            status = trade.get("status") or ""
            
            # 【过滤】不向前端发送毫无意义的历史残留订单（比如完全没建仓的 idle 或已过期的闲置单）
            is_inactive = status in ("idle", "settled", "failed")
            has_no_leg1 = not trade.get("leg1")
            is_expired = ttl < -60
            
            if (has_no_leg1 and is_inactive and not trade.get("filter_reason")) or is_expired:
                continue
                
            profit_usdc = float(trade.get("profit_usdc", 0.0))
            gross_usdc = float(trade.get("gross_profit_usdc", 0.0))
            fee_usdc = float(trade.get("fee_usdc", 0.0))
            dynamic_ttl = trade.get("dynamic_ttl")
            leg1 = trade.get("leg1")
            leg2 = trade.get("leg2")
            if (gross_usdc == 0.0 or fee_usdc == 0.0 or profit_usdc == 0.0) and isinstance(leg1, dict) and isinstance(leg2, dict):
                try:
                    c1 = float(leg1.get("cost", 0.0))
                    s1 = float(leg1.get("size", 0.0))
                    c2 = float(leg2.get("cost", 0.0))
                    s2 = float(leg2.get("size", 0.0))
                    if s1 > 0 and s2 > 0:
                        from polymarket.config import TAKER_FEE_RATE, MAKER_FEE_RATE
                        leg1_type = getattr(bot, "leg1_order_type", "FOK")
                        leg2_type = getattr(bot, "leg2_order_type", "GTC")
                        fee1 = c1 * s1 * (TAKER_FEE_RATE if leg1_type == "FOK" else MAKER_FEE_RATE)
                        fee2 = c2 * s2 * (TAKER_FEE_RATE if leg2_type == "FOK" else MAKER_FEE_RATE)
                        gross_usdc = s1 - (c1 * s1 + c2 * s2)
                        fee_usdc = fee1 + fee2
                        profit_usdc = gross_usdc - fee_usdc
                        trade["gross_profit_usdc"] = round(gross_usdc, 4)
                        trade["fee_usdc"] = round(fee_usdc, 4)
                        trade["profit_usdc"] = round(profit_usdc, 4)
                except Exception as e:
                    import logging
                    logging.warning(f"Failed to calc EV: {e}")
                    
            active_trades.append(
                TradeModel(
                    market_id=market_id,
                    asset=trade.get("asset", ""),
                    status=trade.get("status") or "",
                    end_time=trade.get("end_time", 0.0),
                    leg1=LegModel(**trade["leg1"]) if trade.get("leg1") else None,
                    leg2=LegModel(**trade["leg2"]) if trade.get("leg2") else None,
                    leg1_dir="UP" if (trade.get("leg1") or {}).get("token") == trade.get("yes_token") else ("DOWN" if (trade.get("leg1") or {}).get("token") == trade.get("no_token") else ""),
                    leg2_dir="UP" if (trade.get("leg2") or {}).get("token") == trade.get("yes_token") else ("DOWN" if (trade.get("leg2") or {}).get("token") == trade.get("no_token") else ""),
                    profit_usdc=profit_usdc,
                    gross_profit_usdc=gross_usdc,
                    fee_usdc=fee_usdc,
                    dynamic_ttl=dynamic_ttl,
                    time_to_expiry=float(ttl),
                    strategy_id=bot.strategy_id,
                    filter_reason=trade.get("filter_reason"),
                    settlement_type=trade.get("settlement_type"),
                    dual_orders=trade.get("dual_orders", []),
                    events=trade.get("events", [])
                )
            )

        from polymarket import db as _db
        from polymarket.config import DB_PATH
        import json, os
        db_target_path = DB_PATH
        if not os.path.exists(db_target_path) and os.path.exists("data/trading.db"):
            db_target_path = "data/trading.db"
        historical_rows = _db.get_historical_trades(bot.strategy_id, limit=50, path=db_target_path)
        for row in historical_rows:
            hist_market_id = row["market_id"]
            # 去重：如果这个市场还在内存活跃列表里（还没被定时清理掉），就不重复从历史表里加载
            if hist_market_id in bot.active_trades:
                continue
                
            try:
                trade = json.loads(row["trade_json"])
                profit_usdc = row["ev"] or 0.0
                status_str = trade.get("status") or ""
                
                # [优化] 如果历史订单状态是 failed，直接跳过，不在前端长久展示，保持界面整洁
                if status_str == "failed":
                    continue
                    
                h_leg1 = trade.get("leg1")
                h_leg2 = trade.get("leg2")
                h_gross = float(trade.get("gross_profit_usdc", 0.0))
                h_fee = float(trade.get("fee_usdc", 0.0))
                if (h_gross == 0.0 or h_fee == 0.0) and isinstance(h_leg1, dict) and isinstance(h_leg2, dict):
                    try:
                        c1 = float(h_leg1.get("cost", 0.0))
                        s1 = float(h_leg1.get("size", 0.0))
                        c2 = float(h_leg2.get("cost", 0.0))
                        s2 = float(h_leg2.get("size", 0.0))
                        if s1 > 0 and s2 > 0:
                            from polymarket.config import TAKER_FEE_RATE, MAKER_FEE_RATE
                            leg1_type = getattr(bot, "leg1_order_type", "FOK")
                            leg2_type = getattr(bot, "leg2_order_type", "GTC")
                            fee1 = c1 * s1 * (TAKER_FEE_RATE if leg1_type == "FOK" else MAKER_FEE_RATE)
                            fee2 = c2 * s2 * (TAKER_FEE_RATE if leg2_type == "FOK" else MAKER_FEE_RATE)
                            h_gross = s1 - (c1 * s1 + c2 * s2)
                            h_fee = fee1 + fee2
                            profit_usdc = h_gross - h_fee
                    except Exception:
                        pass
                        
                active_trades.append(
                    TradeModel(
                        market_id=hist_market_id,
                        asset=trade.get("asset", ""),
                        status=trade.get("status") or "",
                        end_time=trade.get("end_time", 0.0),
                        leg1=LegModel(**trade["leg1"]) if trade.get("leg1") else None,
                        leg2=LegModel(**trade["leg2"]) if trade.get("leg2") else None,
                        leg1_dir="UP" if (trade.get("leg1") or {}).get("token") == trade.get("yes_token") else ("DOWN" if (trade.get("leg1") or {}).get("token") == trade.get("no_token") else ""),
                        leg2_dir="UP" if (trade.get("leg2") or {}).get("token") == trade.get("yes_token") else ("DOWN" if (trade.get("leg2") or {}).get("token") == trade.get("no_token") else ""),
                        profit_usdc=profit_usdc,
                        gross_profit_usdc=h_gross,
                        fee_usdc=h_fee,
                        dynamic_ttl=None,
                        time_to_expiry=-1.0,
                        strategy_id=bot.strategy_id,
                        settlement_type=trade.get("settlement_type"),
                        dual_orders=trade.get("dual_orders", []),
                        events=trade.get("events", [])
                    )
                )
            except Exception:
                pass

        strategy_total_pnl = sum(float(t.profit_usdc) for t in active_trades)

        strategies.append(
            StrategyStatusModel(
                strategy_id=bot.strategy_id,
                name=bot.config.get("name", bot.strategy_id),
                is_live=bot.is_live,
                entry_max_price=bot.entry_max_price,
                reentry_trigger=bot.reentry_trigger,
                amount=bot.order_amount,
                strategy_total_pnl=strategy_total_pnl,
                active_trades=active_trades,
            )
        )

    from polymarket.risk_manager import RiskManager
    from polymarket.kline_analyzer import get_asset_status
    from polymarket.config import SUPPORTED_ASSETS
    from polymarket.risk_logger import get_recent_risk_events
    return DashboardStatusModel(
        server_time=now,
        current_markets=current_markets,
        strategies=strategies,
        risk_metrics=RiskManager().get_status(),
        asset_status={a: get_asset_status(a) for a in SUPPORTED_ASSETS},
        risk_events=get_recent_risk_events(),
    )


@app.get("/api/metrics")
def get_metrics() -> Dict[str, Any]:
    """获取内部时序指标引擎的结构化 JSON 性能与交易数据。"""
    from polymarket.metrics import metrics
    return metrics.export_dashboard_json()


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", "8888"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, reload=False, access_log=False)

