#!/usr/bin/env python3
"""
Polymarket VPS 敏捷运维与闭环分析套件 (Fast Dev-Ops & Analysis CLI)

使用场景:
  1. 状态大盘: python scripts/vps_ops.py status
  2. 远程日志: python scripts/vps_ops.py logs -n 50 (-f 实时跟踪)
  3. 深度分析: python scripts/vps_ops.py analyze
  4. 一键发布: python scripts/vps_ops.py release "feat: 优化参数"
"""

import sys
import os
import time
import argparse
import subprocess
from typing import Dict, Any, Optional

# 兼容 Windows GBK 终端
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import requests
except ImportError:
    print("[-] 缺少 requests 依赖，请运行 pip install requests")
    sys.exit(1)

# 默认 VPS 地址，支持环境变量覆盖
DEFAULT_VPS_HOST = os.getenv("VPS_HOST", "http://161.120.187.156:8888")


def get_active_vps_host() -> str:
    """
    智能解析最优先的 VPS 端点：
    1. 若配置了显式环境变量 VPS_HOST，直接使用；
    2. 优先探测本地 SSH 隧道 (http://127.0.0.1:8888)；
    3. 若本地隧道连通，走本地安全通道；
    4. 否则使用 DEFAULT_VPS_HOST (公网地址)。
    """
    if os.getenv("VPS_HOST"):
        return os.getenv("VPS_HOST")
    try:
        r = requests.get("http://127.0.0.1:8888/api/status", timeout=0.6)
        if r.status_code == 200:
            return "http://127.0.0.1:8888"
    except Exception:
        pass
    return DEFAULT_VPS_HOST

# ANSI 终端色彩
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_banner(title: str):
    active_host = get_active_vps_host()
    channel_tag = f"{GREEN}[安全隧道]{RESET}" if "127.0.0.1" in active_host or "localhost" in active_host else f"{YELLOW}[公网通道]{RESET}"
    print(f"\n{BOLD}{CYAN}{'=' * 75}{RESET}")
    print(f"{BOLD}{CYAN}  [*] {title} ({channel_tag} {active_host}){RESET}")
    print(f"{BOLD}{CYAN}{'=' * 75}{RESET}")


def fetch_api(endpoint: str, timeout: int = 5, retries: int = 3) -> Optional[Dict[str, Any]]:
    host = get_active_vps_host()
    url = f"{host.rstrip('/')}{endpoint}"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            print(f"{RED}[-] 请求 {url} 失败 [HTTP {r.status_code}]: {r.text[:200]}{RESET}")
            return None
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                time.sleep(0.5 * attempt)
                continue
            print(f"{RED}[-] 无法连接到 VPS ({url}): {e}{RESET}")
            if "127.0.0.1" not in host:
                print(f"{YELLOW}[提示] 若公网已关闭，请先运行: python scripts/vps_ops.py tunnel 建立 SSH 加密隧道{RESET}")
            return None


def post_api(endpoint: str, timeout: int = 10, json_data: Optional[Dict[str, Any]] = None, retries: int = 3) -> Optional[Dict[str, Any]]:
    host = get_active_vps_host()
    url = f"{host.rstrip('/')}{endpoint}"
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(url, json=json_data or {}, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            print(f"{RED}[-] 请求 {url} 失败 [HTTP {r.status_code}]: {r.text[:200]}{RESET}")
            return None
        except requests.exceptions.RequestException as e:
            if attempt < retries:
                time.sleep(0.5 * attempt)
                continue
            print(f"{RED}[-] 无法连接到 VPS ({url}): {e}{RESET}")
            return None


def cmd_status(args):
    """一键获取 VPS 实时大盘、活跃仓位、各策略盈亏与延迟"""
    print_banner("VPS 实时运行与量化大盘状态")
    status_data = fetch_api("/api/status")
    metrics_data = fetch_api("/api/metrics")

    if not status_data:
        return

    # 1. 市场与风控
    server_time = status_data.get("server_time", time.time())
    markets = status_data.get("current_markets", [])
    print(f"[*] 服务器时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(server_time))}")
    print(f"[*] 当前追踪市场: {len(markets)} 个")
    for m in markets:
        print(f"  - [{m.get('asset', 'UNK')}] {m.get('description', '')} (ID: {m.get('id', '')[:12]}...)")

    # 2. 多品种波动率
    asset_status = status_data.get("asset_status", {})
    if asset_status:
        print("\n[*] 行情波动率守门状态:")
        for asset, bs in asset_status.items():
            if bs.get("timestamp", 0) > 0:
                is_choppy = bs.get("is_choppy", False)
                amp = bs.get("amplitude", 0.0)
                net = bs.get("net_change", 0.0)
                status_str = f"{GREEN}[+] 震荡市 (允许开仓){RESET}" if is_choppy else f"{RED}[!] 单边波动 (拦截开仓){RESET}"
                print(f"  - {asset:4s}: 振幅={amp:.2f}% | 净变动={net:.2f}% | {status_str}")

    # 3. 策略盈亏与活跃仓位
    strategies = status_data.get("strategies", [])
    print(f"\n[*] 各策略盈亏与活跃单汇总 (共 {len(strategies)} 个策略):")
    print(f"  {'策略名称':<32s} | {'模式':<5s} | {'入场价':<7s} | {'持仓数':<6s} | {'总盈亏 (Net PnL)'}")
    print(f"  {'-' * 32}-|-{'-' * 5}-|-{'-' * 7}-|-{'-' * 6}-|-{'-' * 18}")
    
    total_pnl = 0.0
    for s in strategies:
        sid = s.get("name", s.get("strategy_id", ""))
        mode = f"{GREEN}LIVE{RESET}" if s.get("is_live") else f"{BLUE}PAPER{RESET}"
        pnl = s.get("strategy_total_pnl", 0.0)
        total_pnl += pnl
        pnl_str = f"{GREEN}+${pnl:.4f}{RESET}" if pnl > 0 else (f"{RED}-${abs(pnl):.4f}{RESET}" if pnl < 0 else "$0.0000")
        entry_p = f"≤{s.get('entry_max_price', 0):.3f}"
        act_cnt = len(s.get("active_trades", []))
        print(f"  {sid:<32s} | {mode:14s} | {entry_p:<7s} | {act_cnt:<6d} | {pnl_str}")

    total_pnl_str = f"{GREEN}+${total_pnl:.4f}{RESET}" if total_pnl > 0 else (f"{RED}-${abs(total_pnl):.4f}{RESET}" if total_pnl < 0 else "$0.0000")
    print(f"\n  [$] 全组合累计已实现盈亏: {BOLD}{total_pnl_str}{RESET}")

    # 4. 延迟指标
    if metrics_data:
        hists = metrics_data.get("histograms", {})
        order_hist = (hists.get("poly_order_latency_seconds") or [{}])[0].get("summary", {})
        tick_hist = (hists.get("poly_tick_process_latency_seconds") or [{}])[0].get("summary", {})
        
        print("\n[*] 时序与延迟指标快照:")
        print(f"  - 下单往返延迟: P50={order_hist.get('p50', 0)*1000:.1f}ms | Avg={order_hist.get('avg', 0)*1000:.1f}ms | P99={order_hist.get('p99', 0)*1000:.1f}ms (样本: {order_hist.get('count', 0)} 笔)")
        print(f"  - Tick处理分发: P50={tick_hist.get('p50', 0)*1000:.2f}ms | Avg={tick_hist.get('avg', 0)*1000:.2f}ms | P99={tick_hist.get('p99', 0)*1000:.2f}ms (采样: {tick_hist.get('count', 0)} 帧)")


def cmd_logs(args):
    """一键抓取 VPS 日志，支持实时 tail -f"""
    lines = args.lines or 60
    source = args.source or "trade"
    follow = args.follow

    print_banner(f"VPS 日志查看 (源: {source}.log, 行数: {lines})")

    seen_lines = set()

    while True:
        res = fetch_api(f"/api/logs/tail?lines={lines}&source={source}")
        if not res or res.get("status") != "ok":
            print(f"{RED}获取日志失败: {res.get('message') if res else '网络异常'}{RESET}")
            if not follow:
                break
            time.sleep(2)
            continue

        log_lines = res.get("lines", [])
        new_lines = []
        for line in log_lines:
            if follow:
                if line not in seen_lines:
                    seen_lines.add(line)
                    new_lines.append(line)
            else:
                new_lines.append(line)

        for line in new_lines:
            if "ERROR" in line or "CRITICAL" in line or "failed" in line or "强平" in line:
                print(f"{RED}{line}{RESET}")
            elif "WARNING" in line or "拦截" in line or "溢价" in line:
                print(f"{YELLOW}{line}{RESET}")
            elif "LOCKED" in line or "锁仓" in line or "结算" in line or "成功" in line:
                print(f"{GREEN}{line}{RESET}")
            else:
                print(line)

        if not follow:
            break
        time.sleep(2)


def cmd_analyze(args):
    """深度量化诊断与历史归因分析 (消费 VPS /api/diagnostics 转化率指标与出场路由)"""
    print_banner("VPS 深度量化与策略归因分析报告")
    diag = fetch_api("/api/diagnostics")
    if not diag:
        return

    summary = diag.get("conversion_summary", {})
    recent_trades = diag.get("recent_historical_trades", [])
    perf_metrics = diag.get("performance_metrics", {})
    unhedged_hist = perf_metrics.get("histograms", {}).get("poly_unhedged_duration_seconds", {})

    print(f"[*] 样本数据规模: 最近 {len(recent_trades)} 笔归档交易诊断记录\n")

    # 1. 北极星核心指标卡
    tot_trades = summary.get("total_trades", 0)
    locked_cnt = summary.get("locked_count", 0)
    locked_pct = summary.get("locked_rate_pct", 0.0)
    dual_cnt = summary.get("dual_exit_sells_count", 0)
    dual_win_pct = summary.get("dual_exit_win_rate_pct", 0.0)
    force_cnt = summary.get("force_close_count", 0)
    tot_reprices = summary.get("total_reprice_count", 0)
    avg_reprices = summary.get("avg_reprice_per_trade", 0.0)
    net_pnl = summary.get("total_net_pnl", 0.0)
    gross_pnl = summary.get("total_gross_pnl", 0.0)
    total_fee = summary.get("total_fees_usdc", 0.0)

    net_color = GREEN if net_pnl > 0 else (RED if net_pnl < 0 else RESET)
    
    # 安全解析 Histogram 结构
    p50_hold = 0.0
    p90_hold = 0.0
    if isinstance(unhedged_hist, list) and unhedged_hist:
        sum_dict = unhedged_hist[0].get("summary", {})
        p50_hold = sum_dict.get("p50", 0.0)
        p90_hold = sum_dict.get("p90", 0.0)
    elif isinstance(unhedged_hist, dict):
        p50_hold = unhedged_hist.get("p50", 0.0)
        p90_hold = unhedged_hist.get("p90", 0.0)

    print("┌" + "─" * 76 + "┐")
    print(f"│  {BOLD}⭐ 北极星转化率与损益指标卡 (North Star Metrics){RESET}{' '*28}│")
    print("├" + "─" * 76 + "┤")
    print(f"│  • LOCKED 锁仓转化率  : {BOLD}{locked_pct:5.1f}%{RESET} ({locked_cnt} 笔成功锁仓 / {tot_trades} 笔总开仓){' '*17}│")
    print(f"│  • OCO 做T卖出脱手率  : {BOLD}{dual_win_pct:5.1f}%{RESET} ({dual_cnt} 笔做T变现成交){' '*32}│")
    print(f"│  • 二腿追单/改单次数  : {BOLD}{tot_reprices}{RESET} 次 (平均每笔 {avg_reprices:.1f} 次){' '*37}│")
    print(f"│  • 强平 / 止损次数    : {BOLD}{force_cnt}{RESET} 次{' '*57}│")
    print(f"│  • 未对冲时长分布     : P50={p50_hold:.2f}s | P90={p90_hold:.2f}s{' '*38}│")
    print(f"│  • 组合总毛利润       : ${gross_pnl:+.4f}{' '*54}│")
    print(f"│  • 总费率磨损 (Fee)   : ${total_fee:.4f}{' '*55}│")
    print(f"│  • 扣费净净利润 (Net) : {BOLD}{net_color}${net_pnl:+.4f}{RESET}{' '*54}│")
    print("└" + "─" * 76 + "┘")

    # 2. 分策略与出场路径透视表
    by_strat = summary.get("by_strategy", {})
    if by_strat:
        print("\n" + "=" * 80)
        print(f"{'策略 ID':<26s} | {'总笔数':<6s} | {'胜/负/平':<10s} | {'胜率':<8s} | {'毛利 (Gross)':<12s} | {'手续费':<10s} | {'净盈亏 (Net PnL)'}")
        print("=" * 80)

        for sid, s in by_strat.items():
            w_l_t = f"{s.get('wins',0)}/{s.get('losses',0)}/{s.get('ties',0)}"
            s_net = s.get("net_pnl", 0.0)
            s_gross = s.get("gross_pnl", 0.0)
            s_fee = s.get("fee_usdc", 0.0)
            s_win_r = s.get("win_rate_pct", 0.0)
            p_color = GREEN if s_net > 0 else (RED if s_net < 0 else RESET)
            print(f"{sid:<26s} | {s.get('total',0):^6d} | {w_l_t:<10s} | {s_win_r:6.1f}% | ${s_gross:+10.4f} | ${s_fee:8.4f} | {p_color}${s_net:+.4f}{RESET}")

        print("=" * 80)

        print("\n>>> 各策略微观出场路径拆解 (Settlement Route Breakdown):")
        for sid, s in by_strat.items():
            print(f"\n【策略: {sid}】:")
            for st, r in sorted(s.get("routes", {}).items(), key=lambda x: x[1].get("count", 0), reverse=True):
                r_cnt = r.get("count", 0)
                r_net = r.get("net_pnl", 0.0)
                r_gross = r.get("gross_pnl", 0.0)
                r_fee = r.get("fee_usdc", 0.0)
                r_color = GREEN if r_net > 0 else (RED if r_net < 0 else RESET)
                print(f"  - 出场模式 [{st:<24s}]: {r_cnt:2d} 笔 | 净净利润: {r_color}${r_net:+9.4f}{RESET} (毛利: ${r_gross:+9.4f}, 费率: ${r_fee:6.4f})")

    # 3. 失败案例深挖
    failed_cases = [t for t in recent_trades if t.get("status") in ("failed", "stopped") or t.get("settlement_type") == "FORCE_CLOSE"]
    if failed_cases:
        print("\n[*] 强平止损案例深度原因透析 (Recent Stop-Losses):")
        for t in failed_cases[:3]:
            print(f"  - [{t.get('strategy_id')}] 市场: {str(t.get('market_id'))[:14]}... | 时间: {t.get('archived_at')}")
            print(f"    首腿开仓: {t.get('leg1')} -> 二腿平仓: {t.get('leg2')}")
            print(f"    单笔净亏: {RED}${float(t.get('profit_usdc', 0)):.4f}{RESET} (手续费: ${float(t.get('fee_usdc', 0)):.4f}, 动态TTL: {t.get('dynamic_ttl')}s)")


def trigger_vps_update():
    """向 VPS 下发远程动态热更指令并轮询健康状态"""
    print(f"\n{BOLD}[4/4] 远程动态触发 VPS 热重载与自动更新...{RESET}")
    res = post_api("/api/ops/update")
    if not res or res.get("status") != "ok":
        print(f"{YELLOW}[!] 远程热更端点调用未返回成功 (可能需手动执行一次 bash vps.sh update 激活新API)。{RESET}")
        return

    print(f"{GREEN}[+] {res.get('message', '热更指令已下发！')}{RESET}")
    print(f"[*] 正在等待 VPS 自动重启与服务就绪 (预计 5~10 秒)...")
    
    # 轮询健康检查
    success = False
    for i in range(1, 11):
        time.sleep(1.5)
        print(f"  - 探测 VPS 服务状态 (尝试 {i}/10)...", end="\r")
        status_data = fetch_api("/api/status", timeout=3)
        if status_data and "server_time" in status_data:
            success = True
            print(f"\n{GREEN}[+] 🚀 VPS 服务已成功完成平滑重启与热重载！{RESET}")
            break

    if not success:
        print(f"\n{YELLOW}[!] 轮询超时，VPS 可能仍在构建依赖中，可稍后运行 python scripts/vps_ops.py status 检查。{RESET}")


def cmd_update(args):
    """单独远程触发 VPS 拉取最新代码并热重载"""
    print_banner("VPS 远程动态更新与热重载")
    trigger_vps_update()
    cmd_status(args)


def cmd_release(args):
    """一键自动化发布: 本地回归测试 -> Git Commit -> Git Push -> 远程 VPS 自动热更"""
    msg = args.message
    if not msg:
        print(f"{RED}[-] 请提供 commit message，例如: python scripts/vps_ops.py release \"feat: 优化参数\"{RESET}")
        sys.exit(1)

    print_banner(f"一键测试、发布与动态热更流水线 (Release: {msg})")

    # 1. 运行本地自动化测试
    print(f"\n{BOLD}[1/4] 运行本地回归测试套件...{RESET}")
    ret = subprocess.run([sys.executable, "-m", "pytest", "-s", "tests/"], capture_output=False)
    if ret.returncode != 0:
        print(f"{RED}[-] 测试用例未全部通过，已中止发布！请修复后再试。{RESET}")
        sys.exit(1)
    print(f"{GREEN}[+] 自动化测试 100% 绿灯通过！{RESET}")

    # 2. Git Commit
    print(f"\n{BOLD}[2/4] 暂存并提交代码...{RESET}")
    subprocess.run(["git", "add", "."], check=True)
    ret_commit = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(ret_commit.stdout)

    # 3. Git Push
    print(f"\n{BOLD}[3/4] 推送到远程仓库 origin/main...{RESET}")
    subprocess.run(["git", "push", "origin", "main"], check=True, encoding="utf-8", errors="replace")
    print(f"{GREEN}[+] 代码已成功推送到远程仓库！{RESET}")

    # 4. 远程触发 VPS 自动热重载 (除非显式指定 --no-deploy)
    if not getattr(args, "no_deploy", False):
        trigger_vps_update()
        cmd_status(args)
    else:
        print(f"{YELLOW}[*] 已跳过远程自动部署 (--no-deploy)。{RESET}")


def cmd_clean_history(args):
    """远程调用 VPS 执行历史订单与交易数据彻底清理并重置大盘"""
    print_banner("VPS 历史订单与交易数据清理")
    print(f"[*] 正在向远程 VPS 发送清空历史订单指令...")
    res = post_api("/api/ops/clean-history")
    if not res or res.get("status") != "ok":
        print(f"{RED}历史清理请求失败: {res.get('message') if res else '网络异常'}{RESET}")
        return

    print(f"{GREEN}[+] {res.get('message')}{RESET}")
    print(f"[*] 已清空数据表明细: {res.get('deleted_records')}")
    print("[*] 正在等待 VPS 重新初始化并就绪 (5~8 秒)...")
    time.sleep(6)
    cmd_status(args)



def cmd_inspect(args):
    """透视单个市场的交易明细"""
    import json
    market_id = args.market_id
    res = fetch_api(f"/api/trade_detail/{market_id}")
    if not res:
        return
    if "error" in res:
        print(f"{RED}未找到该市场的交易记录：{res['error']}{RESET}")
        return
        
    print_banner(f"Trade Inspector: {market_id}")
    print(f"[{BOLD}Status{RESET}] {res.get('status')} | [{BOLD}Settlement{RESET}] {res.get('settlement_type', '--')}")
    
    if res.get("profit_usdc") is not None:
        pnl = float(res.get("profit_usdc"))
        pnl_str = f"{GREEN}+${pnl:.4f}{RESET}" if pnl > 0 else (f"{RED}-${abs(pnl):.4f}{RESET}" if pnl < 0 else "$0.0000")
        print(f"[{BOLD}Net PnL{RESET}] {pnl_str}")
        
    if res.get("latency_ms"):
        print(f"[{BOLD}Latency{RESET}] {res.get('latency_ms')} ms")
        
    print("\n-- Leg 1 --")
    if res.get("leg1"):
        l = res["leg1"]
        print(f"  {l.get('side')} {l.get('size')} @ {l.get('cost')}")
    else:
        print("  None")
        
    print("\n-- Leg 2 --")
    if res.get("leg2"):
        l = res["leg2"]
        print(f"  {l.get('side')} {l.get('size')} @ {l.get('cost')}")
    else:
        print("  None")
        
    reprices = res.get("reprice_history", [])
    if reprices:
        print(f"\n-- Reprice History ({len(reprices)}) --")
        for rp in reprices:
            ts = time.strftime('%H:%M:%S', time.localtime(rp.get('timestamp', 0)))
            print(f"  [{ts}] {rp.get('old_price')} -> {rp.get('new_price')} ({rp.get('reason')})")


def cmd_sync_snapshots(args):
    """从 VPS 拉取 L2 盘口快照文件到本地 vps-logs/snapshots/。"""
    days = getattr(args, "days", 1)
    print_banner(f"VPS L2 快照同步 (最近 {days} 天)")

    # 1. 列出远端可用文件
    data = fetch_api(f"/api/snapshots/list?days={days}")
    if not data:
        print(f"{RED}[-] 无法连接到 VPS 或快照列表为空{RESET}")
        return

    files = data.get("files", [])
    total_mb = data.get("total_size_mb", 0)
    if not files:
        print(f"{YELLOW}[*] VPS 上没有最近 {days} 天的快照文件。录包守护进程可能尚未启动。{RESET}")
        return

    print(f"{GREEN}[+] 发现 {len(files)} 个快照文件，总计 {total_mb:.2f} MB{RESET}")

    # 2. 确保本地目录存在
    local_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vps-logs", "snapshots")
    os.makedirs(local_dir, exist_ok=True)

    # 3. 逐个下载
    downloaded = 0
    skipped = 0
    for finfo in files:
        fname = finfo["name"]
        remote_size = finfo["size"]
        local_path = os.path.join(local_dir, fname)

        # 跳过已存在且大小一致的文件
        if os.path.exists(local_path) and os.path.getsize(local_path) == remote_size:
            skipped += 1
            continue

        url = f"{DEFAULT_VPS_HOST.rstrip('/')}/api/snapshots/download/{fname}"
        try:
            r = requests.get(url, timeout=30, stream=True)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            downloaded += 1
            print(f"  {GREEN}↓{RESET} {fname} ({remote_size / 1024:.1f} KB)")
        except Exception as e:
            print(f"  {RED}✗{RESET} {fname} 下载失败: {e}")

def cmd_sync_strategies(args):
    """一键将本地 configs/strategies.json 投递并热重载至 VPS。"""
    print_banner("远程同步并动态热重载策略配置 (Zero-Downtime Reload)")
    import json
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    strat_file = os.path.join(project_root, "configs", "strategies.json")
    if not os.path.exists(strat_file):
        print(f"{RED}[-] 策略配置文件不存在: {strat_file}{RESET}")
        return
    try:
        with open(strat_file, "r", encoding="utf-8") as f:
            strategies = json.load(f)
    except Exception as e:
        print(f"{RED}[-] 读取本地策略配置失败: {e}{RESET}")
        return

    print(f"[*] 正在向 VPS 投递 {len(strategies)} 组策略配置...")
    res = post_api("/api/ops/sync-strategies", timeout=15, json_data=strategies)
    if res and res.get("status") == "ok":
        print(f"{GREEN}[+] {res.get('message')}{RESET}")
        print(f"{GREEN}[+] 当前活跃策略数: {res.get('count')} 个: {res.get('loaded_strategies')}{RESET}")
    else:
        print(f"{RED}[-] 同步失败: {res}{RESET}")


def cmd_tunnel(args):
    """一键建立并保持与 VPS 的 SSH 端口加密隧道。"""
    print_banner("建立与管理 VPS SSH 加密安全隧道 (Port Forwarding)")
    vps_ip = getattr(args, "host", "161.120.187.156")
    ssh_user = getattr(args, "user", "ubuntu")
    remote_port = 8888
    local_port = getattr(args, "port", 8888)

    print(f"[*] 目标 VPS: {ssh_user}@{vps_ip}")
    print(f"[*] 映射端口: 本地 localhost:{local_port} -> VPS 127.0.0.1:{remote_port}")

    # 检查本地端口是否已经打通
    try:
        r = requests.get(f"http://127.0.0.1:{local_port}/api/status", timeout=0.8)
        if r.status_code == 200:
            print(f"\n{GREEN}[+] 检测到本地 SSH 隧道当前已处于连通状态！{RESET}")
            print(f"[*] 本地 Web 仪表盘: {CYAN}http://localhost:{local_port}{RESET}")
            print(f"[*] 此时运行任何 vps_ops.py 命令均已自动走本地加密安全通道。")
            return
    except Exception:
        pass

    ssh_cmd = f"ssh -N -L {local_port}:127.0.0.1:{remote_port} {ssh_user}@{vps_ip}"
    print(f"\n{BOLD}[+] 正在启动 SSH 加密隧道进程...{RESET}")
    print(f"{YELLOW}提示: 若提示输入密码，请输入 VPS 的 SSH 登录密码。{RESET}")
    print(f"{YELLOW}提示: 保持此窗口开启即可维持加密隧道；按 Ctrl+C 可随时断开。{RESET}")
    print(f"{CYAN}命令: {ssh_cmd}{RESET}\n")
    try:
        os.system(ssh_cmd)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[*] SSH 隧道已由用户主动断开。{RESET}")


def cmd_live_check(args):
    """实盘真金上线前置五维全链路护航体检 (Live Pilot Pre-flight Check)。"""
    skip_probe = getattr(args, "skip_probe", False)
    run_local = getattr(args, "local", False)
    print_banner("实盘真金上线前置全链路五维自检 (Live Pilot Pre-flight)")

    if run_local:
        print("[*] 探测视角: 本地开发机直接探测")
        from polymarket.services.live_checker import LivePreflightChecker
        checker = LivePreflightChecker()
        res = checker.run_all(skip_probe=skip_probe)
    else:
        print("[*] 探测视角: 远程 VPS 生产节点透视 (真实网络与撮合)")
        endpoint = f"/api/ops/live-check?skip_probe={str(skip_probe).lower()}"
        res = fetch_api(endpoint, timeout=20)

    if not res:
        print(f"{RED}[-] 无法获取实盘体检报告，请检查 VPS 服务或 SSH 隧道状态{RESET}")
        return

    checks = res.get("checks", {})
    overall = res.get("overall_status", "UNKNOWN")
    conclusion = res.get("conclusion", "")
    elapsed = res.get("elapsed_seconds", 0.0)

    color_map = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED, "SKIPPED": BLUE}

    def format_status(st: str) -> str:
        c = color_map.get(st, RESET)
        return f"{c}[{st}]{RESET}"

    # 1. 凭证
    c_cred = checks.get("credentials", {})
    print(f"\n{BOLD}1. 钱包与 API 凭证规范性:{RESET} {format_status(c_cred.get('status', ''))}")
    print(f"   • 钱包地址: {c_cred.get('masked_address', '--')} (签名类型 Type={c_cred.get('signature_type', 0)})")
    print(f"   • API Key 凭证: {'✅ 已配置' if c_cred.get('has_api_creds') else '⚠️ 未配置'}")
    print(f"   • 诊断说明: {c_cred.get('message', '')}")

    # 2. 时钟与网络
    c_clock = checks.get("clock_and_latency", {})
    print(f"\n{BOLD}2. NTP 时钟校准与撮合延迟:{RESET} {format_status(c_clock.get('status', ''))}")
    print(f"   • 本地与撮合时差: {c_clock.get('drift_ms')} ms (要求: |Δt| < 500ms)")
    print(f"   • CLOB HTTP/2 延迟: {c_clock.get('latency_ms')} ms")
    print(f"   • 诊断说明: {c_clock.get('message', '')}")

    # 3. 链上资产
    c_chain = checks.get("chain_balances", {})
    print(f"\n{BOLD}3. Polygon 链上 Gas 与 USDC 储备:{RESET} {format_status(c_chain.get('status', ''))}")
    pol_val = c_chain.get('pol_balance', c_chain.get('matic_balance', 0.0))
    print(f"   • POL 原生代币 (Gas 费): {pol_val} POL (预警线: 0.1 POL)")
    print(f"   • 链上原生 pUSD  : ${c_chain.get('pusd_balance', 0.0):.4f}")
    print(f"   • 链上原生 USDC  : ${c_chain.get('usdc_native_balance', 0.0):.2f}")
    print(f"   • 链上跨链 USDC.e: ${c_chain.get('usdc_bridged_balance', 0.0):.2f}")
    print(f"   • 响应 RPC 节点  : {c_chain.get('rpc_node', '--')}")
    print(f"   • 诊断说明: {c_chain.get('message', '')}")

    # 4. CLOB 托管与授权
    c_clob = checks.get("clob_collateral", {})
    print(f"\n{BOLD}4. CLOB 托管资金与合约授权 (Allowance):{RESET} {format_status(c_clob.get('status', ''))}")
    print(f"   • CLOB 内部可交易余额: ${c_clob.get('clob_balance_usdc', 0.0):.4f} USDC")
    print(f"   • 合约授权状态: {c_clob.get('allowance_status', '已就绪')}")
    print(f"   • 是否已完成 Approve 授权: {'✅ 是' if c_clob.get('is_approved') else '❌ 否 (阻断开仓)'}")
    print(f"   • 诊断说明: {c_clob.get('message', '')}")

    # 5. 发单/撤单探针
    c_probe = checks.get("order_roundtrip_probe", {})
    print(f"\n{BOLD}5. EIP-712 极低价发单/撤单穿透探针:{RESET} {format_status(c_probe.get('status', ''))}")
    if c_probe.get("order_id"):
        print(f"   • 测试探测订单 ID: {c_probe.get('order_id')}")
    print(f"   • 诊断说明: {c_probe.get('message', '')}")

    print(f"\n{BOLD}{CYAN}{'=' * 75}{RESET}")
    print(f"[*] 综合自检评级: {format_status(overall)} (总耗时: {elapsed}s)")
    print(f"[*] 评估结论: {conclusion}")
    print(f"{BOLD}{CYAN}{'=' * 75}{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="Polymarket VPS 敏捷运维与闭环分析 CLI")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # live-check
    p_live = subparsers.add_parser("live-check", help="实盘真金上线前置全链路五维自检")
    p_live.add_argument("--skip-probe", action="store_true", help="跳过发单与撤单穿透探针 (仅做纯只读检查)")
    p_live.add_argument("--local", action="store_true", help="在本地开发机执行检查 (默认连接远程 VPS 探测)")
    p_live.set_defaults(func=cmd_live_check)

    # tunnel
    p_tunnel = subparsers.add_parser("tunnel", help="一键建立并保持与 VPS 的 SSH 端口加密安全隧道")
    p_tunnel.add_argument("--user", type=str, default="ubuntu", help="VPS SSH 用户名 (默认: ubuntu)")
    p_tunnel.add_argument("--host", type=str, default="161.120.187.156", help="VPS IP 地址")
    p_tunnel.add_argument("-p", "--port", type=int, default=8888, help="本地映射端口 (默认: 8888)")
    p_tunnel.set_defaults(func=cmd_tunnel)

    # status
    p_status = subparsers.add_parser("status", help="获取 VPS 实时大盘、活跃仓位与延迟")
    p_status.set_defaults(func=cmd_status)

    # logs
    p_logs = subparsers.add_parser("logs", help="获取 VPS 最新运行日志")
    p_logs.add_argument("-n", "--lines", type=int, default=60, help="读取日志行数 (默认: 60)")
    p_logs.add_argument("-s", "--source", type=str, default="trade", choices=["trade", "nohup", "error"], help="日志源 (trade / nohup / error)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="实时持续跟踪日志 (类似 tail -f)")
    p_logs.set_defaults(func=cmd_logs)

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="深度量化诊断与胜率归因分析")
    p_analyze.set_defaults(func=cmd_analyze)

    # update / reload
    p_update = subparsers.add_parser("update", help="远程直接触发 VPS 执行动态更新与热重载 (免登录)")
    p_update.set_defaults(func=cmd_update)

    # clean-history
    p_clean = subparsers.add_parser("clean-history", help="远程清空 VPS 所有历史订单与交易数据并重置大盘")
    p_clean.set_defaults(func=cmd_clean_history)


    # inspect
    p_inspect = subparsers.add_parser("inspect", help="透视单笔历史交易明细 (生命周期、让价轨迹与滑点)")
    p_inspect.add_argument("market_id", type=str, help="市场 ID")
    p_inspect.set_defaults(func=cmd_inspect)

    # release
    p_release = subparsers.add_parser("release", help="一键运行单测 -> 提交 -> 推送 -> 远程动态热更")
    p_release.add_argument("message", type=str, help="Git 提交信息 (全中文)")
    p_release.add_argument("--no-deploy", action="store_true", help="仅推送仓库，不触发远程 VPS 热更")
    p_release.set_defaults(func=cmd_release)

    # sync-snapshots
    p_sync = subparsers.add_parser("sync-snapshots", help="从 VPS 拉取 L2 盘口快照到本地 vps-logs/snapshots/")
    p_sync.add_argument("--days", type=int, default=1, help="拉取最近 N 天的快照 (默认: 1)")
    p_sync.set_defaults(func=cmd_sync_snapshots)

    # sync-strategies
    p_sync_strat = subparsers.add_parser("sync-strategies", help="一键将本地 configs/strategies.json 投递并热重载至 VPS")
    p_sync_strat.set_defaults(func=cmd_sync_strategies)

    args = parser.parse_args()
    if not args.command:
        # 默认执行 status
        cmd_status(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
