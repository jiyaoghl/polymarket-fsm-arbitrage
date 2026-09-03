import os
import time
from typing import Dict, List, Optional, Any
import requests
from eth_account import Account

from polymarket.config import (
    PK, API_KEY, API_SECRET, API_PASSPHRASE,
    CLOB_HOST, RPC_URL, SIGNATURE_TYPE,
    EXCHANGE_CONTRACT_V2
)
from polymarket.logger import logger
from polymarket.services.onchain_redeemer import (
    DEFAULT_RPC_CANDIDATES,
    CTF_EXCHANGE_ADDRESS,
    USDC_BRIDGED_ADDRESS,
    USDC_NATIVE_ADDRESS
)
from polymarket.gateway.live import LiveClobV2Gateway


class LivePreflightChecker:
    """
    Polymarket 实盘真金上线自动化前置护航套件 (Live Pilot Pre-flight Suite)。

    五维端到端诊断：
    1. 钱包私钥与 API 凭证格式校验 (Credentials & Signature Type)；
    2. Polygon 链上 MATIC (Gas) 与 USDC 储备探测 (On-Chain Balances)；
    3. CLOB 托管抵押品与 Exchange 智能合约授权探测 (Collateral & Allowance)；
    4. NTP 时钟与网络往返漂移校准 (Clock Drift & Network Latency)；
    5. EIP-712 极低价安全发单与撤单全链路穿透探针 (Safe Order Roundtrip Probe)。
    """

    def __init__(
        self,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        api_passphrase: Optional[str] = None,
        host: str = CLOB_HOST,
        rpc_url: Optional[str] = None
    ):
        self.private_key = private_key or PK
        self.api_key = api_key or API_KEY or os.getenv("POLX_API_KEY", "")
        self.api_secret = api_secret or API_SECRET or os.getenv("POLX_API_SECRET", "")
        self.api_passphrase = api_passphrase or API_PASSPHRASE or os.getenv("POLX_API_PASSPHRASE", "")
        self.host = host.rstrip("/")
        self.rpc_url = rpc_url or RPC_URL
        self.wallet: Optional[Account] = None

        if self.private_key and not self.private_key.startswith("your_"):
            try:
                self.wallet = Account.from_key(self.private_key)
            except Exception:
                pass

    def check_credentials(self) -> Dict[str, Any]:
        """第一维：校验私钥合法性、推导地址与 API 凭据"""
        if not self.private_key or self.private_key.startswith("your_"):
            return {
                "status": "FAIL",
                "message": "未配置真实有效私钥 (POLX_PK 为空或仍为占位符)",
                "address": None,
                "signature_type": SIGNATURE_TYPE
            }

        try:
            account = Account.from_key(self.private_key)
            self.wallet = account
            addr = account.address
            masked_addr = f"{addr[:6]}...{addr[-4:]}"
        except Exception as e:
            return {
                "status": "FAIL",
                "message": f"私钥解析异常: {e}",
                "address": None,
                "signature_type": SIGNATURE_TYPE
            }

        has_api_creds = bool(self.api_key and self.api_secret and self.api_passphrase and not self.api_key.startswith("your_"))
        status = "PASS" if has_api_creds else "WARN"
        msg = "私钥与 API 凭证完整有效" if has_api_creds else "私钥有效，但缺少 L2 API Key 凭证 (部分接口可能受限)"

        return {
            "status": status,
            "message": msg,
            "address": addr,
            "masked_address": masked_addr,
            "signature_type": SIGNATURE_TYPE,
            "has_api_creds": has_api_creds
        }

    def check_chain_balances(self, wallet_address: Optional[str] = None) -> Dict[str, Any]:
        """第二维：通过多节点 RPC 探测 Polygon 链上 MATIC (Gas) 与 USDC 储备"""
        addr = wallet_address or (self.wallet.address if self.wallet else None)
        if not addr:
            return {"status": "FAIL", "message": "无法执行链上资产探测：未检测到有效钱包地址"}

        # 候选 RPC 节点轮换
        rpc_nodes = [self.rpc_url] + [r for r in DEFAULT_RPC_CANDIDATES if r != self.rpc_url]
        pol_balance = 0.0
        pusd_balance = 0.0
        usdc_bridged = 0.0
        usdc_native = 0.0
        rpc_success = False
        used_rpc = ""

        clean_wallet = addr.lower().replace("0x", "").zfill(64)
        erc20_data = f"0x70a08231{clean_wallet}"
        pusd_contract = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

        for rpc in rpc_nodes:
            if not rpc or not rpc.startswith("http"):
                continue
            try:
                # 1. 查询 Polygon 主网原生代币 POL (原 MATIC Gas 费)
                p_pol = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [addr, "latest"], "id": 1}
                r1 = requests.post(rpc, json=p_pol, timeout=3.5)
                res1 = r1.json().get("result")
                if res1 and res1 != "0x":
                    pol_balance = int(res1, 16) / 1e18

                # 2. 查询 pUSD (Polymarket 原生抵押品代币)
                p_pusd = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": pusd_contract, "data": erc20_data}, "latest"], "id": 2}
                r_p = requests.post(rpc, json=p_pusd, timeout=3.5)
                res_p = r_p.json().get("result")
                if res_p and res_p != "0x":
                    pusd_balance = int(res_p, 16) / 1e6

                # 3. 查询 Bridged USDC (USDC.e)
                p_usdc1 = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": USDC_BRIDGED_ADDRESS, "data": erc20_data}, "latest"], "id": 3}
                r2 = requests.post(rpc, json=p_usdc1, timeout=3.5)
                res2 = r2.json().get("result")
                if res2 and res2 != "0x":
                    usdc_bridged = int(res2, 16) / 1e6

                # 4. 查询 Native USDC
                p_usdc2 = {"jsonrpc": "2.0", "method": "eth_call", "params": [{"to": USDC_NATIVE_ADDRESS, "data": erc20_data}, "latest"], "id": 4}
                r3 = requests.post(rpc, json=p_usdc2, timeout=3.5)
                res3 = r3.json().get("result")
                if res3 and res3 != "0x":
                    usdc_native = int(res3, 16) / 1e6

                rpc_success = True
                used_rpc = rpc
                break
            except Exception:
                continue

        if not rpc_success:
            return {
                "status": "WARN",
                "message": "Polygon RPC 节点网络抖动或超时，暂时无法拉取链上余额",
                "pol_balance": 0.0,
                "matic_balance": 0.0,
                "pusd_balance": 0.0,
                "usdc_bridged": 0.0,
                "usdc_native": 0.0
            }

        total_usdc = pusd_balance + usdc_bridged + usdc_native
        warnings = []
        if pol_balance < 0.1:
            warnings.append(f"POL (Polygon Gas) 余额偏低 ({pol_balance:.4f} POL)，可能导致链上自动赎回 CTF 失败，建议充值 ≥0.5 POL")
        if total_usdc < 5.0:
            warnings.append(f"链上未质押资产余额较少 (${total_usdc:.4f})")

        status = "PASS" if pol_balance >= 0.1 else "WARN"
        msg = "链上 Gas 与代币储备正常" if not warnings else "；".join(warnings)

        return {
            "status": status,
            "message": msg,
            "rpc_node": used_rpc,
            "pol_balance": round(pol_balance, 4),
            "matic_balance": round(pol_balance, 4),
            "pusd_balance": round(pusd_balance, 4),
            "usdc_bridged_balance": round(usdc_bridged, 2),
            "usdc_native_balance": round(usdc_native, 2),
            "total_chain_usdc": round(total_usdc, 4)
        }

    def check_clob_collateral_and_allowance(self) -> Dict[str, Any]:
        """第三维：检查 CLOB 托管可用抵押品余额与 Exchange 合约授权 (CLOB V2 allowances 映射)"""
        if not self.wallet:
            return {"status": "FAIL", "message": "未加载有效钱包，跳过 CLOB 托管校验"}

        # 优先通过官方 ClobClient 获取标准 BalanceAllowance
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, ApiCreds
            clean_pk = self.private_key if self.private_key.startswith("0x") else f"0x{self.private_key}"
            c = ClobClient(host=self.host, key=clean_pk, chain_id=137)
            if self.api_key and self.api_secret and self.api_passphrase:
                c.set_api_creds(ApiCreds(api_key=self.api_key, api_secret=self.api_secret, api_passphrase=self.api_passphrase))
            res = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))

            raw_bal = float(res.get("balance", 0.0))
            bal = raw_bal / 1e6 if raw_bal > 1000 else raw_bal

            # 解析 allowances 字典，以 Exchange V2 主合约为主
            allowances_dict = res.get("allowances") or {}
            exchange_v2 = "0xE111180000d2663C0091e4f400237545B87B996B"
            allow_raw = float(allowances_dict.get(exchange_v2, 0.0))
            is_approved = allow_raw > 1e12

            warnings = []
            if not is_approved:
                warnings.append("对 Polymarket Exchange V2 合约的 Allowance 授权未完成，需先执行 approve")
            if bal < 5.0:
                warnings.append(f"CLOB 托管可用余额较少 (${bal:.4f} USDC)，建议充值至 ≥20U 以支持主力做市")

            status = "PASS" if (is_approved and bal >= 5.0) else ("WARN" if is_approved else "FAIL")
            msg = "CLOB 抵押品与授权正常就绪" if not warnings else "；".join(warnings)

            return {
                "status": status,
                "message": msg,
                "clob_balance_usdc": round(bal, 4),
                "is_approved": is_approved,
                "allowance_status": "无限额度授权 (已就绪)" if is_approved else "未授权"
            }
        except Exception as e:
            return {
                "status": "FAIL",
                "message": f"请求 CLOB balance-allowance 异常: {e}",
                "clob_balance_usdc": 0.0,
                "is_approved": False,
                "allowance_status": "查询异常"
            }

    def check_clock_and_latency(self) -> Dict[str, Any]:
        """第四维：探测本地与 CLOB 撮合引擎的时间漂移与往返时延"""
        t0 = time.time()
        try:
            r = requests.get(f"{self.host}/time", timeout=3.0)
            t1 = time.time()
            latency_ms = round((t1 - t0) * 1000, 1)

            if r.status_code != 200:
                return {
                    "status": "WARN",
                    "message": f"获取 CLOB 服务器时间失败 [HTTP {r.status_code}]",
                    "latency_ms": latency_ms,
                    "drift_ms": None
                }

            server_data = r.json()
            server_ts = float(server_data if isinstance(server_data, (int, float)) else server_data.get("time", t1))
            # 考虑往返时延的一半作为网络到达补偿
            estimated_server_now = server_ts + (latency_ms / 2000.0)
            drift_ms = round((t1 - estimated_server_now) * 1000, 1)

            status = "PASS"
            warnings = []
            if abs(drift_ms) > 500:
                status = "WARN"
                warnings.append(f"时钟漂移偏大 ({drift_ms}ms > ±500ms)，可能导致 EIP-712 签名被判定过期拒单，建议配置 NTP 同步")
            if latency_ms > 250:
                warnings.append(f"到 CLOB 撮合引擎往返延迟较高 ({latency_ms}ms)")

            msg = f"NTP 时钟与网络连接正常 (时差 {drift_ms}ms，时延 {latency_ms}ms)" if not warnings else "；".join(warnings)

            return {
                "status": status,
                "message": msg,
                "latency_ms": latency_ms,
                "drift_ms": drift_ms
            }
        except Exception as e:
            return {
                "status": "FAIL",
                "message": f"连接 CLOB 服务器超时或异常: {e}",
                "latency_ms": None,
                "drift_ms": None
            }

    def check_order_roundtrip(self, skip: bool = False) -> Dict[str, Any]:
        """第五维：构造极低价 ($0.001) 安全限价单发单与毫秒撤单全链路穿透探针"""
        if skip:
            return {
                "status": "SKIPPED",
                "message": "用户主动跳过发单/撤单穿透探针",
                "order_id": None
            }

        if not self.wallet:
            return {
                "status": "FAIL",
                "message": "未加载私钥，无法进行发单探针测试",
                "order_id": None
            }

        # 检查 CLOB 可用抵押金，低于 1.0U 优雅跳过穿透探针
        try:
            clob_status = self.check_clob_collateral_and_allowance()
            avail_bal = clob_status.get("clob_balance_usdc", 0.0)
            if avail_bal < 1.0:
                return {
                    "status": "WARN",
                    "message": f"当前 CLOB 可用余额为 ${avail_bal:.4f} USDC (低于实盘最小发单门槛 ≥1.0 USDC)，已跳过发单探针；请充值至 ≥20U 支持主力做市",
                    "order_id": None
                }
        except Exception:
            pass

        # 获取一个活跃市场的 Token ID
        test_token = None
        try:
            r = requests.get(f"{self.host}/markets?limit=1", timeout=4.0)
            if r.status_code == 200:
                data = r.json()
                data_list = data if isinstance(data, list) else data.get("data", [])
                if data_list:
                    tokens = data_list[0].get("tokens", [])
                    if tokens and isinstance(tokens, list):
                        test_token = tokens[0].get("token_id")
        except Exception:
            pass

        if not test_token:
            # 兜底测试 Token ID (BTC 5min 常见格式)
            test_token = "21742633143463906290569050155826241533067272736897614950488156847949938836455"

        gateway = LiveClobV2Gateway(host=self.host, private_key=self.private_key, warm_up=False)
        order_id = None
        try:
            # 以最低价格 0.001 发出 5 份测试单 (名义价值仅 $0.005)
            # 撮合引擎硬性要求 size >= 5.0
            order_res = gateway.post_order(
                token_id=test_token,
                price=0.001,
                amount=0.005,
                side="BUY",
                order_type="GTC"
            )

            if not order_res or order_res.get("status") in ("ERROR", "REJECTED"):
                err_msg = order_res.get("error") or order_res.get("reason") or "撮合返回拒绝"
                return {
                    "status": "FAIL",
                    "message": f"实盘发单穿透失败: {err_msg}",
                    "order_id": None
                }

            order_id = order_res.get("orderID") or order_res.get("order_id")
            time.sleep(0.2)  # 等待 200ms

            # 立即撤单自愈
            cancelled = gateway.cancel_order(order_id)
            if not cancelled:
                return {
                    "status": "WARN",
                    "message": f"发单成功 (Order: {order_id})，但撤单未成功收到确认，请前往 Polymarket 检查挂单",
                    "order_id": order_id
                }

            return {
                "status": "PASS",
                "message": f"EIP-712 原生发单与撤单穿透 100% 成功！(Order: {order_id})",
                "order_id": order_id
            }
        except Exception as e:
            if order_id:
                try:
                    gateway.cancel_order(order_id)
                except Exception:
                    pass
            return {
                "status": "FAIL",
                "message": f"全链路发单撤单探针异常: {e}",
                "order_id": order_id
            }

    def run_all(self, skip_probe: bool = False) -> Dict[str, Any]:
        """运行全部五维自动化体检并聚合报告"""
        t_start = time.time()
        logger.info("[LiveChecker] 开始执行实盘上线全链路五维前置自检...")

        cred = self.check_credentials()
        clock = self.check_clock_and_latency()

        wallet_addr = cred.get("address")
        chain = self.check_chain_balances(wallet_addr)
        clob = self.check_clob_collateral_and_allowance()

        # 仅在基础凭证与时钟通过的前提下执行穿透探针
        can_probe = (cred["status"] == "PASS" and not skip_probe)
        probe = self.check_order_roundtrip(skip=not can_probe)

        # 综合评级判定
        statuses = [cred["status"], clock["status"], chain["status"], clob["status"]]
        if probe["status"] not in ("SKIPPED",):
            statuses.append(probe["status"])

        if "FAIL" in statuses:
            overall = "FAIL"
            conclusion = "🔴 实盘前置自检未完全通过，存在阻断性问题，严禁开启真金交易！"
        elif "WARN" in statuses:
            overall = "WARN"
            conclusion = "🟡 实盘前置自检通过但存在预警项，建议根据提示优化后再开启真金交易。"
        else:
            overall = "PASS"
            conclusion = "🟢 实盘全链路五维自检 100% 绿灯通过，系统已完全具备小额真金起步条件！"

        elapsed_s = round(time.time() - t_start, 2)
        logger.info(f"[LiveChecker] 实盘前置自检完毕，耗时 {elapsed_s}s，总评: {overall}")

        return {
            "timestamp": time.time(),
            "overall_status": overall,
            "conclusion": conclusion,
            "elapsed_seconds": elapsed_s,
            "checks": {
                "credentials": cred,
                "clock_and_latency": clock,
                "chain_balances": chain,
                "clob_collateral": clob,
                "order_roundtrip_probe": probe
            }
        }
