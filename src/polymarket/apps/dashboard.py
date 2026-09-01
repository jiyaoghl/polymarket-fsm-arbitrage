import threading
import time
from pathlib import Path
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
    """FastAPI 生命周期管理：启动时初始化后台线程与 Discord 交互机器人。"""
    t = threading.Thread(target=manager.run_all, daemon=True)
    t.start()

    # 启动 Discord 交互式控制机器人 (若未配置 Token 则自动平滑跳过)
    try:
        from polymarket.services.discord_bot import DiscordInteractiveBot
        DiscordInteractiveBot.get_instance().start()
    except Exception as e:
        pass

    # 启动 L2 盘口深度快照录包守护进程 (阶段 3: 真实 L2 录包)
    try:
        from polymarket.services.l2_recorder import L2SnapshotRecorder
        L2SnapshotRecorder.get_instance().start()
    except Exception as e:
        pass

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

_TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
_CACHED_HTML: str = ""


def _get_dashboard_html() -> str:
    """读取并缓存仪表盘前端 HTML 模板。"""
    global _CACHED_HTML
    if not _CACHED_HTML and _TEMPLATE_PATH.exists():
        _CACHED_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _CACHED_HTML


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """前端仪表盘页面 (支持实时盘口、策略监控与微观时序诊断)。"""
    return _get_dashboard_html()



@app.get("/api/prices")
async def api_prices():
    """获取当前市场的实时价格（优先从 OrderbookMemoryGrid 纯内存 0 延迟提取）。"""
    from polymarket.services.grid import OrderbookMemoryGrid
    from polymarket.kline_analyzer import get_asset_status
    grid = OrderbookMemoryGrid()
    now_ts = time.time()
    result = {"timestamp": now_ts, "markets": {}, "assets": {}}
    
    for m in manager.current_markets:
        market_id = m.get("id")
        if not market_id:
            continue
        tokens = m.get("tokens", {})
        yes_token = tokens.get("YES")
        no_token = tokens.get("NO")
        asset = m.get("__asset_type", "BTC")
        
        if asset not in result["assets"]:
            status = get_asset_status(asset)
            result["assets"][asset] = status
        
        m_result = {"yes": None, "no": None, "asset": asset}
        if yes_token:
            snap = grid.get_snapshot(yes_token)
            if snap and snap.best_bid is not None and snap.best_ask is not None:
                bid_size = snap.bids[0][1] if snap.bids else 0.0
                ask_size = snap.asks[0][1] if snap.asks else 0.0
                m_result["yes"] = {
                    "bid": snap.best_bid, 
                    "ask": snap.best_ask,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    "spread": snap.spread,
                    "age_ms": round((now_ts - snap.last_update_ts) * 1000, 1)
                }
            else:
                try:
                    p = await price_client.get_market_price_async(yes_token)
                    if p:
                        m_result["yes"] = p
                except Exception as e:
                    m_result["error_yes"] = str(e)
                    
        if no_token:
            snap = grid.get_snapshot(no_token)
            if snap and snap.best_bid is not None and snap.best_ask is not None:
                bid_size = snap.bids[0][1] if snap.bids else 0.0
                ask_size = snap.asks[0][1] if snap.asks else 0.0
                m_result["no"] = {
                    "bid": snap.best_bid, 
                    "ask": snap.best_ask,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                    "spread": snap.spread,
                    "age_ms": round((now_ts - snap.last_update_ts) * 1000, 1)
                }
            else:
                try:
                    p = await price_client.get_market_price_async(no_token)
                    if p:
                        m_result["no"] = p
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
                
            # 优先提取结构化交易中记录的净损益与手续费
            profit_usdc = float(trade.get("profit_usdc") if trade.get("profit_usdc") is not None else (trade.get("ev") or 0.0))
            gross_usdc = float(trade.get("gross_profit_usdc") if trade.get("gross_profit_usdc") is not None else profit_usdc)
            fee_usdc = float(trade.get("fee_usdc", 0.0) or 0.0)
            dynamic_ttl = trade.get("dynamic_ttl")
            leg1 = trade.get("leg1")
            leg2 = trade.get("leg2")
                    
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
                
                # [优化] 如果历史订单状态是 failed，或从未建仓开单 (无 leg1 与 leg2)，直接跳过，保持界面干净
                h_leg1 = trade.get("leg1")
                h_leg2 = trade.get("leg2")
                if status_str == "failed" or (not h_leg1 and not h_leg2):
                    continue
                # 优先提取数据库/快照中已准确核算的权威净损益与手续费
                profit_usdc = float(trade.get("profit_usdc") if trade.get("profit_usdc") is not None else (row.get("ev") or 0.0))
                h_gross = float(trade.get("gross_profit_usdc") if trade.get("gross_profit_usdc") is not None else profit_usdc)
                h_fee = float(trade.get("fee_usdc", 0.0) or 0.0)
                        
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



@app.get("/api/trade_detail/{market_id}")
def get_trade_detail(market_id: str):
    import sqlite3, json
    from polymarket.config import DB_PATH
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as conn:
            c = conn.cursor()
            # 优先查活跃表
            c.execute("SELECT trade_json FROM active_trades WHERE market_id = ?", (market_id,))
            row = c.fetchone()
            if not row:
                # 查历史表
                c.execute("SELECT trade_json FROM historical_trades WHERE market_id = ?", (market_id,))
                row = c.fetchone()
            if row:
                t_data = json.loads(row[0])
                # 计算各种指标
                t_data['latency_ms'] = 0
                if t_data.get('leg1_filled_time') and t_data.get('leg2_issued_time'):
                    t_data['latency_ms'] = round((float(t_data['leg2_issued_time']) - float(t_data['leg1_filled_time'])) * 1000, 1)
                return t_data
            else:
                return {"error": "Trade not found"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/logs/tail")

def get_logs_tail(lines: int = 100, source: str = "trade") -> Dict[str, Any]:
    """安全读取 VPS 服务端最新的日志切片，支持远程一键排查。"""
    import os
    from polymarket.config import paths
    
    max_lines = min(max(lines, 10), 1000)
    target_file = "trade.log"
    if source == "nohup":
        target_file = "nohup.log"
    elif source == "error":
        target_file = "trade_error.log"
        
    log_path = paths.logs_dir() / target_file
    if not log_path.exists():
        # 兼容 vps-logs 目录
        alt_path = paths.project_root() / "vps-logs" / target_file
        if alt_path.exists():
            log_path = alt_path

    if not log_path.exists():
        return {"status": "error", "message": f"Log file not found: {target_file}", "lines": []}

    try:
        # 使用 deque 快速读取最后 N 行，内存极其安全
        from collections import deque
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            tail_lines = list(deque(f, maxlen=max_lines))
        return {
            "status": "ok",
            "source": target_file,
            "line_count": len(tail_lines),
            "lines": [line.rstrip("\r\n") for line in tail_lines]
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "lines": []}


@app.get("/api/diagnostics")
def get_diagnostics() -> Dict[str, Any]:
    """导出 VPS 全量结构化诊断报告，用于本地一键快速分析与策略调优。"""
    from polymarket.metrics import metrics
    from polymarket.risk_manager import RiskManager
    from polymarket.kline_analyzer import get_asset_status
    from polymarket.config import SUPPORTED_ASSETS
    from polymarket.risk_logger import get_recent_risk_events
    import sqlite3
    import json
    from polymarket.config import DB_PATH
    
    # 1. 抓取近期归档交易 (50 笔)
    recent_history = []
    try:
        with sqlite3.connect(DB_PATH, timeout=5) as conn:
            c = conn.cursor()
            c.execute("SELECT market_id, strategy_id, ev, archived_at, trade_json FROM historical_trades ORDER BY archived_at DESC LIMIT 50")
            for mid, sid, ev, arch_time, t_json in c.fetchall():
                try:
                    t_data = json.loads(t_json)
                except Exception:
                    t_data = {}
                recent_history.append({
                    "market_id": mid,
                    "strategy_id": sid,
                    "ev": ev,
                    "archived_at": arch_time,
                    "status": t_data.get("status"),
                    "profit_usdc": t_data.get("profit_usdc"),
                    "gross_profit_usdc": t_data.get("gross_profit_usdc"),
                    "fee_usdc": t_data.get("fee_usdc"),
                    "dynamic_ttl": t_data.get("dynamic_ttl"),
                    "settlement_type": t_data.get("settlement_type"),
                    "reprice_count": t_data.get("reprice_count", 0),
                    "reprice_history": t_data.get("reprice_history", []),
                    "leg1": t_data.get("leg1"),
                    "leg2": t_data.get("leg2")
                })
    except Exception as e:
        recent_history = [{"error": str(e)}]

    # 2. 计算转化率与出场归因汇总 (Conversion & PnL Summary)
    conversion_summary = {
        "total_trades": 0,
        "locked_count": 0,
        "locked_rate_pct": 0.0,
        "dual_exit_sells_count": 0,
        "dual_exit_win_rate_pct": 0.0,
        "force_close_count": 0,
        "total_reprice_count": 0,
        "avg_reprice_per_trade": 0.0,
        "total_gross_pnl": 0.0,
        "total_fees_usdc": 0.0,
        "total_net_pnl": 0.0,
        "by_strategy": {}
    }

    valid_trades = [t for t in recent_history if "error" not in t]
    conversion_summary["total_trades"] = len(valid_trades)
    tot_reprices = sum(int(t.get("reprice_count") or 0) for t in valid_trades)
    conversion_summary["total_reprice_count"] = tot_reprices
    conversion_summary["avg_reprice_per_trade"] = round(tot_reprices / len(valid_trades), 2) if valid_trades else 0.0

    for t in valid_trades:
        sid = t.get("strategy_id") or "unknown"
        net_pnl = float(t.get("profit_usdc") or t.get("ev") or 0.0)
        gross = float(t.get("gross_profit_usdc") or 0.0)
        fee = float(t.get("fee_usdc") or 0.0)
        st = t.get("settlement_type") or ("FORCE_CLOSE" if t.get("status") == "failed" else "OTHER")

        conversion_summary["total_net_pnl"] += net_pnl
        conversion_summary["total_gross_pnl"] += gross
        conversion_summary["total_fees_usdc"] += fee

        if st == "HEDGED_LOCKED" or t.get("status") == "locked":
            conversion_summary["locked_count"] += 1
        elif st == "DUAL_EXIT_SELL_SETTLED":
            conversion_summary["dual_exit_sells_count"] += 1
        elif st == "FORCE_CLOSE" or t.get("status") in ("failed", "stopped"):
            conversion_summary["force_close_count"] += 1

        if sid not in conversion_summary["by_strategy"]:
            conversion_summary["by_strategy"][sid] = {
                "total": 0, "wins": 0, "losses": 0, "ties": 0, "win_rate_pct": 0.0,
                "net_pnl": 0.0, "gross_pnl": 0.0, "fee_usdc": 0.0,
                "routes": {}
            }
        
        bs = conversion_summary["by_strategy"][sid]
        bs["total"] += 1
        bs["net_pnl"] += net_pnl
        bs["gross_pnl"] += gross
        bs["fee_usdc"] += fee
        if net_pnl > 0.0001:
            bs["wins"] += 1
        elif net_pnl < -0.0001:
            bs["losses"] += 1
        else:
            bs["ties"] += 1
        
        if st not in bs["routes"]:
            bs["routes"][st] = {"count": 0, "net_pnl": 0.0, "gross_pnl": 0.0, "fee_usdc": 0.0}
        bs["routes"][st]["count"] += 1
        bs["routes"][st]["net_pnl"] += net_pnl
        bs["routes"][st]["gross_pnl"] += gross
        bs["routes"][st]["fee_usdc"] += fee

    if conversion_summary["total_trades"] > 0:
        tot = conversion_summary["total_trades"]
        conversion_summary["locked_rate_pct"] = round(conversion_summary["locked_count"] / tot * 100, 1)
        if conversion_summary["dual_exit_sells_count"] > 0:
            dual_wins = sum(1 for t in valid_trades if t.get("settlement_type") == "DUAL_EXIT_SELL_SETTLED" and float(t.get("profit_usdc") or 0.0) > 0)
            conversion_summary["dual_exit_win_rate_pct"] = round(dual_wins / conversion_summary["dual_exit_sells_count"] * 100, 1)

    for sid, bs in conversion_summary["by_strategy"].items():
        if bs["total"] > 0:
            bs["win_rate_pct"] = round(bs["wins"] / bs["total"] * 100, 1)

    return {
        "timestamp": time.time(),
        "risk_metrics": RiskManager().get_status(),
        "asset_status": {a: get_asset_status(a) for a in SUPPORTED_ASSETS},
        "risk_events": get_recent_risk_events(),
        "performance_metrics": metrics.export_dashboard_json(),
        "conversion_summary": conversion_summary,
        "recent_historical_trades": recent_history
    }


@app.post("/api/ops/update")
def remote_update() -> Dict[str, Any]:
    """
    远程敏捷更新与热重载接口 (Remote Dynamic Update & Reload)。
    支持免 SSH 登录，远程自动触发 VPS 执行 git pull 与平滑重启。
    """
    import os
    import subprocess
    import threading
    import logging

    def _async_update():
        time.sleep(1.0)
        try:
            if os.name != "nt":
                subprocess.Popen(
                    ["bash", "vps.sh", "update"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            else:
                subprocess.Popen(
                    ["python", "-m", "polymarket.apps.dashboard"],
                    start_new_session=True
                )
        except Exception as err:
            logging.getLogger("poly_bot").error(f"[RemoteOps] 触发远程热更失败: {err}")

    threading.Thread(target=_async_update, daemon=True).start()

    return {
        "status": "ok",
        "action": "update",
        "message": "已成功下发远程热更指令！VPS 正在拉取最新代码并自动平滑重载...",
        "timestamp": time.time()
    }


@app.post("/api/ops/restart")
def remote_restart() -> Dict[str, Any]:
    """远程平滑重启接口。"""
    import os
    import subprocess
    import threading
    import logging

    def _async_restart():
        time.sleep(1.0)
        try:
            if os.name != "nt":
                subprocess.Popen(
                    ["bash", "vps.sh", "restart"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
        except Exception as err:
            logging.getLogger("poly_bot").error(f"[RemoteOps] 触发远程重启失败: {err}")

    threading.Thread(target=_async_restart, daemon=True).start()

    return {
        "status": "ok",
        "action": "restart",
        "message": "已成功下发远程重启指令！VPS 正在重新加载服务...",
        "timestamp": time.time()
    }


@app.post("/api/ops/clean-history")
def remote_clean_history() -> Dict[str, Any]:
    """
    远程清理 VPS 历史交易与订单数据，并平滑重载服务。
    """
    import os
    import subprocess
    import threading
    import logging
    from polymarket.db import clean_all_historical_trades

    counts = clean_all_historical_trades()

    def _async_restart():
        time.sleep(1.0)
        try:
            if os.name != "nt":
                subprocess.Popen(
                    ["bash", "vps.sh", "restart"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            else:
                subprocess.Popen(
                    ["python", "-m", "polymarket.apps.dashboard"],
                    start_new_session=True
                )
        except Exception as err:
            logging.getLogger("poly_bot").error(f"[RemoteOps] 清理后重启失败: {err}")

    threading.Thread(target=_async_restart, daemon=True).start()

    return {
        "status": "ok",
        "action": "clean-history",
        "deleted_records": counts,
        "message": "已成功清理所有历史交易与订单数据，VPS 正在重新加载服务...",
        "timestamp": time.time()
    }


@app.get("/api/snapshots/list")
def list_snapshots(days: int = 1) -> Dict[str, Any]:
    """列出最近 N 天内的 L2 快照文件（用于 sync-snapshots 拉取）。"""
    from polymarket.config import SNAPSHOT_DIR
    from datetime import datetime, timedelta
    snapshot_dir = Path(SNAPSHOT_DIR)
    if not snapshot_dir.exists():
        return {"files": [], "total_size_mb": 0}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d_%H")
    files = []
    total_size = 0
    for f in sorted(snapshot_dir.glob("*.jsonl*")):
        stem = f.name.replace(".gz", "").replace(".jsonl", "")
        if stem >= cutoff:
            sz = f.stat().st_size
            files.append({"name": f.name, "size": sz})
            total_size += sz
    return {"files": files, "total_size_mb": round(total_size / 1024 / 1024, 2)}


@app.get("/api/snapshots/download/{filename}")
def download_snapshot(filename: str):
    """下载单个快照文件（支持 .jsonl 与 .jsonl.gz）。"""
    from fastapi.responses import FileResponse
    from polymarket.config import SNAPSHOT_DIR
    filepath = Path(SNAPSHOT_DIR) / filename
    if not filepath.exists() or not (filename.endswith(".jsonl") or filename.endswith(".jsonl.gz")):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    media_type = "application/gzip" if filename.endswith(".gz") else "application/x-ndjson"
    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type=media_type
    )


if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("PORT", "8888"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, reload=False, access_log=False)

