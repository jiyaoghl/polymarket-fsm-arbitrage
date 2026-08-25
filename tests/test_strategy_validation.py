import pytest
from polymarket.base_strategy import validate_strategy_config, REQUIRED_STRATEGY_KEYS

def get_valid_config():
    return {
        "strategy_id": "test_strat",
        "name": "测试策略",
        "amount": 10.0,
        "entry_max_price": 0.50,
        "entry_min_price": 0.25,
        "reentry_trigger": 0.42,
        "is_live": False,
        "leg1_order_type": "FOK",
        "leg2_order_type": "GTC",
        "leg2_price_mode": "bid",
        "exit_mode": "dual_exit",
        "initial_margin": 0.025,
        "breakeven_margin": 0.002,
        "flip_timeout_sec": 35.0,
        "leg2_cancel_before_expiry": 30,
        "leg2_fallback_to_maker": True,
    }

def test_valid_config_passes():
    """测试完整合法配置顺利通过"""
    cfg = get_valid_config()
    validate_strategy_config(cfg)  # 不应抛出异常

def test_missing_required_key_raises():
    """测试缺少必填参数时抛出明确异常"""
    for key in REQUIRED_STRATEGY_KEYS:
        cfg = get_valid_config()
        del cfg[key]
        with pytest.raises(ValueError, match="缺少以下必填参数"):
            validate_strategy_config(cfg)

def test_invalid_price_range_raises():
    """测试不合法的价格区间抛出异常"""
    cfg = get_valid_config()
    cfg["entry_max_price"] = 1.50
    with pytest.raises(ValueError, match="必须在 \\(0.0, 1.0\\) 区间内"):
        validate_strategy_config(cfg)

    cfg2 = get_valid_config()
    cfg2["entry_min_price"] = 0.60
    cfg2["entry_max_price"] = 0.50
    with pytest.raises(ValueError, match="必须大于 0 且小于等于 entry_max_price"):
        validate_strategy_config(cfg2)

def test_invalid_exit_mode_raises():
    """测试非法的 exit_mode 取值抛出异常"""
    cfg = get_valid_config()
    cfg["exit_mode"] = "invalid_mode"
    with pytest.raises(ValueError, match="必须为 'dual_exit'、'smart_flip' 或 'pair_only'"):
        validate_strategy_config(cfg)

def test_invalid_order_type_raises():
    """测试非法的订单类型抛出异常"""
    cfg = get_valid_config()
    cfg["leg1_order_type"] = "MARKET"
    with pytest.raises(ValueError, match="必须为 'FOK' 或 'GTC'"):
        validate_strategy_config(cfg)
