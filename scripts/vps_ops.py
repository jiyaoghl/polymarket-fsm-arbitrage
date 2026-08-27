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
DEFAULT_VPS_HOST = os.getenv("VPS_HOST", "http://43.163.193.38:8888")

# ANSI 终端色彩
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_banner(title: str):
    print(f"\n{BOLD}{CYAN}{'=' * 75}{RESET}")
    print(f"{BOLD}{CYAN}  [*] {title} (VPS: {DEFAULT_VPS_HOST}){RESET}")
    print(f"{BOLD}{CYAN}{'=' * 75}{RESET}")


def fetch_api(endpoint: str, timeout: int = 5) -> Optional[Dict[str, Any]]:
    url = f"{DEFAULT_VPS_HOST.rstrip('/')}{endpoint}"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        print(f"{RED}[-] 请求 {url} 失败 [HTTP {r.status_code}]: {r.text[:200]}{RESET}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"{RED}[-] 无法连接到 VPS ({url}): {e}{RESET}")
        return None


def post_api(endpoint: str, timeout: int = 10, json_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    url = f"{DEFAULT_VPS_HOST.rstrip('/')}{endpoint}"
    try:
        r = requests.post(url, json=json_data or {}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        print(f"{RED}[-] 请求 {url} 失败 [HTTP {r.status_code}]: {r.text[:200]}{RESET}")
        return None
    except requests.exceptions.RequestException as e:
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
    """深度量化诊断与历史归因分析"""
    print_banner("VPS 深度量化与策略归因分析报告")
    diag = fetch_api("/api/diagnostics")
    if not diag:
        return

    recent_trades = diag.get("recent_historical_trades", [])
    print(f"[*] 最近 50 笔历史交易诊断数据 (实取: {len(recent_trades)} 笔)")

    if not recent_trades:
        print("  (暂无历史交易数据)")
        return

    stat_by_strat = {}
    for t in recent_trades:
        if "error" in t:
            continue
        sid = t.get("strategy_id", "unknown")
        status = t.get("status", "unknown")
        ev = float(t.get("profit_usdc") or t.get("ev") or 0.0)
        fee = float(t.get("fee_usdc") or 0.0)
        
        k = (sid, status)
        if k not in stat_by_strat:
            stat_by_strat[k] = {"count": 0, "net_pnl": 0.0, "fee": 0.0, "examples": []}
        stat_by_strat[k]["count"] += 1
        stat_by_strat[k]["net_pnl"] += ev
        stat_by_strat[k]["fee"] += fee
        stat_by_strat[k]["examples"].append(t)

    print("\n" + "=" * 75)
    print(f"{'策略 ID':<26s} | {'状态':<8s} | {'笔数':<4s} | {'净净利润 (Net PnL)':<16s} | {'手续费磨损'}")
    print("=" * 75)
    
    total_locked = 0
    total_failed = 0
    total_net = 0.0

    for (sid, status), d in sorted(stat_by_strat.items(), key=lambda x: x[0][0]):
        cnt = d["count"]
        net = d["net_pnl"]
        fee = d["fee"]
        total_net += net
        if status == "locked":
            total_locked += cnt
        elif status == "failed" or status == "stopped":
            total_failed += cnt

        pnl_str = f"{GREEN}+${net:.4f}{RESET}" if net > 0 else (f"{RED}-${abs(net):.4f}{RESET}" if net < 0 else "$0.0000")
        print(f"[{sid:<24s}] | {status:<8s} | {cnt:<4d} | {pnl_str:<25s} | ${fee:.4f}")

    total_trades = total_locked + total_failed
    win_rate = (total_locked / total_trades * 100) if total_trades > 0 else 0.0

    print("=" * 75)
    print(f"[*] 综合套利胜率 (Locked Win Rate): {BOLD}{win_rate:.1f}%{RESET} (锁利: {total_locked} 笔 / 强平: {total_failed} 笔)")
    net_total_str = f"{GREEN}+${total_net:.4f}{RESET}" if total_net > 0 else (f"{RED}-${abs(total_net):.4f}{RESET}" if total_net < 0 else "$0.0000")
    print(f"[*] 累计总已实现净收益 (Net PnL)   : {BOLD}{net_total_str}{RESET}")

    # 失败案例深挖
    print("\n[*] 强平止损案例深度原因透析 (Recent Stop-Losses):")
    failed_cases = [t for t in recent_trades if t.get("status") in ("failed", "stopped")]
    for t in failed_cases[:3]:
        print(f"  - [{t.get('strategy_id')}] 市场: {t.get('market_id')[:14]}... | 时间: {t.get('archived_at')}")
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
    ret_commit = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
    print(ret_commit.stdout)

    # 3. Git Push
    print(f"\n{BOLD}[3/4] 推送到远程仓库 origin/main...{RESET}")
    subprocess.run(["git", "push", "origin", "main"], check=True)
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
    res = fetch_api("/api/ops/clean-history", method="POST")
    if not res or res.get("status") != "ok":
        print(f"{RED}历史清理请求失败: {res.get('message') if res else '网络异常'}{RESET}")
        return

    print(f"{GREEN}[+] {res.get('message')}{RESET}")
    print(f"[*] 已清空数据表明细: {res.get('deleted_records')}")
    print("[*] 正在等待 VPS 重新初始化并就绪 (5~8 秒)...")
    time.sleep(6)
    cmd_status(args)


def main():
    parser = argparse.ArgumentParser(description="Polymarket VPS 敏捷运维与闭环分析 CLI")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

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

    # release
    p_release = subparsers.add_parser("release", help="一键运行单测 -> 提交 -> 推送 -> 远程动态热更")
    p_release.add_argument("message", type=str, help="Git 提交信息 (全中文)")
    p_release.add_argument("--no-deploy", action="store_true", help="仅推送仓库，不触发远程 VPS 热更")
    p_release.set_defaults(func=cmd_release)

    args = parser.parse_args()
    if not args.command:
        # 默认执行 status
        cmd_status(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
