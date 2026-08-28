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
import time
import traceback

from . import __version__
from .committee import Committee
from .env import ENVS, db_path, make_client, resolve_env
from .factors import build_factor_report, format_factor_report
from .risk import RiskManager
from .store import write as w
from .store.db import Store, init_db


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
        self.client = make_client(self.env, self.cfg, logger=self.log)
        self.risk = RiskManager(self.cfg, self.client,
                                RunState(self.store, self.env.name))
        self.committee = Committee(self.cfg, self.client, store=self.store,
                                   env=self.env.name)
        self.round_seq = 0
        self.paused = False
        w.write_event(self.store, self.env.name, "env_switch",
                      f"启动：env={self.env.name} executing={self.executing} "
                      f"v{__version__}", level="info")
        self.log.info("== %s ==（executing=%s）交易循环就绪",
                      self.env.name, self.executing)

    @staticmethod
    def _db_path():
        from .env import db_path
        p = db_path()
        import os
        init_db(p)
        return p

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
        # LLM 调用记账挂到本轮
        self.committee.llm.recorder = (
            lambda role, model, ok, err, lat, raw, _pk=rw.pk:
            w.write_llm_call(self.store, _pk, role, model, ok, err, lat, None, None, raw))

        out = {"round_id": round_id, "status": "error", "env": self.env.name,
               "executing": self.executing}
        try:
            # 1. 快照
            snap = self.take_snapshot()
            # 2. 持仓巡检/对账
            self._patrol_positions(snap, rw)
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
            if plan is None:
                rw.finish("no_action", reason=decision.get("reason"),
                          data_ok=1, symbols_ok=snap["symbols_ok"],
                          symbols_total=snap["symbols_total"],
                          equity=snap["equity"], hwm=snap["hwm"],
                          drawdown=snap["drawdown"], usdt_avail=snap["usdt_avail"],
                          open_positions=len(snap["positions"]),
                          duration_sec=round(time.time() - t0, 2))
                out["status"] = "no_action"
                return out

            # 5. 硬风控终审
            verdict = self.risk.check_open_plan(plan)
            # 把胜出分析师/委员会分带进 sized → trades 归因用（Phase 3 落库）
            verdict.sized["analyst"] = plan.get("analyst")
            verdict.sized["committee_score"] = plan.get("committee_score")
            winner_pk = self._winner_proposal_pk(decision, slot_pks)
            rw.write_risk({"passed": verdict.passed,
                           "failures": verdict.failures,
                           "warnings": verdict.warnings,
                           "sized": verdict.sized}, proposal_pk=winner_pk)
            if not verdict.passed:
                rw.finish("risk_rejected", action="open",
                          reason="；".join(verdict.failures),
                          data_ok=1, symbols_ok=snap["symbols_ok"],
                          symbols_total=snap["symbols_total"],
                          equity=snap["equity"], hwm=snap["hwm"],
                          drawdown=snap["drawdown"], usdt_avail=snap["usdt_avail"],
                          open_positions=len(snap["positions"]),
                          duration_sec=round(time.time() - t0, 2))
                out.update({"status": "risk_rejected", "failures": verdict.failures})
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
                      duration_sec=round(time.time() - t0, 2))
            out.update({"status": status, "execution": execution})
        except Exception as e:  # noqa: BLE001 —— 单轮失败不影响下一轮
            self.log.error("本轮异常：%s\n%s", e, traceback.format_exc())
            try:
                rw.finish("error", error=f"{type(e).__name__}: {e}",
                          duration_sec=round(time.time() - t0, 2))
            except Exception:  # noqa: BLE001
                pass
            out["status"] = "error"
            out["error"] = str(e)
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
        1. 撤残留孤儿挂单；2. 清理已平仓元数据；3. 给无保护持仓补挂止损。"""
        meta = self.risk.state.get_positions_meta()
        live = {p["instId"]: p for p in snap["positions"]}

        changed = False
        for inst_id in list(meta.keys()):
            if inst_id not in live:
                self.log.info("巡检：%s 仓位已离场，清理元数据（出场细节由成交对账回填）",
                              inst_id)
                meta.pop(inst_id)
                changed = True

        if not self.executing:
            if changed:
                self.risk.state.set_positions_meta(meta)
            return

        for o in self.client.get_pending_orders():
            if o["instId"] in self.cfg.SYMBOLS:
                try:
                    self.client.cancel_order(o["instId"], o["ordId"])
                    self.log.warning("巡检：撤销残留挂单 %s（%s）", o["ordId"][:8], o["instId"])
                    rw.write_order(self.env.name, o["instId"], "entry", "post_only",
                                   exch_ord_id=str(o["ordId"]), side=o.get("side"),
                                   state="canceled", note="巡检撤销残留挂单")
                except Exception as e:  # noqa: BLE001
                    self.log.warning("巡检：撤单失败 %s：%s", o["ordId"][:8], e)

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
            try:
                self.client.cancel_order(inst_id, ord_id)
                execution["canceled"] = True
            except Exception as e:  # noqa: BLE001
                order = self.client.get_order(inst_id, ord_id)
                filled = order["acc_fill_sz"]
                execution["fill_state"] = order["state"]
                execution["filled_contracts"] = filled
                execution["cancel_error"] = str(e)
                if filled <= 0:
                    execution["status"] = "no_fill"
                    return execution
        elif order["state"] in ("live", "partially_filled"):
            try:
                self.client.cancel_order(inst_id, ord_id)  # 撤掉未成交的剩余部分
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
            meta = self.risk.state.get_positions_meta()
            meta[inst_id] = {
                "direction": direction, "stop": stop, "target": tp,
                "contracts": filled, "avg_px": order["avg_px"],
                "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "algo_id": execution.get("stop_algo_id"),
                "analyst": sized.get("analyst"),
                "committee_score": sized.get("committee_score"),
                "round_id": execution.get("round_id"),
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
        """定时循环。on_round(result) 回调供 Web 层展示实时状态。"""
        interval = interval_sec or getattr(self.cfg, "LOOP_INTERVAL_SEC", 3600)
        self.log.info("交易循环启动：每 %ds 一轮%s，env=%s executing=%s",
                      interval,
                      f"，共 {max_rounds} 轮" if max_rounds else "，持续运行",
                      self.env.name, self.executing)
        n = 0
        try:
            while max_rounds is None or n < max_rounds:
                if self.paused:
                    time.sleep(2)
                    continue
                n += 1
                result = self.run_round()
                if on_round:
                    try:
                        on_round(result)
                    except Exception:  # noqa: BLE001
                        self.log.debug("on_round 回调失败", exc_info=True)
                if max_rounds is not None and n >= max_rounds:
                    break
                # 分片睡眠，暂停能尽快生效
                slept = 0
                while slept < interval and not self.paused:
                    time.sleep(min(5, interval - slept))
                    slept += min(5, interval - slept)
        except KeyboardInterrupt:
            self.log.info("收到中断，交易循环停止")
