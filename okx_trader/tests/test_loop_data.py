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

from okx_trader import loop as loop_mod
from okx_trader.client import OKXAPIError
from okx_trader.env import ENVS
from okx_trader.store.db import Store


def make_loop(tmp_dir, env="paper"):
    """纸面模式 TradingLoop，SQLite 指向临时目录（不污染真实数据）。"""
    lp = loop_mod.TradingLoop(env_name=env,
                              store=Store(os.path.join(tmp_dir, "trader.db")))
    # 不带 client 构造：默认 OKXClient 会打到真实网络。调用方替换
    # lp.client 后必须同步 risk.client（见 OcoPatrolTest），否则
    # _revalidate_orphan → get_ticker 走真实行情，结果随 ETH 价格漂移
    return lp


def swap_client(lp, client):
    """替换 loop 的 client 并同步 risk/committee 引用（测试装配用）。"""
    lp.client = client
    lp.risk.client = client
    lp.committee.client = client
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
        self.assertEqual(len(record["factor_errors"]), len(lp.cfg.SYMBOLS))


class _fake_rw:
    """巡检写库用的 RoundWriter 替身：只记录事件，不真写库。"""
    pk = None  # NULL round_pk 合法（不挂轮次的事件）
    def write_order(self, *a, **k):
        pass


class FakeLiveClient:
    """duck-typed 真实客户端：脚本化保护单查询结果，记录下单调用。"""

    def __init__(self):
        self.log = logging.getLogger("test")
        self.place_calls = []
        # 固定返回（非一次性队列）：真实 client 每次查询都返回同一份
        # 列表；pop 语义会让孤儿复核多出的那次调用"吃掉"下一次的脚本值，
        # 把"漏查 oco 会叠加止损单"这条回归守卫遮掉
        self._sl = []

    def queue_sl(self, result):
        self._sl = result

    def get_pending_orders(self, inst_id=""):
        return []

    def get_instrument(self, inst_id):
        # 巡检补记 trades 行时需要合约面值
        return {"instId": inst_id, "ctVal": 0.1}

    def get_pending_stop_losses(self, inst_id=""):
        return self._sl

    def get_algo_order_details(self, algo_id):
        return None

    def place_stop_loss(self, inst_id, direction, contracts, stop_px, tp_px=None):
        self.place_calls.append({"inst": inst_id, "dir": direction,
                                 "contracts": contracts, "stop": stop_px,
                                 "tp": tp_px})
        return f"algo{len(self.place_calls)}"

    # ── 孤儿收养复核（P0-2b）所需 ──
    def get_equity(self):
        return {"total_eq": 10000.0, "usdt_eq": 10000.0, "usdt_avail": 10000.0}

    def get_positions(self, inst_id=""):
        return []

    def get_ticker(self, inst_id):
        return {"instId": inst_id, "last": 2505.0, "bid": 2504.5,
                "ask": 2505.5, "ask_sz": 10.0, "bid_sz": 10.0,
                "vol24h_quote": 1e7, "ts": 0}

    def compute_atr(self, inst_id, period=14, bar="1H"):
        return 25.3

    def round_size(self, sz, lot_sz, min_sz):
        return round(sz * 100) / 100

    def cancel_stop_loss(self, inst_id, algo_id):
        pass

    def close_position_market(self, inst_id, direction=""):
        pass


class OcoPatrolTest(unittest.TestCase):
    SNAP = {
        "positions": [{"instId": "ETH-USDT-SWAP", "direction": "long",
                       "contracts": 25.61, "avg_px": 2513.17,
                       "mark_px": 2505.0, "upl": -20.0}],
        # atr=35：孤儿复核的 2% 兜底止损 ≈50U 距离下，ATR 目标 2.5×35=87.5
        # → RR≈1.75 ≥ 1.5，复核可通过（该用例聚焦 OCO 补挂，不测拒收）
        "factors": {"ETH-USDT-SWAP": {"atr": 35.0}},
    }

    def test_patrol_reattaches_once_and_keeps_target(self):
        tmp = tempfile.mkdtemp(prefix="okxt-test-")
        lp = make_loop(tmp)
        # 强制进入会执行的环境（巡检只在 executing 时动账）
        lp.env = ENVS["demo"]
        lp.executing = True
        fake = FakeLiveClient()
        swap_client(lp, fake)     # 同步 risk.client，否则复核打真实网络
        lp.risk.state.set_positions_meta({"ETH-USDT-SWAP": {
            "direction": "long", "stop": 2474.19, "target": 2590.62,
            "contracts": 25.61, "opened_at": "2026-08-28 23:00:00",
        }})

        # 第 1 轮：交易所查不到保护单（旧 bug 场景）→ 补挂一张，且带 meta 里的 target
        fake.queue_sl([])  # 本轮合并查询（conditional+oco）结果为空
        lp._patrol_positions(self.SNAP, _fake_rw())
        self.assertEqual(len(fake.place_calls), 1)
        self.assertEqual(fake.place_calls[0]["tp"], 2590.62)   # OCO 未被降级
        self.assertEqual(fake.place_calls[0]["stop"], 2474.19)
        meta = lp.risk.state.get_positions_meta()["ETH-USDT-SWAP"]
        self.assertEqual(meta["algo_id"], "algo1")

        # 第 2 轮：合并查询能看到那张 OCO → 不再重复挂（回归：漏查 oco 会叠加止损单）
        fake.queue_sl([{"algoId": "algo1", "ord_type": "oco", "state": "live",
                        "side": "sell", "sz": "25.61", "sl_trigger_px": 2474.19,
                        "tp_trigger_px": 2590.62}])
        lp._patrol_positions(self.SNAP, _fake_rw())
        self.assertEqual(len(fake.place_calls), 1)  # 仍是 1 次

    def test_no_position_no_action(self):
        tmp = tempfile.mkdtemp(prefix="okxt-test-")
        lp = make_loop(tmp)
        lp.env = ENVS["demo"]
        lp.executing = True
        fake = FakeLiveClient()
        lp.client = fake
        lp._patrol_positions({"positions": [], "factors": {}}, _fake_rw())
        self.assertEqual(fake.place_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
