# -*- coding: utf-8 -*-
"""kelly.py v2 单元测试（t 统计量驱动，纯逻辑不触网）。

覆盖：样本不足中性、正/负 edge 分档、零胜地板、显著性检验方向正确。
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
    base = {"KELLY_MIN_SAMPLES": 30,
            "KELLY_MIN_MULT": 0.25, "KELLY_SIG_LEVEL": 0.05}
    base.update(kw)
    return type("C", (), base)()


def rows_r(win_rate, n, win_r=1.0, loss_r=-1.0):
    """n 条 r_multiple：win_rate 比例赢（+win_r），其余亏（loss_r）。"""
    nw = round(n * win_rate)
    return [{"r_multiple": win_r if i < nw else loss_r} for i in range(n)]


class KellyV2Test(unittest.TestCase):
    def test_insufficient_sample_neutral(self):
        out = estimate(rows_r(0.5, 10), make_cfg())
        self.assertEqual(out["mult"], 1.0)
        self.assertIn("中性", out["note"])

    def test_strong_positive_edge_full_budget(self):
        # 70% 胜率、每笔 +1R → mean_R=+0.7，t 很大 → 全额预算
        out = estimate(rows_r(0.7, 40), make_cfg())
        self.assertTrue(out["significant"])
        self.assertEqual(out["mult"], 1.0)
        self.assertGreater(out["t"], 2)

    def test_significant_negative_edge_floors(self):
        # 30% 胜率、每笔 −1R → mean_R=−0.4，t 显著为负 → 地板 0.25
        out = estimate(rows_r(0.3, 40), make_cfg())
        self.assertTrue(out["significant"])
        self.assertEqual(out["mult"], 0.25)

    def test_mild_negative_without_significance_shrinks(self):
        # 45% 胜率、+1R/−1R → mean_R=−0.1，t≈−0.63（不显著负）→ 轻度收缩 0.75
        out = estimate(rows_r(0.45, 40), make_cfg())
        self.assertFalse(out["significant"])
        self.assertEqual(out["mult"], 0.75)

    def test_zero_mean_neutral(self):
        # 50% 胜率 ±1R → mean_R=0 → 中性 1.0
        out = estimate(rows_r(0.5, 40), make_cfg())
        self.assertEqual(out["mult"], 1.0)

    def test_zero_win_sample_floors(self):
        out = estimate(rows_r(0.0, 35), make_cfg())
        self.assertEqual(out["mult"], 0.25)
        self.assertTrue(out["significant"])

    def test_p_value_two_sided(self):
        # P1 修复回归：p=0.5、b=0.5（平均赚 0.5R 亏 1R）→ 明显在亏钱，
        # 旧胜率 z 检验会判"不显著→满仓"；v2 的 t 检验必须判负
        rows = [{"r_multiple": 0.5 if i % 2 == 0 else -1.0} for i in range(40)]
        out = estimate(rows, make_cfg())
        self.assertAlmostEqual(out["mean_r"], -0.25, places=6)
        self.assertLess(out["t"], -2)
        self.assertEqual(out["mult"], 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
