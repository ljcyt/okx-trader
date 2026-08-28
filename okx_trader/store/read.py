# -*- coding: utf-8 -*-
"""面板/CLI 的读查询。原则：每条查询对应一个页面/一个 API，SQL 里不做业务。"""


def rounds_page(store, page=1, size=20, status=None, env=None, inst=None,
                frm=None, to=None):
    where, params = [], []
    if status:
        where.append("r.status=?")
        params.append(status)
    if env:
        where.append("r.env=?")
        params.append(env)
    if frm:
        where.append("r.ts>=?")
        params.append(frm)
    if to:
        where.append("r.ts<=?")
        params.append(to)
    if inst:
        where.append("EXISTS (SELECT 1 FROM proposals p WHERE p.round_pk=r.id "
                     "AND p.inst_id=? AND p.is_winner=1)")
        params.append(inst)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    total = store.query_one(f"SELECT COUNT(*) c FROM rounds r {wsql}", params)["c"]
    items = store.query(
        f"SELECT r.* FROM rounds r {wsql} ORDER BY r.ts DESC LIMIT ? OFFSET ?",
        params + [size, (page - 1) * size])
    winners = {}
    if items:
        pks = [r["id"] for r in items]
        marks = ",".join("?" * len(pks))
        for row in store.query(
                f"SELECT round_pk, analyst, inst_id, direction, avg_score "
                f"FROM proposals WHERE round_pk IN ({marks}) AND is_winner=1", pks):
            winners[row["round_pk"]] = dict(row)
    return {"total": total, "page": page, "size": size,
            "items": [{**dict(r), "winner": winners.get(r["id"])} for r in items]}


def round_detail(store, round_id):
    """一次拿全一轮：round / factors / proposals(含judges) / risk / orders / events。"""
    r = store.query_one("SELECT * FROM rounds WHERE round_id=?", (round_id,))
    if not r:
        return None
    pk = r["id"]
    factors = store.query(
        "SELECT * FROM round_factors WHERE round_pk=? ORDER BY inst_id", (pk,))
    proposals = store.query(
        "SELECT * FROM proposals WHERE round_pk=? ORDER BY slot", (pk,))
    judges = {}
    for j in store.query(
            "SELECT * FROM judge_scores WHERE round_pk=? ORDER BY id", (pk,)):
        judges.setdefault(j["proposal_pk"], []).append(dict(j))
    risk = store.query_one("SELECT * FROM risk_verdicts WHERE round_pk=?", (pk,))
    orders = store.query("SELECT * FROM orders WHERE round_pk=? ORDER BY id", (pk,))
    events = store.query(
        "SELECT * FROM app_events WHERE round_pk=? ORDER BY id", (pk,))
    llm_calls = store.query(
        "SELECT * FROM llm_calls WHERE round_pk=? ORDER BY id", (pk,))
    return {
        "round": dict(r),
        "factors": [dict(f) for f in factors],
        "proposals": [{**dict(p), "judges": judges.get(p["id"], [])}
                      for p in proposals],
        "risk": dict(risk) if risk else None,
        "orders": [dict(o) for o in orders],
        "events": [dict(e) for e in events],
        "llm_calls": [dict(x) for x in llm_calls],
    }


def stats(store, env=None, frm=None, to=None):
    params = []
    where = []
    if env:
        where.append("env=?")
        params.append(env)
    if frm:
        where.append("opened_ts>=?")
        params.append(frm)
    if to:
        where.append("opened_ts<=?")
        params.append(to)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    trades = store.query(
        f"SELECT * FROM trades {wsql}", params)
    closed = [t for t in trades if t["status"] == "closed"]
    wins = [t for t in closed if (t["realized_pnl"] or 0) > 0]
    by_symbol, by_analyst = {}, {}
    for t in closed:
        for bucket, key in ((by_symbol, t["inst_id"]), (by_analyst, t["analyst"])):
            b = bucket.setdefault(key or "?", {"n": 0, "pnl": 0.0, "wins": 0})
            b["n"] += 1
            b["pnl"] += t["realized_pnl"] or 0
            b["wins"] += 1 if (t["realized_pnl"] or 0) > 0 else 0
    r_multi = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
    veto = store.query(
        "SELECT rule_code, COUNT(*) count FROM risk_verdicts "
        "WHERE passed=0 AND rule_code IS NOT NULL GROUP BY rule_code "
        "ORDER BY count DESC")
    return {
        "trades": len(trades), "closed": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "sum_pnl": sum(t["realized_pnl"] or 0 for t in closed),
        "avg_r": (sum(r_multi) / len(r_multi)) if r_multi else None,
        "best": max((t["realized_pnl"] or 0) for t in closed) if closed else None,
        "worst": min((t["realized_pnl"] or 0) for t in closed) if closed else None,
        "by_symbol": by_symbol, "by_analyst": by_analyst,
        "veto_by_rule": [dict(v) for v in veto],
    }
