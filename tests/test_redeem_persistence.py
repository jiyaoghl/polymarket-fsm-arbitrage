import os
import tempfile
import pytest

from polymarket.db import (
    init_db,
    mark_market_redeemed,
    get_all_redeemed_market_ids,
    is_market_redeemed
)


@pytest.fixture
def temp_db():
    """创建临时数据库文件用于测试隔离"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_mark_and_query_redeemed(temp_db):
    """测试标记市场已赎回并可查询"""
    assert not is_market_redeemed("m_test_1", path=temp_db)
    
    mark_market_redeemed("m_test_1", tx_hash="0xabc123", amount=15.5, path=temp_db)
    
    assert is_market_redeemed("m_test_1", path=temp_db)
    assert not is_market_redeemed("m_test_2", path=temp_db)


def test_preload_redeemed_market_ids(temp_db):
    """测试启动预热加载已赎回市场集合 (防重启后重复 RPC 探测)"""
    # 模拟在历史运行中结算了 3 个市场
    mark_market_redeemed("m_hist_1", tx_hash="0x111", amount=10.0, path=temp_db)
    mark_market_redeemed("m_hist_2", tx_hash="0x222", amount=20.0, path=temp_db)
    mark_market_redeemed("m_hist_3", tx_hash="0x333", amount=30.0, path=temp_db)

    # 模拟进程重启：全新读取数据库已赎回列表
    preloaded = get_all_redeemed_market_ids(path=temp_db)
    
    assert len(preloaded) == 3
    assert "m_hist_1" in preloaded
    assert "m_hist_2" in preloaded
    assert "m_hist_3" in preloaded
    assert "m_unseen" not in preloaded
