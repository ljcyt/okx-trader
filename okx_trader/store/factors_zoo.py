# -*- coding: utf-8 -*-
"""因子动物园：采集 → 前向收益回填 → IC 打分 → 晋级闸门。

核心原则（照抄 trader.gaagent.ai）：**unproven edge never touches the book**——
未晋级（非 active）的因子只观测、不参与下单；本期连 active 因子也不改仓位，
它的唯一作用是面板标绿 + 进入提示词摘要。

⚠ 前向收益必须按 bar_ts 对齐（close[bar_ts + N根] / close[bar_ts] - 1），
  绝不用 wall clock，绝不用未收盘 K 线。任何错位都会造出未来函数，
  然后 IC 会好看得离谱——那不是 alpha，是 bug。
"""
import json
import math
import time

# 因子登记表：name → (family, tier, 提取函数 report→float|None)
# 全部来自 build_factor_report 的字段；None/异常 → 本轮不采集
def _mom_24h(r):
    """价格相对 60EMA 的位置（趋势代理，从 ema20/ema60/price 派生）。"""
    if r.get("price") and r.get("ema60"):
        return (r["price"] - r["ema60"]) / r["ema60"]
    return None


def _boll_pos(r):
    """布林带内位置，中心化为 [-0.5, 0.5]：0=中轨，>0 偏上轨。"""
    b = r.get("boll") or {}
    width = (b.get("upper") or 0) - (b.get("lower") or 0)
    if width > 0 and r.get("price"):
        return (r["price"] - b["lower"]) / width - 0.5
    return None


FACTOR_EXTRACTORS = {
    "rsi14":        ("momentum", "core", lambda r: r.get("rsi14")),
    "atr_pct":      ("volatility", "core", lambda r: r.get("atr_pct")),
    "macd_hist":    ("momentum", "core", lambda r: (r.get("macd") or {}).get("hist")),
    "vol_ratio":    ("microstructure", "core", lambda r: r.get("vol_ratio")),
    "funding_rate": ("carry", "core", lambda r: r.get("funding_rate")),
    "obi":          ("microstructure", "core", lambda r: r.get("obi")),
    "oi_delta_pct": ("microstructure", "core", lambda r: r.get("oi_delta_pct")),
    "ls_ratio":     ("microstructure", "core", lambda r: r.get("ls_ratio")),
    "taker_ratio":  ("microstructure", "core", lambda r: r.get("taker_ratio")),
    "mom_24h":      ("momentum", "derived", _mom_24h),
    "boll_pos":     ("reversal", "derived", _boll_pos),
}

HORIZONS = ("1b", "4b", "24b")
_BAR_SEC = {"m": 60, "H": 3600, "D": 86400}


def _bar_ms(bar):
    import re
    m = re.match(r"^(\d+)([mHdD])$", str(bar))
    return int(m.group(1)) * _BAR_SEC[m.group(2)] * 1000 if m else 3600 * 1000


# ── 采集（每轮）────────────────────────────────────────────────

def collect_from_report(store, round_pk, inst_id, report, bar):
    """把一轮的因子值摊平成 factor_obs 行。主键幂等：重放不产生重复。"""
    now = time.time()
    bar_ts = report.get("ts")
    if not bar_ts:
        return 0
    n = 0
    for name, (family, tier, extractor) in FACTOR_EXTRACTORS.items():
        try:
            value = extractor(report)
        except Exception:  # noqa: BLE001
            value = None
        if value is None or not math.isfinite(value):
            continue
        store.execute(
            "INSERT OR IGNORE INTO factor_defs(name, family, tier, status, "
            "source, created_ts, status_ts) VALUES (?,?,?,?, 'builtin', ?, ?)",
            (name, family, tier, "observing", now, now))
        store.execute(
            "INSERT OR IGNORE INTO factor_obs(factor, inst_id, bar_ts, round_pk, "
            "value) VALUES (?,?,?,?,?)",
            (name, inst_id, int(bar_ts), round_pk, float(value)))
        n += 1
    return n


# ── 回填（每轮开头 + okxt backfill-returns）────────────────────

def backfill_returns(store, client, bar="1H", horizons=("1b", "4b", "24b"),
                     max_rows=50000):
    """对 filled_ts IS NULL 的观测回填前向收益。按 bar_ts 对齐到已收盘 K 线。
    filled_ts 只在全部 horizon 都填完后才置位（否则 4b/24b 会永远漏填）。"""
    bar_ms = _bar_ms(bar)
    pending_insts = store.query(
        "SELECT DISTINCT inst_id FROM factor_obs WHERE filled_ts IS NULL")
    total = 0
    for row in pending_insts:
        inst = row["inst_id"]
        try:
            candles = client.get_candles(inst, bar=bar, limit=1000)
        except Exception:  # noqa: BLE001 —— 行情不可用就等下一轮
            continue
        closes = {c["ts"]: c["close"] for c in candles}
        obs = store.query(
            "SELECT rowid, bar_ts, fwd_ret_1b, fwd_ret_4b, fwd_ret_24b "
            "FROM factor_obs WHERE inst_id=? AND filled_ts IS NULL", (inst,))
        for o in obs:
            base = closes.get(o["bar_ts"])
            if not base:
                continue
            for hz in horizons:
                if o[f"fwd_ret_{hz}"] is not None:
                    continue
                fwd = closes.get(o["bar_ts"] + int(hz.rstrip("b")) * bar_ms)
                if not fwd:
                    continue  # 目标 bar 还没收盘 → 留待下次
                store.execute(
                    f"UPDATE factor_obs SET fwd_ret_{hz}=? WHERE rowid=?",
                    (fwd / base - 1, o["rowid"]))
                total += 1
            cur = store.query_one(
                "SELECT fwd_ret_1b v1, fwd_ret_4b v4, fwd_ret_24b v24 "
                "FROM factor_obs WHERE rowid=?", (o["rowid"],))
            if cur and all(cur[k] is not None for k in ("v1", "v4", "v24")):
                store.execute("UPDATE factor_obs SET filled_ts=? WHERE rowid=?",
                              (time.time(), o["rowid"]))
            if total >= max_rows:
                return total
    return total


# ── 打分与晋级（okxt score-factors，可挂每周定时）────────────────

def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _spearman(xs, ys):
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    return _pearson(ranks(xs), ranks(ys))


def score_factors(store, gate, bar="1H", days_tracked_from_db=True):
    """逐 (factor, horizon) 打分并走晋级状态机。返回打分快照列表。"""
    now = time.time()
    out = []
    defs = store.query("SELECT name, family, tier, status, created_ts "
                       "FROM factor_defs")
    for d in defs:
        for hz in HORIZONS:
            obs = store.query(
                f"SELECT value, fwd_ret_{hz} v, filled_ts FROM factor_obs "
                f"WHERE factor=? AND fwd_ret_{hz} IS NOT NULL", (d["name"],))
            pairs = [(o["value"], o["v"], o["filled_ts"]) for o in obs]
            n = len(pairs)
            scored_days = len({time.strftime("%Y-%m-%d", time.localtime(f))
                               for _, _, f in pairs if f}) if n else 0
            tracked_days = int((now - d["created_ts"]) / 86400) + 1

            ic = rank_ic = ic_t = hit = None
            if n >= 3:
                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]
                ic = _pearson(xs, ys)
                rank_ic = _spearman(xs, ys)
                if ic is not None and abs(ic) < 1:
                    ic_t = ic * math.sqrt(n - 2) / math.sqrt(1 - ic * ic)
                elif ic is not None:
                    ic_t = math.copysign(math.inf, ic)
                hit = (sum(1 for p in pairs if p[0] * p[1] > 0) / n) if n else None

            gate_passed = 0
            if n >= gate.get("min_obs", 100):
                gate_passed = int(
                    scored_days >= gate.get("scored_days", 15)
                    and tracked_days >= gate.get("days_tracked", 30)
                    and (rank_ic is not None and rank_ic > 0
                         if gate.get("require_positive_rank_ic") else True))

            store.execute(
                "INSERT INTO factor_scores(factor, horizon, computed_ts, n_obs, "
                "scored_days, days_tracked, ic, rank_ic, ic_t, hit_rate, "
                "gate_passed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (d["name"], hz, now, n, scored_days, tracked_days,
                 ic, rank_ic, ic_t, hit, gate_passed))
            out.append({"factor": d["name"], "horizon": hz, "n_obs": n,
                        "ic": ic, "rank_ic": rank_ic, "hit_rate": hit,
                        "scored_days": scored_days, "days_tracked": tracked_days,
                        "gate_passed": gate_passed})

        # 状态机（任意 horizon 过闸即可晋级；全部用最新数据重判）
        _transition(store, d, out, gate)
    return out


def _transition(store, d, scored_rows, gate):
    """candidate → observing → trial → active；反转 → retired；长期不达标 → rejected。

    规则（数据不足只记录不判定）：
      trial   = 任一 horizon gate_passed 且 days_tracked ≥ gate.days_tracked
      active  = trial 保持 gate_passed 连续两批（用最近两批 score 判定）
      retired = 已 active 但最新批次 rank_ic ≤ 0
      rejected = tracked ≥ 2×days_tracked 且从未 trial
    """
    name, status = d["name"], d["status"]
    best = max((r for r in scored_rows if r["factor"] == name),
               key=lambda r: (r["gate_passed"], r["rank_ic"] or 0), default=None)
    if not best:
        return

    def _set(new_status, note):
        store.execute("UPDATE factor_defs SET status=?, status_ts=?, "
                      "status_note=? WHERE name=?", (new_status, time.time(),
                                                     note, name))
        store.execute(
            "INSERT INTO app_events(ts, env, level, kind, message, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), "paper", "info", "factor_status",
             f"因子 {name}: {status} → {new_status}（{note}）", None))

    tracked = best["days_tracked"]
    if status == "observing" and best["gate_passed"] and \
            tracked >= gate.get("days_tracked", 30):
        _set("trial", f"过闸：rank_ic={best['rank_ic']:.3f}")
    elif status == "trial":
        if not best["gate_passed"] or (best["rank_ic"] is not None
                                       and best["rank_ic"] <= 0):
            _set("retired", f"闸门反转：rank_ic={best['rank_ic']}")
        elif _prev_batch_also_passed(store, name):
            _set("active", f"连续两批过闸：rank_ic={best['rank_ic']:.3f}")
    elif status == "active" and best["rank_ic"] is not None and best["rank_ic"] <= 0:
        _set("retired", f"闸门反转：rank_ic={best['rank_ic']:.3f}")
    elif status == "observing" and tracked >= 2 * gate.get("days_tracked", 30):
        _set("rejected", "长期未过闸")


def _prev_batch_also_passed(store, name):
    rows = store.query(
        "SELECT gate_passed FROM factor_scores WHERE factor=? "
        "ORDER BY computed_ts DESC LIMIT 4", (name,))  # 最近 3 个 horizon + 本批
    if len(rows) < 3:
        return False
    return all(r["gate_passed"] for r in rows[:3])
