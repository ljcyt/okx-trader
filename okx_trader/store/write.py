# -*- coding: utf-8 -*-
"""交易循环 → SQLite 的全部写入路径。

RoundWriter 一次封装一个 round_pk，循环里各步骤按序落库。
写入层一次性把「过滤后提案下标 → proposal 行 id」解析掉，歧义不进数据库。
"""
import json
import re
import time


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


class RoundWriter:
    def __init__(self, store, round_pk: int):
        self.store = store
        self.pk = round_pk

    @classmethod
    def open(cls, store, round_id, ts, env, executing, llm_mode):
        """先建 rounds 行拿到 round_pk——后续所有表都挂在它下面。"""
        pk = store.execute(
            "INSERT INTO rounds(round_id, ts, env, executing, llm_mode, status) "
            "VALUES (?,?,?,?,?, 'running')",
            (round_id, ts, env, int(executing), llm_mode))
        return cls(store, pk)

    def finish(self, status, action=None, reason=None, data_ok=1, symbols_ok=0,
               symbols_total=0, equity=None, hwm=None, drawdown=None,
               usdt_avail=None, open_positions=0, duration_sec=None, error=None):
        self.store.execute(
            "UPDATE rounds SET status=?, action=?, reason=?, data_ok=?, "
            "symbols_ok=?, symbols_total=?, equity=?, hwm=?, drawdown=?, "
            "usdt_avail=?, open_positions=?, duration_sec=?, error=? WHERE id=?",
            (status, action, reason, int(data_ok), symbols_ok, symbols_total,
             equity, hwm, drawdown, usdt_avail, open_positions, duration_sec,
             error, self.pk))

    # ── 因子快照（完整 report_json + report_text）────────────────────

    def write_factors(self, inst_id, report, report_text=None, err=None):
        ok = report is not None
        row = {"inst_id": inst_id, "ok": int(ok), "err": err,
               "bar": None, "bar_ts": None, "price": None, "ema20": None,
               "ema60": None, "rsi14": None, "atr": None, "atr_pct": None,
               "macd_dif": None, "macd_dea": None, "macd_hist": None,
               "funding_rate": None, "vol_ratio": None, "trend": None,
               "structure": None, "price_vs_boll": None, "pattern": None,
               "obi": None, "oi": None, "oi_delta_pct": None,
               "ls_ratio": None, "taker_ratio": None,
               "report_json": _json(report) if report else "{}",
               "report_text": report_text}
        if ok:
            macd = report.get("macd") or {}
            row.update({
                "bar": report.get("bar"), "bar_ts": report.get("ts"),
                "price": report.get("price"), "ema20": report.get("ema20"),
                "ema60": report.get("ema60"), "rsi14": report.get("rsi14"),
                "atr": report.get("atr"), "atr_pct": report.get("atr_pct"),
                "macd_dif": macd.get("dif"), "macd_dea": macd.get("dea"),
                "macd_hist": macd.get("hist"),
                "funding_rate": report.get("funding_rate"),
                "vol_ratio": report.get("vol_ratio"),
                "trend": report.get("trend"),
                "structure": report.get("structure"),
                "price_vs_boll": report.get("price_vs_boll"),
                "pattern": report.get("pattern"),
                "obi": report.get("obi"), "oi": report.get("oi"),
                "oi_delta_pct": report.get("oi_delta_pct"),
                "ls_ratio": report.get("ls_ratio"),
                "taker_ratio": report.get("taker_ratio"),
            })
        self.store.execute(
            "INSERT INTO round_factors(round_pk, inst_id, ok, err, bar, bar_ts, "
            "price, ema20, ema60, rsi14, atr, atr_pct, macd_dif, macd_dea, "
            "macd_hist, funding_rate, vol_ratio, trend, structure, "
            "price_vs_boll, pattern, obi, oi, oi_delta_pct, ls_ratio, "
            "taker_ratio, report_json, report_text) "
            "VALUES (:pk, :inst_id, :ok, :err, :bar, :bar_ts, :price, :ema20, "
            ":ema60, :rsi14, :atr, :atr_pct, :macd_dif, :macd_dea, :macd_hist, "
            ":funding_rate, :vol_ratio, :trend, :structure, :price_vs_boll, "
            ":pattern, :obi, :oi, :oi_delta_pct, :ls_ratio, :taker_ratio, "
            ":report_json, :report_text)",
            {"pk": self.pk, **row})

    # ── 委员会（proposals 含弃权者；judging.idx 解析成 proposal_pk）────

    def write_committee(self, decision, threshold):
        """decision 为 committee.decide() 的返回。返回 {slot: proposal_pk}。"""
        analysts = decision.get("analysts") or []
        # 未过滤提案列表（含弃权）：slot = 列表下标
        slot_pks = {}
        for slot, a in enumerate(analysts):
            pk = self.store.execute(
                "INSERT INTO proposals(round_pk, slot, analyst, style, action, "
                "inst_id, direction, stop_loss, entry_hint, confidence, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.pk, slot, a.get("analyst"), a.get("style"),
                 a.get("action"), a.get("instId"), a.get("direction"),
                 a.get("stop_loss"), a.get("entry_hint"),
                 a.get("confidence"), a.get("reason")))
            slot_pks[slot] = pk

        # 过滤后下标 → slot：judging[].idx 是对 action=='open' 的提案列表的序号
        open_slots = [s for s, a in enumerate(analysts) if a.get("action") == "open"]
        scoreboard = {s["analyst"]: s for s in decision.get("scoreboard", [])}
        plan = decision.get("plan") or {}
        winner_key = (plan.get("instId"), plan.get("direction"))
        rows = decision.get("judging", {}).get("rows", []) \
            if isinstance(decision.get("judging"), dict) else decision.get("judging", [])
        for j in rows:
            idx = j.get("idx")
            if not isinstance(idx, int) or idx < 0 or idx >= len(open_slots):
                continue
            slot = open_slots[idx]
            self.store.execute(
                "INSERT INTO judge_scores(round_pk, proposal_pk, judge, score, "
                "approved, concerns) VALUES (?,?,?,?,?,?)",
                (self.pk, slot_pks[slot], j.get("judge"),
                 j.get("score"), int(bool(j.get("approved"))),
                 j.get("concerns")))

        # 聚合结果回填 proposals + is_winner
        for slot, a in enumerate(analysts):
            sb = scoreboard.get(a.get("analyst")) or {}
            is_winner = int(bool(
                a.get("action") == "open"
                and (a.get("instId"), a.get("direction")) == winner_key))
            self.store.execute(
                "UPDATE proposals SET avg_score=?, votes_for=?, votes_total=?, "
                "qualify=?, is_winner=? WHERE id=?",
                (sb.get("avg_score"),
                 int(str(sb.get("votes", "0/0")).split("/")[0] or 0),
                 int(str(sb.get("votes", "0/0")).split("/")[-1] or 0),
                 int(bool(sb.get("qualify"))), is_winner, slot_pks[slot]))
        return slot_pks

    # ── 风控结论 ────────────────────────────────────────────────────

    def write_risk(self, verdict, proposal_pk=None):
        failures = verdict.get("failures", [])
        m = re.match(r"(R\d)", failures[0]) if failures else None
        sized = verdict.get("sized") or {}
        self.store.execute(
            "INSERT INTO risk_verdicts(round_pk, proposal_pk, passed, rule_code, "
            "first_failure, failures_json, warnings_json, inst_id, direction, "
            "contracts, entry_ref, stop_loss, target, rr, target_source, "
            "notional_usdt, risk_usdt, risk_pct, atr, leverage_after) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.pk, proposal_pk, int(bool(verdict.get("passed"))),
             m.group(1) if m else None,
             failures[0] if failures else None,
             _json(failures), _json(verdict.get("warnings", [])),
             sized.get("instId"), sized.get("direction"),
             sized.get("contracts"), sized.get("entry_ref"),
             sized.get("stop_loss"), sized.get("target"), sized.get("rr"),
             sized.get("target_source"), sized.get("notional_usdt"),
             sized.get("risk_usdt"), sized.get("risk_pct"), sized.get("atr"),
             sized.get("leverage_after")))

    # ── 订单 / 权益 / 事件 / run_state ──────────────────────────────

    def write_order(self, env, inst_id, kind, ord_type, exch_ord_id=None,
                    exch_algo_id=None, cl_ord_id=None, side=None, pos_side=None,
                    px=None, sz=None, sl_trigger_px=None, tp_trigger_px=None,
                    state=None, filled_sz=0, avg_px=None, note=None, raw=None,
                    trade_pk=None):
        return self.store.execute(
            "INSERT INTO orders(round_pk, trade_pk, env, inst_id, kind, ord_type, "
            "exch_ord_id, exch_algo_id, cl_ord_id, side, pos_side, px, sz, "
            "sl_trigger_px, tp_trigger_px, state, filled_sz, avg_px, created_ts, "
            "updated_ts, note, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?)",
            (self.pk, trade_pk, env, inst_id, kind, ord_type, exch_ord_id,
             exch_algo_id, cl_ord_id, side, pos_side, px, sz, sl_trigger_px,
             tp_trigger_px, state, filled_sz, avg_px, time.time(), time.time(),
             note, _json(raw) if raw else None))

    def write_equity(self, env, ts, equity, hwm, drawdown, usdt_avail=None,
                     upl=None, open_positions=0):
        self.store.execute(
            "INSERT OR IGNORE INTO equity_curve(env, ts, round_pk, equity, hwm, "
            "drawdown, usdt_avail, upl, open_positions) VALUES (?,?,?,?,?,?,?,?,?)",
            (env, ts, self.pk, equity, hwm, drawdown, usdt_avail, upl,
             open_positions))

    def write_event(self, env, kind, message, level="info", inst_id=None,
                    detail=None):
        self.store.execute(
            "INSERT INTO app_events(ts, env, level, kind, inst_id, round_pk, "
            "message, detail_json) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), env, level, kind, inst_id, self.pk, message,
             _json(detail) if detail else None))


def write_event(store, env, kind, message, level="info", inst_id=None,
                round_pk=None, detail=None):
    """不挂在轮次上的事件（登录、环境切换、暂停……）。"""
    store.execute(
        "INSERT INTO app_events(ts, env, level, kind, inst_id, round_pk, "
        "message, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), env, level, kind, inst_id, round_pk, message,
         _json(detail) if detail else None))


def write_llm_call(store, round_pk, role, model, ok, err=None, latency_ms=None,
                   prompt_tokens=None, completion_tokens=None, raw_reply=None):
    store.execute(
        "INSERT INTO llm_calls(round_pk, role, model, ok, err, latency_ms, "
        "prompt_tokens, completion_tokens, raw_reply) VALUES (?,?,?,?,?,?,?,?,?)",
        (round_pk, role, model, int(ok), err, latency_ms, prompt_tokens,
         completion_tokens, raw_reply))
