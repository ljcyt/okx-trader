# -*- coding: utf-8 -*-
"""回测报告：从回测库汇总交易统计、exit_reason 分布、风控漏斗、权益曲线。"""
import math
import time


def _t_stat_p(rs):
    n = len(rs)
    if n < 2:
        return None, None
    mean = sum(rs) / n
    var = sum((x - mean) ** 2 for x in rs) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    if std == 0:
        return None, None
    t = mean / (std / math.sqrt(n))
    p = math.erfc(abs(t) / math.sqrt(2))
    return t, p


def _params_snapshot(cfg):
    keys = ["MAX_RISK_PER_TRADE", "MAX_TOTAL_LEVERAGE", "MAX_OPEN_POSITIONS",
            "MAX_DRAWDOWN", "ATR_STOP_MULT", "MIN_STOP_DIST_PCT",
            "MAX_STOP_DIST_PCT", "MIN_RR", "MIN_TARGET_ATR", "TARGET_ATR_MULT",
            "MAX_HOLD_BARS", "TRAIL_ATR_MULT", "SAME_DIRECTION_RISK_CAP",
            "SCORE_THRESHOLD", "TREND_THRESHOLD",
            "REGIME_MISMATCH_PENALTY", "KELLY_ENABLED"]
    return {k: getattr(cfg, k, None) for k in keys}


def build_report(store, cfg, *, bar, fill_model, n_bars, warmup, n_placed,
                 n_filled):
    trades = store.query("SELECT * FROM trades WHERE status='closed' "
                         "ORDER BY closed_ts")
    closed = [t for t in trades if t["r_multiple"] is not None]
    rs = [t["r_multiple"] for t in closed]
    wins = [t for t in closed if t["realized_pnl"] > 0]
    tstat, pval = _t_stat_p(rs)

    # exit_reason 分布
    exit_dist = {}
    for tr in trades:
        reason = tr["exit_reason"] or "unknown"
        b = exit_dist.setdefault(reason, {"n": 0, "sum_r": 0.0, "pnl": 0.0})
        b["n"] += 1
        if tr["r_multiple"] is not None:
            b["sum_r"] += tr["r_multiple"]
        b["pnl"] += tr["realized_pnl"] or 0
    for reason, b in exit_dist.items():
        b["avg_r"] = b["sum_r"] / b["n"] if b["n"] else None

    # 风控漏斗 + 否决次数
    veto = store.query(
        "SELECT rule_code, COUNT(*) c FROM risk_verdicts WHERE passed=0 "
        "AND rule_code IS NOT NULL GROUP BY rule_code ORDER BY c DESC")
    n_proposals = store.query_one(
        "SELECT COUNT(*) c FROM proposals WHERE action='open'")["c"]
    n_passed = store.query_one(
        "SELECT COUNT(*) c FROM risk_verdicts WHERE passed=1")["c"]
    n_filled_db = store.query_one(
        "SELECT COUNT(*) c FROM trades")["c"]

    # 权益曲线
    eq = store.query("SELECT equity, hwm FROM equity_curve ORDER BY ts")
    final_equity = eq[-1]["equity"] if eq else None
    max_dd = 0.0
    if eq:
        peak = 0.0
        for r in eq:
            if r["hwm"] and r["hwm"] > peak:
                peak = r["hwm"]
            if peak > 0 and r["equity"] is not None:
                dd = (peak - r["equity"]) / peak
                max_dd = max(max_dd, dd)

    # 分标的
    by_inst = {}
    for tr in closed:
        b = by_inst.setdefault(tr["inst_id"], {"n": 0, "sum_r": 0.0, "pnl": 0.0})
        b["n"] += 1
        b["sum_r"] += tr["r_multiple"] or 0
        b["pnl"] += tr["realized_pnl"] or 0
    for inst, b in by_inst.items():
        b["avg_r"] = b["sum_r"] / b["n"] if b["n"] else None

    return {
        "meta": {
            "bar": bar, "fill_model": fill_model, "n_bars": n_bars,
            "warmup": warmup, "rounds": store.query_one(
                "SELECT COUNT(*) c FROM rounds")["c"],
            "assumed_fill_rate": (n_filled / n_placed) if n_placed else None,
        },
        "params": _params_snapshot(cfg),
        "trades": {
            "n": len(closed), "win_rate": len(wins) / len(closed) if closed else None,
            "mean_r": (sum(rs) / len(rs)) if rs else None,
            "median_r": (sorted(rs)[len(rs) // 2]) if rs else None,
            "max_win_r": max(rs) if rs else None,
            "max_loss_r": min(rs) if rs else None,
            "total_r": sum(rs),
            "t_stat": tstat, "p_value": pval,
            "n_ge_30": len(closed) >= 30, "n_ge_100": len(closed) >= 100,
            "n_ge_200": len(closed) >= 200,
        },
        "exit_reason_distribution": exit_dist,
        "risk": {
            "veto_by_rule": [dict(v) for v in veto],
            "funnel": {"proposals": n_proposals, "passed_risk": n_passed,
                       "filled": n_filled_db},
        },
        "equity": {"final_equity": final_equity, "max_drawdown": max_dd},
        "by_inst": by_inst,
    }


def format_report(rep):
    """人类可读报告（CLI 打印用）。"""
    lines = []
    m = rep["meta"]
    lines.append(f"回测区间: {m['n_bars']} 根 {m['bar']} bar（warmup {m['warmup']}）"
                 f" | 轮次 {m['rounds']} | 成交模型 {m['fill_model']}"
                 f" | 假设成交率 "
                 f"{m['assumed_fill_rate']:.1%}" if m['assumed_fill_rate'] is not None
                 else "回测区间: n/a")
    lines.append("参数快照: " + ", ".join(
        f"{k}={v}" for k, v in rep["params"].items() if v is not None))
    t = rep["trades"]
    n = t["n"]
    lines.append(f"交易: {n} 笔 | 胜率 {t['win_rate']:.0%} | mean R "
                 f"{t['mean_r']:+.3f} | median R {t['median_r']:+.3f}"
                 if t["win_rate"] is not None else f"交易: {n} 笔")
    if t["t_stat"] is not None:
        lines.append(f"  单样本 t = {t['t_stat']:.2f} (p={t['p_value']:.3f}) | "
                     f"样本 ≥30:{t['n_ge_30']} ≥100:{t['n_ge_100']} "
                     f"≥200:{t['n_ge_200']}")
    lines.append("exit_reason 分布:")
    for reason, b in sorted(rep["exit_reason_distribution"].items(),
                            key=lambda kv: -kv[1]["n"]):
        avg = f"{b['avg_r']:+.2f}R" if b["avg_r"] is not None else "n/a"
        lines.append(f"  {reason}: {b['n']} 笔（{b['n']/n:.0%}） avg {avg} "
                     f"pnl {b['pnl']:+.2f}U" if n else f"  {reason}: {b['n']} 笔")
    f = rep["risk"]["funnel"]
    lines.append(f"漏斗: 提案 {f['proposals']} → 过风控 {f['passed_risk']} "
                 f"→ 成交 {f['filled']}")
    if rep["risk"]["veto_by_rule"]:
        lines.append("否决: " + ", ".join(
            f"{v['rule_code']}×{v['c']}" for v in rep["risk"]["veto_by_rule"]))
    e = rep["equity"]
    lines.append(f"权益: 期末 {e['final_equity']:.2f}U 最大回撤 {e['max_drawdown']:.2%}"
                 if e["final_equity"] is not None else "权益: n/a")
    return "\n".join(lines)
