# -*- coding: utf-8 -*-
"""多 Agent 委员会（用户确认的架构：3 分析师提案 + 3 裁判打分 + 聚合）

流程：
    1. 三位分析师（不同交易风格人设）各自读因子报告 → 独立提案或弃权
       提案：{instId, direction, stop_loss, confidence, reason}
    2. 三位裁判（技术/风控/资金管理视角）对每份提案打分 0-10 + 通过/否决
    3. 聚合：平均分 ≥ SCORE_THRESHOLD 且 ≥2/3 裁判通过 → 得分最高者胜出
    4. 胜出提案交给硬风控（risk.py）终审 —— 委员会没有下单权

LLM 模式：每个 agent 独立调用模型（同一模型不同人设 prompt）。
基线模式（无 LLM）：每个 agent 用确定性规则模拟，保证链路可先跑通。
"""
import json
import os
import time
from collections import deque

from llm import LLMClient

HERE = os.path.dirname(os.path.abspath(__file__))
ROUNDS_JSONL = os.path.join(HERE, "data", "rounds", "rounds.jsonl")

# 裁判法定人数：有效票不足此数时提案不能胜出（防止 LLM 超时导致单裁判独裁）
MIN_JUDGE_VOTES = 2

# ── 三位分析师：人设与风格 ──────────────────────────────────────────
ANALYSTS = [
    {
        "name": "趋势猎手", "style": "trend",
        "prompt": ("你是「趋势猎手」，一位趋势跟踪交易员。你只在趋势明确时出手："
                   "多头排列+MACD红柱→做多；空头排列+MACD绿柱→做空。"
                   "震荡市和指标矛盾时果断弃权。止损必须放在趋势失效位（如1.5×ATR外）。"),
    },
    {
        "name": "均值回归者", "style": "meanrev",
        "prompt": ("你是「均值回归者」，专做超买超卖的回归。RSI>70 且价格触布林上轨→做空；"
                   "RSI<30 且价格触布林下轨→做多。中位区间一律弃权。"
                   "止损放在区间外缓冲（约1×ATR），方向逆势所以仓位概念上更保守。"),
    },
    {
        "name": "资金哨兵", "style": "funding",
        "prompt": ("你是「资金哨兵」，关注资金费率透露的头寸拥挤度："
                   "资金费率显著为正(≥+0.05%)说明多头拥挤→倾向做空；"
                   "显著为负(≤-0.05%)→倾向做多；费率中性→弃权。"
                   "你必须同时参考趋势因子，逆势单要求更紧的止损。"),
    },
]

# ── 三位裁判：打分视角 ──────────────────────────────────────────────
JUDGES = [
    {"name": "技术裁判",
     "prompt": ("你是「技术裁判」。审查提案的方向是否与趋势/动量/形态因子自洽："
               "顺趋势且指标配合→8-9分；方向与主趋势相悖→3-4分；理由与数据矛盾→0-2分。"
               "同时检查止损价方向正确、距离合理(0.2%~5%)。")},
    {"name": "风控裁判",
     "prompt": ("你是「风控裁判」。只看风险：止损距离是否落在合理区间；"
               "波动是否极端(ATR/价格>3%要扣分)；资金费率是否异常(|费率|>0.1%要扣分)；"
               "量比异常放大(>3)要扣分。无硬伤→7-8分，有隐患→4-6分，危险→0-3分。")},
    {"name": "资金管理裁判",
     "prompt": ("你是「资金管理裁判」。评估这笔交易值不值得占用风险预算："
               "置信度、盈亏比(止损距离 vs 现实波动)、当前账户是否已有暴露。"
               "高质量出手→8分左右，平庸机会→5-6分，差机会→0-4分。")},
]

SCORE_THRESHOLD_DEFAULT = 6.5


class Committee:
    def __init__(self, cfg, client, llm=None):
        self.cfg = cfg
        self.client = client
        self.llm = llm or LLMClient(cfg, logger=client.log)
        self.log = client.log
        self.threshold = getattr(cfg, "SCORE_THRESHOLD", SCORE_THRESHOLD_DEFAULT)

    # ────────────────────────── 主入口 ──────────────────────────

    def decide(self, snapshot):
        """snapshot 需包含 equity / positions / factors（factors.py 的报告）。
        返回完整决策记录（写进 rounds 日志），其中 plan 字段供下游风控/执行使用。
        """
        factor_text = "\n".join(
            format_one(snapshot["factors"][inst])
            for inst in self.cfg.SYMBOLS if snapshot["factors"].get(inst)
        )
        held = {p["instId"] for p in snapshot["positions"]}
        account_ctx = (f"账户权益 {snapshot['equity']:.2f} USDT；"
                       f"当前持仓：{held if held else '无'}；"
                       f"回撤：{snapshot['drawdown']:.1%}")
        memory = self.recent_rounds_summary()
        if memory:
            account_ctx += f"\n近期委员会决策记录（供反思参考，避免连续重复失败思路）：\n{memory}"

        # 1. 分析师提案
        proposals = []
        analyst_log = []
        for a in ANALYSTS:
            prop = self._ask_analyst(a, factor_text, account_ctx, held, snapshot)
            analyst_log.append(prop)
            if prop.get("action") == "open":
                proposals.append(prop)
        self.log.info("委员会：分析师产出 %d 份有效提案 / %d 份弃权",
                      len(proposals), len(analyst_log) - len(proposals))

        if not proposals:
            return {"action": "hold", "mode": self.llm_mode_name(),
                    "analysts": analyst_log,
                    "reason": "所有分析师弃权，本轮不交易"}

        # 2. 裁判打分
        judging = self._ask_judges(proposals, factor_text, account_ctx, snapshot)

        # 3. 聚合：均分门槛 + 多数决
        #    有效票数 = 该提案实际收到的裁判分（LLM 裁判失败会缺席）
        #    通过票数门槛 = floor(有效票/2)+1（即过半；3票时为2）
        for i, p in enumerate(proposals):
            rows = [row for row in judging["rows"] if row["idx"] == i]
            scores = [row["score"] for row in rows]
            votes = sum(1 for row in rows if row["approved"])
            p["avg_score"] = round(sum(scores) / len(scores), 2) if scores else 0
            p["votes"] = f"{votes}/{len(rows) or len(JUDGES)}"
            p["qualify"] = (len(scores) >= MIN_JUDGE_VOTES
                            and p["avg_score"] >= self.threshold
                            and votes * 2 > len(scores))

        qualified = [p for p in proposals if p.get("qualify")]
        qualified.sort(key=lambda p: (p["avg_score"], _f(p.get("confidence"))), reverse=True)

        decision = {
            "mode": self.llm_mode_name(),
            "analysts": analyst_log,
            "judging": judging["rows"],
            "scoreboard": [
                {"analyst": p["analyst"], "instId": p["instId"],
                 "direction": p["direction"], "avg_score": p["avg_score"],
                 "votes": p["votes"], "qualify": p["qualify"]}
                for p in proposals
            ],
        }
        if qualified:
            win = qualified[0]
            others = [f"{p['analyst']}({p['avg_score']})" for p in qualified[1:]]
            # 把胜出提案对应标的的因子快照（支撑阻力/ATR）附给风控，用于盈亏比检查
            src_report = (snapshot.get("factors") or {}).get(win["instId"]) or {}
            decision["action"] = "open"
            decision["plan"] = {
                "instId": win["instId"],
                "direction": win["direction"],
                "stop_loss": win["stop_loss"],
                "entry_hint": win.get("entry_hint"),
                "order_type": "limit_maker",
                "confidence": win.get("confidence"),
                "reason": (f"[{win['analyst']}] {win['reason']} "
                           f"｜委员会均分 {win['avg_score']}（{win['votes']} 通过）"),
                "committee_score": win["avg_score"],
                "factors": {k: src_report[k] for k in ("sr", "atr") if k in src_report},
            }
            decision["reason"] = (
                f"胜出提案：{win['analyst']} → {win['instId']} {win['direction']} "
                f"均分 {win['avg_score']}（{win['votes']} 通过）"
                + (f"；同分候选：{', '.join(others)}" if others else ""))
            self.log.info("委员会：%s", decision["reason"])
        else:
            decision["action"] = "hold"
            decision["reason"] = (
                "有提案但未达标：" +
                ("；".join(f"{p['analyst']} 均分{p['avg_score']}（{p['votes']}）"
                           for p in proposals) or "无"))
            self.log.info("委员会：%s", decision["reason"])
        return decision

    def llm_mode_name(self):
        return "llm" if self.llm.available else "baseline"

    def recent_rounds_summary(self, n=8):
        """轻量记忆/反思：读最近 n 轮的落盘记录，压成几行文字回喂给 LLM agent，
        让委员会知道自己最近提了什么、结果如何（借鉴 LLM_trader 的历史记忆机制）。"""
        if not os.path.exists(ROUNDS_JSONL):
            return ""
        lines = []
        try:
            with open(ROUNDS_JSONL, "r", encoding="utf-8") as f:
                recent = deque(f, maxlen=n)  # 只驻留最后 n 行，不整文件载入
            for raw in recent:
                try:
                    r = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = time.strftime("%m-%d %H:%M", time.localtime(float(r.get("ts") or 0)))
                plan = (r.get("committee") or {}).get("plan") or r.get("planner") or {}
                what = ""
                if plan:
                    what = (f"{plan.get('instId')} {plan.get('direction')}"
                            f"（均分{plan.get('committee_score', '—')}）")
                lines.append(f"  {ts} 状态={r.get('status')} {what}")
        except OSError:
            return ""
        return "\n".join(lines) if lines else ""

    # ────────────────────────── 分析师 ──────────────────────────

    def _ask_analyst(self, analyst, factor_text, account_ctx, held, snapshot):
        """单个分析师：LLM 或基线规则。返回统一结构的提案/弃权记录。"""
        base = {"analyst": analyst["name"], "style": analyst["style"]}
        if self.llm.available:
            user = (f"{factor_text}\n\n{account_ctx}\n\n"
                    f"注意：已有持仓的标的不允许再提（当前持仓：{held or '无'}）。"
                    f"请以你的人设给出本轮决策，只输出 JSON：\n"
                    f'开仓: {{"action":"open","instId":"...","direction":"long|short",'
                    f'"stop_loss":数字,"confidence":0~1,"reason":"..."}}\n'
                    f'弃权: {{"action":"hold","reason":"..."}}')
            try:
                out = self.llm.chat(analyst["prompt"], user, expect_json=True)
                out.update(base)
                if out.get("action") == "open":
                    out.setdefault("order_type", "limit_maker")
                    # 基本合法性：字段缺失或标的不在白名单，直接降级为弃权
                    if not (out.get("instId") in self.cfg.SYMBOLS
                            and out.get("direction") in ("long", "short")
                            and float(out.get("stop_loss") or 0) > 0):
                        return {**base, "action": "hold",
                                "reason": f"提案不合法被丢弃（标的需在 {self.cfg.SYMBOLS} 内，"
                                          f"且方向/止损齐全）"}
                    out["stop_loss"] = float(out["stop_loss"])
                self.log.info("分析师[%s]（LLM）：%s", analyst["name"],
                              "开仓 %s %s" % (out.get("instId"), out.get("direction"))
                              if out.get("action") == "open" else "弃权")
                return out
            except Exception as e:  # noqa: BLE001 —— 单个 agent 失败不影响委员会
                self.log.warning("分析师[%s] LLM 失败：%s", analyst["name"], e)
                return {**base, "action": "hold", "reason": f"调用失败：{e}"}
        ctx = {"cfg": self.cfg,
               "reports": snapshot.get("factors") or {},
               "held": held}
        return {**_baseline_analyst(analyst["style"], ctx), **base}

    # ────────────────────────── 裁判 ──────────────────────────

    def _ask_judges(self, proposals, factor_text, account_ctx, snapshot):
        """所有裁判对所有提案打分。返回 {"rows": [{idx, judge, score, approved, concerns}]}。"""
        rows = []
        if self.llm.available:
            props_text = "\n".join(
                f"提案{i}（来自{p['analyst']}）：{p['instId']} {p['direction']} "
                f"止损 {p['stop_loss']} 置信度 {p.get('confidence')}；理由：{p['reason']}"
                for i, p in enumerate(proposals))
            for j in JUDGES:
                user = (f"因子报告：\n{factor_text}\n\n{account_ctx}\n\n候选提案：\n{props_text}\n\n"
                        f"请以你的人设给每份提案打分，只输出 JSON："
                        f'{{"scores":[{{"idx":0,"score":0~10,"approved":true/false,'
                        f'"concerns":"一句话"}}]}}')
                try:
                    out = self.llm.chat(j["prompt"], user, expect_json=True)
                    for row in out.get("scores", []):
                        idx = int(row.get("idx", -1))
                        if 0 <= idx < len(proposals):
                            rows.append({"idx": idx, "judge": j["name"],
                                         "score": float(row.get("score", 0)),
                                         "approved": bool(row.get("approved")),
                                         "concerns": row.get("concerns", "")})
                except Exception as e:  # noqa: BLE001
                    self.log.warning("裁判[%s] LLM 失败，该票作废：%s", j["name"], e)
        else:
            # 基线裁判：确定性规则打分
            for j in JUDGES:
                for i, p in enumerate(proposals):
                    score, concerns = _baseline_judge(j["name"], p, snapshot, self.cfg)
                    rows.append({"idx": i, "judge": j["name"], "score": score,
                                 "approved": score >= 6, "concerns": concerns})
        return {"rows": rows}


# ────────────────────────── 基线（无 LLM）确定性规则 ──────────────────────────

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _baseline_analyst(style, ctx):
    """ctx: {"reports": {instId: factor_report}, "held": set, "atr_mult": cfg值}"""
    reports, held, cfg = ctx["reports"], ctx["held"], ctx["cfg"]
    stop_mult = cfg.ATR_STOP_MULT
    for inst_id in cfg.SYMBOLS:
        r = reports.get(inst_id)
        if not r or inst_id in held:
            continue
        price, atr = r["price"], r["atr"]

        if style == "trend":
            mtf = r.get("mtf") or {}
            t4h = (mtf.get("4H") or {}).get("trend", "")
            if "多头排列" in r["trend"] and r["rsi14"] < 75 and "下降" not in t4h:
                return {"action": "open", "instId": inst_id, "direction": "long",
                        "stop_loss": round(price - stop_mult * atr, 4),
                        "confidence": 0.65, "order_type": "limit_maker",
                        "reason": f"多头排列+MACD{r['macd']['state']}，4H不逆势，趋势做多"}
            if "空头排列" in r["trend"] and r["rsi14"] > 25 and "上升" not in t4h:
                return {"action": "open", "instId": inst_id, "direction": "short",
                        "stop_loss": round(price + stop_mult * atr, 4),
                        "confidence": 0.65, "order_type": "limit_maker",
                        "reason": f"空头排列+MACD{r['macd']['state']}，4H不逆势，趋势做空"}
        elif style == "meanrev":
            if r["rsi14"] >= 70 and "上轨" in r["price_vs_boll"]:
                return {"action": "open", "instId": inst_id, "direction": "short",
                        "stop_loss": round(price + 1.0 * atr, 4),
                        "confidence": 0.5, "order_type": "limit_maker",
                        "reason": f"RSI {r['rsi14']:.0f} 超买且触布林上轨，回归做空"}
            if r["rsi14"] <= 30 and "下轨" in r["price_vs_boll"]:
                return {"action": "open", "instId": inst_id, "direction": "long",
                        "stop_loss": round(price - 1.0 * atr, 4),
                        "confidence": 0.5, "order_type": "limit_maker",
                        "reason": f"RSI {r['rsi14']:.0f} 超卖且触布林下轨，回归做多"}
        elif style == "funding":
            fr = r["funding_rate"]
            if fr >= 0.0005:
                return {"action": "open", "instId": inst_id, "direction": "short",
                        "stop_loss": round(price + 1.2 * atr, 4),
                        "confidence": 0.45, "order_type": "limit_maker",
                        "reason": f"资金费率 {fr:+.3%} 多头拥挤，逆向做空"}
            if fr <= -0.0005:
                return {"action": "open", "instId": inst_id, "direction": "long",
                        "stop_loss": round(price - 1.2 * atr, 4),
                        "confidence": 0.45, "order_type": "limit_maker",
                        "reason": f"资金费率 {fr:+.3%} 空头拥挤，逆向做多"}
    return {"action": "hold", "reason": "无符合人设信号"}


def _baseline_judge(judge_name, p, snapshot, cfg):
    """基线裁判打分（0-10）。p 为提案，snapshot 用于取该标的因子。"""
    r = (snapshot.get("factors") or {}).get(p.get("instId")) or {}
    score, concerns = 8.0, []
    direction, stop = p.get("direction"), _f(p.get("stop_loss"))
    entry = p.get("entry_hint") or r.get("price") or 0

    if judge_name == "技术裁判":
        if r:
            if direction == "long" and "空头" in r["trend"]:
                score, concerns = 3.5, ["做多逆主趋势"]
            elif direction == "short" and "多头" in r["trend"]:
                score, concerns = 3.5, ["做空逆主趋势"]
    elif judge_name == "风控裁判":
        if entry > 0 and stop > 0:
            dist = abs(entry - stop) / entry
            if dist < cfg.MIN_STOP_DIST_PCT:
                score, concerns = 5.0, [f"止损过近 {dist:.2%}"]
            elif dist > cfg.MAX_STOP_DIST_PCT:
                score, concerns = 2.0, [f"止损过远 {dist:.2%}"]
        else:
            score, concerns = 2.0, ["止损字段异常"]
        if r and abs(r.get("funding_rate", 0)) > 0.001:
            score -= 1.5
            concerns.append("资金费率异常")
        if r and r.get("atr_pct", 0) > 0.03:
            score -= 1.0
            concerns.append("波动极端")
    elif judge_name == "资金管理裁判":
        conf = _f(p.get("confidence"))
        score = 8.0 if conf >= 0.6 else 6.5 if conf >= 0.45 else 5.0
        if r and r.get("vol_ratio", 1) and r["vol_ratio"] > 3:
            score -= 1.0
            concerns.append("异常放量")
    score = max(0.0, min(10.0, score))
    return round(score, 1), "；".join(concerns) or "无异议"


def format_one(report):
    from factors import format_factor_report
    return format_factor_report(report)
