# -*- coding: utf-8 -*-
"""K 线因子计算层（多 agent 委员会的数据底座）

全部用代码计算（不依赖模型），保证每一轮所有 agent 看到的是同一份客观因子报告，
避免"模型幻觉出指标数值"。因子包括：
    趋势：EMA20 / EMA60 排列、MACD(12,26,9)
    动量：RSI14
    波动：ATR14、ATR 占价比、布林带(20,2)宽度
    量能：量比（最新一根成交量 / 前 20 根均值）
    资金：资金费率
    形态：最后一根 K 线的简单形态分类
"""
import re
import time


def regime_label(report, cfg):
    """市况标签（代码判定，不问模型）：
    adx_proxy = |EMA20-EMA60|/ATR —— 趋势强度的廉价代理。"""
    atr = report.get("atr") or 0
    adx_proxy = 0.0
    if atr and report.get("ema20") is not None and report.get("ema60") is not None:
        adx_proxy = abs(report["ema20"] - report["ema60"]) / atr
    if report.get("atr_pct", 0) > getattr(cfg, "HIGH_VOL_ATR_PCT", 0.03):
        return "high_vol"
    if adx_proxy > getattr(cfg, "TREND_THRESHOLD", 0.45):
        return "trending"
    return "ranging"


def overall_regime(factors, cfg):
    """多标的整体 regime：逐标的判定后投票（平票取第一）。"""
    from collections import Counter
    labels = [regime_label(r, cfg) for r in (factors or {}).values() if r]
    if not labels:
        return None
    return Counter(labels).most_common(1)[0][0]


def _bar_seconds(bar):
    """"1H"/"15m"/"4D" 之类的周期串 → 秒数；不认识返回 None。"""
    m = re.match(r"^(\d+)([mHdD])$", str(bar))
    if not m:
        return None
    return int(m.group(1)) * {"m": 60, "H": 3600, "D": 86400}[m.group(2)]


def ema(values, period):
    """标准 EMA。values 为时间升序收盘价序列。"""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # 用 SMA 作种子
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    """Wilder RSI。返回 0~100。"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[-period - 1:], closes[-period:]):
        diff = cur - prev
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def macd(closes, fast=12, slow=26, signal=9):
    """返回 (dif, dea, hist)。数据不足时返回 None。"""
    if len(closes) < slow + signal:
        return None
    dif_series = _ema_series(closes, fast)
    dea_series = _ema_series(closes, slow)
    # 两条 EMA 序列起点不同（EMA12 比 EMA26 长 slow-fast 个点），必须先对齐再相减，
    # 否则 DEA/柱状值整体错位甚至符号翻转
    off = len(dif_series) - len(dea_series)
    dif_hist = [a - b for a, b in zip(dif_series[off:], dea_series)]
    dif = dif_hist[-1]
    dea = ema(dif_hist[-(signal * 3):], signal)
    if dea is None:
        return None
    return dif, dea, dif - dea


def _ema_series(values, period):
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def bollinger(closes, period=20, k=2.0):
    """返回 (mid, upper, lower)。"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    sd = var ** 0.5
    return mid, mid + k * sd, mid - k * sd


def volume_ratio(candles, n=20):
    """量比：最新一根成交量 / 前 n 根均值。"""
    if len(candles) < n + 1:
        return None
    base = [c["vol"] for c in candles[-n - 1:-1]]
    avg = sum(base) / len(base)
    return candles[-1]["vol"] / avg if avg > 0 else None


def candle_pattern(candle):
    """最后一根 K 线的简单形态分类。"""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    rng = h - l
    if rng <= 0:
        return "平盘"
    body = abs(c - o)
    body_ratio = body / rng
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if body_ratio >= 0.7:
        return "大阳线" if c > o else "大阴线"
    if body_ratio <= 0.12:
        return "十字星"
    if lower_wick >= body * 2 and lower_wick / rng >= 0.5:
        return "长下影（锤子类）"
    if upper_wick >= body * 2 and upper_wick / rng >= 0.5:
        return "长上影（流星类）"
    return "常规"


# ════════════════ 多周期结构 / 形态 / 支撑阻力（借鉴 TradingAgents、
#                   Vibe-Trading 的确定性 Skill 思路：复杂计算全部下沉到代码）════════════════

def swing_points(candles, left=2, right=2):
    """分形摆动高低点：某根 K 线的高点是左右各 left/right 根中的最高值 → 摆动高点。
    返回 (highs, lows)，元素为 (bar_index, price)，index 为 candles 下标。"""
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        window_h = [c["high"] for c in candles[i - left:i + right + 1]]
        window_l = [c["low"] for c in candles[i - left:i + right + 1]]
        if candles[i]["high"] == max(window_h):
            highs.append((i, candles[i]["high"]))
        if candles[i]["low"] == min(window_l):
            lows.append((i, candles[i]["low"]))
    return highs, lows


def structure_state(candles, left=2, right=2):
    """用最近两个摆动高/低点判断市场结构：HH+HL=上升，LH+LL=下降，其余=震荡。"""
    highs, lows = swing_points(candles, left, right)
    if len(highs) < 2 or len(lows) < 2:
        return "样本不足"
    h_prev, h_last = highs[-2][1], highs[-1][1]
    l_prev, l_last = lows[-2][1], lows[-1][1]
    if h_last > h_prev and l_last > l_prev:
        return "上升结构（HH+HL）"
    if h_last < h_prev and l_last < l_prev:
        return "下降结构（LH+LL）"
    return "震荡（区间结构）"


def sr_levels(candles, lookback=100, cluster_ratio=0.002, max_levels=4):
    """支撑阻力位：lookback 根内的摆动高低点做近邻聚合。
    返回 {"supports": [降序，离价最近在前], "resistances": [升序，离价最近在前]}。"""
    price = candles[-1]["close"]
    window = candles[-lookback:]
    highs, lows = swing_points(window)
    tol = price * cluster_ratio

    def cluster(points):
        pts = sorted(p for _, p in points)
        out = []
        for p in pts:
            if out and abs(p - out[-1][-1]) <= tol:
                out[-1].append(p)
            else:
                out.append([p])
        return sorted(sum(g) / len(g) for g in out)

    supports = [lv for lv in cluster(lows) if lv < price]
    resistances = [lv for lv in cluster(highs) if lv > price]
    return {
        "supports": [round(x, 6) for x in supports[-max_levels:]][::-1],
        "resistances": [round(x, 6) for x in resistances[:max_levels]],
    }


def detect_patterns(candles):
    """最近 3 根 K 线的经典形态识别（纯 Python，不依赖 TA-Lib）。
    返回形态名列表（可能为空）。"""
    if len(candles) < 3:
        return []
    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    def body(x):
        return abs(x["close"] - x["open"])

    def rng(x):
        return max(x["high"] - x["low"], 1e-12)

    patterns = []
    # 吞没：第二根实体完全包住第一根实体且方向相反
    if (c2["close"] > c2["open"] and c3["close"] < c3["open"]
            and c3["open"] >= c2["close"] and c3["close"] <= c2["open"]
            and body(c3) > body(c2)):
        patterns.append("看跌吞没")
    if (c2["close"] < c2["open"] and c3["close"] > c3["open"]
            and c3["open"] <= c2["close"] and c3["close"] >= c2["open"]
            and body(c3) > body(c2)):
        patterns.append("看涨吞没")
    # 晨星 / 暮星：中继小实体 + 反向大实体
    small_mid = body(c2) <= body(c1) * 0.5 and body(c2) <= body(c3) * 0.5
    if c1["close"] < c1["open"] and small_mid and c3["close"] > c3["open"] \
            and c3["close"] > (c1["open"] + c1["close"]) / 2:
        patterns.append("晨星（看涨反转）")
    if c1["close"] > c1["open"] and small_mid and c3["close"] < c3["open"] \
            and c3["close"] < (c1["open"] + c1["close"]) / 2:
        patterns.append("暮星（看跌反转）")
    # 锤子 / 流星（基于最后一根的影线比例）
    b, r = body(c3), rng(c3)
    upper = c3["high"] - max(c3["open"], c3["close"])
    lower = min(c3["open"], c3["close"]) - c3["low"]
    if lower >= b * 2 and lower / r >= 0.55:
        patterns.append("锤子线")
    if upper >= b * 2 and upper / r >= 0.55:
        patterns.append("射击之星")
    if b / r <= 0.1:
        patterns.append("十字星（犹豫）")
    return patterns


def fvg_list(candles, lookback=30, max_out=3):
    """公允价值缺口（FVG）：三根K线中第一根与第三根影线不重叠留下的缺口。
    最新一根形成的缺口视为未回补（后面还没有K线去回补它）。"""
    out = []
    n = len(candles)
    start = max(2, n - lookback)
    for i in range(start, n):
        a, c = candles[i - 2], candles[i]
        later = candles[i + 1:]
        # 看涨缺口：c.low > a.high
        if c["low"] > a["high"]:
            gap_low, gap_high = a["high"], c["low"]
            if not later or min(x["low"] for x in later) > gap_low:  # 未回补
                out.append({"dir": "bull", "low": gap_low, "high": gap_high,
                            "bars_ago": n - 1 - i})
        # 看跌缺口：c.high < a.low
        if c["high"] < a["low"]:
            gap_low, gap_high = c["high"], a["low"]
            if not later or max(x["high"] for x in later) < gap_high:
                out.append({"dir": "bear", "low": gap_low, "high": gap_high,
                            "bars_ago": n - 1 - i})
    return out[-max_out:]


def multi_timeframe(client, inst_id, cfg, bars=("15m", "1H", "4H")):
    """多周期结构：每个周期的价格位置 + 市场结构。"""
    out = {}
    for bar in bars:
        try:
            candles = client.get_candles(inst_id, bar=bar, limit=80)
            if len(candles) < 20:
                continue
            closes = [c["close"] for c in candles]
            e20 = ema(closes, 20)
            struct = structure_state(candles)
            px = closes[-1]
            trend = ("上升" if "上升" in struct else
                     "下降" if "下降" in struct else
                     "偏多" if px > e20 else "偏空" if px < e20 else "震荡")
            out[bar] = {"price": px, "ema20": e20, "structure": struct, "trend": trend}
        except Exception:  # noqa: BLE001 —— 单周期失败不阻塞整体
            continue
    return out


def build_factor_report(cfg, client, inst_id):
    """拉一次 K 线 + 资金费率，计算该标的全套因子。返回 dict（给 agent 和日志）。"""
    bar = cfg.ATR_BAR
    # 80 根足够：EMA60 需 60、MACD 需 35、量比需 21、RSI/ATR 需 15；
    # 载荷比 120 小，弱网环境下更不容易被中断
    candles = client.get_candles(inst_id, bar=bar, limit=80)
    if len(candles) < 60:
        raise ValueError(f"{inst_id} K 线数量不足（{len(candles)} 根），无法计算因子")
    # 过期数据守卫：最新已收盘 K 线比 2×bar 还旧，说明行情断流或时钟异常——
    # 静默拿过期数据算因子比直接报错更糟
    bar_sec = _bar_seconds(bar)
    if bar_sec and candles:
        stale_ms = time.time() * 1000 - candles[-1]["ts"]
        if stale_ms > 2 * bar_sec * 1000:
            raise ValueError(
                f"{inst_id} 最新已收盘K线已过期 {stale_ms / 60000:.0f} 分钟"
                f"（阈值 2×{bar}），数据疑似断流")
    closes = [c["close"] for c in candles]
    price = closes[-1]

    ema20 = ema(closes, 20)
    ema60 = ema(closes, 60)
    if price > ema20 > ema60:
        trend = "多头排列（价>EMA20>EMA60）"
    elif price < ema20 < ema60:
        trend = "空头排列（价<EMA20<EMA60）"
    else:
        trend = "交织/震荡"

    macd_v = macd(closes)
    boll = bollinger(closes)
    atr = _atr(candles, period=cfg.ATR_PERIOD)
    funding = client.get_funding_rate(inst_id)

    dif, dea, hist = macd_v
    ccy = inst_id.split("-")[0]
    obi_raw = client.get_orderbook(inst_id, depth=20)
    obi = None
    if obi_raw:
        bid_qty = sum(q for _, q in obi_raw["bids"][:20])
        ask_qty = sum(q for _, q in obi_raw["asks"][:20])
        if bid_qty + ask_qty > 0:
            obi = bid_qty / (bid_qty + ask_qty)  # >0.5 买盘厚

    oi_hist = client.get_oi_history(inst_id, period=bar, limit=2)
    oi, oi_delta_pct = None, None
    if oi_hist and len(oi_hist) >= 2:
        oi = oi_hist[-1]["oi"]
        if oi_hist[-2]["oi"] > 0:
            oi_delta_pct = (oi_hist[-1]["oi"] - oi_hist[-2]["oi"]) / oi_hist[-2]["oi"]

    report = {
        "instId": inst_id,
        "bar": bar,
        "ts": candles[-1]["ts"],
        "time": time.strftime("%m-%d %H:%M", time.localtime(candles[-1]["ts"] / 1000)),
        "price": price,
        "ema20": ema20,
        "ema60": ema60,
        "trend": trend,
        "macd": {
            "dif": dif, "dea": dea, "hist": hist,
            "state": ("金叉红柱" if dif > dea and hist > 0 else
                      "死叉绿柱" if dif < dea and hist < 0 else
                      "金叉回落" if dif > dea else "死叉修复"),
        },
        "rsi14": rsi(closes, 14),
        "atr": atr,
        "atr_pct": atr / price,
        "boll": {
            "mid": boll[0], "upper": boll[1], "lower": boll[2],
            "width_pct": (boll[1] - boll[2]) / boll[0],
        },
        "price_vs_boll": ("上轨上方" if price > boll[1] else
                          "下轨下方" if price < boll[2] else "轨道内"),
        "vol_ratio": volume_ratio(candles),
        "funding_rate": funding["funding_rate"],
        "pattern": candle_pattern(candles[-1]),
        # ── 扩展因子（确定性 Skills）──
        "patterns": detect_patterns(candles),                    # 最近3根经典形态
        "structure": structure_state(candles),                   # 主周期市场结构
        "sr": sr_levels(candles),                                # 支撑阻力位
        "fvg": fvg_list(candles),                                # 未回补公允价值缺口
        "mtf": multi_timeframe(client, inst_id, cfg),            # 多周期结构
        "obi": obi,                                              # 订单簿失衡（买盘占比）
        "oi": oi,                                                # 未平仓合约量（张）
        "oi_delta_pct": oi_delta_pct,                            # OI 一周期变化
        "ls_ratio": client.get_long_short_ratio(ccy, period=bar),      # 多空人数比
        "taker_ratio": client.get_taker_volume_ratio(ccy, period=bar), # 主动买卖量比
    }
    return report


def _atr(candles, period=14):
    trs = []
    for prev, cur in zip(candles[-period - 1:], candles[-period:]):
        trs.append(max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        ))
    return sum(trs) / len(trs)


def format_factor_report(r):
    """把因子报告压缩成一段中文文本，作为所有 agent 的共享输入（None 值安全）。"""
    macd = r["macd"]
    rsi_s = f"{r['rsi14']:.1f}" if r.get("rsi14") is not None else "—"
    vol_s = f"{r['vol_ratio']:.2f}" if r.get("vol_ratio") is not None else "—"
    lines = [
        f"{r['instId']}（{r['bar']}，截至 {r['time']}）",
        f"  价格 {r['price']:g}；EMA20 {r['ema20']:.4g} / EMA60 {r['ema60']:.4g}"
        f" → {r['trend']}；市场结构：{r['structure']}",
        f"  MACD：DIF {macd['dif']:.4g} / DEA {macd['dea']:.4g} / 柱 {macd['hist']:.4g}"
        f"（{macd['state']}）",
        f"  RSI14 {rsi_s}；ATR {r['atr']:.4g}（{r['atr_pct']:.2%}）；"
        f"布林宽 {r['boll']['width_pct']:.2%}，价格{r['price_vs_boll']}",
        f"  量比 {vol_s}；资金费率 {r['funding_rate']:+.4%}；"
        f"末根K线形态：{r['pattern']}",
    ]
    if r.get("patterns"):
        lines.append(f"  近3根形态：{'、'.join(r['patterns'])}")
    sr = r.get("sr") or {}
    if sr.get("supports") or sr.get("resistances"):
        sup = ", ".join(f"{x:g}" for x in sr.get("supports", [])) or "无"
        res = ", ".join(f"{x:g}" for x in sr.get("resistances", [])) or "无"
        lines.append(f"  支撑位（近→远）：{sup}；阻力位（近→远）：{res}")
    if r.get("fvg"):
        gaps = "；".join(
            f"{'看涨' if g['dir']=='bull' else '看跌'}缺口 {g['low']:g}~{g['high']:g}"
            f"（{g['bars_ago']}根前）" for g in r["fvg"])
        lines.append(f"  未回补FVG：{gaps}")
    mtf = r.get("mtf") or {}
    if mtf:
        mtf_str = " | ".join(
            f"{tf}:{v['trend']}({v['structure'][:2]})" for tf, v in mtf.items())
        lines.append(f"  多周期：{mtf_str}")
    micro = []
    if r.get("obi") is not None:
        micro.append(f"订单簿买盘占比 {r['obi']:.0%}")
    if r.get("oi") is not None:
        od = f"（近{r['bar']}变化 {r['oi_delta_pct']:+.1%}）" if r.get("oi_delta_pct") is not None else ""
        micro.append(f"未平仓规模 {r['oi']:.3g}{od}")
    if r.get("ls_ratio") is not None:
        micro.append(f"多空人数比 {r['ls_ratio']:.2f}")
    if r.get("taker_ratio") is not None:
        micro.append(f"主动买卖量比 {r['taker_ratio']:.2f}")
    if micro:
        lines.append(f"  衍生品/微观：{'；'.join(micro)}")
    return "\n".join(lines)
