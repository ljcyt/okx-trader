# -*- coding: utf-8 -*-
"""Critic 角色（第四步交付物）：审查 Planner 的计划，做第二道软风控。

与 risk.py 的分工：
    risk.py   —— 硬约束，代码强制，一票否决（最终闸门，永远生效）
    critic    —— 软审查（逻辑一致性、市场环境异常），可否决但不放松 risk.py

两种模式：
    llm      —— 把计划+市场快照交给模型审
    baseline —— 无 LLM 时自动通过（硬约束已由 risk.py 兜底），只做基本字段检查
"""
from llm import LLMClient
from planner import markets_view

CRITIC_SYSTEM = """你是一个保守的量化风控 Critic。你会收到一份交易计划和市场快照。
请从以下角度审查，任何一条不满足就拒绝：
1. 止损价与方向一致（多仓在下方/空仓在上方），且距离合理（0.2%~5%）。
2. 理由与方向逻辑自洽（不能说看空却开多）。
3. 资金费率异常（|费率| > 0.3%）或波动极端（ATR/价格 > 3%）时倾向拒绝。
4. 账户已有多方向混杂持仓、或当前接近回撤阈值时倾向拒绝。
只输出 JSON：{"approved": true/false, "concerns": "审查意见，一句话"}"""


class Critic:
    def __init__(self, cfg, client, llm=None):
        self.cfg = cfg
        self.client = client
        self.llm = llm or LLMClient(cfg, logger=client.log)
        self.log = client.log

    def review(self, plan, snapshot):
        """返回 {"approved": bool, "concerns": str, "critic": "llm"/"baseline"}。"""
        if plan.get("action") != "open":
            return {"approved": True, "concerns": "非开仓计划，无需审查", "critic": "none"}

        if self.llm.available:
            try:
                return self._review_llm(plan, snapshot)
            except Exception as e:  # noqa: BLE001
                self.log.warning("LLM Critic 失败（%s），降级为基线审查", e)
        return self._review_baseline(plan, snapshot)

    # ────────────────────────── LLM 模式 ──────────────────────────

    def _review_llm(self, plan, snapshot):
        user = self._build_context(plan, snapshot)
        verdict = self.llm.chat(CRITIC_SYSTEM, user, expect_json=True)
        verdict["approved"] = bool(verdict.get("approved"))
        verdict["critic"] = "llm"
        self.log.info("Critic(LLM)：%s（%s）",
                      "通过" if verdict["approved"] else "拒绝", verdict.get("concerns", ""))
        return verdict

    def _build_context(self, plan, snapshot):
        lines = ["交易计划:", str({k: plan.get(k) for k in (
            "instId", "direction", "stop_loss", "entry_hint", "reason")})]
        lines.append(f"账户权益: {snapshot['equity']:.2f} USDT")
        lines.append(f"当前持仓: {snapshot['positions'] or '无'}")
        m = markets_view(snapshot).get(plan.get("instId", ""), {})
        if m:
            t, f, a = m["ticker"], m["funding"], m["atr"]
            lines.append(
                f"该标的市场: 最新 {t['last']}, ATR={a:.1f}, 资金费率 {f['funding_rate']:.4%}")
        lines.append("请审查并输出 JSON。")
        return "\n".join(lines)

    # ────────────────────────── 基线模式（无 LLM）──────────────────────────

    def _review_baseline(self, plan, snapshot):
        """无 LLM 的机械审查：方向/止损自洽 + 资金费率极端拒绝。其余交给 risk.py。"""
        concerns = []
        try:
            stop = float(plan.get("stop_loss") or 0)
            entry = float(plan.get("entry_hint") or 0)
            direction = plan.get("direction")
            if entry > 0 and stop > 0:
                dist_pct = abs(entry - stop) / entry
                if direction == "long" and stop >= entry:
                    concerns.append("多仓止损不在入场价下方")
                elif direction == "short" and stop <= entry:
                    concerns.append("空仓止损不在入场价上方")
                elif dist_pct > 0.05:
                    concerns.append(f"止损距离 {dist_pct:.1%} 过远")
        except (TypeError, ValueError):
            concerns.append("计划字段类型异常")

        m = markets_view(snapshot).get(plan.get("instId", ""), {})
        if m:
            rate = m["funding"]["funding_rate"]
            if abs(rate) > 0.003:
                concerns.append(f"资金费率异常 {rate:.4%}")

        approved = not concerns
        self.log.info("Critic(基线)：%s%s", "通过" if approved else "拒绝",
                      "" if approved else "（" + "；".join(concerns) + "）")
        return {"approved": approved,
                "concerns": "；".join(concerns) or "基线审查通过", "critic": "baseline"}
