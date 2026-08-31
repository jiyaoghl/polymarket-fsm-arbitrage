from typing import Dict, Any, Tuple, Optional, List
from polymarket.config import TAKER_FEE_RATE, MAKER_FEE_RATE
from polymarket.logger import logger

class PricingEngine:
    """
    纯数学计算与定价引擎 (Stateless Pure Math Service)。
    不产生任何 I/O 与网络请求，纯粹基于盘口与头寸进行确定性数学核算。
    """

    @staticmethod
    def calculate_vwap(orderbook_asks: List[Any], target_shares: float) -> Optional[float]:
        """
        基于订单簿深度计算买入 target_shares 份数的加权平均成交价 (Ask VWAP)。
        若深度不足，返回 None。
        """
        if not orderbook_asks or target_shares <= 0:
            return None

        # 统一解析格式: [price, size] 或 {"price": ..., "size": ...}
        parsed_asks = []
        for item in orderbook_asks:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                parsed_asks.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict):
                parsed_asks.append((float(item.get("price", 0.0)), float(item.get("size", 0.0))))

        # 卖单按价格升序排列 (从低到高吃)
        sorted_asks = sorted(parsed_asks, key=lambda x: x[0])
        
        accum_shares = 0.0
        total_cost = 0.0

        for price, size in sorted_asks:
            if size <= 0:
                continue

            needed = target_shares - accum_shares
            if size >= needed:
                total_cost += needed * price
                accum_shares += needed
                break
            else:
                total_cost += size * price
                accum_shares += size

        if accum_shares < target_shares:
            return None

        return round(total_cost / target_shares, 4)

    @staticmethod
    def calculate_bid_vwap_and_marginal(
        orderbook_bids: List[Any],
        target_shares: float,
        allow_partial: bool = True
    ) -> Tuple[Optional[float], Optional[float], float]:
        """
        穿透订单簿买盘深度计算:
        1. 卖出 target_shares 份数的加权平均成交价 (Bid VWAP)
        2. 吞没 target_shares 份数所触及的最深一档买价 (Marginal Fill Price)
        3. 实际吃到的总份数 (Filled Shares)
        
        若买盘深度不足且 allow_partial=True，则以可用最大深度全量穿透计算并返回最后一档价格；
        若 allow_partial=False 且深度不足，则返回 (None, None, accum_shares)。
        
        Returns:
            (vwap_price, marginal_price, filled_shares)
        """
        if not orderbook_bids or target_shares <= 0:
            return None, None, 0.0

        parsed_bids = []
        for item in orderbook_bids:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                parsed_bids.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict):
                parsed_bids.append((float(item.get("price", 0.0)), float(item.get("size", 0.0))))

        # 买单按价格降序排列 (从高到低吃买盘)
        sorted_bids = sorted(parsed_bids, key=lambda x: x[0], reverse=True)

        accum_shares = 0.0
        total_revenue = 0.0
        last_marginal_price = None

        for price, size in sorted_bids:
            if size <= 0:
                continue

            last_marginal_price = price
            needed = target_shares - accum_shares
            if size >= needed:
                total_revenue += needed * price
                accum_shares += needed
                break
            else:
                total_revenue += size * price
                accum_shares += size

        if accum_shares <= 0 or last_marginal_price is None:
            return None, None, 0.0

        if not allow_partial and accum_shares < target_shares:
            return None, None, round(accum_shares, 4)

        vwap_price = round(total_revenue / accum_shares, 4)
        return vwap_price, last_marginal_price, round(accum_shares, 4)

    @staticmethod
    def calculate_bid_vwap(orderbook_bids: List[Any], target_shares: float) -> Optional[float]:
        """
        基于订单簿买盘深度计算卖出平仓 target_shares 份数的加权平均成交价 (Bid VWAP)。
        买单按价格降序排列 (从高到低吃买盘)。
        若深度不足，返回 None。
        """
        vwap, _, _ = PricingEngine.calculate_bid_vwap_and_marginal(orderbook_bids, target_shares, allow_partial=False)
        return vwap

    @staticmethod
    def calculate_net_ev(
        leg1_cost: float,
        leg1_size: float,
        leg2_cost: float,
        leg2_size: float,
        leg1_order_type: str = "FOK",
        leg2_order_type: str = "GTC"
    ) -> Tuple[float, float, float]:
        """
        精确核算双腿扣费净 EV。
        
        Returns:
            (gross_profit, total_fee, net_ev)
        """
        if leg1_size <= 0 or leg2_size <= 0:
            return 0.0, 0.0, 0.0

        fee1_rate = TAKER_FEE_RATE if leg1_order_type == "FOK" else MAKER_FEE_RATE
        fee2_rate = TAKER_FEE_RATE if leg2_order_type == "FOK" else MAKER_FEE_RATE

        fee1 = leg1_cost * leg1_size * fee1_rate
        fee2 = leg2_cost * leg2_size * fee2_rate
        total_fee = fee1 + fee2

        # Guaranteed payout is min(leg1_size, leg2_size) * $1.00
        gross_profit = min(leg1_size, leg2_size) - (leg1_cost * leg1_size + leg2_cost * leg2_size)
        net_ev = gross_profit - total_fee

        return round(gross_profit, 4), round(total_fee, 4), round(net_ev, 4)

    @staticmethod
    def verify_hedged_profitability(
        leg1_cost: float,
        leg1_size: float,
        leg2_cost: float,
        leg2_size: float,
        min_profit_margin: float = 0.015,
        leg1_order_type: str = "GTC",
        leg2_order_type: str = "GTC"
    ) -> Tuple[bool, float, str]:
        """
        双腿净收益严格数学校验拦截器。
        
        Returns:
            (is_profitable, net_ev, reason_msg)
        """
        gross_profit, total_fee, net_ev = PricingEngine.calculate_net_ev(
            leg1_cost, leg1_size, leg2_cost, leg2_size, leg1_order_type, leg2_order_type
        )

        min_shares = min(leg1_size, leg2_size)
        if min_shares <= 0:
            return False, 0.0, "持仓份数必须大于 0"

        # 净收益率 (基于 1.0 兑付基准)
        net_margin = net_ev / min_shares
        
        if net_margin < min_profit_margin:
            return False, net_ev, f"净利润率 {net_margin:.4f} < 门槛 {min_profit_margin:.4f} (Net EV: ${net_ev:.4f})"

        return True, net_ev, f"锁利达标：Net EV ${net_ev:.4f} (Margin: {net_margin:.2%})"

    @staticmethod
    def calculate_obi(
        bids: List[Any],
        asks: List[Any],
        top_n_levels: int = 5,
        min_total_shares: float = 30.0
    ) -> Tuple[float, float, bool]:
        """
        计算前 N 档订单簿深度不平衡度 (Orderbook Imbalance)。
        
        Formula:
            OBI = (Total Bid Size - Total Ask Size) / (Total Bid Size + Total Ask Size)
            
        Returns:
            (obi_value, total_depth_shares, is_valid_depth)
            - obi_value: 范围 [-1.0, 1.0]。>0 表示买盘强，<0 表示卖盘压迫
            - total_depth_shares: 前 N 档买卖总份额
            - is_valid_depth: 总份额是否达到有效性门槛 (>= min_total_shares)
        """
        if not bids and not asks:
            return 0.0, 0.0, False

        def _extract_sizes(levels: List[Any], n: int) -> float:
            tot = 0.0
            for item in levels[:n]:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    tot += float(item[1])
                elif isinstance(item, dict):
                    tot += float(item.get("size", 0.0))
            return tot

        bid_vol = _extract_sizes(bids, top_n_levels)
        ask_vol = _extract_sizes(asks, top_n_levels)
        total_vol = bid_vol + ask_vol

        if total_vol <= 0:
            return 0.0, 0.0, False

        obi = round((bid_vol - ask_vol) / total_vol, 4)
        is_valid = total_vol >= min_total_shares
        return obi, round(total_vol, 2), is_valid

    @staticmethod
    def calculate_dual_bracket_prices(
        best_bid_yes: float,
        best_bid_no: float,
        entry_max_price: float = 0.50,
        entry_min_price: float = 0.30,
        min_profit_margin: float = 0.015,
        best_ask_yes: Optional[float] = None,
        best_ask_no: Optional[float] = None,
        anti_penny_step: float = 0.001
    ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """
        计算 Maker-Maker 双挂做市定价（对称贴盘 + Anti-Pennying 防穿透做市机制）。
        1. 基于动态阶梯 step 贴盘做市；
        2. 严格施加 safe_price <= best_ask - 0.001 防穿透保护，绝不意外成为 Taker 吃单；
        3. 校验双边总成本 (yes_bid + no_bid) <= 1.0 - min_profit_margin 保证纯利；
        4. 溢价防爆盾：两边挂单均不得高出买一 0.010 以上。
        
        Returns:
            (yes_bid_price, no_bid_price, filter_reason)
        """
        if best_bid_yes <= 0 or best_bid_no <= 0:
            return None, None, "盘口买一价格无效 (<=0)"

        step = max(anti_penny_step, 0.001)

        # 尝试双边贴买一 + step，并施加卖一防穿透保护
        raw_yes = best_bid_yes + step
        if best_ask_yes is not None and best_ask_yes > best_bid_yes:
            target_yes = round(min(raw_yes, best_ask_yes - 0.001), 4)
        else:
            target_yes = round(raw_yes, 4)

        raw_no = best_bid_no + step
        if best_ask_no is not None and best_ask_no > best_bid_no:
            target_no = round(min(raw_no, best_ask_no - 0.001), 4)
        else:
            target_no = round(raw_no, 4)

        # 校验双边直接贴盘的总成本
        total_cost = round(target_yes + target_no, 4)
        max_cost_allowed = round(1.0 - min_profit_margin, 4)

        if total_cost <= max_cost_allowed:
            # 盘口利差充裕：双边均以防穿透阶梯贴盘挂单，最大化提升两腿被吃概率并锁定超额利润
            yes_bid_price = target_yes
            no_bid_price = target_no
        else:
            # 盘口利差较薄：以不超过 entry_max_price 的一侧为锚点，另一侧按保利推算
            if best_bid_yes <= best_bid_no:
                yes_bid_price = round(min(target_yes, entry_max_price), 4)
                no_bid_price = round(1.0 - yes_bid_price - min_profit_margin, 4)
            else:
                no_bid_price = round(min(target_no, entry_max_price), 4)
                yes_bid_price = round(1.0 - no_bid_price - min_profit_margin, 4)

        if yes_bid_price < entry_min_price or no_bid_price < entry_min_price:
            return None, None, f"双挂价格偏斜: YES={yes_bid_price:.4f}, NO={no_bid_price:.4f} < {entry_min_price}"

        # 溢价防爆盾：两边挂单均不得高出当前买一 0.010 以上（防止市价高位被动接盘）
        if yes_bid_price > (best_bid_yes + 0.010):
            return None, None, f"YES 侧溢价过高 ({yes_bid_price:.4f} > 买一 {best_bid_yes:.4f} + 0.01)"
        if no_bid_price > (best_bid_no + 0.010):
            return None, None, f"NO 侧溢价过高 ({no_bid_price:.4f} > 买一 {best_bid_no:.4f} + 0.01)"

        return yes_bid_price, no_bid_price, None

    @staticmethod
    def calculate_adaptive_flip_duration(
        base_duration: float = 35.0,
        asset_amplitude: float = 0.0,
        max_amplitude_threshold: float = 0.30
    ) -> float:
        """
        基于实时 K 线振幅动态计算本轮自适应做 T 让价周期 (秒)。
        - 超平稳横盘期 (ratio <= 0.35): 适度延长至 45s~50s 赚取更厚利差；
        - 微波动加剧期 (ratio >= 0.70): 压缩至 18s~25s 加速幂律降价脱手保本；
        - 标准震荡期: 保持 base_duration。
        """
        if max_amplitude_threshold <= 0 or asset_amplitude <= 0:
            return round(base_duration, 1)

        ratio = asset_amplitude / max_amplitude_threshold
        if ratio <= 0.35:
            scale = 1.0 + ((0.35 - ratio) / 0.35) * 0.428  # 1.0 -> 1.428 (35s -> 50s)
            return round(min(base_duration * scale, 50.0), 1)
        elif ratio >= 0.70:
            compression = min(max((ratio - 0.70) / 0.30, 0.0), 1.0)
            return round(base_duration - (compression * (base_duration - 18.0)), 1)
        else:
            return round(base_duration, 1)

    @staticmethod
    def calculate_decayed_margin(
        elapsed_seconds: float,
        initial_margin: float = 0.025,
        min_margin: float = 0.002,
        decay_duration: float = 30.0,
        convex_power: float = 1.8
    ) -> float:
        """
        基于幂律连续平滑衰减计算当前时刻的动态目标利润率 (Power-Law Convex Decayed Margin)。
        
        Formula:
            ratio = (elapsed / decay_duration) ^ convex_power
            current_margin = initial_margin - ratio * (initial_margin - min_margin)
            
        特性：
        - 前期 (t < 50% T) 斜率平缓，充分获取最大做 T / 锁利利润；
        - 后期 (t >= 50% T) 凸性加速度连续平滑让价，快速促成二腿成交，杜绝超时打损。
        """
        if decay_duration <= 0 or elapsed_seconds <= 0:
            return initial_margin
        decay_ratio = min(max(elapsed_seconds / decay_duration, 0.0), 1.0)
        convex_ratio = decay_ratio ** convex_power
        current_margin = initial_margin - (convex_ratio * (initial_margin - min_margin))
        return round(max(current_margin, min_margin), 4)

    @staticmethod
    def calculate_flip_sell_price(
        leg1_cost: float,
        elapsed_seconds: float = 0.0,
        initial_margin: float = 0.025,
        min_margin: float = 0.002,
        decay_duration: float = 30.0,
        leg1_is_taker: bool = True
    ) -> float:
        """
        计算二腿同向做 T 高抛限价卖单价格 (Maker Sell Flip Price)。
        公式: Sell Price = Leg1 Cost + Taker Fee + Decayed Margin
        """
        margin = PricingEngine.calculate_decayed_margin(elapsed_seconds, initial_margin, min_margin, decay_duration)
        fee_rate = TAKER_FEE_RATE if leg1_is_taker else MAKER_FEE_RATE
        leg1_fee = leg1_cost * fee_rate
        target_sell_price = leg1_cost + leg1_fee + margin
        return round(min(max(target_sell_price, 0.001), 0.999), 4)

    @staticmethod
    def calculate_hedged_pair_price(
        leg1_cost: float,
        elapsed_seconds: float = 0.0,
        initial_margin: float = 0.025,
        min_margin: float = 0.002,
        decay_duration: float = 30.0,
        leg1_is_taker: bool = True,
        leg2_is_taker: bool = False
    ) -> float:
        """
        计算二腿反向配对限价买单价格 (Pair Hedging Buy Price)。
        公式: Pair Price = 1.0 - Leg1 Cost - Total Fees - Decayed Margin
        """
        margin = PricingEngine.calculate_decayed_margin(elapsed_seconds, initial_margin, min_margin, decay_duration)
        fee1 = leg1_cost * (TAKER_FEE_RATE if leg1_is_taker else MAKER_FEE_RATE)
        # 预估二腿费率
        fee2_rate = TAKER_FEE_RATE if leg2_is_taker else MAKER_FEE_RATE
        # 预估二腿成本基准
        approx_leg2_cost = max(0.0, 1.0 - leg1_cost)
        total_fees = fee1 + (approx_leg2_cost * fee2_rate)
        
        target_pair_price = 1.0 - leg1_cost - total_fees - margin
        return round(min(max(target_pair_price, 0.001), 0.999), 4)

    @staticmethod
    def evaluate_taker_ev_opportunity(
        best_ask_yes: float,
        best_bid_yes: Optional[float],
        best_ask_no: float,
        best_bid_no: Optional[float],
        entry_max_price: float = 0.50,
        entry_min_price: float = 0.30,
        min_profit_margin: float = 0.010,
        leg1_amount: float = 10.0
    ) -> Tuple[bool, Optional[str], Optional[float], Optional[float], str]:
        """
        全盘口净 EV 驱动的 Taker 开首腿套利机会评估 (纯无状态数学函数)。
        
        核算流程：
        1. 路径 A (吃 YES + 挂 NO 对冲)：
           - 首腿 Taker 吃 YES @ best_ask_yes；
           - 二腿 Maker 挂 NO @ min(best_bid_no + 0.001, 1.0 - best_ask_yes - fees - margin)；
           - 严格计算 Net EV 与净利润率。
        2. 路径 B (吃 NO + 挂 YES 对冲)：
           - 首腿 Taker 吃 NO @ best_ask_no；
           - 二腿 Maker 挂 YES @ min(best_bid_yes + 0.001, 1.0 - best_ask_no - fees - margin)；
           - 严格计算 Net EV 与净利润率。
        3. 单边超跌保底分支 (min_ask <= entry_max_price)。
        
        Returns:
            (is_opportunity, target_side, entry_price, expected_net_ev, reason_msg)
        """
        best_opp: Tuple[bool, Optional[str], Optional[float], Optional[float], str] = (False, None, None, None, "未发现达标利差")
        max_net_margin = -999.0

        # --- 路径 A: 吃 YES ---
        if best_ask_yes is not None and best_ask_yes >= entry_min_price and best_ask_yes <= 0.95:
            # 必须要求对侧买一有效且 >= 0.25，保证对冲流动性真实存在
            if best_bid_no is not None and best_bid_no >= 0.25:
                no_hedge_p = round(max(min(best_bid_no, 1.0 - best_ask_yes - 0.005), 0.01), 4)
                gross_ev, fee, net_ev = PricingEngine.calculate_net_ev(
                    leg1_cost=best_ask_yes, leg1_size=leg1_amount,
                    leg2_cost=no_hedge_p, leg2_size=leg1_amount,
                    leg1_order_type="FOK", leg2_order_type="GTC"
                )
                margin = net_ev / leg1_amount if leg1_amount > 0 else 0.0
                
                if margin >= min_profit_margin and margin > max_net_margin:
                    max_net_margin = margin
                    best_opp = (True, "YES", best_ask_yes, net_ev, f"YES侧EV达标: Net EV=${net_ev:.4f} (Margin: {margin:.2%}, 吃YES@{best_ask_yes:.4f} 挂NO@{no_hedge_p:.4f})")

        # --- 路径 B: 吃 NO ---
        if best_ask_no is not None and best_ask_no >= entry_min_price and best_ask_no <= 0.95:
            # 必须要求对侧买一有效且 >= 0.25
            if best_bid_yes is not None and best_bid_yes >= 0.25:
                yes_hedge_p = round(max(min(best_bid_yes, 1.0 - best_ask_no - 0.005), 0.01), 4)
                gross_ev, fee, net_ev = PricingEngine.calculate_net_ev(
                    leg1_cost=best_ask_no, leg1_size=leg1_amount,
                    leg2_cost=yes_hedge_p, leg2_size=leg1_amount,
                    leg1_order_type="FOK", leg2_order_type="GTC"
                )
                margin = net_ev / leg1_amount if leg1_amount > 0 else 0.0
                
                if margin >= min_profit_margin and margin > max_net_margin:
                    max_net_margin = margin
                    best_opp = (True, "NO", best_ask_no, net_ev, f"NO侧EV达标: Net EV=${net_ev:.4f} (Margin: {margin:.2%}, 吃NO@{best_ask_no:.4f} 挂YES@{yes_hedge_p:.4f})")

        # --- 保底分支: 单边超跌/极度超跌 ---
        if not best_opp[0]:
            min_ask, min_side, opp_bid = (
                (best_ask_yes, "YES", best_bid_no)
                if (best_ask_yes is not None and (best_ask_no is None or best_ask_yes <= best_ask_no))
                else (best_ask_no, "NO", best_bid_yes)
            )
            # 1. 极度超跌 (0.02 <= min_ask <= 0.25 且对侧买一 >= 0.15 具备做 T 空间)
            if min_ask is not None and 0.02 <= min_ask <= 0.25:
                if opp_bid is not None and opp_bid >= 0.15:
                    best_opp = (True, min_side, min_ask, 0.0, f"[极度超跌做T达标] 吃{min_side}@{min_ask:.4f} <= 0.25 (对侧买一 {opp_bid:.4f} >= 0.15)")
            # 2. 单边常规超跌 (min_ask <= entry_max_price 且对侧买一 >= 0.25 具备真实对手盘)
            elif min_ask is not None and min_ask <= entry_max_price and min_ask >= entry_min_price:
                if opp_bid is not None and opp_bid >= 0.25:
                    best_opp = (True, min_side, min_ask, 0.0, f"单边超跌达标: 吃{min_side}@{min_ask:.4f} <= 门槛{entry_max_price:.4f} (对侧买一 {opp_bid:.4f} >= 0.25)")

        return best_opp
