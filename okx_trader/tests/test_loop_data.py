# -*- coding: utf-8 -*-
"""loop.py 行为测试（离线）：数据降级短路 + OCO 巡检回归

覆盖（Phase 1 验证要求）：
    - data_ok=False 时状态记为 data_unavailable，且 Committee.decide 从未被调用
    - OCO 巡检：保护单查询合并 conditional+oco 后，连续两轮恰好只挂一张保护单，
      且重挂时透传 meta 里的 target（不把 OCO 降级成纯止损）
"""
import logging
import os
import sys
import tempfile
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import loop as loop_mod
from client import OKXAPIError
from state import StateStore


def make_loop(tmp_dir):
    """纸面模式的 TradingLoop，状态与 rounds 落盘都指向临时目录。"""
    lp = loop_mod.TradingLoop()
    lp.state = StateStore(path=os.path.join(tmp_dir, "state.json"))
    lp.risk.state = lp.state
    loop_mod.ROUNDS_DIR = os.path.join(tmp_dir, "rounds")
    return lp


class AllFactorsFailClient:
    """所有行情调用都失败——模拟网络断流。"""

    def __init__(self):
        self.log = logging.getLogger("test")

    def get_equity(self):
        return {"total_eq": 10000.0, "usdt_eq": 10000.0, "usdt_avail": 10000.0}

    def get_positions(self, inst_id=""):
        return []

    def get_candles(self, inst_id, bar="1H", limit=100):
        raise OKXAPIError("NET", "SSL UNEXPECTED_EOF (simulated)")

    def get_funding_rate(self, inst_id):
        raise OKXAPIError("NET", "boom")


class DataUnavailableTest(unittest.TestCase):
    def test_data_unavailable_short_circuits_committee(self):
        tmp = tempfile.mkdtemp(prefix="okxt-test-")
        lp = make_loop(tmp)
        lp.client = AllFactorsFailClient()

        def _must_not_run(snapshot):
            raise AssertionError("data_ok=False 时 Committee.decide 不应被调用")

        lp.committee.decide = _must_not_run
        record = lp.run_round()
        self.assertEqual(record["status"], "data_unavailable")
        self.assertFalse(record["data_ok"])
        self.assertEqual(record["symbols_ok"], 0)
        self.assertEqual(len(record["factor_errors"]), lp.cfg.SYMBOLS.__len__())


class FakeLiveClient:
    """duck-typed 真实客户端：脚本化保护单查询结果，记录下单调用。"""

    def __init__(self):
        self.log = logging.getLogger("test")
        self.place_calls = []
        self._sl_results = []   # 每次 get_pending_stop_losses 的返回（队列）

    def queue_sl(self, result):
        self._sl_results.append(result)

    def get_pending_orders(self, inst_id=""):
        return []

    def get_pending_stop_losses(self, inst_id=""):
        return self._sl_results.pop(0) if self._sl_results else []

    def get_algo_order_details(self, algo_id):
        return None

    def place_stop_loss(self, inst_id, direction, contracts, stop_px, tp_px=None):
        self.place_calls.append({"inst": inst_id, "dir": direction,
                                 "contracts": contracts, "stop": stop_px,
                                 "tp": tp_px})
        return f"algo{len(self.place_calls)}"


class OcoPatrolTest(unittest.TestCase):
    SNAP = {
        "positions": [{"instId": "ETH-USDT-SWAP", "direction": "long",
                       "contracts": 25.61, "avg_px": 2513.17,
                       "mark_px": 2505.0, "upl": -20.0}],
        "factors": {"ETH-USDT-SWAP": {"atr": 25.3}},
    }

    def test_patrol_reattaches_once_and_keeps_target(self):
        tmp = tempfile.mkdtemp(prefix="okxt-test-")
        lp = make_loop(tmp)
        # 强制进入"真实账户"分支（巡检只在 live 非 dry 模式动账）
        lp.creds_ok = True
        lp.dry_run = False
        fake = FakeLiveClient()
        lp.client = fake
        lp.state.set_positions_meta({"ETH-USDT-SWAP": {
            "direction": "long", "stop": 2474.19, "target": 2590.62,
            "contracts": 25.61, "opened_at": "2026-08-28 23:00:00",
        }})

        # 第 1 轮：交易所查不到保护单（旧 bug 场景）→ 补挂一张，且带 meta 里的 target
        fake.queue_sl([])  # 本轮合并查询（conditional+oco）结果为空
        lp._patrol_positions(self.SNAP)
        self.assertEqual(len(fake.place_calls), 1)
        self.assertEqual(fake.place_calls[0]["tp"], 2590.62)   # OCO 未被降级
        self.assertEqual(fake.place_calls[0]["stop"], 2474.19)
        meta = lp.state.get_positions_meta()["ETH-USDT-SWAP"]
        self.assertEqual(meta["algo_id"], "algo1")

        # 第 2 轮：合并查询能看到那张 OCO → 不再重复挂（回归：漏查 oco 会叠加止损单）
        fake.queue_sl([{"algoId": "algo1", "ord_type": "oco", "state": "live",
                        "side": "sell", "sz": "25.61", "sl_trigger_px": 2474.19,
                        "tp_trigger_px": 2590.62}])
        lp._patrol_positions(self.SNAP)
        self.assertEqual(len(fake.place_calls), 1)  # 仍是 1 次

    def test_no_position_no_action(self):
        tmp = tempfile.mkdtemp(prefix="okxt-test-")
        lp = make_loop(tmp)
        lp.creds_ok = True
        lp.dry_run = False
        fake = FakeLiveClient()
        lp.client = fake
        lp._patrol_positions({"positions": [], "factors": {}})
        self.assertEqual(fake.place_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
