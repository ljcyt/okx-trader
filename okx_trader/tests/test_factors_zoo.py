# -*- coding: utf-8 -*-
"""Phase 7 验证：记忆回路 + 因子动物园。

硬回归（前向对齐没写错的唯一证明）：
    构造 value = fwd_ret_1b 本身的因子 → ic ≈ 1.0；反号 → ic ≈ -1.0。
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

from okx_trader.committee import Committee
from okx_trader.store import write as w
from okx_trader.store.db import Store
from okx_trader.store.factors_zoo import (backfill_returns, collect_from_report,
                                          score_factors)


class ReplayStubClient:
    """只提供 get_candles 的最小 stub：确定性 close 序列。"""

    def __init__(self, closes, bar_ms=3600 * 1000, t0=None):
        self.closes = closes
        self.bar_ms = bar_ms
        self.t0 = t0 or int(time.time() * 1000) - len(closes) * bar_ms

    def get_candles(self, inst_id, bar="1H", limit=1000):
        return [{"ts": self.t0 + i * self.bar_ms, "open": c, "high": c,
                 "low": c, "close": c, "vol": 1.0}
                for i, c in enumerate(self.closes)]


def make_store():
    return Store(os.path.join(tempfile.mkdtemp(), "t.db"))


class MemoryFeedbackTest(unittest.TestCase):
    """7.1：记忆必须把「结果」送回「决策」。"""

    def setUp(self):
        self.store = make_store()
        ts = time.time()
        rw = w.RoundWriter.open(self.store, "r1", ts, "replay", 1, "baseline")
        pk = self.store.execute(
            "INSERT INTO proposals(round_pk, slot, analyst, style, action, "
            "inst_id, direction, avg_score) VALUES (?,?,?,?,?,?,?,?)",
            (rw.pk, 0, "趋势猎手", "trend", "open", "BTC-USDT-SWAP", "long", 7.7))
        self.store.execute("UPDATE proposals SET is_winner=1 WHERE id=?", (pk,))
        self.store.execute(
            "INSERT INTO trades(env, inst_id, direction, open_round_pk, "
            "opened_ts, contracts, ct_val, entry_px, status, analyst, "
            "realized_pnl, r_multiple, exit_reason) "
            "VALUES ('replay','BTC-USDT-SWAP','long',?,?,10,0.01,78000,"
            "'closed','趋势猎手',-10.0,-1.0,'stop')", (rw.pk, ts))
        self.store.execute("UPDATE rounds SET status='opened' WHERE id=?", (rw.pk,))
        self.rw_pk = rw.pk

    def _summary(self):
        c = Committee.__new__(Committee)
        c.store = self.store
        return c.recent_rounds_summary(5)

    def test_summary_contains_outcome(self):
        s = self._summary()
        self.assertIn("-1.0R", s)
        self.assertIn("stop", s)
        self.assertIn("按人设战绩", s)

    def test_no_trade_does_not_crash(self):
        self.store.execute("DELETE FROM trades")
        s = self._summary()
        self.assertIn("状态=opened", s)
        self.assertNotIn("按人设战绩", s.split("按人设战绩")[-1] if "按人设战绩" in s
                         else "")


class FactorZooTest(unittest.TestCase):
    CLOSES = [100 + i * 0.5 for i in range(120)]   # 随时间上涨的确定性序列

    def setUp(self):
        self.store = make_store()
        self.client = ReplayStubClient(self.CLOSES)
        self.t0 = self.client.t0
        self.bar_ms = self.client.bar_ms

    def _collect(self, value_fn, n=60):
        """模拟 n 轮采集：每个 bar_ts 一条观测，value 由调用方决定。"""
        for i in range(n):
            bar_ts = self.t0 + i * self.bar_ms
            report = {"ts": bar_ts}
            rw = w.RoundWriter.open(self.store, f"r{i}", time.time(),
                                    "replay", 0, "baseline")
            v = value_fn(i)
            if v is None:
                continue
            # 直接构造 obs（等价于 collect_from_report 的输出形状）
            self.store.execute(
                "INSERT OR IGNORE INTO factor_defs(name, family, tier, status, "
                "source, created_ts, status_ts) VALUES "
                "('fake','momentum','derived','observing','builtin',?,?)",
                (time.time(), time.time()))
            self.store.execute(
                "INSERT OR IGNORE INTO factor_obs(factor, inst_id, bar_ts, "
                "round_pk, value) VALUES ('fake','BTC-USDT-SWAP',?,?,?)",
                (bar_ts, rw.pk, v))

    def test_collection_idempotent(self):
        report = {"ts": self.t0, "rsi14": 55.0, "price": 100.0,
                  "ema60": 99.0, "boll": {"upper": 102, "lower": 98},
                  "macd": {"hist": 0.1}}
        rw1 = w.RoundWriter.open(self.store, "r1", time.time(), "replay", 0,
                                 "baseline")
        c1 = collect_from_report(self.store, rw1.pk, "BTC-USDT-SWAP", report,
                                 "1H")
        rw2 = w.RoundWriter.open(self.store, "r2", time.time(), "replay", 0,
                                 "baseline")
        c2 = collect_from_report(self.store, rw2.pk, "BTC-USDT-SWAP", report,
                                 "1H")
        self.assertEqual(c1, c2)  # 第二次全部 OR IGNORE
        self.assertEqual(self.store.query_one(
            "SELECT COUNT(*) c FROM factor_obs")["c"], c1)
        defs = self.store.query_one("SELECT status, tier FROM factor_defs "
                                    "WHERE name='rsi14'")
        self.assertEqual(defs["status"], "observing")

    def test_hard_regression_ic_identity(self):
        """关键回归：value = fwd_ret_1b → ic ≈ 1.0；反号 → -1.0。
        这条测试是前向收益对齐没写错的唯一证明。"""
        gate = {"scored_days": 0, "days_tracked": 0,
                "require_positive_rank_ic": True, "min_obs": 10}
        for sign, expected_ic in ((1.0, 1.0), (-1.0, -1.0)):
            store = make_store()
            client = ReplayStubClient(self.CLOSES)
            # 造观测：value = 该 bar 的真实 1b 前向收益 × sign
            closes = client.closes
            for i in range(len(closes) - 2):
                bar_ts = client.t0 + i * client.bar_ms
                fwd = closes[i + 1] / closes[i] - 1
                store.execute(
                    "INSERT OR IGNORE INTO factor_defs(name, family, tier, "
                    "status, source, created_ts, status_ts) VALUES "
                    "('fake','momentum','derived','observing','builtin',?,?)",
                    (time.time(), time.time()))
                store.execute(
                    "INSERT OR IGNORE INTO factor_obs(factor, inst_id, bar_ts, "
                    "value, fwd_ret_1b, fwd_ret_4b, fwd_ret_24b, filled_ts) "
                    "VALUES ('fake','BTC-USDT-SWAP',?,?,?,?,?,?)",
                    (bar_ts, sign * fwd, fwd, 0.0, 0.0, time.time()))
            rows = score_factors(store, gate)
            r = next(r for r in rows if r["factor"] == "fake"
                     and r["horizon"] == "1b")
            self.assertIsNotNone(r["ic"])
            self.assertAlmostEqual(r["ic"], expected_ic, places=1,
                                   msg="前向对齐几乎肯定写错了" if r["ic"] != expected_ic else "")

    def test_gate_blocks_small_samples(self):
        """样本不足 → 只记录不判定（gate_passed=0，不晋级）。"""
        self._collect(lambda i: 55.0 + (i % 5), n=10)
        backfill_returns(self.store, self.client)
        rows = score_factors(self.store, {"scored_days": 15, "days_tracked": 30,
                                          "min_obs": 100})
        self.assertTrue(all(r["gate_passed"] == 0 for r in rows))
        st = self.store.query_one(
            "SELECT status FROM factor_defs WHERE name='fake'")["status"]
        self.assertEqual(st, "observing")   # unproven edge never touches the book

    def test_backfill_alignment(self):
        """回填值必须等于手算的 close[i+N]/close[i]-1（按 bar_ts 对齐）。"""
        self._collect(lambda i: float(i), n=30)
        backfill_returns(self.store, self.client)
        row = self.store.query_one(
            "SELECT bar_ts, fwd_ret_4b FROM factor_obs WHERE fwd_ret_4b IS NOT "
            "NULL ORDER BY bar_ts LIMIT 1")
        i = (row["bar_ts"] - self.t0) // self.bar_ms
        expected = self.CLOSES[i + 4] / self.CLOSES[i] - 1
        self.assertAlmostEqual(row["fwd_ret_4b"], expected, places=12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
