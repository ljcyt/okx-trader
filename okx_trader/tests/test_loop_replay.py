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
    cfg.MAKER_PRICE_OFFSET = 0.0005    # 显式固定——不随本机/CI 配置缺省漂移
    cfg.MAX_HOLD_BARS = max_hold
    loop = TradingLoop(cfg=cfg, env_name="replay",
                       store=Store(os.path.join(tmp, "trader.db")))
    loop.executing = True              # 测试强制"会执行"语义（client 是 ReplayClient）
    loop.client = ReplayClient(cfg, logger=loop.log, script=script)
    # 构造器先把 OKXClient 装进了 risk/committee——换 ReplayClient 后同步引用，
    # 否则风控复核（get_ticker 等）打到真实 OKX 网络上
    loop.risk.client = loop.client
    loop.committee.client = loop.client
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


class PatrolBackfillTest(unittest.TestCase):
    """工作单在等待期外/重启间隙成交 → 巡检补记账本。

    回归背景：SOL 入场单在部署重启间隙成交，巡检的补记块因先 pop 工作单
    而成为死代码，导致交易所有仓位但 trades 表无记录、入场单永远 'live'。"""

    def _place_working(self, loop, script_price=78000.0):
        """挂单不成交 → 进工作单名册（_execute_open 的 working 分支）。
        附带批准行：巡检收养前会重跑 R1-R8，从裁决恢复计划字段。"""
        entry = maker_fill_px(script_price)
        rw = w.RoundWriter.open(loop.store, "round_1", 1.0, "replay", 1, "baseline")
        plan = sized_plan(entry=entry)
        ex = loop._execute_open(plan, rw)
        self.assertEqual(ex["status"], "working", f"execution={ex}")
        loop.store.execute(
            "INSERT INTO risk_verdicts(round_pk, passed, rule_code, "
            "failures_json, warnings_json, inst_id, stop_loss, target, rr, "
            "risk_usdt) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rw.pk, 1, "OK", "[]", "[]", INST, plan["stop_loss"],
             plan["target"], plan["rr"], plan["risk_usdt"]))
        self.assertIsNone(loop.store.query_one("SELECT * FROM trades"))
        return rw, entry, ex["ord_id"]

    def _fill_on_exchange(self, loop, ord_id):
        """模拟交易所侧成交（本地记账之外，如重启间隙发生的那样）。"""
        o = loop.client.orders[ord_id]
        o["state"] = "filled"
        o["acc_fill_sz"] = o["sz"]
        o["avg_px"] = o["px"]
        loop.client._open(INST, "buy", o["sz"], o["px"])

    def test_working_order_fill_backfilled_with_registry(self):
        tmp = tempfile.mkdtemp(prefix="okxpb-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False}])
        _, entry, ord_id = self._place_working(loop)
        self._fill_on_exchange(loop, ord_id)
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        tr = dict(loop.store.query_one("SELECT * FROM trades"))
        self.assertEqual(tr["status"], "open")
        self.assertEqual(tr["analyst"], "趋势猎手")          # 来自工作单名册
        self.assertAlmostEqual(tr["stop_px"], entry - 100.0, places=6)
        self.assertAlmostEqual(tr["entry_px"], entry, places=6)
        order = loop.store.query_one(
            "SELECT * FROM orders WHERE kind='entry' AND exch_ord_id=?",
            (ord_id,))
        self.assertEqual(order["state"], "filled")           # 入场单闭环
        self.assertEqual(order["trade_pk"], tr["id"])
        meta = loop.risk.state.get_positions_meta()[INST]
        self.assertEqual(meta["trade_pk"], tr["id"])
        # 工作单已清册，且残余挂单被撤
        self.assertNotIn(INST, loop.risk.state.get_working_orders())
        self.assertNotEqual(loop.client.orders[ord_id]["state"], "live")

    def test_fill_backfilled_after_registry_lost(self):
        """名册已丢（上一轮巡检已 pop）→ 按交易所侧入场单回填。"""
        tmp = tempfile.mkdtemp(prefix="okxpb-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False}])
        _, entry, ord_id = self._place_working(loop)
        loop.risk.state.set_working_orders({})               # 名册丢失
        self._fill_on_exchange(loop, ord_id)
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        tr = dict(loop.store.query_one("SELECT * FROM trades"))
        self.assertEqual(tr["status"], "open")
        order = loop.store.query_one(
            "SELECT * FROM orders WHERE kind='entry' AND exch_ord_id=?",
            (ord_id,))
        self.assertEqual(order["state"], "filled")
        self.assertEqual(order["trade_pk"], tr["id"])
        meta = loop.risk.state.get_positions_meta()[INST]
        self.assertEqual(meta["trade_pk"], tr["id"])

    def test_fill_backfill_is_idempotent_when_open_row_exists(self):
        """meta 丢了 trade_pk 但 open 行已在（崩溃窗口半截记账）→ 不重复建行。"""
        tmp = tempfile.mkdtemp(prefix="okxpb-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": True}])
        entry = maker_fill_px(78000.0)
        rw1 = w.RoundWriter.open(loop.store, "round_1", 1.0, "replay", 1, "baseline")
        self.assertEqual(loop._execute_open(sized_plan(entry=entry), rw1)["status"],
                         "opened")                 # 正常开仓，trades pk=1
        loop.risk.state.set_positions_meta(        # 模拟 meta 丢 trade_pk
            {INST: {"direction": "long", "stop": 77900.0}})
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        rows = loop.store.query("SELECT id, status FROM trades")
        self.assertEqual(len(rows), 1)             # 没有重复行
        meta = loop.risk.state.get_positions_meta()[INST]
        self.assertEqual(meta["trade_pk"], rows[0]["id"])  # meta 已接回

    def test_backfill_recovers_plan_from_risk_verdict(self):
        """重启间隙成交：trades 的计划字段从风控裁决恢复——R8 同向聚合
        （SUM(risk_usdt)）与 R 倍数分母都依赖它，不能因补记而断线。"""
        tmp = tempfile.mkdtemp(prefix="okxpb-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False}])
        rw1, entry, ord_id = self._place_working(loop)
        self._fill_on_exchange(loop, ord_id)
        loop.risk.state.set_working_orders({})
        # 批准值相对成交价构造（绝对价 103.56/107.825 是 SOL 的真实案例，
        # 移植到 BTC 77960.5 上会被 R2 距离检查拒收）
        plan = {"risk_usdt": 969.61, "target": entry + 300.0,
                "stop_loss": entry - 100.0, "rr": 3.0}
        loop.store.execute("UPDATE risk_verdicts SET risk_usdt=?, target=?, "
                           "stop_loss=?, rr=? WHERE round_pk=?",
                           (plan["risk_usdt"], plan["target"],
                            plan["stop_loss"], plan["rr"], rw1.pk))
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        tr = dict(loop.store.query_one("SELECT * FROM trades"))
        self.assertEqual(tr["status"], "open")
        self.assertAlmostEqual(tr["risk_usdt"], plan["risk_usdt"], places=6)
        self.assertAlmostEqual(tr["target_px"], plan["target"], places=6)
        self.assertAlmostEqual(tr["stop_px"], plan["stop_loss"], places=6)
        self.assertAlmostEqual(tr["planned_rr"], plan["rr"], places=6)
        # 审计链：open_round_pk 指向批准轮（入场单所属），不是巡检轮
        self.assertEqual(tr["open_round_pk"], rw1.pk)

    def test_protect_reattach_recovers_target_from_verdict(self):
        """meta 全丢时补挂保护单：target 从风控裁决恢复 → 挂 OCO 而非
        纯止损（否则"因 RR≥1.5 批准"和"挂出能实现 RR 的单"断开）。"""
        tmp = tempfile.mkdtemp(prefix="okxpb-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False}])
        rw1, entry, ord_id = self._place_working(loop)
        self._fill_on_exchange(loop, ord_id)
        loop.risk.state.set_working_orders({})
        loop.risk.state.set_positions_meta({})
        loop.store.execute("UPDATE risk_verdicts SET stop_loss=?, target=?, "
                           "rr=?, risk_usdt=? WHERE round_pk=?",
                           (entry - 100.0, entry + 300.0, 3.0, 100.0, rw1.pk))
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        protect = loop.store.query_one("SELECT * FROM orders WHERE kind='protect'")
        self.assertIsNotNone(protect)
        self.assertAlmostEqual(protect["sl_trigger_px"], entry - 100.0, places=6)
        self.assertAlmostEqual(protect["tp_trigger_px"], entry + 300.0, places=6)
        meta = loop.risk.state.get_positions_meta()[INST]
        self.assertEqual(meta["trade_pk"],
                         loop.store.query_one("SELECT id FROM trades")["id"])

    def test_fill_backfill_links_existing_protect(self):
        """保护单先于补记存在（止损先挂上、trade 后补）→ 挂链到同一 trade。"""
        tmp = tempfile.mkdtemp(prefix="okxpb-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False}])
        rw1, entry, ord_id = self._place_working(loop)
        self._fill_on_exchange(loop, ord_id)
        # 模拟"先补挂了保护单但没建 trades 行"（服务器 SOL 的实际状态：
        # 巡检补挂保护单已写 orders 行，trade_pk 挂空）
        algo_id = loop.client.place_stop_loss(INST, "long", 10.0, entry - 100.0)
        rw1.write_order("replay", INST, "protect", "conditional",
                        exch_algo_id=str(algo_id), side="sell", sz=10.0,
                        sl_trigger_px=entry - 100.0, state="live",
                        note="巡检补挂保护单")
        loop.risk.state.set_positions_meta(
            {INST: {"algo_id": str(algo_id), "stop": entry - 100.0}})
        loop.risk.state.set_working_orders({})               # 名册已丢
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        tr = dict(loop.store.query_one("SELECT * FROM trades"))
        protect = loop.store.query_one(
            "SELECT * FROM orders WHERE kind='protect' AND exch_algo_id=?",
            (str(algo_id),))
        self.assertIsNotNone(protect)
        self.assertEqual(protect["trade_pk"], tr["id"])
        meta = loop.risk.state.get_positions_meta()[INST]
        self.assertEqual(meta["trade_pk"], tr["id"])


class OrphanRevalidationTest(unittest.TestCase):
    """P0-2：孤儿成交仓位收养前必须重跑 R1-R8，不过即平。"""

    def _orphan_setup(self, stop=None):
        """挂单不成交 → 名册丢 → 交易所侧成交（SOL 事件的形状）。
        附带 risk_verdicts 批准行（收养复核从裁决恢复计划字段）。"""
        loop = make_loop(tempfile.mkdtemp(prefix="okxpo-"),
                         script=[{"price": 78000.0, "fill": False}])
        entry = maker_fill_px(78000.0)
        rw1 = w.RoundWriter.open(loop.store, "round_1", 1.0, "replay", 1, "baseline")
        ex = loop._execute_open(sized_plan(entry=entry,
                                           stop_dist=stop or 100.0), rw1)
        self.assertEqual(ex["status"], "working")
        ord_id = ex["ord_id"]
        stop_px = entry - (stop or 100.0)
        loop.store.execute(
            "INSERT INTO risk_verdicts(round_pk, passed, rule_code, "
            "failures_json, warnings_json, inst_id, stop_loss, target, rr, "
            "risk_usdt) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rw1.pk, 1, "OK", "[]", "[]", INST, stop_px, entry + 300.0, 3.0,
             100.0))
        loop.risk.state.set_working_orders({})
        self._fill_on_exchange(loop, ord_id)
        return loop, rw1, ord_id, entry

    @staticmethod
    def _fill_on_exchange(loop, ord_id):
        o = loop.client.orders[ord_id]
        o["state"] = "filled"
        o["acc_fill_sz"] = o["sz"]
        o["avg_px"] = o["px"]
        loop.client._open(INST, "buy", o["sz"], o["px"])

    def test_orphan_passing_revalidation_adopted(self):
        """当前数据仍过 R1-R8 → 收养，事件 + 审计链指向批准轮。"""
        loop, rw1, ord_id, entry = self._orphan_setup()
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        tr = dict(loop.store.query_one("SELECT * FROM trades"))
        self.assertIsNotNone(tr)
        self.assertEqual(tr["open_round_pk"], rw1.pk)      # 审计链
        self.assertEqual(tr["risk_usdt"], tr["risk_usdt"])  # 字段齐
        ev = loop.store.query_one(
            "SELECT * FROM app_events WHERE kind='orphan_adopted'")
        self.assertIsNotNone(ev)

    def test_orphan_failing_revalidation_closed(self):
        """止损距离超 MAX_STOP_DIST_PCT → 拒收：平仓、不建 trades 行。"""
        loop, rw1, ord_id, entry = self._orphan_setup(stop=10000.0)  # ~12.8% 距离
        rw2 = w.RoundWriter.open(loop.store, "round_2", 2.0, "replay", 1, "baseline")
        loop._patrol_positions(snap_for(loop), rw2)
        self.assertIsNone(loop.store.query_one("SELECT * FROM trades"))
        ev = loop.store.query_one(
            "SELECT * FROM app_events WHERE kind='orphan_rejected'")
        self.assertIsNotNone(ev)
        self.assertEqual(ev["level"], "critical")
        self.assertIn(INST, ev["message"])
        # 仓位已被平掉
        self.assertNotIn(INST, loop.client.positions)

    def test_round_error_cancels_leftover_entry(self):
        """轮次异常收尾 → finally 撤掉本轮残留入场单（堵孤儿源头）。"""
        tmp = tempfile.mkdtemp(prefix="okxpo-")
        loop = make_loop(tmp, script=[{"price": 78000.0, "fill": False}])
        entry = maker_fill_px(78000.0)
        rw = w.RoundWriter.open(loop.store, "round_e", 1.0, "replay", 1, "baseline")
        ex = loop._execute_open(sized_plan(entry=entry), rw)
        self.assertEqual(ex["status"], "working")
        # 模拟轮次异常路径：out.status=error → finally 撤单
        out = {"status": "error"}
        loop._cancel_round_entry_orders(rw)
        order = loop.store.query_one(
            "SELECT * FROM orders WHERE kind='entry'")
        self.assertEqual(order["state"], "canceled")
        ev = loop.store.query_one(
            "SELECT * FROM app_events WHERE kind='round_cleanup'")
        self.assertIsNotNone(ev)
        self.assertEqual(loop.client.orders[ex["ord_id"]]["state"], "canceled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
