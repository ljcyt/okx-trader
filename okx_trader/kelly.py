# -*- coding: utf-8 -*-
"""Kelly 仓位系数引擎（影子模式默认）。

从 trades 表按人设滚动校准。v1 的两结果公式（p̂/b̂/f* = p̂−(1−p̂)/b̂，
kelly_mult = clamp(f*×KELLY_FRACTION, …)）已被否决：f* 的自然单位是
"占权益的下注比例"，直接当 1% 风险预算的乘数存在量纲错配——按本系统
现实参数（b≈1.67），0.45~0.55 的真实胜率算出的 f*×fraction 全被夹到
地板，开闸等于永久四分之一仓。

v2 改用【单样本 t 检验】驱动（检验 mean(r_multiple) > 0，而非胜率偏离
50%——后者会漏掉"胜率正常但盈亏比差"这一整类亏损策略）：
    t < -2        → 0.25   显著负 edge：该人设被证明亏钱，缩到地板
    -2 <= t < 0   → 0.75   亏但不可判显著：轻度收缩
    t >= 0        → 1.0    无负证据 → 全额预算（未证明不影响仓位）

设计红线（与仓库哲学一致）：
    1. 校准只用历史真实成交（trades 表），绝不接受 LLM 报的置信度——
       未校准的概率会无声地放大风险；
    2. 只做单假设分段，不上 n 资产 f=H⁻¹E（逐笔离散开仓系统，
       不是组合再平衡器；协方差在加密市场也不可靠）；
    3. 只缩放、不越顶——输出永远在 [KELLY_MIN_MULT, 1.0]，R4/R5/R8 硬顶
       原样生效。

影子模式（KELLY_ENABLED=false）下 mult 照算照入库，但不改实际仓位——
校准曲线先在面板上跑几周，数据说话之后再开闸。
"""
import math


def _wins_losses(rows):
    """rows: 含 r_multiple 的已平仓行。返回 (wins, losses, wins_r, loss_r)。"""
    wins = [r for r in rows if r["r_multiple"] is not None and r["r_multiple"] > 0]
    losses = [r for r in rows if r["r_multiple"] is not None and r["r_multiple"] <= 0]
    return wins, losses


def estimate(rows, cfg):
    """从已平仓 trades 行（同一人设）估计 kelly_mult。

    量纲修正（v2）：
      f* = p − (1−p)/b 的自然单位是"占权益的下注比例"，直接拿它当 1% 风险
      预算的乘数存在量纲错配——按本系统的现实参数（b≈1.67），任何 0.45~0.55
      的真实胜率算出的 f*×KELLY_FRACTION 都会被夹到地板，开闸等于永久四分
      之一仓。

    v2 改用【单样本 t 检验】驱动（检验 mean(r_multiple) > 0，而非胜率偏离
    50%——后者会漏掉"胜率正常但盈亏比差"这一整类亏损策略）：
        t < -2        → 0.25   显著负 edge：该人设被证明亏钱，缩到地板
        -2 <= t < 0   → 0.75   亏但不可判显著：轻度收缩
        t >= 0        → 1.0    无负证据 → 全额预算（未证明不影响仓位）

    全部中间量（mean_r/t/p_value）照算照入库——影子模式下校准曲线先积累。"""
    n_min = int(getattr(cfg, "KELLY_MIN_SAMPLES", 30) or 30)
    floor = float(getattr(cfg, "KELLY_MIN_MULT", 0.25) or 0.25)
    sig = float(getattr(cfg, "KELLY_SIG_LEVEL", 0.05) or 0.05)

    valid = [r for r in rows if r["r_multiple"] is not None]
    n = len(valid)
    out = {"n": n, "mean_r": None, "t": None, "p_value": None,
           "significant": False, "mult": 1.0,
           "note": "样本不足 → 中性 1.0（不惩罚无证据的策略）"}
    if n < n_min:
        return out

    rs = [r["r_multiple"] for r in valid]
    mean_r = sum(rs) / n
    var = sum((x - mean_r) ** 2 for x in rs) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    if std == 0:
        t = 0.0 if mean_r == 0 else math.copysign(1e9, mean_r)
    else:
        t = mean_r / (std / math.sqrt(n))
    # |z| 保证负方向的 t 也能判显著（单尾上侧对负 t 永远≈1，会漏掉整类亏损策略）
    p_value = 0.5 * math.erfc(abs(t) / math.sqrt(2))
    significant = p_value < sig

    if t < -2:
        mult, band = floor, "显著负 edge → 保守地板"
    elif t < 0:
        mult, band = 0.75, "弱负（未达显著）→ 轻度收缩"
    else:
        mult, band = 1.0, "无负证据 → 全额预算"

    out.update({"mean_r": mean_r, "t": round(t, 3), "p_value": p_value,
                "significant": significant,
                "mult": round(mult, 3), "note": band})
    return out


def mult_for(store, analyst, cfg):
    """查 trades 表估计指定人设的 kelly_mult。store 不可用时返回中性 1.0。"""
    if store is None or not analyst:
        return {"mult": 1.0, "n": 0, "note": "无人设归因 → 中性"}
    try:
        rows = store.query(
            "SELECT r_multiple FROM trades "
            "WHERE status='closed' AND analyst=? AND r_multiple IS NOT NULL",
            (analyst,))
    except Exception:  # noqa: BLE001
        return {"mult": 1.0, "n": 0, "note": "查询失败 → 中性"}
    return estimate(rows, cfg)


def clamp(mult, cfg):
    return max(float(getattr(cfg, "KELLY_MIN_MULT", 0.25) or 0.25),
               min(1.0, float(mult)))
