import unittest
import time
from polymarket.metrics.engine import MetricsEngine
from polymarket.apps.dashboard import get_diagnostics

class TestMetricsAndDiagnostics(unittest.TestCase):
    """测试时序指标引擎增强与 /api/diagnostics 转化率计算"""

    def test_metrics_engine_new_instruments(self):
        """测试新增的未对冲时长与出场指标正常工作"""
        engine = MetricsEngine.get_instance()
        
        # 1. 记录未对冲时长
        engine.unhedged_duration_seconds.observe(12.5)
        engine.unhedged_duration_seconds.observe(45.0)
        
        # 2. 增加计数
        engine.dual_exit_sells_total.inc()
        engine.expiry_resolved_total.inc()

        exported = engine.export_dashboard_json()
        self.assertIn("poly_unhedged_duration_seconds", exported.get("histograms", {}))
        self.assertIn("poly_dual_exit_sells_total", exported.get("counters", {}))
        self.assertIn("poly_expiry_resolved_total", exported.get("counters", {}))

    def test_diagnostics_conversion_summary_structure(self):
        """测试 /api/diagnostics 返回包含合法的 conversion_summary 结构"""
        diag = get_diagnostics()
        self.assertIn("conversion_summary", diag)
        summary = diag["conversion_summary"]
        
        self.assertIn("total_trades", summary)
        self.assertIn("locked_count", summary)
        self.assertIn("locked_rate_pct", summary)
        self.assertIn("dual_exit_sells_count", summary)
        self.assertIn("dual_exit_win_rate_pct", summary)
        self.assertIn("force_close_count", summary)
        self.assertIn("total_gross_pnl", summary)
        self.assertIn("total_fees_usdc", summary)
        self.assertIn("total_net_pnl", summary)
        self.assertIn("by_strategy", summary)

if __name__ == "__main__":
    unittest.main()
