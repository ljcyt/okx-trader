# -*- coding: utf-8 -*-
"""Phase 8 验证：5 分钟机械 tick + 回撤阶梯。

    - tick 从不调用 Committee.decide；risk_ticks 计数正确；equity_curve 每 tick 一行
    - tick 与 round 撞点时 round 先跑且不重复巡检
    - 回撤阶梯 4%/7%/10% 断点：risk_mult 减半 / 禁开 / 全平+停机；降档滞回 80%
"""
import os
import sys
import tempfile
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.config import load_config
from okx_trader.env import ENVS
from okx_trader.loop import TradingLoop
from okx_trader.replay import ReplayClient
from okx_trader.store.db import Store


def make_loop(tmp, script=None, **cfg_kw):
    cfg = load_config()
    cfg.TRADING_ENV = "replay"
    cfg.ORDER_TIMEOUT_SEC = 0
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    loop = TradingLoop(cfg=cfg, env_name="replay",
                       store=Store(os.path.join(tmp, "trader.db")))
    loop.executing = True
    loop.client = ReplayClient(cfg, logger=loop.log, script=script or [])
    # 替换 client 后同步 risk/committee 持有的引用（生产路径不换 client，仅测试装配）
    loop.risk.client = loop.client
    loop.committee.client = loop.client
    return loop


class TickBehaviorTest(unittest.TestCase):
    def test_tick_never_calls_committee_and_counts(self):
        tmp = tempfile.mkdtemp(prefix="okxt8-")
        loop = make_loop(tmp)

        def _must_not_run(snapshot):
            raise AssertionError("risk tick 不允许调用 Committee.decide")

        loop.committee.decide = _must_not_run
        for _ in range(12):
            loop.risk_tick()
        self.assertEqual(int(loop.store.state_get("replay", "risk_ticks")), 12)
        # 12 次 tick 权益采样 + 无 round → equity_curve 12 行（round_pk NULL）
        n = loop.store.query_one("SELECT COUNT(*) c FROM equity_curve")["c"]
        self.assertEqual(n, 12)
        ticks_flag = loop.store.state_get("replay", "last_risk_tick_ts")
        self.assertIsNotNone(ticks_flag)

    def test_tick_plus_round_rows(self):
        tmp = tempfile.mkdtemp(prefix="okxt8-")
        loop = make_loop(tmp, script=[{"price": 78000.0}])
        for _ in range(12):
            loop.risk_tick()
        loop.run_round()   # round 也写一行 equity（round_pk 非空）
        n = loop.store.query_one("SELECT COUNT(*) c FROM equity_curve")["c"]
        self.assertEqual(n, 13)

    def test_collision_round_first_no_double_patrol(self):
        tmp = tempfile.mkdtemp(prefix="okxt8-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False},
                                      {"price": 78010.0, "fill": False}],
                         RISK_TICK_SEC=1, LOOP_INTERVAL_SEC=1)
        patrol_calls = []
        original = loop._patrol_positions

        def counting_patrol(snap, rw):
            patrol_calls.append(1)
            original(snap, rw)

        loop._patrol_positions = counting_patrol
        loop.run(max_rounds=2, interval_sec=1)
        # 两个 round 各自带一次巡检；tick 与 round 撞点时 round 先跑 → tick 顺延
        self.assertEqual(len(patrol_calls), 2)


class DrawdownLadderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="okxt8b-")
        self.loop = make_loop(self.tmp)
        self.rw = type("RW", (), {"pk": None,
                                  "write_order": lambda *a, **k: None})()

    def _set_dd(self, dd):
        """直接以 (equity, hwm) 参数驱动阶梯（与 tick 的取数方式一致）。"""
        hwm = 10000.0
        self.loop._evaluate_drawdown_ladder(self.rw, equity=hwm * (1 - dd),
                                            hwm=hwm)

    def test_rung_upgrade_and_hysteresis_downgrade(self):
        self._set_dd(0.05)                      # ≥4% → 第 1 档
        self.assertEqual(self.loop.risk.state.get_rung(), 1)
        self._set_dd(0.039)                     # <3.2%？否 → 不降档（滞回）
        self.assertEqual(self.loop.risk.state.get_rung(), 1)
        self._set_dd(0.031)                     # <3.2% → 降档
        self.assertEqual(self.loop.risk.state.get_rung(), 0)
        events = self.store_events()
        self.assertEqual(events.count("circuit_breaker"), 2)  # 升档 + 降档

    def test_tick_uses_fresh_equity_not_stale_snapshot(self):
        """高2 回归：risk_tick 必须用本 tick 新采样的权益评估阶梯，
        即使 last_snapshot 是 None（首次 round 之前）也能触发。"""
        loop = make_loop(tempfile.mkdtemp(prefix="okxt8c-"))
        self.assertIsNone(loop.last_snapshot)   # 尚未跑过任何 round
        loop.risk.state.update_hwm(10000.0)     # 高水位来自此前的高点
        loop.client.equity = 9500.0             # 权益跌到 9500 → 回撤 5%
        loop.risk_tick()
        self.assertEqual(loop.risk.state.get_rung(), 1)
        # 权益采样也写进了 equity_curve
        n = loop.store.query_one(
            "SELECT COUNT(*) c FROM equity_curve WHERE round_pk IS NULL")["c"]
        self.assertEqual(n, 1)

    def test_tick_refreshes_last_snapshot_pnl(self):
        """面板读 last_snapshot——tick 必须把新鲜权益/持仓刷进去，
        否则两轮之间（最长 1 小时）面板显示冻结旧数（实测曾虚报 170 U）。"""
        loop = make_loop(tempfile.mkdtemp(prefix="okxt8d-"))
        loop.last_snapshot = {"equity": 10000.0, "hwm": 10000.0,
                              "drawdown": 0.0, "usdt_avail": 0.0,
                              "positions": []}
        loop.client.equity = 9950.0
        loop.client._open("BTC-USDT-SWAP", "buy", 5.0, 78000.0)  # 新持仓出现
        loop.risk_tick()
        # 权益 = tick 实时采样值（回放客户端会扣持仓名义），绝非旧的 10000
        self.assertEqual(loop.last_snapshot["equity"],
                         loop.client.get_equity()["total_eq"])
        self.assertNotEqual(loop.last_snapshot["equity"], 10000.0)
        self.assertEqual([p["instId"] for p in loop.last_snapshot["positions"]],
                         ["BTC-USDT-SWAP"])

    def store_events(self):
        return [r["kind"] for r in self.loop.store.query(
            "SELECT kind FROM app_events")]

    def test_top_tier_flattens_and_pauses(self):
        script = [{"price": 78000.0, "fill": True}]
        self.loop.client.script = script
        self.loop._open = None
        # 直接造一个持仓 + 元数据
        self.loop.client._open("BTC-USDT-SWAP", "buy", 5.0, 78000.0)
        self.loop.risk.state.set_positions_meta({"BTC-USDT-SWAP": {
            "direction": "long", "stop": 77000.0, "contracts": 5.0}})
        self._set_dd(0.11)                      # ≥10% → 末档
        self.assertEqual(self.loop.risk.state.get_rung(), 3)
        self.assertTrue(self.loop.paused)                      # 停机待人工
        self.assertEqual(len(self.loop.client.positions), 0)   # 全平
        kinds = self.store_events()
        self.assertIn("circuit_breaker", kinds)

    def test_r5_uses_ladder_allow_open(self):
        from okx_trader.risk import RiskManager
        self.loop.risk.state.set_rung(2)        # 第 2 档：allow_open=False
        self.loop.client.script = [{"price": 78000.0}]
        plan = {"instId": "BTC-USDT-SWAP", "direction": "long",
                "stop_loss": 77000.0, "order_type": "limit_maker",
                "factors": {"atr": 400.0, "sr": {"supports": [], "resistances": []}}}
        v = self.loop.risk.check_open_plan(plan)
        self.assertFalse(v.passed)
        self.assertTrue(any("R5" in f for f in v.failures))

    def test_equity_zero_triggers_top_tier(self):
        """复查遗留：权益恰好 0.0 不能被真值判断当成 falsy 跳过——
        那正是最该触发末档（全平+停机）的状态。"""
        loop = make_loop(tempfile.mkdtemp(prefix="okxt8d-"))
        loop.risk.state.update_hwm(10000.0)
        loop.client.equity = 0.0                   # 爆仓级
        rw = type("RW", (), {"pk": None,
                             "write_order": lambda *a, **k: None})()
        loop._evaluate_drawdown_ladder(rw, equity=0.0, hwm=10000.0)
        self.assertEqual(loop.risk.state.get_rung(), 3)
        self.assertTrue(loop.paused)

    def test_risk_mult_halves_budget_at_rung1(self):
        self.loop.risk.state.set_rung(1)        # 第 1 档：risk_mult=0.5
        self.loop.client.script = [{"price": 78000.0}]
        plan = {"instId": "BTC-USDT-SWAP", "direction": "long",
                "stop_loss": 77600.0, "order_type": "limit_maker",
                "factors": {"atr": 400.0, "sr": {"supports": [], "resistances": []}}}
        v = self.loop.risk.check_open_plan(plan)
        self.assertTrue(v.passed)
        # 预算 = 10000×1%×0.5 = 50U；有效距离 max(400,600)=600 → 50/(600×0.01)=8.33 张
        self.assertAlmostEqual(v.sized["contracts"], 8.33, places=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
