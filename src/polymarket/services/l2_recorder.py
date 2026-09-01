"""
L2 盘口深度快照录包守护进程 (L2 Snapshot Recorder Daemon)。

核心职责：
1. 常驻后台以 1 帧/秒 的频率从 OrderbookMemoryGrid 内存网格中零阻塞读取所有活跃 Token 的不可变快照；
2. 序列化为 JSONL 格式，按小时滚动写入 gzip 压缩文件至 data/snapshots/；
3. 启动时自动清理超过保留天数的历史文件；
4. 磁盘异常时仅降级告警，绝不阻断主交易事件循环。
"""

import gzip
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from polymarket.logger import logger
from polymarket.config import (
    SNAPSHOT_ENABLED,
    SNAPSHOT_INTERVAL_SEC,
    SNAPSHOT_RETENTION_DAYS,
    SNAPSHOT_DIR,
    SUPPORTED_ASSETS,
)


class L2SnapshotRecorder:
    """
    全局单例 L2 盘口快照录包守护线程。

    设计原则：
    - 零阻塞：仅读取 OrderbookMemoryGrid 的不可变 frozen dataclass 引用，无写锁竞争
    - 磁盘安全：所有 I/O 操作均被 try/except 严密包裹，磁盘满时仅降级日志告警
    - 自动滚动：按小时切换输出文件，文件名格式 YYYY-MM-DD_HH.jsonl.gz
    - 自动清理：启动时扫描并删除超过 SNAPSHOT_RETENTION_DAYS 的历史文件
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(L2SnapshotRecorder, cls).__new__(cls)
                cls._instance._started = False
        return cls._instance

    @classmethod
    def get_instance(cls) -> "L2SnapshotRecorder":
        return cls()

    def start(self):
        """启动录包守护线程（幂等，重复调用自动跳过）。"""
        if not SNAPSHOT_ENABLED:
            logger.info("[L2Recorder] 快照录包功能已被 SNAPSHOT_ENABLED=false 关闭，跳过启动。")
            return

        with self._lock:
            if self._started:
                return
            self._started = True

        # 确保输出目录存在
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)

        # 启动时清理过期文件
        self._cleanup_old_files()

        t = threading.Thread(target=self._run_loop, daemon=True, name="L2SnapshotRecorder")
        t.start()
        logger.info(
            f"[L2Recorder] L2 盘口快照录包守护线程已启动 "
            f"(间隔={SNAPSHOT_INTERVAL_SEC}s, 保留={SNAPSHOT_RETENTION_DAYS}天, "
            f"目录={SNAPSHOT_DIR})"
        )

    def _run_loop(self):
        """主循环：每隔 SNAPSHOT_INTERVAL_SEC 秒采集一帧全量快照。"""
        from polymarket.services.grid import OrderbookMemoryGrid
        from polymarket.kline_analyzer import get_asset_status

        current_hour_key: Optional[str] = None
        current_file = None
        frame_count = 0
        last_cleanup_ts = time.time()

        while True:
            try:
                now = time.time()
                now_dt = datetime.fromtimestamp(now)
                hour_key = now_dt.strftime("%Y-%m-%d_%H")

                # 按小时滚动切换输出文件
                if hour_key != current_hour_key:
                    if current_file is not None:
                        try:
                            current_file.close()
                            # 后台异步压缩上一个小时的 jsonl 文件
                            old_filepath = os.path.join(SNAPSHOT_DIR, f"{current_hour_key}.jsonl")
                            threading.Thread(target=self._compress_file, args=(old_filepath,), daemon=True).start()
                        except Exception as e:
                            logger.warning(f"[L2Recorder] 关闭旧文件异常: {e}")

                    filepath = os.path.join(SNAPSHOT_DIR, f"{hour_key}.jsonl")
                    current_file = open(filepath, "a", encoding="utf-8", buffering=8192)
                    current_hour_key = hour_key
                    logger.info(f"[L2Recorder] 切换输出文件: {filepath}")

                # 从内存网格零阻塞读取所有活跃 Token 快照
                grid = OrderbookMemoryGrid.get_instance()
                books = dict(grid._books)

                if not books:
                    time.sleep(SNAPSHOT_INTERVAL_SEC)
                    continue

                # 构建资产到 K 线状态的映射缓存
                kline_cache = {}
                for asset in SUPPORTED_ASSETS:
                    try:
                        st = get_asset_status(asset)
                        kline_cache[asset.upper()] = {
                            "amplitude": round(st.get("amplitude", 0.0), 4),
                            "net_change": round(st.get("net_change", 0.0), 4),
                            "is_choppy": st.get("is_choppy", True),
                        }
                    except Exception:
                        pass

                # 序列化每个 Token 的快照
                for token_id, snap in books.items():
                    if snap.is_stale(30.0):
                        continue

                    record = {
                        "ts": round(now, 3),
                        "token_id": token_id,
                        "best_bid": snap.best_bid,
                        "best_ask": snap.best_ask,
                        "bids": list(snap.bids[:10]),
                        "asks": list(snap.asks[:10]),
                        "spread": snap.spread,
                        "mid_price": snap.mid_price,
                        "obi": snap.obi,
                    }
                    try:
                        current_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    except Exception as e:
                        logger.warning(f"[L2Recorder] 写入快照失败: {e}")

                frame_count += 1

                # 每 10 秒 flush 一次
                if frame_count % max(1, int(10.0 / SNAPSHOT_INTERVAL_SEC)) == 0:
                    try:
                        current_file.flush()
                    except Exception:
                        pass

                # 每小时触发一次过期清理
                if now - last_cleanup_ts > 3600:
                    self._cleanup_old_files()
                    last_cleanup_ts = now

                time.sleep(SNAPSHOT_INTERVAL_SEC)

            except Exception as e:
                logger.warning(f"[L2Recorder] 录包循环异常 (已安全兜底): {e}")
                time.sleep(5.0)

    @staticmethod
    def _compress_file(src_path: str):
        """将已归档的 jsonl 文件压缩为 jsonl.gz 并删除源文件。"""
        if not os.path.exists(src_path):
            return
        dst_path = f"{src_path}.gz"
        try:
            with open(src_path, "rb") as f_in, gzip.open(dst_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(src_path)
            logger.info(f"[L2Recorder] 已成功压缩历史快照归档: {dst_path}")
        except Exception as e:
            logger.warning(f"[L2Recorder] 压缩归档文件 {src_path} 异常: {e}")

    def _cleanup_old_files(self):
        """清理超过保留天数的历史快照文件 (支持 .jsonl 与 .jsonl.gz)。"""
        try:
            cutoff = datetime.now() - timedelta(days=SNAPSHOT_RETENTION_DAYS)
            cutoff_str = cutoff.strftime("%Y-%m-%d_%H")
            removed = 0
            for f in Path(SNAPSHOT_DIR).glob("*.jsonl*"):
                stem = f.name.replace(".gz", "").replace(".jsonl", "")
                if stem < cutoff_str:
                    f.unlink()
                    removed += 1
            if removed > 0:
                logger.info(f"[L2Recorder] 已清理 {removed} 个过期快照文件 (保留策略: {SNAPSHOT_RETENTION_DAYS} 天)")
        except Exception as e:
            logger.warning(f"[L2Recorder] 清理过期文件异常: {e}")

