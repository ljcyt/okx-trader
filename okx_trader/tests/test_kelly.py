# -*- coding: utf-8 -*-
"""kelly.py 单元测试（纯逻辑，不触网）。

覆盖：分数 Kelly 数学、显著性门槛、样本不足中性、负 edge 地板、影子模式。
"""
import os
import sys
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.kelly import estimate


def make_cfg(**kw):
    base = {"KELLY_MIN_SAMPLES": 30, "KELLY_FRACTION": 0.5,
            "KELLY_MIN_MULT": 0.25, "KELLY_SIG_LEVEL": 0.05}
    base.update(kw)
    return type("C", (), base)()


def rows_from(win_rate, n, win_r=1.5, loss_r=-1.0):
    """构造 n 条 r_multiple 行：前 win_rate×n 条赢，其余亏。"""
    nw = round(n * win_rate)
    return [{"r_multiple": win_r if i < nw else loss_r} for i in range(n)]


class KellyEstimateTest(unittest.TestCase):
    def test_insufficient_sample_neutral(self):
        # 样本不足 → 中性 1.0（不惩罚无证据的策略）
        out = estimate(rows_from(0.5, 10), make_cfg())
        self.assertEqual(out["mult"], 1.0)
        self.assertEqual(out["n"], 10)
        self.assertIn("中性", out["note"])

    def test_significant_positive_edge_scales(self):
        # 30 笔 70% 胜率、盈亏比 1.5 → f* = 0.7-0.3/1.5 = 0.5 → ×0.5 = 0.25 → floor
        out = estimate(rows_from(0.7, 30), make_cfg())
        self.assertTrue(out["significant"])
        self.assertAlmostEqual(out["p"], 0.7, places=6)
        self.assertAlmostEqual(out["b"], 1.5, places=6)
        self.assertAlmostEqual(out["f_star"], 0.5, places=6)
        self.assertGreaterEqual(out["mult"], 0.25)
        self.assertLessEqual(out["mult"], 1.0)

    def test_strong_edge_uses_half_kelly(self):
        # 30 笔 80% 胜率、盈亏比 2.0 → f* = 0.8-0.2/2.0 = 0.7 → ×0.5 = 0.35
        out = estimate(rows_from(0.8, 30, win_r=2.0, loss_r=-1.0), make_cfg())
        self.assertTrue(out["significant"])
        self.assertAlmostEqual(out["mult"], 0.35, places=3)

    def test_significant_negative_edge_floors(self):
        # 30 笔 20% 胜率 → f* < 0 显著 → 地板 0.25
        out = estimate(rows_from(0.2, 30), make_cfg())
        self.assertTrue(out["significant"])
        self.assertEqual(out["mult"], 0.25)

    def test_not_significant_stays_neutral(self):
        # 50% 胜率 → 无优势（不显著）→ 中性 1.0
        out = estimate(rows_from(0.5, 40), make_cfg())
        self.assertFalse(out["significant"])
        self.assertEqual(out["mult"], 1.0)

    def test_clamped_between_floor_and_one(self):
        # 极端正 edge（全胜）→ 盈亏比不可估 → 中性（不冒进到 >1）
        out = estimate(rows_from(1.0, 40), make_cfg())
        self.assertEqual(out["mult"], 1.0)
        # 极端负 edge → 地板
        out = estimate(rows_from(0.0, 40), make_cfg())
        self.assertEqual(out["mult"], 0.25)

    def test_all_win_unestimable_b(self):
        out = estimate(rows_from(1.0, 35), make_cfg())
        self.assertEqual(out["mult"], 1.0)
        self.assertIn("不可估", out["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
