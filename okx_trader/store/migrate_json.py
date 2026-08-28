# -*- coding: utf-8 -*-
"""旧 JSON 记录 → SQLite 的一次性迁移（`python -m okx_trader migrate`）。

规则（与改造文档一致）：
- 以 rounds.jsonl 为真相源（含单文件 JSON 缺的 no_action 轮），再扫 round_*.json 补漏；
  round_id 上 INSERT OR IGNORE 保证幂等。
- mode → (env, executing)：paper→(paper,0)、dry_run→(demo,0)、live→(demo,1)。
  旧标签 live 的含义是"真实 OKX 模拟盘"，映射到 demo，绝不能映射到 live。
- 因子：旧记录只有 9 个键 → ok=(value is not None)，填列，report_json 加
  "_partial": true，report_text 留 NULL——不合成缺失的证据。
- data_ok = any(f is not None)；前两条被追溯改写为 data_unavailable，并补
  app_events(kind='data_degraded')。
- 委员会：analysts[] → proposals（slot=下标）；judging[].idx 按同样过滤顺序解析成
  proposal_pk；is_winner 由 plan 的 instId+direction 确定；scoreboard 丢弃（冗余）。
- 风控：rule_code 用 re.match(r'(R\\d)', failures[0])。
- 执行：dry_run_planned 不写 orders——意图已在 risk_verdicts 里，合成订单会污染 PnL。
- 状态：state_paper.json → run_state('paper','equity_hwm')；无命名空间 state.json
  仅当 state_live.json 不存在时导入 env='demo' 并写 warn 事件。
- 收尾：data/rounds/ 改名 data/rounds_legacy/（若仍是原名）。
"""
import glob
import json
import os
import re
import time

from .db import Store, init_db
from .write import RoundWriter, write_event

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

MODE_MAP = {"paper": ("paper", 0), "dry_run": ("demo", 0), "live": ("demo", 1)}
FACTOR_KEYS = ("price", "trend", "rsi14", "atr", "atr_pct", "funding_rate",
               "pattern", "vol_ratio", "price_vs_boll")


def _load_records():
    """rounds.jsonl 为真相源，round_*.json 补漏（rounds/ 与 rounds_legacy/ 都扫，
    按 round_id 去重）。"""
    records, seen = [], set()
    sources = [os.path.join(DATA_DIR, "rounds"),
               os.path.join(DATA_DIR, "rounds_legacy")]
    for base in sources:
        jsonl = os.path.join(base, "rounds.jsonl")
        if os.path.exists(jsonl):
            with open(jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("round_id") and r["round_id"] not in seen:
                        records.append(r)
                        seen.add(r["round_id"])
        for path in sorted(glob.glob(os.path.join(base, "round_*.json"))):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    r = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if r.get("round_id") and r["round_id"] not in seen:
                records.append(r)
                seen.add(r["round_id"])
    return sorted(records, key=lambda r: r.get("ts") or 0)


def _extract_factor_row(rec, inst_id):
    f = (rec.get("factors") or {}).get(inst_id)
    if f is None:
        return {"ok": 0, "err": None, "report": {"_partial": True,
                                                 "_note": "本轮完整因子未留存"},
                "text": None, "values": {}}
    values = {k: f.get(k) for k in FACTOR_KEYS}
    macd = (rec.get("factors") or {}).get(inst_id, {})  # 旧记录无 macd 明细
    row = {"ok": 1, "err": None,
           "report": {**f, "_partial": True},
           "text": None, "values": {**values, "macd_dif": macd.get("macd_dif")}}
    return row


def migrate(db_path=None, dry_run=False):
    db = db_path or os.path.join(DATA_DIR, "trader.db")
    init_db(db)
    store = Store(db)
    records = _load_records()
    imported = 0

    for rec in records:
        round_id = rec["round_id"]
        exists = store.query_one("SELECT 1 FROM rounds WHERE round_id=?", (round_id,))
        if exists:
            continue
        env_name, executing = MODE_MAP.get(rec.get("mode", "paper"), ("paper", 0))
        status = rec.get("status", "error")
        factors = rec.get("factors") or {}
        data_ok = any(v is not None for v in factors.values())
        if not data_ok and status in ("no_action", "data_unavailable"):
            status = "data_unavailable"
        acct = rec.get("account") or {}
        committee = rec.get("committee") or {}
        llm_mode = committee.get("mode", "baseline")
        plan = committee.get("plan") or {}

        if dry_run:
            imported += 1
            continue
        rw = RoundWriter.open(store, round_id, rec.get("ts") or time.time(),
                              env_name, executing, llm_mode)
        # 因子
        for inst_id in (factors or {}):
            row = _extract_factor_row(rec, inst_id)
            rw.store.execute(
                "INSERT INTO round_factors(round_pk, inst_id, ok, err, bar, "
                "bar_ts, price, ema20, ema60, rsi14, atr, atr_pct, macd_dif, "
                "macd_dea, macd_hist, funding_rate, vol_ratio, trend, structure, "
                "price_vs_boll, pattern, obi, oi, oi_delta_pct, ls_ratio, "
                "taker_ratio, report_json, report_text) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rw.pk, inst_id, row["ok"], row["err"], None, None,
                 row["values"].get("price"), None, None,
                 row["values"].get("rsi14"), row["values"].get("atr"),
                 None, row["values"].get("macd_dif"), None, None,
                 row["values"].get("funding_rate"),
                 row["values"].get("vol_ratio"),
                 row["values"].get("trend"), None,
                 row["values"].get("price_vs_boll"),
                 row["values"].get("pattern"), None, None, None, None, None,
                 json.dumps(row["report"], ensure_ascii=False, default=str),
                 row["text"]))
        # 委员会
        slot_pks = rw.write_committee(committee, None)
        # 风控
        risk = rec.get("risk") or {}
        failures = risk.get("failures", [])
        m = re.match(r"(R\d)", failures[0]) if failures else None
        sized = risk.get("sized") or {}
        winner_pk = None
        if plan:
            for slot, a in enumerate(committee.get("analysts") or []):
                if (a.get("instId"), a.get("direction")) == (
                        plan.get("instId"), plan.get("direction")):
                    winner_pk = slot_pks.get(slot)
                    break
        store.execute(
            "INSERT OR IGNORE INTO risk_verdicts(round_pk, proposal_pk, passed, "
            "rule_code, first_failure, failures_json, warnings_json, inst_id, "
            "direction, contracts, entry_ref, stop_loss, target, rr, "
            "target_source, notional_usdt, risk_usdt, risk_pct, atr, "
            "leverage_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rw.pk, winner_pk, int(bool(risk.get("passed"))),
             m.group(1) if m else None,
             failures[0] if failures else None,
             json.dumps(failures, ensure_ascii=False),
             json.dumps(risk.get("warnings", []), ensure_ascii=False),
             sized.get("instId"), sized.get("direction"),
             sized.get("contracts"), sized.get("entry_ref"),
             sized.get("stop_loss"), sized.get("target"), sized.get("rr"),
             sized.get("target_source"), sized.get("notional_usdt"),
             sized.get("risk_usdt"), sized.get("risk_pct"), sized.get("atr"),
             sized.get("leverage_after")))
        # 收尾 rounds 行
        positions = rec.get("positions") or []
        rw.finish(status, plan.get("action"),
                  rec.get("decision_reason") or plan.get("reason"),
                  data_ok=1 if data_ok else 0,
                  symbols_ok=sum(1 for v in factors.values() if v is not None),
                  symbols_total=len(factors) if factors else 0,
                  equity=acct.get("equity"), hwm=acct.get("hwm"),
                  drawdown=acct.get("drawdown"), usdt_avail=acct.get("usdt_avail"),
                  open_positions=len(positions),
                  duration_sec=rec.get("duration_sec"), error=rec.get("error"))
        # 被追溯修正的数据降级事件
        if not data_ok:
            write_event(store, env_name, "data_degraded",
                        "迁移追溯修正：本轮全标的因子缺失，状态由 "
                        f"{rec.get('status')} 改写为 data_unavailable",
                        level="warn", round_pk=rw.pk)
        imported += 1

    # 旧 run_state
    paper_state = os.path.join(DATA_DIR, "state", "state_paper.json")
    if os.path.exists(paper_state):
        try:
            with open(paper_state, "r", encoding="utf-8") as f:
                hwm = json.load(f).get("equity_hwm")
            if hwm:
                store.state_set("paper", "equity_hwm", hwm)
        except (json.JSONDecodeError, OSError):
            pass
    legacy_state = os.path.join(DATA_DIR, "state", "state.json")
    live_state = os.path.join(DATA_DIR, "state", "state_live.json")
    if os.path.exists(legacy_state) and not os.path.exists(live_state):
        try:
            with open(legacy_state, "r", encoding="utf-8") as f:
                hwm = json.load(f).get("equity_hwm")
            if hwm:
                store.state_set("demo", "equity_hwm", hwm)
                write_event(store, "demo", "legacy_state_import",
                            "无命名空间 state.json 出处不明，已导入 env='demo' 并建告警",
                            level="warn")
        except (json.JSONDecodeError, OSError):
            pass

    # 收尾：rounds 目录改名留档
    rounds_dir = os.path.join(DATA_DIR, "rounds")
    legacy_dir = os.path.join(DATA_DIR, "rounds_legacy")
    if os.path.isdir(rounds_dir) and not os.path.isdir(legacy_dir):
        os.rename(rounds_dir, legacy_dir)

    return {"records": len(records), "imported": imported, "db": db}
