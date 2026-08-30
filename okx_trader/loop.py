# -*- coding: utf-8 -*-
"""自主交易循环（多 agent 委员会版）

每轮流程（1H K线，默认每小时一轮）：
    1. 快照：账户权益 + 持仓 + 各标的行情与因子报告（factors.py 代码计算）
    2. 持仓巡检/对账（崩溃恢复：撤残留挂单、补挂丢失的保护单）
    3. 委员会决策（committee.py）：3 分析师提案 → 3 裁判打分 → 聚合胜出
    4. 硬风控终审（risk.py 一票否决，AI 不可越过）
    5. 执行：env.executing 才真实挂单（post_only Maker → 等成交 → 交易所保护单）；
       非 executing 环境只记录"本来会怎么做"（status='planned'）
    6. 全部落 SQLite（rounds/round_factors/proposals/judge_scores/risk_verdicts/
       orders/equity_curve/llm_calls/app_events）——单一数据源，无并行 JSONL

状态说明：env.name 决定环境；--no-execute 只把 executing 压成 False，
demo 环境可以"只观察"，不需要发明第五种环境。
"""
import threading
import time
import traceback

from . import __version__
from .committee import Committee
from .exits import manage_open_positions, open_trade_row, reconcile_closed_trade
from .env import ENVS, db_path, make_client, resolve_env
from .factors import (build_factor_report, format_factor_report,
                      overall_regime)
from .hooks import register as hook_register, trigger as hook_trigger
from .risk import RiskManager
from .store import write as w
from .store.db import Store, init_db
from .store.factors_zoo import backfill_returns, collect_from_report


_hooks_wired = False


class _TickWriter:
    """tick 的写库适配：订单/事件 round_pk=NULL（不属于任何 cognition 轮）。"""
    pk = None

    def __init__(self, store):
        self.store = store

    def write_order(self, env, inst_id, kind, ord_type, **kw):
        w.write_order(self.store, env, inst_id, kind, ord_type, **kw)


class RunState:
    """risk.py 需要的 state 接口（get_hwm/update_hwm）适配到 run_state 表。
    key 按 env 隔离——纸面高水位污染真实账户熔断器在结构上不可能。"""

    def __init__(self, store, env_name):
        self.store = store
        self.env = env_name

    def get_hwm(self):
        v = self.store.state_get(self.env, "equity_hwm")
        return float(v) if v else 0.0

    def update_hwm(self, equity):
        hwm = max(self.get_hwm(), float(equity))
        self.store.state_set(self.env, "equity_hwm", hwm)
        dd = (hwm - float(equity)) / hwm if hwm > 0 else 0.0
        return hwm, dd

    # 回撤档位（tick 评估，滞回降档）
    def get_rung(self):
        v = self.store.state_get(self.env, "dd_rung")
        return int(v) if v else 0

    def set_rung(self, rung):
        self.store.state_set(self.env, "dd_rung", int(rung))

    def get_paused(self):
        return bool(self.store.state_get(self.env, "paused"))

    def set_paused_state(self, paused):
        self.store.state_set(self.env, "paused", bool(paused))

    # 工作单名册（挂单策略：未成交报价留盘口覆盖整轮）
    def get_working_orders(self):
        v = self.store.state_get(self.env, "working_orders")
        return v or {}

    def set_working_orders(self, working):
        self.store.state_set(self.env, "working_orders", working or {})

    # 持仓元数据（崩溃对账用）
    def get_positions_meta(self):
        v = self.store.state_get(self.env, "positions_meta")
        return v or {}

    def set_positions_meta(self, meta):
        self.store.state_set(self.env, "positions_meta", meta or {})


class TradingLoop:
    def __init__(self, cfg=None, logger=None, env_name=None, executing=None,
                 store=None):
        from .config import get_logger, load_config
        self.cfg = cfg or load_config()
        self.log = logger or get_logger(level=getattr(self.cfg, "LOG_LEVEL", "INFO"))
        self.env = resolve_env(self.cfg) if env_name is None else ENVS[env_name]
        # --no-execute：把 executing 压成 False，demo 可只观察而不变成另一个环境
        self.executing = self.env.executing if executing is None else executing

        self.store = store or Store(db_path=self._db_path())
        self.state = RunState(self.store, self.env.name)   # run_state 适配器
        self.client = make_client(self.env, self.cfg, logger=self.log)
        self.risk = RiskManager(self.cfg, self.client,
                                self.state)
        self.committee = Committee(self.cfg, self.client, store=self.store,
                                   env=self.env.name)
        self.round_seq = 0
        self.paused = bool(self.state.get_paused())  # 末档熔断后的人工恢复
        self._wire_alert_hooks()
        self.last_snapshot = None       # Web 面板读的活状态
        self.next_round_ts = None
        self.last_round_id = None
        self.current_step = None
        self._busy = False
        self._run_now = threading.Event()
        w.write_event(self.store, self.env.name, "env_switch",
                      f"启动：env={self.env.name} executing={self.executing} "
                      f"v{__version__}", level="info")
        self.log.info("== %s ==（executing=%s）交易循环就绪",
                      self.env.name, self.executing)

    def _wire_alert_hooks(self):
        """告警钩子只注册一次：飞书 webhook（可选）监听关键交易事件。"""
        global _hooks_wired
        if _hooks_wired:
            return
        _hooks_wired = True
        url = getattr(self.cfg, "ALERT_WEBHOOK_URL", "")
        if url:
            from .alerts.webhook import make_hook
            for event, floor in (("data_degraded", "warn"),
                                 ("circuit_breaker", "warn"),
                                 ("naked_position", "critical"),
                                 ("trade_closed", "info"),
                                 ("order_placed", "info"),
                                 ("order_filled", "info"),
                                 ("time_stop", "warn"),
                                 ("trailing_stop", "info"),
                                 ("risk_rejected", "info"),
                                 ("round_done", "info")):
                hook_register(event, make_hook(url, floor))

    @staticmethod
    def _db_path():
        from .env import db_path
        p = db_path()
        import os
        init_db(p)
        return p

    # ── 面板控制（认证后可暂停/触发，但永远不能交易）─────────────────

    def request_run_now(self):
        self._run_now.set()

    def set_paused(self, paused, reason=""):
        self.paused = bool(paused)
        self.state.set_paused_state(self.paused)
        w.write_event(self.store, self.env.name,
                      "paused" if paused else "resumed",
                      f"循环已{'暂停' if paused else '恢复'}"
                      + (f"（{reason}）" if reason else ""),
                      level="warn" if paused else "info")

    # ────────────────────────── 快照 ──────────────────────────

    def take_snapshot(self):
        """账户 + 持仓 + 各标的因子报告。单标因子失败记入 factor_errors，
        "没数据"绝不伪装成"没信号"。"""
        equity_info = self.client.get_equity()
        equity = equity_info["total_eq"]
        hwm, drawdown = self.risk.state.update_hwm(equity)
        positions = self.client.get_positions()

        factors, factor_errors = {}, {}
        for inst_id in self.cfg.SYMBOLS:
            try:
                factors[inst_id] = build_factor_report(self.cfg, self.client, inst_id)
            except Exception as e:  # noqa: BLE001
                self.log.warning("%s 因子计算失败：%s", inst_id, e)
                factors[inst_id] = None
                factor_errors[inst_id] = f"{type(e).__name__}: {e}"

        symbols_total = len(self.cfg.SYMBOLS)
        symbols_ok = sum(1 for f in factors.values() if f is not None)
        self.log.info("快照：权益 %.2f U，高水位 %.2f，回撤 %.2f%%，持仓 %d 个，因子 %d/%d",
                      equity, hwm, drawdown * 100, len(positions),
                      symbols_ok, symbols_total)
        self.last_snapshot = {
            "equity": equity, "hwm": hwm, "drawdown": drawdown,
            "usdt_avail": equity_info["usdt_avail"], "positions": positions,
            "data_ok": symbols_ok > 0, "symbols_ok": symbols_ok,
            "symbols_total": symbols_total}
        for inst_id, f in factors.items():
            if f:
                self.log.info("因子：%s", format_factor_report(f).replace("\n", " | "))
        return {
            "ts": time.time(),
            "equity": equity,
            "usdt_avail": equity_info["usdt_avail"],
            "hwm": hwm,
            "drawdown": drawdown,
            "positions": positions,
            "factors": factors,
            "factor_errors": factor_errors,
            "data_ok": symbols_ok > 0,
            "symbols_ok": symbols_ok,
            "symbols_total": symbols_total,
        }

    # ────────────────────────── 单轮 ──────────────────────────

    def run_round(self):
        """跑一轮完整闭环，落 SQLite 并返回轻量结果 dict。异常记录后吞掉，循环不中断。"""
        t0 = time.time()
        self.round_seq += 1
        round_id = time.strftime("%Y%m%d_%H%M%S") + f"_{self.round_seq:03d}"
        llm_mode = "llm" if self.committee.llm.available else "baseline"
        rw = w.RoundWriter.open(self.store, round_id, t0, self.env.name,
                                self.executing, llm_mode)
        # LLM 调用记账挂到本轮（含成本）
        self.committee.llm.recorder = (
            lambda role, model, ok, err, lat, raw, pt=None, ct=None, cost=None,
            _pk=rw.pk:
            w.write_llm_call(self.store, _pk, role, model, ok, err, lat, pt,
                             ct, raw, cost_usd=cost))

        out = {"round_id": round_id, "status": "error", "env": self.env.name,
               "executing": self.executing}
        self.last_round_id = round_id
        self._busy = True
        try:
            # 1. 快照
            snap = self.take_snapshot()
            # 2. 持仓巡检/对账（移动/时间止损已挪到 5 分钟 tick，见 risk_tick）
            self._patrol_positions(snap, rw)
            # 2.5 因子动物园：前向收益回填 + 本轮观测采集（都幂等，失败不影响交易）
            try:
                backfill_returns(self.store, self.client, bar=self.cfg.ATR_BAR)
                for inst_id, f in snap["factors"].items():
                    if f:
                        collect_from_report(self.store, rw.pk, inst_id, f,
                                            self.cfg.ATR_BAR)
            except Exception:  # noqa: BLE001
                self.log.debug("因子动物园采集/回填失败", exc_info=True)
            # 因子快照全量入库
            for inst_id, f in snap["factors"].items():
                rw.write_factors(
                    inst_id, f,
                    report_text=format_factor_report(f) if f else None,
                    err=snap["factor_errors"].get(inst_id))
            rw.write_equity(self.env.name, snap["ts"], snap["equity"], snap["hwm"],
                            snap["drawdown"], snap["usdt_avail"], None,
                            len(snap["positions"]))
            out.update({"equity": snap["equity"], "data_ok": snap["data_ok"],
                        "symbols_ok": snap["symbols_ok"],
                        "symbols_total": snap["symbols_total"],
                        "factor_errors": snap["factor_errors"]})

            # 3. 数据降级短路：全标的因子失败 → 不跑委员会
            if not snap["data_ok"]:
                msg = f"全部标的因子获取失败 {snap['factor_errors']}"
                self.log.error("数据降级：%s —— 本轮记为 data_unavailable", msg)
                w.write_event(self.store, self.env.name, "data_degraded", msg,
                              level="warn", round_pk=rw.pk)
                hook_trigger("data_degraded", {"kind": "data_degraded",
                             "level": "warn", "message": msg})
                rw.finish("data_unavailable", data_ok=0, symbols_ok=0,
                          symbols_total=snap["symbols_total"],
                          equity=snap["equity"], hwm=snap["hwm"],
                          drawdown=snap["drawdown"], usdt_avail=snap["usdt_avail"],
                          open_positions=len(snap["positions"]),
                          duration_sec=round(time.time() - t0, 2))
                out["status"] = "data_unavailable"
                return out

            # 4. 委员会决策
            decision = self.committee.decide(snap)
            slot_pks = rw.write_committee(decision, self.committee.threshold)
            plan = decision.get("plan") if decision.get("action") == "open" else None
            out["decision"] = decision.get("reason")
            # Phase 10 轮次词汇：intent（想做什么）/ final_action（实际做了什么）
            regime = overall_regime(snap["factors"], self.cfg)
            adv = next((s.get("votes") for s in decision.get("scoreboard", [])
                        if plan and s.get("instId") == plan.get("instId")
                        and s.get("direction") == plan.get("direction")
                        and s.get("analyst") == plan.get("analyst")), None)
            revisions = int(decision.get("revisions") or 0)
            out["regime"] = regime
            if plan is None:
                rw.finish("no_action", reason=decision.get("reason"),
                          data_ok=1, symbols_ok=snap["symbols_ok"],
                          symbols_total=snap["symbols_total"],
                          equity=snap["equity"], hwm=snap["hwm"],
                          drawdown=snap["drawdown"], usdt_avail=snap["usdt_avail"],
                          open_positions=len(snap["positions"]),
                          duration_sec=round(time.time() - t0, 2),
                          intent="hold", final_action="steady", regime=regime,
                          advisor_endorsed=adv, revisions=revisions)
                out["status"] = "no_action"
                return out

            # 5. 硬风控终审
            verdict = self.risk.check_open_plan(plan)
            # 把胜出分析师/委员会分带进 sized → trades 归因用（Phase 3 落库）
            verdict.sized["analyst"] = plan.get("analyst")
            verdict.sized["committee_score"] = plan.get("committee_score")
            verdict.kelly = getattr(self.risk, "last_kelly", None)
            winner_pk = self._winner_proposal_pk(decision, slot_pks)
            rw.write_risk({"passed": verdict.passed,
                           "failures": verdict.failures,
                           "warnings": verdict.warnings,
                           "sized": verdict.sized,
                           "kelly": getattr(self.risk, "last_kelly", None)},
                          proposal_pk=winner_pk)
            if not verdict.passed:
                rw.finish("risk_rejected", action="open",
                          reason="；".join(verdict.failures),
                          data_ok=1, symbols_ok=snap["symbols_ok"],
                          symbols_total=snap["symbols_total"],
                          equity=snap["equity"], hwm=snap["hwm"],
                          drawdown=snap["drawdown"], usdt_avail=snap["usdt_avail"],
                          open_positions=len(snap["positions"]),
                          duration_sec=round(time.time() - t0, 2),
                          intent="place",
                          final_action="revise" if revisions else "steady",
                          regime=regime, advisor_endorsed=adv,
                          revisions=revisions)
                out.update({"status": "risk_rejected", "failures": verdict.failures})
                if any("熔断" in f for f in verdict.failures):
                    w.write_event(self.store, self.env.name, "circuit_breaker",
                                  "回撤熔断生效，禁止开新仓", level="warn", round_pk=rw.pk)
                    hook_trigger("circuit_breaker", {"kind": "circuit_breaker",
                                 "level": "warn",
                                 "message": "；".join(verdict.failures)})
                else:
                    hook_trigger("risk_rejected", {"kind": "risk_rejected",
                                 "level": "info",
                                 "inst_id": verdict.sized.get("instId"),
                                 "direction": verdict.sized.get("direction"),
                                 "score": verdict.sized.get("committee_score"),
                                 "message": "；".join(verdict.failures)})
                return out

            # 6. 执行
            if self.executing:
                execution = self._execute_open(verdict.sized, rw)
                status = execution["status"]
            else:
                execution = self._dry_run_execute(verdict.sized)
                status = "planned"  # 非执行环境：意图已记录，不下单
            rw.finish(status, action="open",
                      reason=(decision.get("plan") or {}).get("reason"),
                      data_ok=1, symbols_ok=snap["symbols_ok"],
                      symbols_total=snap["symbols_total"],
                      equity=snap["equity"], hwm=snap["hwm"],
                      drawdown=snap["drawdown"], usdt_avail=snap["usdt_avail"],
                      open_positions=len(snap["positions"]),
                      duration_sec=round(time.time() - t0, 2),
                      intent="place",
                      final_action="deploy" if status == "opened" else "place",
                      regime=regime, advisor_endorsed=adv, revisions=revisions)
            out.update({"status": status, "execution": execution})
            if status == "opened":
                hook_trigger("order_filled", {"kind": "order_filled", "level": "info",
                             "inst_id": verdict.sized["instId"],
                             "direction": verdict.sized["direction"],
                             "contracts": verdict.sized["contracts"],
                             "avg_px": out.get("execution", {}).get("avg_fill_px"),
                             "stop": stop, "target": tp or None,
                             "equity": (self.last_snapshot or {}).get("equity"),
                             "message": "成交，交易所保护单（止损/止盈）已挂"})
        except Exception as e:  # noqa: BLE001 —— 单轮失败不影响下一轮
            self.log.error("本轮异常：%s\n%s", e, traceback.format_exc())
            try:
                rw.finish("error", error=f"{type(e).__name__}: {e}",
                          duration_sec=round(time.time() - t0, 2))
            except Exception:  # noqa: BLE001
                pass
            out["status"] = "error"
            out["error"] = str(e)
        finally:
            self._busy = False
        snap = self.last_snapshot or {}
        hook_trigger("round_done", {"kind": "round_done", "level": "info",
                     "equity": snap.get("equity"),
                     "drawdown": snap.get("drawdown"),
                     "positions_n": len(snap.get("positions") or []),
                     "regime": out.get("regime"),
                     "message": f"status={out.get('status')}，{out.get('decision') or ''}"})
        return out

    @staticmethod
    def _winner_proposal_pk(decision, slot_pks):
        plan = decision.get("plan") or {}
        for a in decision.get("analysts") or []:
            if (a.get("action") == "open"
                    and (a.get("instId"), a.get("direction"))
                    == (plan.get("instId"), plan.get("direction"))):
                return slot_pks.get(a.get("slot"))
        return None

    # ────────────────────────── 持仓巡检 / 崩溃对账 ──────────────────────────

    def _patrol_positions(self, snap, rw):
        """开轮对账（只有会真实下单的环境才动账）：
        0. 清理已平仓元数据；1. 盘中成交补记（工作单等待期外/重启间隙
        成交 → 补 trades 行）；2. 工作单生命周期（超龄撤换、撤残余）；
        3. 撤孤儿挂单；4. 补挂缺失保护单。"""
        meta = self.risk.state.get_positions_meta()
        working = self.risk.state.get_working_orders()
        live = {p["instId"]: p for p in snap["positions"]}

        changed = False
        for inst_id in list(meta.keys()):
            if inst_id not in live:
                self.log.info("巡检：%s 仓位已离场，回填交易记录并清理元数据", inst_id)
                reconcile_closed_trade(self, snap, rw, inst_id, meta)
                working.pop(inst_id, None)
                changed = True

        if not self.executing:
            if changed:
                self.risk.state.set_positions_meta(meta)
                self.risk.state.set_working_orders(working)
            return

        # ── 盘中成交/重启间隙成交补记（必须在撤残余之前）──
        # 交易所有仓位但本地没有 trades 行：工作单在等待期外成交、或成交
        # 瞬间进程重启，会漏掉 _execute_open 的即时成交记账。这里统一补：
        # 建 trades 行 + 入场单回填 filled + 已有保护单挂到 trade。
        # 先于工作单生命周期跑，才能从工作单名册取到 stop/analyst。
        for inst_id, p in live.items():
            m = meta.get(inst_id) or {}
            if m.get("trade_pk"):
                continue
            wo = working.get(inst_id)
            # 幂等防护：meta 丢了 trade_pk 但 open 行已在（如崩溃窗口半截
            # 记账）——只把 meta/订单重新挂链，绝不重复建行
            dup = self.store.query_one(
                "SELECT id FROM trades WHERE env=? AND inst_id=? "
                "AND status='open' ORDER BY id DESC LIMIT 1",
                (self.env.name, inst_id))
            if dup:
                m["trade_pk"] = dup["id"]
                meta[inst_id] = m
                changed = True
                continue
            sized = {"instId": inst_id, "direction": p["direction"],
                     "stop_loss": m.get("stop") or (wo or {}).get("stop"),
                     "target": m.get("target") or (wo or {}).get("target"),
                     "rr": None, "risk_usdt": None,
                     "analyst": (wo or {}).get("analyst") or m.get("analyst"),
                     "committee_score": m.get("committee_score")}
            pk = open_trade_row(self, rw, sized, p["contracts"], p["avg_px"])
            m["trade_pk"] = pk
            meta[inst_id] = m
            if wo:
                w.mark_order_filled(self.store, self.env.name, wo["ord_id"],
                                    p["contracts"], p["avg_px"], trade_pk=pk)
            else:
                # 工作单名册已丢（如上次巡检已 pop）：按交易所侧入场单回填
                row = self.store.query_one(
                    "SELECT exch_ord_id FROM orders WHERE env=? AND inst_id=? "
                    "AND kind='entry' AND state='live' ORDER BY id DESC LIMIT 1",
                    (self.env.name, inst_id))
                if row and row["exch_ord_id"]:
                    w.mark_order_filled(self.store, self.env.name,
                                        row["exch_ord_id"], p["contracts"],
                                        p["avg_px"], trade_pk=pk)
            if m.get("algo_id"):
                w.link_protect_to_trade(self.store, self.env.name,
                                        m["algo_id"], pk)
            changed = True
            self.log.info("巡检：%s 仓位存在但无 trades 行，已补记（pk=%s）",
                          inst_id, pk)
        if changed:
            self.risk.state.set_positions_meta(meta)
            self.risk.state.set_working_orders(working)

        # ── 工作单生命周期 ──
        now = time.time()
        requote = int(getattr(self.cfg, "REQUOTE_AGE_SEC", 900) or 900)
        for inst_id in list(working.keys()):
            wo = working[inst_id]
            if inst_id in live:
                try:  # 已成交 → 撤掉可能残余的工作单
                    self.client.cancel_order(inst_id, wo["ord_id"])
                except Exception:  # noqa: BLE001
                    pass
                working.pop(inst_id)
                changed = True
                continue
            if now - wo.get("placed_ts", 0) > requote:
                try:
                    self.client.cancel_order(inst_id, wo["ord_id"])
                    self.log.warning("巡检：报价超龄（>%ds 未成交），撤单 %s（%s）"
                                     "等待重新评估", requote, wo["ord_id"][:8], inst_id)
                    working.pop(inst_id)
                    changed = True
                except Exception as e:  # noqa: BLE001
                    self.log.warning("巡检：撤工作单失败 %s：%s", wo["ord_id"][:8], e)

        # 撤残留孤儿挂单：不在工作单名册里的才算残留
        working_ids = {w["ord_id"] for w in working.values()}
        for o in self.client.get_pending_orders():
            if o["instId"] in self.cfg.SYMBOLS and str(o["ordId"]) not in working_ids:
                try:
                    self.client.cancel_order(o["instId"], o["ordId"])
                    self.log.warning("巡检：撤销残留挂单 %s（%s）", o["ordId"][:8], o["instId"])
                    rw.write_order(self.env.name, o["instId"], "entry", "post_only",
                                   exch_ord_id=str(o["ordId"]), side=o.get("side"),
                                   state="canceled", note="巡检撤销残留挂单")
                except Exception as e:  # noqa: BLE001
                    self.log.warning("巡检：撤单失败 %s：%s", o["ordId"][:8], e)

        if changed:
            self.risk.state.set_positions_meta(meta)
            self.risk.state.set_working_orders(working)

        for inst_id, p in live.items():
            try:
                existing = self.client.get_pending_stop_losses(inst_id)
            except Exception as e:  # noqa: BLE001
                self.log.error("巡检：读取 %s 保护单失败（不据此判定裸仓）：%s", inst_id, e)
                continue
            if existing:
                continue
            m = meta.get(inst_id) or {}
            recorded_algo = m.get("algo_id")
            if recorded_algo:
                try:
                    d = self.client.get_algo_order_details(recorded_algo)
                    if d and str(d.get("state")) in ("live", "effective", "running", "pause"):
                        self.log.info("巡检：%s 列表为空但 algoId=%s 状态=%s 仍有效，不重复补挂",
                                      inst_id, recorded_algo, d.get("state"))
                        continue
                except Exception as e:  # noqa: BLE001
                    self.log.warning("巡检：%s 对账 algoId=%s 失败（%s），按缺失处理",
                                     inst_id, recorded_algo, e)
            stop = m.get("stop")
            if not stop:
                atr = ((snap.get("factors") or {}).get(inst_id) or {}).get("atr")
                if not atr:
                    self.log.error("巡检：%s 止损缺失且无法估算，请人工检查！", inst_id)
                    w.write_event(self.store, self.env.name, "naked_position",
                                  f"{inst_id} 止损缺失且无法自动估算",
                                  level="critical", inst_id=inst_id, round_pk=rw.pk)
                    continue
                stop = (p["avg_px"] - 1.5 * atr if p["direction"] == "long"
                        else p["avg_px"] + 1.5 * atr)
            tp = m.get("target")
            try:
                algo_id = self.client.place_stop_loss(inst_id, p["direction"],
                                                      p["contracts"], stop, tp_px=tp)
                meta[inst_id] = {**m, "algo_id": algo_id, "stop": stop, "target": tp}
                if m.get("trade_pk"):  # 补记过 trade 的仓位：保护单挂到同一 trade
                    w.link_protect_to_trade(self.store, self.env.name,
                                            str(algo_id), m["trade_pk"])
                changed = True
                rw.write_order(self.env.name, inst_id, "protect",
                               "oco" if tp else "conditional",
                               exch_algo_id=str(algo_id),
                               side="sell" if p["direction"] == "long" else "buy",
                               sz=p["contracts"], sl_trigger_px=stop,
                               tp_trigger_px=tp, state="live",
                               note="巡检补挂保护单")
                w.write_event(self.store, self.env.name, "stop_reattached",
                              f"{inst_id} 保护单缺失，已补挂 止损@{stop:.4g}"
                              + (f" 止盈@{tp:.4g}" if tp else ""),
                              level="warn", inst_id=inst_id, round_pk=rw.pk)
            except Exception as e:  # noqa: BLE001
                self.log.error("巡检：%s 补挂止损失败：%s —— 仓位当前无保护，请人工处理！",
                               inst_id, e)
                w.write_event(self.store, self.env.name, "naked_position",
                              f"{inst_id} 补挂止损失败：{e}",
                              level="critical", inst_id=inst_id, round_pk=rw.pk)
        if changed:
            self.risk.state.set_positions_meta(meta)

    # ────────────────────────── 执行 ──────────────────────────

    def _dry_run_execute(self, sized):
        """非执行环境：计算并记录将要挂的单，不触碰交易所下单接口。"""
        inst_id, direction = sized["instId"], sized["direction"]
        side = "buy" if direction == "long" else "sell"
        try:
            px = self.client.maker_price(inst_id, side)
        except Exception:  # noqa: BLE001
            px = None
        self.log.info(
            "【planned】将挂 Maker %s 单：%s %s 张 @ %s；成交后保护单 止损@%s%s"
            "（名义 %.0f U，单笔风险 %.2f%%）",
            side, inst_id, sized["contracts"], px, sized["stop_loss"],
            f" 止盈@{sized.get('target'):.4g}" if sized.get("target") else "",
            sized["notional_usdt"], sized["risk_pct"] * 100)
        return {"status": "simulated", "would": {
            "instId": inst_id, "direction": direction,
            "contracts": sized["contracts"], "maker_px": px,
            "stop_loss": sized["stop_loss"], "target": sized.get("target"),
            "risk_usdt": sized["risk_usdt"], "risk_pct": sized["risk_pct"],
            "notional_usdt": sized["notional_usdt"]}}

    def _execute_open(self, sized, rw):
        """真实执行：post_only 限价 → 等成交 → 交易所保护单（止损[+止盈]）。"""
        inst_id = sized["instId"]
        direction = sized["direction"]
        contracts = sized["contracts"]
        stop = sized["stop_loss"]
        tp = sized.get("target")
        side = "buy" if direction == "long" else "sell"

        try:
            self.client.set_leverage(inst_id, self.cfg.LEVERAGE)
        except Exception as e:  # noqa: BLE001
            self.log.warning("设置杠杆失败（继续，可能已是目标杠杆）：%s", e)

        ord_id = self.client.place_maker_limit(inst_id, side, contracts)
        if not ord_id:
            # post_only 被交易所拒（挂单瞬间会吃单）——竞争失败，按未成交处理
            self.log.info("post_only 挂单被拒，本轮按未成交处理")
            return {"status": "no_fill", "note": "post_only rejected",
                    "contracts_planned": contracts, "stop_px": stop,
                    "direction": direction}
        self.log.info("已挂 Maker 限价单 %s %s %s 张 @订单 %s", inst_id, side,
                      contracts, ord_id)
        rw.write_order(self.env.name, inst_id, "entry", "post_only",
                       exch_ord_id=str(ord_id), side=side,
                       px=sized.get("entry_ref"), sz=contracts, state="live")
        hook_trigger("order_placed", {"kind": "order_placed", "level": "info",
                     "inst_id": inst_id, "direction": direction,
                     "contracts": contracts, "px": sized.get("entry_ref"),
                     "stop": stop, "target": tp or None,
                     "notional_usdt": sized.get("notional_usdt"),
                     "risk_usdt": sized.get("risk_usdt"),
                     "message": f"Maker 限价单已挂（post_only），"
                                f"等待成交（最长 {self.cfg.ORDER_TIMEOUT_SEC}s）"})
        execution = {"ord_id": ord_id, "contracts_planned": contracts,
                     "stop_px": stop, "tp_px": tp, "direction": direction}

        # 等待成交
        deadline = time.time() + self.cfg.ORDER_TIMEOUT_SEC
        order = None
        while time.time() < deadline:
            time.sleep(2)
            order = self.client.get_order(inst_id, ord_id)
            if order["state"] in ("filled", "canceled"):
                break
            if order["state"] == "partially_filled" and order["acc_fill_sz"] >= contracts:
                break
        if order is None:
            order = self.client.get_order(inst_id, ord_id)

        execution["fill_state"] = order["state"]
        execution["filled_contracts"] = order["acc_fill_sz"]
        execution["avg_fill_px"] = order["avg_px"]

        filled = order["acc_fill_sz"]
        if filled <= 0:
            # 未成交：报价保留在工作面上覆盖整轮（挂单策略核心），
            # 超龄撤换与盘中成交补记由巡检（risk_tick / 下轮开头）处理
            working = self.risk.state.get_working_orders()
            working[inst_id] = {"ord_id": str(ord_id), "side": side,
                                "px": order.get("px") or sized.get("entry_ref"),
                                "sz": contracts,
                                "placed_ts": time.time(),
                                "stop": stop, "target": tp,
                                "analyst": sized.get("analyst")}
            self.risk.state.set_working_orders(working)
            execution["status"] = "working"
            execution["working"] = True
            self.log.info("限价单未即时成交，保留为工作单 %s（%s %s @%s）覆盖盘口",
                          ord_id, inst_id, side, order.get("px"))
            return execution
        if order["state"] in ("live", "partially_filled"):
            try:
                self.client.cancel_order(inst_id, ord_id)  # 部分成交：撤掉残余
            except Exception:  # noqa: BLE001
                pass

        if filled <= 0:
            execution["status"] = "no_fill"
            self.log.info("限价单未成交，已撤单")
            return execution

        # 成交了 → 立刻挂交易所保护单（有目标价时 OCO，否则纯止损）
        execution["status"] = self._attach_protect(inst_id, direction, filled,
                                                   stop, tp, execution, rw)
        if execution["status"] == "opened":
            trade_pk = open_trade_row(self, rw, sized, filled,
                                      order["avg_px"] or sized.get("entry_ref"))
            execution["trade_pk"] = trade_pk
            # 对账闭环：入场单回填为 filled 并与 trade 关联；保护单挂到同一 trade
            w.mark_order_filled(self.store, self.env.name, ord_id, filled,
                                order["avg_px"] or sized.get("entry_ref"),
                                trade_pk)
            if execution.get("stop_algo_id"):
                w.link_protect_to_trade(self.store, self.env.name,
                                        execution["stop_algo_id"], trade_pk)
            meta = self.risk.state.get_positions_meta()
            meta[inst_id] = {
                "direction": direction, "stop": stop, "target": tp,
                "contracts": filled, "avg_px": order["avg_px"],
                "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "algo_id": execution.get("stop_algo_id"),
                "analyst": sized.get("analyst"),
                "committee_score": sized.get("committee_score"),
                "trade_pk": trade_pk,
            }
            self.risk.state.set_positions_meta(meta)
        return execution

    def _attach_protect(self, inst_id, direction, contracts, stop_px, tp_px,
                        execution, rw):
        """挂交易所保护单；失败则市价平仓兜底（绝不允许无止损仓位）。"""
        try:
            algo_id = self.client.place_stop_loss(inst_id, direction, contracts,
                                                  stop_px, tp_px=tp_px)
            execution["stop_algo_id"] = algo_id
            self.log.info("交易所保护单已挂：%s %s %s 张 止损@%s%s（algoId=%s）",
                          inst_id, direction, contracts, stop_px,
                          f" 止盈@{tp_px:.4g}" if tp_px else "", algo_id)
            rw.write_order(self.env.name, inst_id, "protect",
                           "oco" if tp_px else "conditional",
                           exch_algo_id=str(algo_id),
                           side="sell" if direction == "long" else "buy",
                           sz=contracts, sl_trigger_px=stop_px,
                           tp_trigger_px=tp_px, state="live")
            return "opened"
        except Exception as e:  # noqa: BLE001
            self.log.error("挂保护单失败（%s），市价平仓兜底！", e)
            try:
                self.client.close_position_market(inst_id, direction)
                execution["emergency_closed"] = True
                rw.write_order(self.env.name, inst_id, "exit", "market",
                               side="sell" if direction == "long" else "buy",
                               sz=contracts, state="filled",
                               note=f"保护单挂设失败，市价兜底平仓：{e}")
            except Exception as e2:  # noqa: BLE001
                self.log.critical("兜底平仓也失败：%s —— 请人工处理！", e2)
                execution["emergency_close_failed"] = True
                w.write_event(self.store, self.env.name, "naked_position",
                              f"{inst_id} 兜底平仓也失败：{e2}",
                              level="critical", inst_id=inst_id)
            return "stop_failed_closed"

    # ────────────────────────── 循环 ──────────────────────────

    def run(self, interval_sec=None, max_rounds=None, on_round=None):
        """双层调度：cognition round（默认 1h）+ 机械 risk tick（默认 5min），
        同线程串行；撞点时 round 先跑（它开头本来就含一次巡检，不重复）。"""
        interval = interval_sec or getattr(self.cfg, "LOOP_INTERVAL_SEC", 3600)
        tick_sec = int(getattr(self.cfg, "RISK_TICK_SEC", 300) or 0)
        self.log.info("交易循环启动：round 每 %ds，tick 每 %ds，env=%s executing=%s",
                      interval, tick_sec, self.env.name, self.executing)
        next_round_at = time.time()      # 立即跑第一轮
        next_tick_at = time.time() + (tick_sec or interval)
        n = 0
        try:
            while max_rounds is None or n < max_rounds:
                if self.paused:
                    time.sleep(2)
                    continue
                now = time.time()
                if now >= next_round_at:
                    n += 1
                    self.current_step = "running"
                    try:
                        result = self.run_round()
                    finally:
                        self.current_step = None
                    if on_round:
                        try:
                            on_round(result)
                        except Exception:  # noqa: BLE001
                            self.log.debug("on_round 回调失败", exc_info=True)
                    next_round_at = time.time() + interval
                    # round 已含巡检：tick 顺延，避免重复巡检
                    next_tick_at = max(next_tick_at, time.time() + tick_sec)
                    self.next_round_ts = next_round_at
                elif tick_sec and now >= next_tick_at:
                    try:
                        self.risk_tick()
                    except Exception:  # noqa: BLE001
                        self.log.error("risk tick 异常：%s", traceback.format_exc())
                    next_tick_at = time.time() + tick_sec
                else:
                    nxt = min(next_round_at, next_tick_at)
                    chunk = min(2.0, max(0.1, nxt - time.time()))
                    time.sleep(chunk)
                    if self._run_now.is_set():
                        self._run_now.clear()
                        next_round_at = time.time()
        except KeyboardInterrupt:
            self.log.info("收到中断，交易循环停止")

    def risk_tick(self):
        """5 分钟机械风控 tick（不花 LLM）：巡检保护单、移动/时间止损、
        回撤阶梯、权益采样。只做 3 类只读调用 + 必要时的撤挂单。
        权益必须先采样——阶梯判断用新鲜数字，不能用最多 1 小时前的快照。"""
        if self.paused:
            return
        try:  # 先采样权益并抬升高水位（阶梯与曲线都用这份新鲜值）
            eq = self.client.get_equity()
            hwm, dd = self.risk.state.update_hwm(eq["total_eq"])
            equity = eq["total_eq"]
        except Exception:  # noqa: BLE001
            equity = hwm = dd = None
            self.log.warning("tick 权益采样失败，本轮阶梯跳过", exc_info=True)
        positions = self.client.get_positions()
        tick_rw = _TickWriter(self.store)
        # ATR 用最近一轮的因子值（避免每 tick 重复拉 K 线）
        factors = {}
        for p in positions:
            row = self.store.query_one(
                "SELECT atr, price FROM round_factors WHERE inst_id=? AND ok=1 "
                "ORDER BY round_pk DESC LIMIT 1", (p["instId"],))
            if row:
                factors[p["instId"]] = {"atr": row["atr"], "price": row["price"]}
        snap = {"positions": positions, "factors": factors}
        self._patrol_positions(snap, tick_rw)
        manage_open_positions(self, snap, tick_rw)
        # is not None 而非真值判断：权益恰好 0.0（爆仓级回撤）正是最该触发末档的时刻
        if equity is not None and hwm is not None and dd is not None:
            self._evaluate_drawdown_ladder(tick_rw, equity=equity, hwm=hwm)
            w.write_equity(self.store, self.env.name, time.time(),
                           equity, hwm, dd, eq["usdt_avail"], None,
                           len(positions))
        # 面板读 last_snapshot——tick 把新鲜权益/持仓刷进去，否则两轮之间
        # （最长 1 小时）面板显示的是冻结的旧数（实测曾虚报 170 U）
        if self.last_snapshot is None:
            self.last_snapshot = {}
        if equity is not None:
            self.last_snapshot.update({
                "equity": equity, "hwm": hwm, "drawdown": dd,
                "usdt_avail": eq["usdt_avail"]})
        try:  # 移动止损/巡检可能刚改过持仓，取管理动作之后的
            self.last_snapshot["positions"] = self.client.get_positions()
        except Exception:  # noqa: BLE001
            self.last_snapshot["positions"] = positions
        ticks = int(self.store.state_get(self.env.name, "risk_ticks") or 0) + 1
        self.store.state_set(self.env.name, "risk_ticks", ticks)
        self.store.state_set(self.env.name, "last_risk_tick_ts", time.time())

    def _evaluate_drawdown_ladder(self, rw, equity, hwm):
        """回撤阶梯（升档立即生效；降档需回撤 < 当前档阈值 80%，防抖动）。
        equity/hwm 由调用方传入（tick 每次新采样，round 用当轮快照）。
        注意用 is not None：权益 0.0 是最该触发末档的状态，不能当 falsy 跳过。"""
        ladder = list(getattr(self.cfg, "DRAWDOWN_LADDER", []) or [])
        if not ladder or equity is None or hwm is None or hwm <= 0:
            return
        drawdown = (hwm - equity) / hwm
        rung = self.risk.state.get_rung()

        target = 0
        for i, t in enumerate(ladder):
            if drawdown >= t.get("dd", 1):
                target = i + 1
        if target > rung:  # 升档立即
            self.risk.state.set_rung(target)
            tier = ladder[target - 1]
            msg = (f"回撤 {drawdown:.1%} 触发第 {target} 档"
                   f"（阈值 {tier.get('dd'):.0%}，risk_mult={tier.get('risk_mult')}，"
                   f"allow_open={tier.get('allow_open')}）")
            w.write_event(self.store, self.env.name, "circuit_breaker", msg,
                          level="warn", round_pk=getattr(rw, "pk", None))
            hook_trigger("circuit_breaker", {"kind": "circuit_breaker",
                         "level": "warn", "message": msg})
            self.log.warning("回撤阶梯：%s", msg)
            if tier.get("flatten"):
                self._flatten_all(rw)
        elif target < rung:  # 降档滞回
            cur_threshold = ladder[rung - 1].get("dd", 1)
            if drawdown < cur_threshold * 0.8:
                self.risk.state.set_rung(target)
                w.write_event(self.store, self.env.name, "circuit_breaker",
                              f"回撤回落至 {drawdown:.1%}（低于 {cur_threshold:.0%} "
                              f"的 80%），降档到第 {target} 档", level="info",
                              round_pk=getattr(rw, "pk", None))
                self.log.info("回撤阶梯降档：%.1f%% → 第 %d 档", drawdown * 100,
                              target)

    def _flatten_all(self, rw):
        """末档：市价平掉全部持仓 + 撤全部挂单 + 停机待人工恢复。"""
        self.log.critical("回撤阶梯末档：全平并停机待人工恢复！")
        for o in self.client.get_pending_orders():
            try:
                self.client.cancel_order(o["instId"], o["ordId"])
            except Exception:  # noqa: BLE001
                pass
        for p in self.client.get_positions():
            try:
                self.client.close_position_market(p["instId"], p["direction"])
            except Exception as e:  # noqa: BLE001
                self.log.critical("强平 %s 失败：%s —— 请人工处理！", p["instId"], e)
        w.write_event(self.store, self.env.name, "circuit_breaker",
                      "末档触发：全部持仓已市价平掉、挂单已撤，循环暂停待人工恢复",
                      level="critical")
        hook_trigger("circuit_breaker", {"kind": "circuit_breaker",
                     "level": "critical", "message": "末档全平并停机"})
        self.set_paused(True, "回撤阶梯末档")
