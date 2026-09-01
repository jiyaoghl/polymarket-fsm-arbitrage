import gzip
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from polymarket.services.l2_recorder import L2SnapshotRecorder
from polymarket.services.grid import OrderbookSnapshot


@pytest.fixture
def temp_snapshot_dir():
    temp_dir = tempfile.mkdtemp(prefix="test_snapshots_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_l2_recorder_singleton():
    """测试 L2SnapshotRecorder 单例模式。"""
    r1 = L2SnapshotRecorder.get_instance()
    r2 = L2SnapshotRecorder.get_instance()
    assert r1 is r2


def test_l2_recorder_file_cleanup(temp_snapshot_dir, monkeypatch):
    """测试历史快照自动清理逻辑。"""
    monkeypatch.setattr("polymarket.services.l2_recorder.SNAPSHOT_DIR", temp_snapshot_dir)
    monkeypatch.setattr("polymarket.services.l2_recorder.SNAPSHOT_RETENTION_DAYS", 7)

    old_time = datetime.now() - timedelta(days=10)
    recent_time = datetime.now() - timedelta(days=3)

    old_file = Path(temp_snapshot_dir) / f"{old_time.strftime('%Y-%m-%d_%H')}.jsonl.gz"
    recent_file = Path(temp_snapshot_dir) / f"{recent_time.strftime('%Y-%m-%d_%H')}.jsonl.gz"

    with gzip.open(old_file, "wt", encoding="utf-8") as f:
        f.write('{"test": "old"}\n')

    with gzip.open(recent_file, "wt", encoding="utf-8") as f:
        f.write('{"test": "recent"}\n')

    assert old_file.exists()
    assert recent_file.exists()

    recorder = L2SnapshotRecorder.get_instance()
    recorder._cleanup_old_files()

    assert not old_file.exists()
    assert recent_file.exists()


def test_snapshot_serialization_format():
    """测试快照记录字段的完整性与合法性。"""
    snap = OrderbookSnapshot(
        token_id="0xtoken123",
        best_bid=0.42,
        best_ask=0.58,
        bids=((0.42, 10.0), (0.41, 20.0)),
        asks=((0.58, 15.0), (0.59, 25.0)),
        spread=0.16,
        mid_price=0.50,
        obi=0.12,
        last_update_ts=time.time()
    )

    record = {
        "ts": round(snap.last_update_ts, 3),
        "token_id": snap.token_id,
        "best_bid": snap.best_bid,
        "best_ask": snap.best_ask,
        "bids": list(snap.bids[:10]),
        "asks": list(snap.asks[:10]),
        "spread": snap.spread,
        "mid_price": snap.mid_price,
        "obi": snap.obi,
    }

    serialized = json.dumps(record, ensure_ascii=False)
    deserialized = json.loads(serialized)

    assert deserialized["token_id"] == "0xtoken123"
    assert deserialized["best_bid"] == 0.42
    assert deserialized["best_ask"] == 0.58
    assert len(deserialized["bids"]) == 2
    assert len(deserialized["asks"]) == 2
    assert deserialized["spread"] == 0.16
    assert deserialized["mid_price"] == 0.50
    assert deserialized["obi"] == 0.12
