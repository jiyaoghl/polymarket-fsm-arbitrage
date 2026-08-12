import threading
import time
from typing import Any, Dict, List
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from polymarket.apps.manager import StrategyManager
from polymarket.client import PolyClient


# 全局共享的策略管理器与行情客户端
manager = StrategyManager()
price_client = PolyClient(is_live=False)


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
    status: str
    end_time: float
    leg1: LegModel | None = None
    leg2: LegModel | None = None
    profit_usdc: float
    time_to_expiry: float
    strategy_id: str


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
    current_market: Dict[str, Any] | None
    strategies: List[StrategyStatusModel]
    risk_metrics: Dict[str, Any] = {}


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
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#0b1120; color:#e5e7eb; margin:0; padding:0; min-height: 100vh; }
    
    /* 通知样式 */
    #notifications { position: fixed; top: 16px; right: 16px; z-index: 1000; max-width: 400px; }
    .notification { background: #1f2937; border-radius: 8px; padding: 12px 16px; margin-bottom: 8px; border-left: 4px solid #3b82f6; animation: slideIn 0.3s ease; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
    .notification.success { border-left-color: #10b981; }
    .notification.warning { border-left-color: #f59e0b; }
    .notification.error { border-left-color: #ef4444; }
    .notification .title { font-weight: 600; margin-bottom: 4px; }
    .notification .message { font-size: 12px; color: #9ca3af; }
    @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
    
    header { padding: 16px 24px; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
    h1 { font-size: 20px; margin: 0; display: flex; align-items: center; gap: 8px; }
    .tag { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #111827; color: #9ca3af; }
    .tag.active { background: #10b98133; color: #6ee7b7; }
    
    /* 倒计时样式 */
    .countdown-container { display: flex; align-items: center; gap: 16px; }
    .countdown { font-size: 32px; font-weight: 700; font-variant-numeric: tabular-nums; color: #6ee7b7; }
    .countdown.warning { color: #fbbf24; }
    .countdown.danger { color: #f87171; }
    .next-market { font-size: 12px; color: #9ca3af; }
    
    main { padding: 16px 24px; display: grid; grid-template-columns: 1fr 1.5fr; gap: 16px; }
    @media (max-width: 1024px) { main { grid-template-columns: 1fr; } }
    
    section { background: #020617; border-radius: 12px; border: 1px solid #1f2937; padding: 16px; }
    h2 { font-size: 16px; margin: 0 0 12px 0; display: flex; align-items: center; gap: 8px; }
    h2 .badge { font-size: 11px; padding: 2px 6px; border-radius: 4px; background: #3b82f633; color: #93c5fd; }
    
    /* 价格卡片 */
    .price-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
    .price-card { background: #111827; border-radius: 8px; padding: 12px; border: 1px solid #1f2937; }
    .price-card .label { font-size: 11px; color: #6b7280; margin-bottom: 4px; text-transform: uppercase; }
    .price-card .price-row { display: flex; justify-content: space-between; align-items: center; }
    .price-card .side { font-weight: 600; font-size: 14px; }
    .price-card .side.YES { color: #6ee7b7; }
    .price-card .side.NO { color: #f87171; }
    .price-card .prices { font-size: 12px; color: #9ca3af; }
    .price-card .ask { color: #fbbf24; }
    .price-card .bid { color: #6ee7b7; }
    .price-card .spread { font-size: 10px; color: #6b7280; margin-top: 4px; }
    
    /* 市场信息 */
    .market-info { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; font-size: 12px; }
    .info-item { display: flex; flex-direction: column; gap: 2px; }
    .info-item .label { color: #6b7280; font-size: 10px; }
    .info-item .value { font-variant-numeric: tabular-nums; }
    
    /* 策略卡片 */
    .strategy-card { background: #111827; border-radius: 8px; padding: 12px; margin-bottom: 12px; border: 1px solid #1f2937; }
    .strategy-card.has-trades { border-color: #3b82f6; }
    .strategy-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .strategy-name { font-weight: 600; }
    .strategy-mode { font-size: 11px; padding: 2px 6px; border-radius: 4px; }
    .strategy-mode.live { background: #10b98133; color: #6ee7b7; }
    .strategy-mode.paper { background: #6b728033; color: #9ca3af; }
    .strategy-params { font-size: 11px; color: #6b7280; margin-bottom: 8px; }
    
    /* 仓位表格 */
    table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
    th, td { padding: 6px 8px; text-align: left; }
    th { background: #0b1120; color: #6b7280; font-weight: 500; border-bottom: 1px solid #1f2937; }
    tr:nth-child(even) td { background: #0b1120; }
    .pill { padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 500; }
    .pill-open { background: #0f766e33; color: #6ee7b7; }
    .pill-locked { background: #1d4ed833; color: #93c5fd; }
    .pill-stopped { background: #b91c1c33; color: #fecaca; }
    .pill-leg1_only { background: #f59e0b33; color: #fbbf24; }
    
    .no-trades { color: #6b7280; font-size: 12px; font-style: italic; }
    
    /* 状态指示器 */
    .status-indicator { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; }
    .status-dot { width: 6px; height: 6px; border-radius: 50%; background: #6b7280; }
    .status-dot.active { background: #10b981; animation: pulse 2s infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    
    /* 统计卡片 */
    .stats-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }
    .stat-card { background: #111827; border-radius: 6px; padding: 8px 12px; text-align: center; }
    .stat-card .value { font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
    .stat-card .label { font-size: 10px; color: #6b7280; }
  </style>
</head>
<body>
  <div id="notifications"></div>
  
  <header>
    <div>
      <h1>
        5min Symmetric Bot Dashboard
        <span class="tag active" id="status-tag">● 运行中</span>
      </h1>
      <div style="font-size: 12px; color: #6b7280; margin-top: 4px;" id="server-time">--</div>
    </div>
    <div class="countdown-container">
      <div>
        <div class="countdown" id="countdown">--:--</div>
        <div class="next-market" id="next-market">下一个市场</div>
      </div>
    </div>
  </header>
  
  <main>
    <section>
      <h2>📊 当前市场 <span class="badge" id="market-badge">等待中</span></h2>
      
      <!-- 实时价格 -->
      <div class="price-grid">
        <div class="price-card">
          <div class="label">YES Token</div>
          <div class="price-row">
            <span class="side YES">YES</span>
            <span class="prices" id="yes-price">--</span>
          </div>
          <div class="spread" id="yes-spread">spread: --</div>
        </div>
        <div class="price-card">
          <div class="label">NO Token</div>
          <div class="price-row">
            <span class="side NO">NO</span>
            <span class="prices" id="no-price">--</span>
          </div>
          <div class="spread" id="no-spread">spread: --</div>
        </div>
      </div>
      
      <!-- 市场信息 -->
      <div class="market-info">
        <div class="info-item">
          <span class="label">市场描述</span>
          <span class="value" id="m-desc">--</span>
        </div>
        <div class="info-item">
          <span class="label">Market ID</span>
          <span class="value" id="m-id" style="font-size: 10px;">--</span>
        </div>
        <div class="info-item">
          <span class="label">到期时间</span>
          <span class="value" id="m-expiry">--</span>
        </div>
        <div class="info-item">
          <span class="label">剩余时间</span>
          <span class="value" id="m-ttl">--</span>
        </div>
      </div>
    </section>
    
    <section>
      <h2>🛡️ 系统防御与战绩 (System Metrics)</h2>
      <div class="stats-row" id="risk-stats-row">
        <div class="stat-card">
          <div class="value" id="stat-intercept-count" style="color: #ef4444;">0</div>
          <div class="label">风控拦截笔数</div>
        </div>
        <div class="stat-card">
          <div class="value" id="stat-intercept-amt" style="color: #fbbf24;">$0.00</div>
          <div class="label">挽回资金 (USDC)</div>
        </div>
        <div class="stat-card">
          <div class="value" id="stat-retry-win" style="color: #10b981;">0 / 0</div>
          <div class="label">滑点微调成功</div>
        </div>
        <div class="stat-card">
          <div class="value" id="stat-exposure" style="color: #3b82f6;">0.0%</div>
          <div class="label">实时敞口利用率</div>
        </div>
      </div>
      
      <div style="background: #374151; border-radius: 99px; height: 8px; margin-top: 12px; overflow: hidden;">
        <div id="exposure-bar" style="background: linear-gradient(90deg, #3b82f6, #f59e0b); height: 100%; width: 0%; transition: width 0.5s ease;"></div>
      </div>
      <div style="font-size: 10px; color: #9ca3af; text-align: right; margin-top: 4px;" id="exposure-text">0.00 / 0.00 USDC</div>
    </section>
    
    <section>
      <h2>🎯 策略与持仓</h2>
      
      <!-- 统计信息 -->
      <div class="stats-row">
        <div class="stat-card">
          <div class="value" id="stat-strategies">0</div>
          <div class="label">策略数</div>
        </div>
        <div class="stat-card">
          <div class="value" id="stat-trades">0</div>
          <div class="label">活跃仓位</div>
        </div>
        <div class="stat-card">
          <div class="value" id="stat-locked">0</div>
          <div class="label">已锁仓</div>
        </div>
        <div class="stat-card">
          <div class="value" id="stat-ev">$0</div>
          <div class="label">总EV</div>
        </div>
      </div>
      
      <div id="strategies"></div>
    </section>
  </main>
  
  <script>
    // 状态管理
    let lastTrades = {};
    let lastMarketId = null;
    
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
      if (s === "stopped") return '<span class="pill pill-stopped">⏹ 已止损</span>';
      if (s === "leg1_only") return '<span class="pill pill-leg1_only">📈 首腿持仓</span>';
      return '<span class="pill pill-open">' + (status || 'open') + '</span>';
    }
    
    // 格式化倒计时
    function formatCountdown(seconds) {
      if (seconds <= 0) return '00:00';
      const mins = Math.floor(seconds / 60);
      const secs = Math.floor(seconds % 60);
      return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
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
        const res = await fetch('/api/prices');
        const data = await res.json();
        
        if (data.yes) {
          const yesAsk = data.yes.ask?.toFixed(4) || '--';
          const yesBid = data.yes.bid?.toFixed(4) || '--';
          document.getElementById('yes-price').innerHTML = 
            `<span class="ask">Ask: ${yesAsk}</span> <span class="bid">Bid: ${yesBid}</span>`;
          const spread = data.yes.ask && data.yes.bid ? 
            ((data.yes.ask - data.yes.bid) * 100).toFixed(2) : '--';
          document.getElementById('yes-spread').textContent = `spread: ${spread}%`;
        }
        
        if (data.no) {
          const noAsk = data.no.ask?.toFixed(4) || '--';
          const noBid = data.no.bid?.toFixed(4) || '--';
          document.getElementById('no-price').innerHTML = 
            `<span class="ask">Ask: ${noAsk}</span> <span class="bid">Bid: ${noBid}</span>`;
          const spread = data.no.ask && data.no.bid ? 
            ((data.no.ask - data.no.bid) * 100).toFixed(2) : '--';
          document.getElementById('no-spread').textContent = `spread: ${spread}%`;
        }
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
          '服务器时间: ' + new Date(data.server_time * 1000).toLocaleString('zh-CN');
        
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
          '下一期: ' + new Date(nextTs * 1000).toLocaleTimeString('zh-CN');
        
        // 市场信息
        const m = data.current_market;
        if (m) {
          document.getElementById('market-badge').textContent = '监控中';
          document.getElementById('m-desc').textContent = m.description || '--';
          document.getElementById('m-id').textContent = m.id ? m.id.slice(0, 20) + '...' : '--';
          
          if (m.end_time) {
            const expiry = new Date(m.end_time * 1000);
            document.getElementById('m-expiry').textContent = expiry.toLocaleTimeString('zh-CN');
            
            const ttl = m.end_time - data.server_time;
            document.getElementById('m-ttl').textContent = formatCountdown(ttl);
          }
          
          // 检测市场切换
          if (lastMarketId && lastMarketId !== m.id) {
            showNotification('🔄 市场切换', `新市场: ${m.description || m.id.slice(0, 15)}`, 'warning');
          }
          lastMarketId = m.id;
        } else {
          document.getElementById('market-badge').textContent = '等待中';
          document.getElementById('m-desc').textContent = '等待新市场...';
          document.getElementById('m-id').textContent = '--';
          document.getElementById('m-expiry').textContent = '--';
          document.getElementById('m-ttl').textContent = '--';
        }
        
        // 检测新交易
        checkNewTrades(data.strategies);
        
        // 统计信息
        let totalTrades = 0, lockedTrades = 0, totalEV = 0;
        data.strategies.forEach(s => {
          totalTrades += s.active_trades.length;
          s.active_trades.forEach(t => {
            if (t.status === 'locked') lockedTrades++;
            totalEV += t.profit_usdc || 0;
          });
        });
        
        document.getElementById('stat-strategies').textContent = data.strategies.length;
        document.getElementById('stat-trades').textContent = totalTrades;
        document.getElementById('stat-locked').textContent = lockedTrades;
        document.getElementById('stat-ev').textContent = `$${totalEV.toFixed(4)}`;
        
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
          div.className = `strategy-card ${s.active_trades.length ? 'has-trades' : ''}`;
          
          let html = `
            <div class="strategy-header">
              <span class="strategy-name">${s.name}</span>
              <span class="strategy-mode ${s.is_live ? 'live' : 'paper'}">${s.is_live ? 'LIVE' : 'PAPER'}</span>
            </div>
            <div class="strategy-params">
              入场价 ≤ ${s.entry_max_price.toFixed(3)} | 
              补仓触发 < ${s.reentry_trigger.toFixed(3)} | 
              仓位 $${s.amount.toFixed(2)} | 
              <strong style="color:${s.strategy_total_pnl > 0 ? '#6ee7b7' : (s.strategy_total_pnl < 0 ? '#f87171' : '#9ca3af')}">总盈亏: $${s.strategy_total_pnl.toFixed(4)}</strong>
            </div>
          `;
          
          if (!s.active_trades.length) {
            html += '<div class="no-trades">暂无活跃仓位</div>';
          } else {
            html += `<table><thead><tr>
              <th>Market</th><th>状态</th><th>首腿</th><th>二腿</th><th>TTL</th><th>EV</th>
            </tr></thead><tbody>`;
            
            s.active_trades.forEach((t) => {
              const leg1 = t.leg1 
                ? `${t.leg1.side} ${t.leg1.cost.toFixed(3)}×${t.leg1.size.toFixed(2)}`
                : '--';
              const leg2 = t.leg2 
                ? `${t.leg2.side} ${t.leg2.cost.toFixed(3)}×${t.leg2.size.toFixed(2)}`
                : '--';
                
              const timeStr = new Date(t.end_time * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute:'2-digit' });
              
              html += `<tr>
                <td style="font-size:10px;" title="${t.market_id}">${timeStr}</td>
                <td>${statusPill(t.status)}</td>
                <td>${leg1}</td>
                <td>${leg2}</td>
                <td>${formatCountdown(t.time_to_expiry)}</td>
                <td style="color:${t.profit_usdc > 0 ? '#6ee7b7' : '#f87171'};">${t.profit_usdc.toFixed(4)}</td>
              </tr>`;
            });
            
            html += '</tbody></table>';
          }
          
          div.innerHTML = html;
          container.appendChild(div);
        });
        
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
def api_prices():
    """获取当前市场的实时价格。"""
    if not manager.last_market:
        return {"yes": None, "no": None}
    
    tokens = manager.last_market.get("tokens", {})
    yes_token = tokens.get("YES")
    no_token = tokens.get("NO")
    
    result = {"yes": None, "no": None, "timestamp": time.time()}
    
    try:
        if yes_token:
            prices = price_client.get_market_price(yes_token)
            if prices:
                result["yes"] = prices
    except Exception:
        pass
    
    try:
        if no_token:
            prices = price_client.get_market_price(no_token)
            if prices:
                result["no"] = prices
    except Exception:
        pass
    
    return result


@app.get("/api/status", response_model=DashboardStatusModel)
def api_status() -> DashboardStatusModel:
    """返回当前市场、策略与持仓的快照，用于前端轮询。"""
    now = time.time()

    # 当前市场
    current_market = None
    if manager.last_market:
        m = manager.last_market.copy()
        # 统一字段名
        current_market = {
            "id": m.get("id"),
            "description": m.get("description"),
            "tokens": m.get("tokens"),
            "end_time": m.get("expiry"),
        }

    strategies: List[StrategyStatusModel] = []

    for bot in manager.bots:
        active_trades: List[TradeModel] = []
        for market_id, trade in bot.active_trades.items():
            ttl = trade.get("end_time", 0) - now
            
            profit_usdc = float(trade.get("profit_usdc", 0.0))
            leg1 = trade.get("leg1")
            leg2 = trade.get("leg2")
            if profit_usdc == 0.0 and isinstance(leg1, dict) and isinstance(leg2, dict):
                try:
                    c1 = float(leg1.get("cost", 0.0))
                    s1 = float(leg1.get("size", 0.0))
                    c2 = float(leg2.get("cost", 0.0))
                    s2 = float(leg2.get("size", 0.0))
                    if s1 > 0 and s2 > 0:
                        profit_usdc = s1 - (c1 * s1 + c2 * s2)
                        trade["profit_usdc"] = profit_usdc
                except Exception as e:
                    import logging
                    logging.warning(f"Failed to calc EV: {e}")
                    
            active_trades.append(
                TradeModel(
                    market_id=market_id,
                    status=trade.get("status") or "",
                    end_time=trade.get("end_time", 0.0),
                    leg1=LegModel(**trade["leg1"]) if trade.get("leg1") else None,
                    leg2=LegModel(**trade["leg2"]) if trade.get("leg2") else None,
                    profit_usdc=float(trade.get("profit_usdc", 0.0)),
                    time_to_expiry=float(ttl),
                    strategy_id=bot.strategy_id,
                )
            )

        strategy_total_pnl = sum(float(t.get("profit_usdc", 0.0)) for t in bot.active_trades.values())

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
    return DashboardStatusModel(
        server_time=now,
        current_market=current_market,
        strategies=strategies,
        risk_metrics=RiskManager().get_status(),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888, reload=False)

