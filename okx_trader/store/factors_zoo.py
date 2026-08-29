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
        # 零方差守卫：某因子已积累 ≥8 条且全部等值（demo 的 oi_delta 恒 0
        # 之类）→ 继续入库只会占观测额度，std=0 永远算不出 IC
        flat = store.query_one(
            "SELECT COUNT(*) c, MIN(value) mn, MAX(value) mx FROM factor_obs "
            "WHERE factor=? AND inst_id=?", (name, inst_id))
        if flat and flat["c"] >= 8 and flat["mx"] == flat["mn"] == value:
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
            need_oldest = store.query_one(
                "SELECT MIN(bar_ts) m FROM factor_obs "
                "WHERE inst_id=? AND filled_ts IS NULL", (inst,))["m"]
            closes, oldest_ts = _fetch_closes_paged(client, inst, bar,
                                                    need_oldest_ts=need_oldest)
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


def _fetch_closes_paged(client, inst, bar, need_oldest_ts=None):
    """拉 close 序列（升序）。单页 300，after 游标向更旧翻页；
    覆盖到 need_oldest_ts（最早的待回填观测）即停——稳态只有 1 页，
    不会每次都拉满 _MAX_PAGES×300 根。返回 ({ts: close}, oldest_ts)。"""
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
        if need_oldest_ts is not None and oldest <= need_oldest_ts:
            break  # 已覆盖最早的待回填观测 → 按需停
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

    统计修正：
    - n_eff：重叠前向收益（4b/24b）共享 K 线，有效样本 = n/hz。
      ic_t 和 gate 的 min_obs 都用 n_eff——否则 24b 的 t 值虚高 ~5 倍。
    - BH/FDR：33 次检验在 α=0.05 下的期望假阳性 1.65 个；对当批 p 值做
      Benjamini-Hochberg 校正，gate_passed 建立在校正后的显著性上——
      未证明的因子永不影响下单。
    - scored_days 数的是观测 bar_ts 的自然日（计分日），不是回填执行日。
    - IC 跨标的混算，但 value 先按标的 z-score 去量纲。
    """
    now = time.time()
    out = []
    defs = store.query("SELECT name, family, tier, status, created_ts "
                       "FROM factor_defs")
    fdr_q = float(gate.get("fdr_q", 0.05) or 0.05)

    # Phase 1: 逐 (factor, horizon) 计算统计量
    scored = []  # (factor, horizon, n, n_eff, scored_days, tracked_days, ic, rank_ic, ic_t, p_value)
    for d in defs:
        for hz in HORIZONS:
            hz_n = int(hz.rstrip("b"))
            obs = store.query(
                f"SELECT inst_id, value, fwd_ret_{hz} v, bar_ts FROM factor_obs "
                f"WHERE factor=? AND fwd_ret_{hz} IS NOT NULL", (d["name"],))
            n = len(obs)
            scored_days = len({time.strftime("%Y-%m-%d",
                                             time.localtime(o["bar_ts"] / 1000))
                               for o in obs}) if n else 0
            tracked_days = int((now - d["created_ts"]) / 86400) + 1

            ic = rank_ic = ic_t = p_value = hit = None
            n_eff = 0.0
            if n >= 3:
                xs = _zscore_by_inst(obs)
                ys = [o["v"] for o in obs]
                ic = _pearson(xs, ys)
                rank_ic = _spearman(xs, ys)
                # 重叠窗口修正：fwd_ret_hb 相邻观测共享 (hb-1)/hb 根 K 线，
                # 有效样本量 = n/hz——ic_t 不修正则 24b 的 t 值虚高 ~5 倍
                n_eff = max(n / hz_n, 1.0)
                if ic is not None and abs(ic) >= 1:
                    # 完全相关是简并情形（合成信号/重复值），t 无定义但证据是决定性的
                    ic_t = math.copysign(math.inf, ic)
                    p_value = 0.0
                elif ic is not None and n_eff > 3:
                    ic_t = ic * math.sqrt(n_eff - 2) / math.sqrt(1 - ic * ic)
                    p_value = math.erfc(abs(ic_t) / math.sqrt(2))
                # 其余情形（样本不足）p_value 留 None：不参与 BH，闸门必不过——
                # 不能让"没有数据"伪装成"p=0 的最强证据"
                # 命中率只在因子取值有正有负时有意义
                vals = [o["value"] for o in obs]
                if any(v > 0 for v in vals) and any(v < 0 for v in vals):
                    hit = sum(1 for o in obs if o["value"] * o["v"] > 0) / n

            scored.append({
                "factor": d["name"], "horizon": hz,
                "n_obs": n, "n_eff": n_eff,
                "scored_days": scored_days, "days_tracked": tracked_days,
                "ic": ic, "rank_ic": rank_ic, "ic_t": ic_t,
                "p_value": p_value, "hit_rate": hit,
            })

    # Phase 2: BH/FDR 多重检验校正（当批全部 p 值一起校正）
    all_p = [s["p_value"] for s in scored if s["p_value"] is not None]
    bh_sig = _bh_fdr(all_p, q=fdr_q)
    valid_indices = [i for i, s in enumerate(scored) if s["p_value"] is not None]
    for j, idx in enumerate(valid_indices):
        scored[idx]["bh_significant"] = bh_sig[j]

    # Phase 3: 入库 + 晋级闸门
    for d in defs:
        for s in scored:
            if s["factor"] != d["name"]:
                continue
            # 闸门：min_obs 管数据充足性（原始观测数）；统计有效性由 BH 显著性
            # 把守——ic_t 已按 n_eff 修正，低 n_eff 的 horizon p 值自然不显著。
            # 两道守卫各司其职，min_obs 不再套 n_eff（那是重复计数同一修正）
            gate_passed = 0
            if s["n_obs"] >= gate.get("min_obs", 100):
                gate_passed = int(
                    s["scored_days"] >= gate.get("scored_days", 15)
                    and s["days_tracked"] >= gate.get("days_tracked", 30)
                    and (s["rank_ic"] is not None and s["rank_ic"] > 0
                         if gate.get("require_positive_rank_ic") else True)
                    and s.get("bh_significant", False))
            s["gate_passed"] = gate_passed

            store.execute(
                "INSERT INTO factor_scores(factor, horizon, computed_ts, n_obs, "
                "n_eff, scored_days, days_tracked, ic, rank_ic, ic_t, hit_rate, "
                "gate_passed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["name"], s["horizon"], now, s["n_obs"], s["n_eff"],
                 s["scored_days"], s["days_tracked"],
                 s["ic"], s["rank_ic"], s["ic_t"], s["hit_rate"],
                 s["gate_passed"]))
            out.append({"factor": d["name"], "horizon": s["horizon"],
                        "n_obs": s["n_obs"], "n_eff": s["n_eff"],
                        "ic": s["ic"], "rank_ic": s["rank_ic"],
                        "hit_rate": s["hit_rate"],
                        "scored_days": s["scored_days"],
                        "days_tracked": s["days_tracked"],
                        "gate_passed": s["gate_passed"],
                        "bh_significant": s.get("bh_significant", False)})

        _transition(store, d, out, gate, env=env, batch_ts=now)
    return out


def _bh_fdr(p_values, q=0.05):
    """Benjamini-Hochberg FDR：返回与 p_values 等长的布尔列表。

    BH 控制错误发现率（期望假阳性 / 总发现数 ≤ q），比 Bonferroni 宽松
    但不会把所有检验一刀切死。"""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    result = [False] * m
    # 从最大 k 往回找：p(k) <= (k/m) * q 的最大 k，全部 rank ≤ k 的判显著
    for k in range(m, 0, -1):
        idx = order[k - 1]
        if p_values[idx] <= (k / m) * q:
            for j in range(k):
                result[order[j]] = True
            break
    return result


def _zscore_by_inst(obs):
    """value 按标的 z-score 后摊平（跨标的混算前去量纲）。std=0 的标的全 0。

    ⚠ 备查：这里用全样本均值/标准差，某条观测的 z 值依赖它之后的数据——
    对"固定样本上的描述性 IC"是标准做法，没问题；但这个 helper 一旦被搬到
    实时信号路径上就是真正的未来函数，届时必须改滚动窗口。"""
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
      active  = trial 且【上一批】（不含本批）也有 horizon 过闸 —— 连续两批
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
    """看【上一批】（computed_ts 严格早于本批）是否也有过闸的 horizon。
    同一次 score_factors 里三个 horizon 共享 computed_ts，必须排除本批。

    只要求上一批至少一个 horizon 过闸（与 trial 晋级同一标准）而非三个
    全过：n_eff 修正后高 horizon 样本不足拿不到显著性是常态，要求三全
    过等于要求因子在所有周期同时显著，超出连续两批稳定的设计意图。"""
    now = batch_ts if batch_ts is not None else time.time()
    rows = store.query(
        "SELECT gate_passed FROM factor_scores WHERE factor=? "
        "AND computed_ts < ? ORDER BY computed_ts DESC LIMIT 3",
        (name, now))
    if len(rows) < 3:
        return False
    return any(r["gate_passed"] for r in rows)
