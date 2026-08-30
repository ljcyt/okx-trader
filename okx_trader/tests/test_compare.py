# -*- coding: utf-8 -*-
"""compare.py：LLM vs 基线影子盘的决策分叉四象限。

用两个临时库构造已知分叉，验证：桶对齐、四象限计数、
被过滤机会的前向收益计算与结论方向。
"""
import os
import sys
import tempfile
import time
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.store.compare import compare
from okx_trader.store.db import Store


def _mk_db(tmp, name):
    return Store(os.path.join(tmp, name))


def _round(store, ts, final_action, env="demo"):
    pk = store.execute(
        "INSERT INTO rounds(round_id, ts, env, executing, llm_mode, status, "
        "final_action) VALUES (?,?,?,?,?,?,?)",
        (f"r{ts}", ts, env, 0, "baseline", "no_action", final_action))
    return pk


def _winner_proposal(store, round_pk, inst, direction, analyst):
    p = store.execute(
        "INSERT INTO rounds(round_id, ts, env, executing, llm_mode, status) "
        "VALUES ('x', 0, 'demo', 0, 'llm', 'no_action')")
    store.execute("DELETE FROM rounds WHERE id=?", (p,))
    pk = store.execute(
        "INSERT INTO proposals(round_pk, slot, analyst, style, action, "
        "inst_id, direction, avg_score, is_winner) "
        "VALUES (?,?,?,?,?,?,?,1,1)",
        (round_pk, 0, analyst, "trend", "open", inst, direction))
    return pk


def _price_point(store, ts, inst, px):
    pk = store.execute(
        "INSERT INTO rounds(round_id, ts, env, executing, llm_mode, status) "
        "VALUES (?,?, 'demo', 0, 'llm', 'no_action')", (f"f{ts}", ts))
    store.execute(
        "INSERT INTO round_factors(round_pk, inst_id, ok, price, report_json) "
        "VALUES (?,?,1,?,?)", (pk, inst, px, "{}"))


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="okxtcmp-")
        self.main_db = _mk_db(self.tmp, "main.db")
        self.shadow_db = _mk_db(self.tmp, "shadow.db")
        self.now = time.time()

    def _seed(self):
        """构造 4 个决策点：双开 / LLM独有 / LLM过滤 / 双弃。

        被过滤的那格价格从 100 → 106（+6%），应判"过滤在损失期望"。"""
        h = int(self.now) // 3600 * 3600
        insts = [
            ("BTC-USDT-SWAP", "open", "open"),    # both_open
            ("ETH-USDT-SWAP", "open", "hold"),     # llm_only（基线也跑了但弃权）
            ("SOL-USDT-SWAP", "hold", "open"),    # ★ LLM 过滤掉了
            ("DOGE-USDT-SWAP", "hold", "hold"),   # both_hold
        ]
        for i, (inst, llm_a, base_a) in enumerate(insts):
            ts = h + i * 3600
            # 每轮每标的都写一行因子（生产形状：评估过就留痕），
            # hold 轮的决策点骨架靠它
            def _with_factors(db, ts, inst, action):
                rpk = _round(db, ts, "place" if action == "open" else None,
                             env="demo" if db is self.main_db else "paper")
                if action == "open":
                    _winner_proposal(db, rpk, inst, "long", "趋势猎手")
                db.execute(
                    "INSERT INTO round_factors(round_pk, inst_id, ok, price, "
                    "report_json) VALUES (?,?,1,100,'{}')", (rpk, inst))
            _with_factors(self.main_db, ts, inst, llm_a)
            _with_factors(self.shadow_db, ts, inst, base_a)
        # 被过滤格（SOL）的价格轨迹：100 → 106
        fts = h + 2 * 3600
        _price_point(self.main_db, fts, "SOL-USDT-SWAP", 100.0)
        _price_point(self.main_db, fts + 24 * 3600, "SOL-USDT-SWAP", 106.0)

    def test_quadrant_counts_and_filtered_return(self):
        self._seed()
        out = compare(self.main_db.path if hasattr(self.main_db, "path")
                      else os.path.join(self.tmp, "main.db"),
                      os.path.join(self.tmp, "shadow.db"),
                      env="demo", shadow_env="paper", horizon_h=24)
        st = out["stats"]
        q = st["quadrant"]
        self.assertEqual(q["both_open"], 1)
        self.assertEqual(q["llm_only"], 1)
        self.assertEqual(q["filtered"], 1)
        self.assertEqual(q["both_hold"], 1)
        self.assertEqual(st["filtered_with_return"], 1)
        self.assertAlmostEqual(st["filtered_mean_fwd_return"], 0.06, places=6)
        self.assertIn("损失期望", st["verdict"])

    def test_no_overlap_returns_empty(self):
        _round(self.main_db, self.now, "place")
        out = compare(os.path.join(self.tmp, "main.db"),
                      os.path.join(self.tmp, "shadow.db"))
        self.assertEqual(out["stats"]["aligned_decision_points"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
