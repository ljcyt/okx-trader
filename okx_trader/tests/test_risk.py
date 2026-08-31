# -*- coding: utf-8 -*-
"""risk.py 单元测试（纯逻辑，用 Stub 客户端，不触网、不需要 API Key）

运行：python -m unittest okx_trader.tests.test_risk -v   或   python okx_trader/tests/test_risk.py
（unittest 用例同样可被 pytest 收集执行）
"""
import os
import sys
import tempfile
import unittest

try:  # GBK 控制台兜底；包在 try 里以便 pytest 收集时不炸
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.store.db import Store


# ── Stub：替代 OKXDemoClient 的最小接口 ─────────────────────────────
class StubClient:
    def __init__(self, equity=10000.0, positions=None, atr=400.0, pending=None):
        import logging
        self.log = logging.getLogger("test")
        self.log.addHandler(logging.NullHandler())
        self._equity = equity
        self._positions = positions or []
        self._atr = atr
        self._pending = pending or []
        self.compute_atr_calls = 0

    def get_equity(self):
        return {"total_eq": self._equity, "usdt_eq": self._equity,
                "usdt_avail": self._equity}

    def get_positions(self):
        return list(self._positions)

    def get_pending_orders(self, inst_id=""):
        return list(self._pending)  # R4 挂单查重用

    def get_instrument(self, inst_id):
        return {"instId": inst_id, "ctVal": 0.01, "lotSz": 0.01, "minSz": 0.01,
                "tickSz": 0.1, "settleCcy": "USDT"}

    def get_ticker(self, inst_id):
        return {"instId": inst_id, "last": 80000.0, "ask": 80000.5, "bid": 79999.5}

    def compute_atr(self, inst_id, period=14, bar="1H"):
        self.compute_atr_calls += 1
        return self._atr

    def round_size(self, sz, lot_sz, min_sz):
        from okx_trader.client import OKXClient
        return OKXClient.round_size(sz, lot_sz, min_sz)


class StubState:
    def __init__(self, hwm=0.0):
        self._hwm = hwm

    def get_hwm(self):
        return self._hwm

    def update_hwm(self, equity):
        self._hwm = max(self._hwm, equity)
        dd = (self._hwm - equity) / self._hwm if self._hwm else 0
        return self._hwm, dd


def make_cfg(**kw):
    base = {
        "MAX_RISK_PER_TRADE": 0.01, "MAX_TOTAL_LEVERAGE": 3.0,
        "MAX_OPEN_POSITIONS": 3, "MAX_DRAWDOWN": 0.10,
        "ATR_PERIOD": 14, "ATR_BAR": "1H", "ATR_STOP_MULT": 1.5,
        "MIN_STOP_DIST_PCT": 0.002, "MAX_STOP_DIST_PCT": 0.05,
        "MIN_RR": 1.5, "MIN_TARGET_ATR": 0.5, "TARGET_ATR_MULT": 2.5,
        # stub state 无 store：R8 fail-closed 的测试豁免（生产默认 False，
        # 真 store 路径见 SameDirectionRiskTest / R8FailClosedTest）
        "RISK_ALLOW_NO_STORE": True,
    }
    base.update(kw)
    return type("C", (), base)()


def make_plan(**kw):
    plan = {
        "instId": "BTC-USDT-SWAP",
        "direction": "long",
        "stop_loss": 79200.0,          # 距入场 80000 约 1% 的止损
        "order_type": "limit_maker",
        "reason": "测试计划",
    }
    plan.update(kw)
    return plan


class RiskRulesTest(unittest.TestCase):
    def setUp(self):
        from okx_trader.risk import RiskManager
        self.RiskManager = RiskManager
        self.rm = RiskManager(make_cfg(), StubClient(), StubState())

    def test_r3_normal_plan(self):
        # 新 R7 规则下，无结构位时 ATR 兜底目标 1000U 需要止损距离 < 667U，
        # 所以基准计划用 1×ATR（400U）止损
        v = self.rm.check_open_plan(make_plan(stop_loss=79600.0))
        self.assertTrue(v.passed, f"failures={v.failures}")
        # 有效止损距离 = max(400, 1.5*400=600) = 600 → 预算 100U → 16.66 张
        self.assertAlmostEqual(v.sized["contracts"], 16.66, places=2)
        self.assertLessEqual(v.sized["risk_pct"], 0.01001)
        self.assertLessEqual(v.sized["leverage_after"], 3.0)

    def test_r1_no_stop_rejected(self):
        v = self.rm.check_open_plan(make_plan(stop_loss=None))
        self.assertFalse(v.passed)
        self.assertTrue(any("止损" in f for f in v.failures))

    def test_r1_stop_wrong_side_rejected(self):
        v = self.rm.check_open_plan(make_plan(stop_loss=81000.0))
        self.assertFalse(v.passed)
        self.assertTrue(any("低于" in f for f in v.failures))

    def test_r1_live_price_beyond_stop_rejected(self):
        # 快照与执行之间行情快速移动：现价已触及计划止损 → 计划过期，拒绝
        # （入场参考 80100、止损 80050，但现价 80000 已低于止损）
        v = self.rm.check_open_plan(make_plan(entry_hint=80100.0, stop_loss=80050.0))
        self.assertFalse(v.passed)
        self.assertTrue(any("现价" in f for f in v.failures))

    def test_r2_stop_too_far_rejected(self):
        v = self.rm.check_open_plan(make_plan(stop_loss=71000.0))
        self.assertFalse(v.passed)
        self.assertTrue(any("过远" in f for f in v.failures))

    def test_r2_stop_too_close_uses_atr_floor(self):
        # 止损 0.05% 过近 → 有效距离按 ATR 下限 600 处理：仓位 100/(600*0.01)=16.66 张，
        # 实际止损在 40 距离处，实际风险远小于预算（保守方向正确）
        v = self.rm.check_open_plan(make_plan(stop_loss=79960.0))
        self.assertTrue(v.passed)
        self.assertAlmostEqual(v.sized["contracts"], 16.66, places=2)
        self.assertLessEqual(v.sized["risk_pct"], 0.01001)

    def test_r6_non_maker_rejected(self):
        v = self.rm.check_open_plan(make_plan(order_type="market"))
        self.assertFalse(v.passed)
        self.assertTrue(any("Maker" in f for f in v.failures))

    def test_r4_same_inst_position_rejected(self):
        rm = self.RiskManager(make_cfg(), StubClient(positions=[{
            "instId": "BTC-USDT-SWAP", "direction": "long", "contracts": 1.0,
            "avg_px": 79000.0, "mark_px": 80000.0, "upl": 10.0,
        }]), StubState())
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("已有持仓" in f for f in v.failures))

    def test_r4_pending_entry_order_rejected(self):
        # 挂着的入场单也算暴露：已有未成交挂单 → 不重复提交
        rm = self.RiskManager(make_cfg(), StubClient(pending=[
            {"instId": "BTC-USDT-SWAP", "ordId": "123", "side": "buy"}]), StubState())
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("入场挂单" in f for f in v.failures))

    def test_r4_max_positions_rejected(self):
        pos3 = [{"instId": f"{c}-USDT-SWAP", "direction": "long", "contracts": 1.0,
                 "avg_px": 1000.0, "mark_px": 1000.0, "upl": 0.0}
                for c in ("ETH", "SOL", "XRP")]
        rm = self.RiskManager(make_cfg(), StubClient(positions=pos3), StubState())
        v = rm.check_open_plan(make_plan(instId="DOGE-USDT-SWAP"))
        self.assertFalse(v.passed)
        self.assertTrue(any("上限" in f for f in v.failures))

    def test_r4_total_leverage_rejected(self):
        # stub ctVal=0.01：2900 张 × 0.01 × 1000 = 29000 = 2.9x 权益
        rm = self.RiskManager(make_cfg(), StubClient(equity=10000.0, positions=[{
            "instId": "ETH-USDT-SWAP", "direction": "long", "contracts": 2900.0,
            "avg_px": 1000.0, "mark_px": 1000.0, "upl": 0.0,
        }]), StubState())
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("总杠杆" in f for f in v.failures))

    def test_r5_drawdown_circuit_breaker(self):
        rm = self.RiskManager(make_cfg(), StubClient(equity=8800.0), StubState(hwm=10000.0))
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("熔断" in f for f in v.failures))

    def test_r3_short_direction(self):
        v = self.rm.check_open_plan(make_plan(direction="short", stop_loss=80400.0))
        self.assertTrue(v.passed, f"failures={v.failures}")


class R7TargetSelectionTest(unittest.TestCase):
    """R7 目标选择：MIN_TARGET_ATR 过滤贴脸位 → 第一个够本的位 → ATR 兜底。"""

    def setUp(self):
        from okx_trader.risk import RiskManager
        self.rm = RiskManager(make_cfg(), StubClient(), StubState())

    def test_real_record_case_passes_with_far_structure(self):
        # 生产记录回归（round_20260828_232427_001）：入场贴着 2515.75 阻力，
        # 旧逻辑取最近位 RR=0.31 否决；新逻辑过滤贴脸位、跳过不够本的 2538.835，
        # 选中 2590.62 → RR≈2.54 通过
        plan = make_plan(
            instId="ETH-USDT-SWAP", entry_hint=2505.38, stop_loss=2471.7961,
            factors={"atr": 25.30, "sr": {"supports": [2482.7, 2470.33, 2462.52],
                                          "resistances": [2515.75, 2538.835, 2590.62]}},
        )
        v = self.rm.check_open_plan(plan)
        self.assertTrue(v.passed, f"failures={v.failures}")
        self.assertAlmostEqual(v.sized["target"], 2590.62, places=2)
        self.assertAlmostEqual(v.sized["rr"], 2.54, places=2)
        self.assertEqual(v.sized["target_source"], "structure")

    def test_atr_single_source(self):
        # ATR 单一来源：risk 必须用 plan.factors.atr（25.30），不再自己拉（400.0）
        plan = make_plan(
            instId="ETH-USDT-SWAP", entry_hint=2505.38, stop_loss=2471.7961,
            factors={"atr": 25.30, "sr": {"supports": [], "resistances": [2590.62]}},
        )
        v = self.rm.check_open_plan(plan)
        self.assertTrue(v.passed)
        self.assertAlmostEqual(v.sized["atr"], 25.30, places=6)
        self.assertEqual(self.rm.client.compute_atr_calls, 0)

    def test_no_structure_falls_back_to_atr_multiple(self):
        # 无结构位 → ATR 兜底目标（2.5×400=1000 空间），RR=1000/400=2.5 通过
        v = self.rm.check_open_plan(make_plan(
            stop_loss=79600.0,
            factors={"atr": 400.0, "sr": {"supports": [], "resistances": []}}))
        self.assertTrue(v.passed, f"failures={v.failures}")
        self.assertEqual(v.sized["target_source"], "atr_multiple")
        self.assertAlmostEqual(v.sized["target"], 81000.0, places=6)

    def test_all_levels_too_close_falls_back(self):
        # 唯一结构位贴脸（150 < 0.5×400=200）被过滤 → ATR 兜底，不能让近位否决交易
        v = self.rm.check_open_plan(make_plan(
            stop_loss=79600.0,
            factors={"atr": 400.0, "sr": {"supports": [], "resistances": [80150.0]}}))
        self.assertTrue(v.passed)
        self.assertEqual(v.sized["target_source"], "atr_multiple")

    def test_near_level_no_longer_vetoes_far_target(self):
        # 近位 RR 不够也不否决：第一个够本的位是 81000（跳过贴脸的 80050）
        v = self.rm.check_open_plan(make_plan(
            stop_loss=79600.0,
            factors={"atr": 400.0,
                     "sr": {"supports": [], "resistances": [80050.0, 81000.0]}}))
        self.assertTrue(v.passed)
        self.assertAlmostEqual(v.sized["target"], 81000.0, places=6)
        self.assertEqual(v.sized["target_source"], "structure")

    def test_wide_stop_still_rejected_by_atr_fallback(self):
        # ATR 兜底也要过 RR：止损距离 1000 > 2.5×400/1.5=667 → RR=1000/1000=1.0 拒绝
        v = self.rm.check_open_plan(make_plan(
            stop_loss=79000.0,
            factors={"atr": 400.0, "sr": {"supports": [], "resistances": [81000.0]}}))
        self.assertFalse(v.passed)
        self.assertTrue(any("盈亏比" in f for f in v.failures))


class SameDirectionRiskTest(unittest.TestCase):
    """R8：高相关标的同向持仓的聚合风险约束。"""

    def _rm(self, store, trades_rows):
        from okx_trader.loop import RunState
        from okx_trader.risk import RiskManager
        for t in trades_rows:
            store.execute(
                "INSERT INTO trades(env, inst_id, direction, opened_ts, "
                "contracts, ct_val, entry_px, status, analyst, risk_usdt, "
                "realized_pnl) VALUES ('demo',?,?,?,?,?,?,?,?,?,NULL)",
                (t[0], t[1], 1000000.0, 0.01, 0.01, 78000.0, "open",
                 "测试", t[2]))
        return RiskManager(make_cfg(), StubClient(), RunState(store, "demo"))

    def test_same_dir_budget_exhausted_rejected(self):
        from okx_trader.risk import RiskManager
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        rm = self._rm(store, [("BTC-USDT-SWAP", "long", 150.0),
                              ("ETH-USDT-SWAP", "long", 150.0)])  # 已用 300U = 3%
        v = rm.check_open_plan(make_plan())   # 新增 1% → 2.6% > 2% 上限
        self.assertFalse(v.passed)
        self.assertTrue(any("R8" in f for f in v.failures))

    def test_same_dir_budget_shrinks(self):
        from okx_trader.risk import RiskManager
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        rm = self._rm(store, [("BTC-USDT-SWAP", "long", 40.0)])  # 已用 40U = 0.4%
        v = rm.check_open_plan(make_plan(stop_loss=79600.0))  # 预算压缩至 1.5%
        self.assertTrue(v.passed)
        self.assertLessEqual(v.sized["risk_usdt"] / 10000.0, 0.015 + 1e-9)

    def test_opposite_direction_not_affected(self):
        from okx_trader.risk import RiskManager
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        rm = self._rm(store, [("BTC-USDT-SWAP", "long", 0.009)])
        v = rm.check_open_plan(make_plan(direction="short", stop_loss=80400.0))
        self.assertTrue(v.passed, f"反向持仓不应占用同向预算: {v.failures}")


class R8FailClosedTest(unittest.TestCase):
    """P0-1：R8 缺数据一律 fail-closed——同向风险不可知时拒绝，不再折算 0。"""

    def _rm(self, store):
        from okx_trader.loop import RunState
        from okx_trader.risk import RiskManager
        return RiskManager(make_cfg(), StubClient(), RunState(store, "demo"))

    def test_null_risk_open_trade_rejects(self):
        """存在 status='open' 且 risk_usdt IS NULL 的同向仓 → 拒绝。"""
        from okx_trader.risk import RiskManager
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        store.execute(
            "INSERT INTO trades(env, inst_id, direction, opened_ts, contracts, "
            "ct_val, entry_px, status, analyst, risk_usdt) VALUES "
            "('demo','BTC-USDT-SWAP','long',1000.0,0.01,0.01,78000.0,'open','t',NULL)")
        rm = self._rm(store)
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("R8" in f and "fail-closed" in f
                            for f in v.failures), v.failures)
        ev = store.query("SELECT * FROM app_events WHERE kind='rule_fail_closed'")
        self.assertEqual(len(ev), 1)

    def test_full_risk_open_trades_aggregate_normally(self):
        """risk_usdt 齐全且已用 1.5% → 本笔预算压缩到剩余 0.5%（原行为不变）。"""
        from okx_trader.risk import RiskManager
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        store.execute(
            "INSERT INTO trades(env, inst_id, direction, opened_ts, contracts, "
            "ct_val, entry_px, status, analyst, risk_usdt) VALUES "
            "('demo','BTC-USDT-SWAP','long',1000.0,0.01,0.01,78000.0,'open','t',150.0)")
        rm = self._rm(store)
        v = rm.check_open_plan(make_plan(stop_loss=79600.0))
        self.assertTrue(v.passed, v.failures)
        # 1.5% 已用 → 预算压至 0.5% = 50U
        self.assertLessEqual(v.sized["risk_usdt"] / 10000.0, 0.005 + 1e-9)

    def test_store_query_error_rejects(self):
        """store 查询抛异常 → 拒绝。"""
        from okx_trader.risk import RiskManager
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        rm = self._rm(store)
        rm._same_dir_open_risk = lambda d: (None, False)   # 模拟查询失败
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("R8" in f for f in v.failures))

    def test_allow_no_store_passes(self):
        """RISK_ALLOW_NO_STORE=True + 无 store → 放行（测试 stub 路径）。"""
        from okx_trader.risk import RiskManager
        rm = RiskManager(make_cfg(), StubClient(), StubState())
        v = rm.check_open_plan(make_plan(stop_loss=79600.0))
        self.assertTrue(v.passed, v.failures)

    def test_no_store_default_rejects(self):
        """默认配置 + 无 store → 拒绝（生产 fail-closed 默认）。"""
        from okx_trader.risk import RiskManager
        rm = RiskManager(make_cfg(RISK_ALLOW_NO_STORE=False), StubClient(),
                         StubState())
        v = rm.check_open_plan(make_plan())
        self.assertFalse(v.passed)
        self.assertTrue(any("R8" in f and "fail-closed" in f
                            for f in v.failures), v.failures)


class SelectTargetTest(unittest.TestCase):
    """R7 目标选择（select_target）——评审发现：空头下方支撑为空时，
    旧口径判"RR 无法度量"而系统性否决空头提案。ATR 兜底必须对称可用。"""

    def test_short_with_empty_supports_uses_atr_fallback(self):
        from okx_trader.risk import select_target
        cfg = make_cfg()
        # 空头：supports 为空，resistances 有值但不属于空头目标方向
        sr = {"supports": [], "resistances": [81000.0, 82000.0]}
        st = select_target(cfg, entry_ref=80000.0, stop_dist=800.0,
                           direction="short", sr=sr, atr_val=400.0)
        self.assertEqual(st["target_source"], "atr_multiple")
        # 兜底目标 = 80000 - 2.5*400 = 79000；RR = 1000/800 = 1.25
        self.assertAlmostEqual(st["target"], 79000.0, places=6)
        self.assertAlmostEqual(st["rr"], 1.25, places=6)
        self.assertIsNone(st["nearest_note"])   # 无结构位，无需近位提示

    def test_short_with_structure_uses_first_qualifying_support(self):
        from okx_trader.risk import select_target
        cfg = make_cfg()
        # 空头目标：79000（近，RR=1000/800=1.25 < 1.5 过滤）、
        # 78000（远，RR=2000/800=2.5 达标）
        sr = {"supports": [79000.0, 78000.0], "resistances": []}
        st = select_target(cfg, entry_ref=80000.0, stop_dist=800.0,
                           direction="short", sr=sr, atr_val=400.0)
        self.assertEqual(st["target_source"], "structure")
        self.assertAlmostEqual(st["target"], 78000.0, places=6)
        self.assertAlmostEqual(st["rr"], 2.5, places=6)

    def test_long_unchanged(self):
        from okx_trader.risk import select_target
        cfg = make_cfg()
        sr = {"supports": [], "resistances": [81200.0]}
        st = select_target(cfg, entry_ref=80000.0, stop_dist=800.0,
                           direction="long", sr=sr, atr_val=400.0)
        self.assertEqual(st["target_source"], "structure")
        self.assertAlmostEqual(st["target"], 81200.0, places=6)
        self.assertAlmostEqual(st["rr"], 1.5, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
