# -*- coding: utf-8 -*-
"""Kelly 仓位系数引擎（影子模式默认）。

从 trades 表按人设滚动校准两结果 Kelly：
    p̂ = 胜率（r_multiple > 0 的占比）
    b̂ = 平均盈利 / |平均亏损|（都用 R 倍数，天然去量纲）
    f* = p̂ − (1 − p̂) / b̂
    kelly_mult = clamp(f* × KELLY_FRACTION, KELLY_MIN_MULT, 1.0)

设计红线（与仓库哲学一致）：
    1. 校准只用历史真实成交（trades 表），绝不接受 LLM 报的置信度——
       未校准的概率会无声地放大风险；
    2. 只做两结果分数 Kelly，不上 n 资产 f=H⁻¹E（逐笔离散开仓系统，
       不是组合再平衡器；协方差在加密市场也不可靠）；
    3. 只缩放、不越顶——输出永远在 [KELLY_MIN_MULT, 1.0]，R4/R5/R8 硬顶
       原样生效。

状态机（防"没证据就惩罚"）：
    n < KELLY_MIN_SAMPLES 或无显著性 → mult = 1.0（中性，维持现状）
    显著正 edge（z 检验 p < KELLY_SIG_LEVEL 且 f* > 0）→ 用校准值
    显著负 edge（f* < 0 显著）→ KELLY_MIN_MULT（保守地板）
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
    """从已平仓 trades 行估计 kelly_mult。返回 dict（含全部中间量，落库用）。

    rows: 同一人设的已平仓交易（含 r_multiple 列）。
    状态机：n < MIN_SAMPLES 或检验不显著 → mult=1.0（中性，维持现状）；
    显著负 edge → KELLY_MIN_MULT（保守地板）；
    显著正 edge → clamp(f* × KELLY_FRACTION, floor, 1.0)。"""
    n_min = int(getattr(cfg, "KELLY_MIN_SAMPLES", 30) or 30)
    frac = float(getattr(cfg, "KELLY_FRACTION", 0.5) or 0.5)
    floor = float(getattr(cfg, "KELLY_MIN_MULT", 0.25) or 0.25)
    sig = float(getattr(cfg, "KELLY_SIG_LEVEL", 0.05) or 0.05)

    valid = [r for r in rows if r["r_multiple"] is not None]
    n = len(valid)
    out = {"n": n, "p": None, "b": None, "f_star": None,
           "significant": False, "p_value": None, "mult": 1.0,
           "note": "样本不足 → 中性 1.0（不惩罚无证据的策略）"}
    if n < n_min:
        return out

    wins, losses = _wins_losses(valid)
    p_hat = len(wins) / n
    if not wins:
        # 零胜样本：盈亏比不可估，但 p=0 对任何 b>0 都是显著负 edge → 保守地板
        out.update({"p": 0.0, "b": None, "f_star": -1.0,
                    "significant": True, "p_value": 0.0,
                    "mult": floor, "note": "零胜样本 → 保守地板"})
        return out
    if not losses:
        out.update({"p": p_hat, "note": "全胜样本，盈亏比不可估 → 中性"})
        return out
    avg_win = sum(r["r_multiple"] for r in wins) / len(wins)
    avg_loss = sum(r["r_multiple"] for r in losses) / len(losses)
    if avg_loss == 0 or avg_win <= 0:
        out.update({"p": p_hat, "note": "盈亏比不可估 → 中性"})
        return out
    b_hat = avg_win / abs(avg_loss)
    f_star = p_hat - (1 - p_hat) / b_hat

    z = (p_hat - 0.5) / math.sqrt(0.25 / n)
    p_value = math.erfc(abs(z) / math.sqrt(2))            # 双尾
    significant = p_value < sig

    if significant and f_star < 0:
        mult, note = floor, f"显著负 edge（p={p_hat:.2f}, b={b_hat:.2f}）→ 地板"
    elif significant and f_star > 0:
        mult = max(floor, min(1.0, f_star * frac))
        note = f"显著正 edge（p={p_hat:.2f}, b={b_hat:.2f}）→ 分数 Kelly"
    else:
        mult, note = 1.0, f"检验不显著（p_value={p_value:.3f}）→ 中性 1.0"

    out.update({"p": p_hat, "b": b_hat, "f_star": f_star,
                "significant": significant, "p_value": p_value,
                "mult": round(mult, 3), "note": note})
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
