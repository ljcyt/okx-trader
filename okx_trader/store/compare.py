# -*- coding: utf-8 -*-
"""对照实验：LLM 委员会 vs 无 LLM 基线（影子盘）。

问题：LLM 层（分析师人设 + 裁判合议）在机械因子之上到底加了什么值？
本模块按 bar_ts 对齐主库（LLM 决策）与影子库（基线决策），输出决策
分叉四象限：

                    基线开仓        基线弃权
    LLM 开仓        一致            LLM 独有（增益 or 噪音）
    LLM 弃权        ★LLM 过滤掉了   一致

★ 是核心问题格：被过滤的机会事后 24h 前向收益平均为正 → LLM 层在
损失期望（拦掉了赚钱机会）；平均为负 → 它在做有用的过滤。

注意口径：
- 影子盘用 paper 环境（不发单），它的"成交"是理想化的——只看决策
  分叉，不比净值；
- 两个库的 SYMBOLS / R1-R8 参数必须逐项相同，否则对照无效；
- 按小时桶对齐（round_ts 落进同一根 1H K 线即视为同一决策点）。
"""
import math
import sqlite3
import time


def _hour_bucket(ts):
    return int(ts) // 3600 * 3600


def _decisions(db_path, env):
    """(hour_bucket, inst_id) → action。同一桶同标的取最后一轮（修订后）。

    action 归一化为 open / hold：open 判定标准 = 该轮 final_action 是
    place/deploy（走到执行），hold = 其余一切（弃权/否决/风控拒）。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT r.ts, r.final_action, p.analyst, p.inst_id, p.direction, "
            "       p.avg_score, p.reason "
            "FROM rounds r "
            "LEFT JOIN proposals p ON p.round_pk = r.id AND p.is_winner = 1 "
            "WHERE r.env=? AND r.ts >= ? ORDER BY r.ts ASC",
            (env, time.time() - 30 * 86400)).fetchall()
    finally:
        conn.close()
    out = {}
    meta = {}
    for r in rows:
        if not r["inst_id"]:
            continue
        key = (_hour_bucket(r["ts"]), r["inst_id"])
        opened = str(r["final_action"] or "") in ("place", "deploy")
        out[key] = "open" if opened else "hold"
        meta[key] = {"analyst": r["analyst"], "score": r["avg_score"],
                     "ts": r["ts"], "direction": r["direction"]}
    # 决策点骨架：round_factors 每轮每标的一行（因子算出来了 = 该轮
    # 确实评估过该标的）。没出现在上面 winner 集合里的 → hold。
    conn2 = None
    try:
        conn2 = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn2.row_factory = sqlite3.Row
        frs = conn2.execute(
            "SELECT r.ts, f.inst_id FROM round_factors f "
            "JOIN rounds r ON r.id = f.round_pk "
            "WHERE r.env=? AND f.ok=1 AND r.ts >= ?",
            (env, time.time() - 30 * 86400)).fetchall()
    finally:
        if conn2:
            conn2.close()
    for r in frs:
        key = (_hour_bucket(r["ts"]), r["inst_id"])
        if key not in out:
            out[key] = "hold"
    return out, meta


def _fwd_return(db_path, inst_id, ts, horizon_h=24):
    """ts 之后 horizon_h 小时的前向收益（用 1H K 线，只读）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        a = conn.execute(
            "SELECT price FROM round_factors WHERE inst_id=? AND ok=1 "
            "AND round_pk IN (SELECT id FROM rounds WHERE ts>=?) "
            "ORDER BY rowid ASC LIMIT 1", (inst_id, ts)).fetchone()
        b = conn.execute(
            "SELECT price FROM round_factors WHERE inst_id=? AND ok=1 "
            "AND round_pk IN (SELECT id FROM rounds WHERE ts>=?) "
            "ORDER BY rowid ASC LIMIT 1",
            (inst_id, ts + horizon_h * 3600)).fetchone()
    finally:
        conn.close()
    if not a or not b or not a["price"]:
        return None
    return (b["price"] - a["price"]) / a["price"]


def compare(main_db, shadow_db, env="demo", shadow_env="paper",
            horizon_h=24):
    """输出四象限 + 被过滤机会的前向收益统计。返回 dict（供 CLI/面板用）。"""
    llm, llm_meta = _decisions(main_db, env)
    base, _ = _decisions(shadow_db, shadow_env)
    keys = sorted(set(llm) & set(base))          # 只比两库都有数据的桶
    quad = {"both_open": 0, "llm_only": 0, "filtered": 0, "both_hold": 0}
    filtered_rows = []
    for k in keys:
        a, b = llm[k], base[k]
        if a == "open" and b == "open":
            quad["both_open"] += 1
        elif a == "open" and b == "hold":
            quad["llm_only"] += 1
        elif a == "hold" and b == "open":
            quad["filtered"] += 1
            fr = _fwd_return(main_db, k[1], k[0], horizon_h)
            m = llm_meta.get(k) or {}
            filtered_rows.append({
                "ts": k[0], "inst": k[1],
                "baseline_analyst": m.get("analyst"),
                "baseline_direction": m.get("direction"),
                "fwd_return": fr})
        else:
            quad["both_hold"] += 1
    rets = [r["fwd_return"] for r in filtered_rows
            if r["fwd_return"] is not None]
    stats = {
        "aligned_decision_points": len(keys),
        "quadrant": quad,
        "filtered_n": len(filtered_rows),
        "filtered_with_return": len(rets),
        "horizon_h": horizon_h,
    }
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) \
            if len(rets) > 1 else 0.0
        t = mean / (math.sqrt(var) / math.sqrt(len(rets))) \
            if var > 0 and len(rets) > 1 else None
        stats["filtered_mean_fwd_return"] = mean
        stats["filtered_t_stat"] = t
        stats["verdict"] = (
            "LLM 过滤平均放过的机会为负收益（过滤有效）" if mean < 0 else
            "LLM 过滤平均放过的机会为正收益（过滤在损失期望）")
    return {"stats": stats, "filtered": filtered_rows}
