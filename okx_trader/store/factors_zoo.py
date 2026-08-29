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
_BAR_SEC = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}
_CANDLE_PAGE = 300          # OKX /market/candles 单页上限
_MAX_PAGES = 6              # 最多翻 6 页（1H×1800 根 ≈ 75 天，远超 24b 需求）


def _bar_ms(bar):
    """周期串 → 毫秒。不认识的周期直接抛错——静默兜底 1H 会造出错误的前向收益。"""
    import re
    m = re.match(r"^(\d+)([mHhdDwM])$", str(bar))
    if not m:
        raise ValueError(f"未知 K 线周期：{bar!r}")
    unit = {"H": "h", "D": "d"}.get(m.group(2), m.group(2))
    return int(m.group(1)) * _BAR_SEC[unit] * 1000


# ── 采集（每轮）────────────────────────────────────────────────

def collect_from_report(store, round_pk, inst_id, report, bar):
    """把一轮的因子值摊平成 factor_obs 行。主键幂等：重放不产生重复。
    factor_defs 一次性批量注册（不再每因子一条）。"""
    now = time.time()
    bar_ts = report.get("ts")
    if not bar_ts:
        return 0
    for name, (family, tier, _) in FACTOR_EXTRACTORS.items():
        store.execute(
            "INSERT OR IGNORE INTO factor_defs(name, family, tier, status, "
            "source, created_ts, status_ts) VALUES (?,?,?,?, 'builtin', ?, ?)",
            (name, family, tier, "observing", now, now))
    n = 0
    for name, (_, _, extractor) in FACTOR_EXTRACTORS.items():
        try:
            value = extractor(report)
        except Exception:  # noqa: BLE001
            value = None
        if value is None or not math.isfinite(value):
            continue
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
    filled_ts 只在全部 horizon 都填完后才置位（否则 4b/24b 会永远漏填）。

    K 线拉取：单页上限 300（OKX /market/candles 限制），用 after 游标向更旧
    翻页，直到覆盖最早的待回填观测 + 24 根；翻页耗尽仍未覆盖的观测保持
    pending，下次再试。任何异常必须记 warning——静默跳过会让观测永远填不上、
    IC 永远是空而无人知晓。
    """
    bar_ms = _bar_ms(bar)
    pending_insts = store.query(
        "SELECT DISTINCT inst_id FROM factor_obs WHERE filled_ts IS NULL")
    total = 0
    for row in pending_insts:
        inst = row["inst_id"]
        try:
            closes, oldest_ts = _fetch_closes_paged(client, inst, bar)
        except Exception as e:  # noqa: BLE001 —— 失败必须可见，不能静默空转
            client.log.warning(
                "backfill_returns：%s K 线拉取失败（%s: %s）——本标的观测保持 pending",
                inst, type(e).__name__, e)
            continue
        obs = store.query(
            "SELECT rowid, bar_ts, fwd_ret_1b, fwd_ret_4b, fwd_ret_24b "
            "FROM factor_obs WHERE inst_id=? AND filled_ts IS NULL", (inst,))
        for o in obs:
            base = closes.get(o["bar_ts"])
            if not base:
                if o["bar_ts"] < oldest_ts:
                    client.log.warning(
                        "backfill_returns：%s bar_ts=%s 早于可回看范围（翻页耗尽），"
                        "放弃该观测", inst, o["bar_ts"])
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


def _fetch_closes_paged(client, inst, bar):
    """拉 close 序列，覆盖最近 _CANDLE_PAGE 根 + 向更旧翻页直到连续两页空。
    返回 ({ts: close}, oldest_ts)。"""
    closes = {}
    after = None
    oldest = None
    for _ in range(_MAX_PAGES):
        candles = client.get_candles(inst, bar=bar, limit=_CANDLE_PAGE,
                                     after=after)
        if not candles:
            break
        for c in candles:
            closes[c["ts"]] = c["close"]
        page_oldest = min(c["ts"] for c in candles)
        if oldest is not None and page_oldest >= oldest:
            break  # 游标不再前进（到头了）
        oldest = page_oldest
        after = oldest  # after=返回比该 ts 更旧的记录
    return closes, (oldest or 0)


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


def score_factors(store, gate, bar="1H", env="paper"):
    """逐 (factor, horizon) 打分并走晋级状态机。返回打分快照列表。

    口径说明：
    - scored_days 数的是观测 bar_ts 的自然日（计分日），不是回填执行日——
      补跑/重放不会把一批观测盖上同一个日期而推迟晋级。
    - IC 跨标的混算，但 value 先按标的 z-score：否则 atr_pct 这类量级差异
      会直接进 Pearson，数出来的东西量纲可疑。
    """
    now = time.time()
    out = []
    defs = store.query("SELECT name, family, tier, status, created_ts "
                       "FROM factor_defs")
    for d in defs:
        for hz in HORIZONS:
            obs = store.query(
                f"SELECT inst_id, value, fwd_ret_{hz} v, bar_ts FROM factor_obs "
                f"WHERE factor=? AND fwd_ret_{hz} IS NOT NULL", (d["name"],))
            n = len(obs)
            scored_days = len({time.strftime("%Y-%m-%d",
                                             time.localtime(o["bar_ts"] / 1000))
                               for o in obs}) if n else 0
            tracked_days = int((now - d["created_ts"]) / 86400) + 1

            ic = rank_ic = ic_t = hit = None
            if n >= 3:
                xs = _zscore_by_inst(obs)
                ys = [o["v"] for o in obs]
                ic = _pearson(xs, ys)
                rank_ic = _spearman(xs, ys)
                if ic is not None and abs(ic) < 1:
                    ic_t = ic * math.sqrt(n - 2) / math.sqrt(1 - ic * ic)
                elif ic is not None:
                    ic_t = math.copysign(math.inf, ic)
                # 命中率只在因子取值有正有负时有意义——恒正因子（rsi14、
                # atr_pct、量比…）会退化成"市场上涨比例"，量的是行情不是因子
                vals = [o["value"] for o in obs]
                if any(v > 0 for v in vals) and any(v < 0 for v in vals):
                    hit = sum(1 for o in obs if o["value"] * o["v"] > 0) / n

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
        _transition(store, d, out, gate, env=env, batch_ts=now)
    return out


def _zscore_by_inst(obs):
    """value 按标的 z-score 后摊平（跨标的混算前去量纲）。std=0 的标的全 0。"""
    by_inst = {}
    for o in obs:
        by_inst.setdefault(o["inst_id"], []).append(o["value"])
    stats = {}
    for inst, vals in by_inst.items():
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        stats[inst] = (mean, math.sqrt(var))
    out = []
    for o in obs:
        mean, std = stats[o["inst_id"]]
        out.append((o["value"] - mean) / std if std > 0 else 0.0)
    return out


def _transition(store, d, scored_rows, gate, env="paper", batch_ts=None):
    """candidate → observing → trial → active；反转 → retired；长期不达标 → rejected。

    规则（数据不足只记录不判定）：
      trial   = 任一 horizon gate_passed 且 days_tracked ≥ gate.days_tracked
      active  = trial 且【上一批】（不含本批）也全部过闸 —— 连续两批
      retired = 已 active 但最新批次 rank_ic ≤ 0
      rejected = tracked ≥ 2×days_tracked 且从未 trial
    """
    name, status = d["name"], d["status"]
    best = max((r for r in scored_rows if r["factor"] == name),
               key=lambda r: (r["gate_passed"], r["rank_ic"] or 0), default=None)
    if not best:
        return

    rank_txt = (f"{best['rank_ic']:.3f}" if best["rank_ic"] is not None
                else "n/a")

    def _set(new_status, note):
        store.execute("UPDATE factor_defs SET status=?, status_ts=?, "
                      "status_note=? WHERE name=?", (new_status, time.time(),
                                                     note, name))
        store.execute(
            "INSERT INTO app_events(ts, env, level, kind, message, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), env, "info", "factor_status",
             f"因子 {name}: {status} → {new_status}（{note}）", None))

    tracked = best["days_tracked"]
    if status == "observing" and best["gate_passed"] and \
            tracked >= gate.get("days_tracked", 30):
        _set("trial", f"过闸：rank_ic={rank_txt}")
    elif status == "trial":
        if not best["gate_passed"] or (best["rank_ic"] is not None
                                       and best["rank_ic"] <= 0):
            _set("retired", f"闸门反转：rank_ic={best['rank_ic']}")
        elif _prev_batch_also_passed(store, name, batch_ts):
            _set("active", f"连续两批过闸：rank_ic={rank_txt}")
    elif status == "active" and best["rank_ic"] is not None and best["rank_ic"] <= 0:
        _set("retired", f"闸门反转：rank_ic={best['rank_ic']:.3f}")
    elif status == "observing" and tracked >= 2 * gate.get("days_tracked", 30):
        _set("rejected", "长期未过闸")


def _prev_batch_also_passed(store, name, batch_ts=None):
    """看【上一批】（computed_ts 严格早于本批）的 3 个 horizon 是否全过。
    同一次 score_factors 里三个 horizon 共享 computed_ts，必须排除本批。"""
    now = batch_ts if batch_ts is not None else time.time()
    rows = store.query(
        "SELECT gate_passed FROM factor_scores WHERE factor=? "
        "AND computed_ts < ? ORDER BY computed_ts DESC LIMIT 3",
        (name, now))
    if len(rows) < 3:
        return False
    return all(r["gate_passed"] for r in rows)
