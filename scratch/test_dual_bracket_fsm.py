import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from polymarket.fsm import TradeFSM, TradeState
from polymarket.strategy_fsm import ArbitrageBotFSM

class TestDualBracketFSM(unittest.TestCase):

    def test_fsm_dual_bracket_transitions(self):
        fsm = TradeFSM("market_test_1", initial_state=TradeState.IDLE)
        
        # 1. IDLE -> PENDING_BOTH_LEGS
        self.assertTrue(fsm.transition_to(TradeState.PENDING_BOTH_LEGS))
        self.assertEqual(fsm.current_state, TradeState.PENDING_BOTH_LEGS)
        
        # 2. PENDING_BOTH_LEGS -> LOCKED (双腿同时成交)
        self.assertTrue(fsm.transition_to(TradeState.LOCKED))
        self.assertEqual(fsm.current_state, TradeState.LOCKED)
        
        # 3. LOCKED -> SETTLED
        self.assertTrue(fsm.transition_to(TradeState.SETTLED))
        self.assertEqual(fsm.current_state, TradeState.SETTLED)

    def test_fsm_dual_bracket_single_fill_to_leg1_only(self):
        fsm = TradeFSM("market_test_2", initial_state=TradeState.IDLE)
        
        # IDLE -> PENDING_BOTH_LEGS
        self.assertTrue(fsm.transition_to(TradeState.PENDING_BOTH_LEGS))
        
        # 单边先成交 -> LEG1_ONLY
        self.assertTrue(fsm.transition_to(TradeState.LEG1_ONLY))
        self.assertEqual(fsm.current_state, TradeState.LEG1_ONLY)
        
        # 二腿成交 -> LOCKED
        self.assertTrue(fsm.transition_to(TradeState.LOCKED))
        self.assertEqual(fsm.current_state, TradeState.LOCKED)

    def test_dual_bracket_pricing_margin(self):
        yes_price = 0.380
        margin = 0.015
        no_price = round(1.0 - yes_price - margin, 4)
        self.assertEqual(no_price, 0.605)
        self.assertAlmostEqual(yes_price + no_price, 0.985, places=4)

if __name__ == "__main__":
    unittest.main()
