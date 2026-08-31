# -*- coding: utf-8 -*-
"""回测驱动器验收单测：前视保护 / 盘中触发(SL优先) / 虚拟时钟 / 成交模型 / 确定性。"""
import json
import os
import sys
import time
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.config import load_config
from okx_trader.replay import ReplayClient

INST = "BTC-USDT-SWAP"


def _bars(n, base=100.0, step=1.0):
    now = int(time.time() * 1000) - n * 3600 * 1000
    out = []
    for i in range(n):
        c = base + i * step
        out.append({"ts": now + i * 3600 * 1000, "open": c, "high": c + 1,
                    "low": c - 1, "close": c, "vol": 1000.0})
    return out


def _client(bars, script=None, fill_model="touch"):
    cfg = load_config()
    return ReplayClient(cfg, candles={INST: bars},
                        script=script or _script(bars),
                        fill_model=fill_model)


def _script(bars):
    return [{"ts": b["ts"], "open": b["open"], "high": b["high"],
             "low": b["low"], "close": b["close"], "price": b["close"]}
            for b in bars]


class ForwardLookTest(unittest.TestCase):
    def test_get_candles_max_ts_strictly_before_cursor(self):
        bars = _bars(100)
        cl = _client(bars)
        cl.cursor = 50
        got = cl.get_candles(INST, limit=200)
        self.assertTrue(all(c["ts"] < bars[50]["ts"] for c in got),
                        "前视保护被破坏：get_candles 泄露了游标之后的 bar")


class IntrabarTriggerTest(unittest.TestCase):
    def test_low_breaks_stop_but_close_does_not(self):
        """收盘价未破止损但最低价破了 → 判 stop（旧实现只看收盘价会漏掉）。"""
        bars = _bars(10)
        cl = _client(bars)
        cl._open(INST, "buy", 1.0, 100.0)
        cl.positions[INST]["stop"] = 99.5   # 止损 99.5
        # 下一根 bar：low 99.0 破止损，但 close 100.5 未破
        bars[5] = {"ts": bars[5]["ts"], "open": 100.0, "high": 101.0,
                   "low": 99.0, "close": 100.5, "vol": 1.0}
        cl.script = _script(bars)
        cl.cursor = 4
        cl.advance()   # 游标 → 5，判定
        self.assertNotIn(INST, cl.positions)
        self.assertEqual(cl.closed_trades[-1]["exit_reason"], "stop")

    def test_same_bar_sl_and_tp_judges_stop_first(self):
        """同一根 bar 内止损与止盈都在区间内 → 保守判止损先成交。"""
        bars = _bars(10)
        cl = _client(bars)
        cl._open(INST, "buy", 1.0, 100.0)
        cl.positions[INST]["stop"] = 99.0
        cl.positions[INST]["target"] = 101.0
        bars[5] = {"ts": bars[5]["ts"], "open": 100.0, "high": 101.5,
                   "low": 98.5, "close": 100.0, "vol": 1.0}   # 两端都破
        cl.script = _script(bars)
        cl.cursor = 4
        cl.advance()
        self.assertEqual(cl.closed_trades[-1]["exit_reason"], "stop")


class FillModelTest(unittest.TestCase):
    def test_touch_vs_strict_vs_always(self):
        """三种成交模型在同一根 bar 上给出预期不同的结果。"""
        bars = _bars(10)
        # bar[5]: low 99.5，买单价 99.6 → touch 不触（99.5 > 99.6 才触？）
        # 构造：买单价 99.6，touch 需 low<=99.6；strict 需 low<=99.6-tick
        bars[5] = {"ts": bars[5]["ts"], "open": 100.0, "high": 100.5,
                   "low": 99.55, "close": 100.0, "vol": 1.0}
        px = 99.6
        # always：挂即成交
        cl = _client(bars, fill_model="always")
        cl.cursor = 5
        cl.place_maker_limit(INST, "buy", 1.0, px=px)
        self.assertEqual(list(cl.orders.values())[-1]["state"], "filled")
        # touch：low 99.55 <= 99.6 → 成交
        cl = _client(bars, fill_model="touch")
        cl.cursor = 5
        cl.place_maker_limit(INST, "buy", 1.0, px=px)
        self.assertEqual(list(cl.orders.values())[-1]["state"], "filled")
        # strict：需 low <= 99.6 - tick；tick=0.1 → 需 low<=99.5，而 99.55>99.5 → 不成交
        cl = _client(bars, fill_model="strict")
        cl.cursor = 5
        cl.place_maker_limit(INST, "buy", 1.0, px=px)
        self.assertEqual(list(cl.orders.values())[-1]["state"], "canceled")


class VirtualClockTest(unittest.TestCase):
    def test_opened_ts_equals_injected_clock(self):
        from okx_trader.loop import TradingLoop
        from okx_trader.store.db import Store
        import tempfile
        cfg = load_config()
        cfg.TRADING_ENV = "replay"
        cfg.ORDER_TIMEOUT_SEC = 0
        loop = TradingLoop(cfg=cfg, env_name="replay", executing=True,
                           store=Store(os.path.join(tempfile.mkdtemp(), "t.db")))
        fixed = 1789000000.0
        loop._clock = lambda: fixed
        # 直接建一笔 trades 行，断言 opened_ts 用注入时钟
        from okx_trader.exits import open_trade_row
        from okx_trader.store import write as w
        loop.client = ReplayClient(cfg, logger=loop.log,
                                   script=[{"price": 100.0, "ts": int(fixed * 1000),
                                            "open": 100, "high": 101,
                                            "low": 99, "close": 100}])
        rw = w.RoundWriter.open(loop.store, "r1", fixed, "replay", 0, "baseline")
        sized = {"instId": INST, "direction": "long", "stop_loss": 99.0,
                 "target": 102.0, "rr": 3.0, "risk_usdt": 10.0,
                 "analyst": "趋势猎手"}
        pk = open_trade_row(loop, rw, sized, 1.0, 100.0)
        opened = loop.store.query_one(
            "SELECT opened_ts FROM trades WHERE id=?", (pk,))["opened_ts"]
        self.assertEqual(opened, fixed)


class DeterminismTest(unittest.TestCase):
    def test_same_candles_twice_identical_json(self):
        from okx_trader.backtest.runner import run_backtest
        cfg = load_config()
        bars = _bars(150)
        candles = {INST: bars}
        r1 = run_backtest(cfg, candles, collect_factors=False)
        r2 = run_backtest(cfg, candles, collect_factors=False)
        self.assertEqual(json.dumps(r1, sort_keys=True),
                         json.dumps(r2, sort_keys=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
