# -*- coding: utf-8 -*-
"""Phase 3 验证：三个脚本化回放场景（打到止损 / 打到目标 / 时间止损）+ 移动止损。

全部离线：行情来自 ReplayClient 的合成序列与脚本价格，成交即刻判定。
断言：trades 行的 exit_reason、realized_pnl 符号、r_multiple == realized_pnl/risk_usdt。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.config import load_config
from okx_trader.env import ENVS
from okx_trader.exits import manage_open_positions
from okx_trader.loop import TradingLoop
from okx_trader.replay import ReplayClient
from okx_trader.store import write as w
from okx_trader.store.db import Store

INST = "BTC-USDT-SWAP"


def make_loop(tmp, script, max_hold=24):
    cfg = load_config()
    cfg.TRADING_ENV = "replay"
    cfg.ORDER_TIMEOUT_SEC = 0          # 回放中成交即刻判定，零等待
    cfg.MAX_HOLD_BARS = max_hold
    loop = TradingLoop(cfg=cfg, env_name="replay",
                       store=Store(os.path.join(tmp, "trader.db")))
    loop.executing = True              # 测试强制"会执行"语义（client 是 ReplayClient）
    loop.client = ReplayClient(cfg, logger=loop.log, script=script)
    return loop


def maker_fill_px(script_price):
    """ReplayClient 的 Maker 成交价：买单价 = bid×(1-offset)，按 tickSz 取整。"""
    bid = script_price - 0.5
    return round(bid * (1 - 0.0005) * 10) / 10


def sized_plan(entry, contracts=10.0, stop_dist=100.0, target_dist=300.0):
    """入场/止损/目标全部相对【实际成交价】构造，与回放状态机口径一致。"""
    stop = entry - stop_dist
    target = entry + target_dist
    return {"instId": INST, "direction": "long", "contracts": contracts,
            "entry_ref": entry, "stop_loss": stop, "target": target, "rr": 3.0,
            "risk_usdt": contracts * 0.01 * stop_dist,
            "notional_usdt": contracts * 0.01 * entry,
            "risk_pct": 0.001, "atr": 100.0,
            "analyst": "趋势猎手", "committee_score": 7.5}


def snap_for(loop):
    positions = loop.client.get_positions()
    return {"positions": positions,
            "factors": {INST: {"atr": 100.0, "price": loop.client._last_close()}}}


class TradeLifecycleTest(unittest.TestCase):
    def _open(self, loop, round_id, script_price=78000.0):
        entry = maker_fill_px(script_price)      # 实际 Maker 成交价
        rw = w.RoundWriter.open(loop.store, round_id, 1.0, "replay", 1, "baseline")
        ex = loop._execute_open(sized_plan(entry=entry), rw)
        self.assertEqual(ex["status"], "opened", f"execution={ex}")
        self.assertAlmostEqual(ex["avg_fill_px"], entry, places=6)
        return rw, entry

    def _trade(self, store):
        row = store.query_one("SELECT * FROM trades")
        return dict(row) if row else None

    def test_stop_hit_closes_with_negative_r(self):
        tmp = tempfile.mkdtemp(prefix="okxt3-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": True},
                                      {"price": 77400.0}])
        _, entry = self._open(loop, "round_1")
        self.assertIsNotNone(self._trade(loop.store))
        loop.client.advance()  # 价格 77400 → 穿过止损 77900 → 交易所侧触发
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        snap = snap_for(loop)
        loop._patrol_positions(snap, rw2)          # 巡检发现仓位消失 → 回填
        manage_open_positions(loop, snap, rw2)
        tr = self._trade(loop.store)
        self.assertEqual(tr["status"], "closed")
        self.assertEqual(tr["exit_reason"], "stop")
        self.assertAlmostEqual(tr["realized_pnl"], -10.0, places=6)   # -1R
        self.assertAlmostEqual(tr["r_multiple"], -1.0, places=6)
        self.assertAlmostEqual(tr["entry_px"], entry, places=6)
        self.assertEqual(tr["analyst"], "趋势猎手")

    def test_target_hit_closes_with_positive_r(self):
        tmp = tempfile.mkdtemp(prefix="okxt3-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": True},
                                      {"price": 78400.0}])
        _, entry = self._open(loop, "round_1")
        loop.client.advance()  # 价格 78400 ≥ 目标（entry+300 的 OCO 会在 78260.5 之前触发判定按 target 价成交）
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        snap = snap_for(loop)
        loop._patrol_positions(snap, rw2)
        manage_open_positions(loop, snap, rw2)
        tr = self._trade(loop.store)
        self.assertEqual(tr["status"], "closed")
        self.assertEqual(tr["exit_reason"], "target")
        self.assertAlmostEqual(tr["realized_pnl"], 30.0, places=6)   # +3R（目标价出场）
        self.assertAlmostEqual(tr["r_multiple"], 3.0, places=6)

    def test_time_stop_after_max_hold(self):
        tmp = tempfile.mkdtemp(prefix="okxt3-")
        entry = maker_fill_px(78000.0)              # 77960.5
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": True},
                                      {"price": entry + 10.0}], max_hold=2)
        _, entry = self._open(loop, "round_1")
        loop.client.advance()  # 价格横盘 entry+10 → 0.1R
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        snap = snap_for(loop)
        future = self._trade(loop.store)["opened_ts"]
        # 模拟时间前进 3 根 K 线
        with mock.patch("okx_trader.exits.time.time",
                        return_value=future + 3 * 3600):
            manage_open_positions(loop, snap, rw2)
        tr = self._trade(loop.store)
        self.assertEqual(tr["status"], "closed")
        self.assertEqual(tr["exit_reason"], "time_stop")
        self.assertAlmostEqual(tr["realized_pnl"], 1.0, places=6)    # (entry+10-entry)*10*0.01
        self.assertLess(abs(tr["r_multiple"]), 0.3)

    def test_trailing_stop_moves_to_breakeven_then_trails(self):
        tmp = tempfile.mkdtemp(prefix="okxt3-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": True},
                                      {"price": 78150.0}])
        _, entry = self._open(loop, "round_1")
        meta = loop.risk.state.get_positions_meta()[INST]
        self.assertAlmostEqual(meta["stop"], entry - 100.0, places=6)
        loop.client.advance()  # 价格 entry+150 → 浮盈 1.5R
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        snap = snap_for(loop)
        manage_open_positions(loop, snap, rw2)
        meta = loop.risk.state.get_positions_meta()[INST]
        # 保本(entry=77960.5) 与 跟随(mark-1×ATR=78150-100=78050) 取大者 → 78050
        self.assertAlmostEqual(meta["stop"], 78050.0, places=6)
        # 仓位仍在（未触发止损），保护单已换成新价
        self.assertIn(INST, loop.client.positions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
