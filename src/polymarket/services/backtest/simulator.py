"""
Polymarket 多市场高保真离线回测与快照并发模拟器。
"""

import gzip
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional

from src.polymarket.services.pricing import PricingEngine
from .models import SnapshotFrame, EvalResult


class SnapshotLoader:
    """高效快照流式加载器"""

    @staticmethod
    def load_all_frames(snapshot_dir: str, max_frames: Optional[int] = None) -> List[SnapshotFrame]:
        """从指定目录流式加载 .jsonl 或 .jsonl.gz 快照帧并按时间戳升序排序"""
        frames: List[SnapshotFrame] = []
        p = Path(snapshot_dir)
        if not p.exists():
            return frames

        files = sorted(list(p.glob("*.jsonl")) + list(p.glob("*.jsonl.gz")))
        for fpath in files:
            is_gz = str(fpath).endswith(".gz")
            opener = gzip.open(fpath, "rt", encoding="utf-8") if is_gz else open(fpath, "r", encoding="utf-8")
            try:
                with opener as f:
                    for line in f:
                        line = line.strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            d = json.loads(line)
                            frame = SnapshotFrame(
                                ts=float(d.get("ts", 0.0)),
                                token_id=str(d.get("token_id", "")),
                                best_bid=d.get("best_bid"),
                                best_ask=d.get("best_ask"),
                                bids=[(float(b[0]), float(b[1])) for b in d.get("bids", []) if len(b) >= 2],
                                asks=[(float(a[0]), float(a[1])) for a in d.get("asks", []) if len(a) >= 2],
                                spread=float(d.get("spread", 0.0)),
                                mid_price=float(d.get("mid_price", 0.5)),
                                obi=float(d.get("obi", 0.0)),
                                kline=d.get("kline", {}),
                                asset=d.get("asset")
                            )
                            frames.append(frame)
                            if max_frames and len(frames) >= max_frames:
                                return frames
                        except Exception:
                            continue
            except Exception as e:
                print(f"[Warn] 读取快照文件 {fpath} 异常: {e}")

        frames.sort(key=lambda x: x.ts)
        return frames


class MultiMarketSimulator:
    """全出场路径多市场并发高保真模拟器"""

    @staticmethod
    def simulate(frames: List[SnapshotFrame], params: Dict[str, Any], cooldown_sec: float = 120.0) -> EvalResult:
        """
        基于时间序列 L2 快照完整重现 TradeFSM 状态流转与损益：
        1. 资产级 Garman-Klass 极值波动率动态防御与 OBI 阶梯压迫；
        2. 多资产并发 120s 独立冷却锁；
        3. 首腿挂单与严格成交核验 (可配置 strict_fill)；
        4. 二腿未来 85s 时序卖盘击穿判定 (HEDGED_LOCKED)；
        5. 首腿买盘回升 OCO 做 T 变现 (SMART_FLIP)；
        6. 超时买盘前 5 档 VWAP 穿透市价强平与 2026 抛物线手续费真实惩罚 (FORCE_CLOSED)。
        """
        res = EvalResult(params=params)
        if not frames:
            return res

        # 组织时间戳索引与市场对
        ts_token_map = defaultdict(dict)
        for f in frames:
            ts_token_map[f.ts][f.token_id] = f

        sorted_ts = sorted(ts_token_map.keys())
        ts_idx_map = {t: idx for idx, t in enumerate(sorted_ts)}
        # 分市场并发锁：market_key -> last_trade_ts
        market_locks: Dict[str, float] = {}

        total_pnl = 0.0
        locked_cnt = 0
        flip_cnt = 0
        liq_cnt = 0

        mm_min_bid = params.get("mm_min_bid", 0.38)
        max_spread = params.get("max_spread", 0.05)
        obi_floor = params.get("obi_floor", -0.35)
        initial_margin = params.get("initial_margin", 0.018)
        amount = params.get("amount", 10.0)
        entry_max = params.get("entry_max_price", 0.42)
        entry_min = params.get("entry_min_price", 0.28)
        max_amp = params.get("max_amplitude", 0.35)
        max_net_chg = params.get("max_net_change", 0.15)
        strict_fill = bool(params.get("strict_fill", False))

        for ts in sorted_ts:
            tokens_dict = ts_token_map[ts]
            tids = sorted(tokens_dict.keys())
            if len(tids) < 2:
                continue

            # 两两配对评估市场 (通常一个预测盘口有 2 个 Token: YES / NO)
            for i in range(0, len(tids) - 1, 2):
                tid1, tid2 = tids[i], tids[i+1]
                m_key = f"{tid1[:10]}_{tid2[:10]}"

                # 检查该市场是否处于 120s 持仓冷却期
                if ts - market_locks.get(m_key, 0.0) < cooldown_sec:
                    continue

                f1, f2 = tokens_dict[tid1], tokens_dict[tid2]
                if not f1.best_bid or not f2.best_bid:
                    continue

                # 1. 资产波动率守门 (Garman-Klass + EWMA 多资产微观极值方差过滤)
                target_asset = (f1.asset or f2.asset or "BTC").upper()
                asset_k = f1.kline.get(target_asset) or f1.kline.get("BTC") or (next(iter(f1.kline.values())) if f1.kline else {})

                # 双模态自适应感知：最新快照含 gk_volatility，旧快照按 0.35x 折算
                if "gk_volatility" in asset_k:
                    cur_amp = float(asset_k["gk_volatility"])
                    cur_net = float(asset_k.get("net_change", 0.02))
                else:
                    raw_amp = float(asset_k.get("amplitude", 0.15))
                    cur_amp = round(raw_amp * 0.35, 4)
                    cur_net = round(float(asset_k.get("net_change", 0.05)) * 0.35, 4)

                if cur_amp > max_amp or abs(cur_net) > max_net_chg:
                    continue

                # 2. 基础成熟度与价差守门
                if f1.best_bid < mm_min_bid or f2.best_bid < mm_min_bid:
                    continue
                if f1.spread > max_spread or f2.spread > max_spread:
                    continue

                # 3. 动态 OBI 守门 (GK 极值尺度下联动)
                dynamic_floor = min(max(obi_floor + (cur_amp * 3.0), obi_floor), 0.0)
                if f1.obi < dynamic_floor or f2.obi < dynamic_floor:
                    continue

                # 4. 双挂定价与核算
                best_ask_1 = f1.best_ask if f1.best_ask else 0.60
                best_ask_2 = f2.best_ask if f2.best_ask else 0.60
                yes_p, no_p, err = PricingEngine.calculate_dual_bracket_prices(
                    best_bid_yes=f1.best_bid,
                    best_bid_no=f2.best_bid,
                    entry_max_price=entry_max,
                    entry_min_price=entry_min,
                    min_profit_margin=initial_margin,
                    best_ask_yes=best_ask_1,
                    best_ask_no=best_ask_2,
                    anti_penny_step=0.001
                )
                if err or not yes_p or not no_p:
                    continue

                is_prof, net_ev, _ = PricingEngine.verify_hedged_profitability(
                    yes_p, amount, no_p, amount,
                    min_profit_margin=initial_margin,
                    leg1_order_type="GTC", leg2_order_type="GTC"
                )

                if is_prof and net_ev > 0:
                    cur_idx = ts_idx_map.get(ts, 0)

                    # 严格被动成交模式：若开启，要求在当前帧或随后 3 帧内必须存在卖盘砸穿 (best_ask_1 <= yes_p)
                    if strict_fill:
                        leg1_filled = (best_ask_1 <= yes_p)
                        if not leg1_filled:
                            for fut_check_ts in sorted_ts[cur_idx + 1 : cur_idx + 4]:
                                fut_chk_f1 = ts_token_map.get(fut_check_ts, {}).get(tid1)
                                if fut_chk_f1 and fut_chk_f1.best_ask and fut_chk_f1.best_ask <= yes_p:
                                    leg1_filled = True
                                    break
                        if not leg1_filled:
                            continue

                    # 高保真时序撮合演化追踪 (Forward Temporal Scan)
                    window_ts = sorted_ts[cur_idx + 1 : cur_idx + 90]
                    is_settled = False
                    shares = round(amount / max(yes_p, 0.01), 2)

                    # 1. 优先判定当前帧二腿卖一是否即时打穿二腿买单 (best_ask_2 <= no_p)
                    if f2.best_ask and f2.best_ask <= no_p:
                        locked_cnt += 1
                        total_pnl += net_ev
                        is_settled = True

                    if not is_settled:
                        for fut_ts in window_ts:
                            fut_tokens = ts_token_map.get(fut_ts, {})
                            fut_f1 = fut_tokens.get(tid1)
                            fut_f2 = fut_tokens.get(tid2)
                            if not fut_f1 or not fut_f2:
                                continue

                            # 2. 时序双买锁仓判定：未来窗口内二腿卖一打穿二腿买单 (best_ask_2 <= no_p)
                            if fut_f2.best_ask and fut_f2.best_ask <= no_p:
                                locked_cnt += 1
                                total_pnl += net_ev
                                is_settled = True
                                break

                            # 3. 阶梯做 T 判定：首腿买一回升突破保利高抛线 (best_bid_1 >= yes_p + 0.005)
                            if fut_f1.best_bid and fut_f1.best_bid >= (yes_p + 0.005):
                                flip_cnt += 1
                                flip_pnl = round(shares * 0.005, 4)
                                total_pnl += flip_pnl
                                is_settled = True
                                break

                    # 4. 超时真实强平：85s 内二腿未打穿且未能做 T，市价割肉斩仓并扣除 2026 抛物线手续费
                    if not is_settled:
                        liq_cnt += 1
                        last_fut_ts = window_ts[-1] if window_ts else ts
                        last_f1 = ts_token_map.get(last_fut_ts, {}).get(tid1, f1)

                        # 买盘前 5 档 VWAP 深度穿透核算与流动性枯竭滑点折算
                        exit_price = None
                        if last_f1.bids:
                            vwap, marginal_p, filled = PricingEngine.calculate_bid_vwap_and_marginal(
                                last_f1.bids, shares, allow_partial=True
                            )
                            if vwap and filled >= shares:
                                exit_price = vwap
                            elif vwap and filled > 0:
                                # 深度不足时，未被吃完的缺口按边缘价下浮 0.02 施加滑点惩罚
                                shortfall = shares - filled
                                penalized_p = max((marginal_p or vwap) - 0.02, 0.01)
                                blended_rev = (filled * vwap) + (shortfall * penalized_p)
                                exit_price = round(blended_rev / shares, 4)

                        if not exit_price:
                            exit_price = last_f1.best_bid if last_f1.best_bid else max(yes_p - 0.04, 0.01)

                        gross_loss = round(shares * (exit_price - yes_p), 4)
                        # 纠正：参数必须为 (price=exit_price, size=shares)
                        liq_fee = PricingEngine.calculate_parabolic_fee(exit_price, shares, fee_rate=0.07)
                        net_loss = round(gross_loss - liq_fee, 4)
                        total_pnl += net_loss

                    market_locks[m_key] = ts

        total_trades = locked_cnt + flip_cnt + liq_cnt
        res.total_trades = total_trades
        res.hedged_locked_count = locked_cnt
        res.smart_flip_count = flip_cnt
        res.liquidated_count = liq_cnt
        res.total_net_ev = round(total_pnl, 4)
        res.avg_net_margin = round(total_pnl / total_trades, 4) if total_trades > 0 else 0.0
        res.win_rate = round((locked_cnt + flip_cnt) / max(total_trades, 1) * 100.0, 1)
        res.score = round(total_pnl * (1.0 + min(total_trades, 100) / 100.0), 4)
        return res
