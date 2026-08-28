# -*- coding: utf-8 -*-
"""Planner 角色（第四步交付物）：根据市场快照生成交易计划。

两种模式：
    llm      —— 有 LLM 配置时，把行情/账户上下文喂给模型，要求输出 JSON 计划
    baseline —— 无 LLM 时的内置基线策略：1H 均线趋势方向 + 1.5×ATR 止损，
                逻辑简单确定，主要用于先跑通全流程，不是赚钱策略

计划 schema（与 risk.py 对齐）：
    {"action": "open"/"hold",
     "instId": "...-SWAP", "direction": "long"/"short",
     "stop_loss": px, "entry_hint": px(可选),
     "order_type": "limit_maker",      # 固定，风控强制
     "confidence": 0~1, "reason": "..."}
"""
import time

from llm import LLMClient


def markets_view(snapshot):
    """把委员会版快照（factors 键）适配成旧版 markets 视图，保持两种决策模式可用。"""
    if snapshot.get("markets"):
        return snapshot["markets"]
    view = {}
    for inst, f in (snapshot.get("factors") or {}).items():
        if not f:
            continue
        view[inst] = {
            "ticker": {"last": f.get("price"), "bid": None, "ask": None},
            "atr": f.get("atr"),
            "funding": {"funding_rate": f.get("funding_rate", 0.0)},
        }
    return view

PLANNER_SYSTEM = """你是一个谨慎的加密货币永续合约交易 Planner（OKX 模拟盘）。
你会收到若干标的的市场快照（价格、ATR 波动、资金费率、近期K线）和账户状态。
你的任务是给出【最多一个】开仓计划，或者选择观望（hold）。

硬性要求：
1. 必须给出明确止损价：多仓止损在当前价下方，空仓在上方，距离在 0.2%~5% 之间。
2. 不要提出市场价单，只做 Maker 限价单（order_type 固定为 limit_maker）。
3. 仓位大小不用你计算（风控模块会按单笔最大亏损 1% 权益反推张数）。
4. 没有清晰信号就 hold，宁缺毋滥。
5. 只输出 JSON，格式：
   开仓: {"action":"open","instId":"BTC-USDT-SWAP","direction":"long","stop_loss":79000.0,
          "entry_hint":79500.0,"order_type":"limit_maker","confidence":0.6,
          "reason":"一句话理由"}
   观望: {"action":"hold","reason":"一句话理由"}"""


class Planner:
    def __init__(self, cfg, client, llm=None):
        self.cfg = cfg
        self.client = client
        self.llm = llm or LLMClient(cfg, logger=client.log)
        self.log = client.log

    # ────────────────────────── 主入口 ──────────────────────────

    def decide(self, snapshot):
        """snapshot: {"equity", "positions", "markets": {instId: {ticker, atr, funding}}}"""
        if self.llm.available:
            try:
                return self._decide_llm(snapshot)
            except Exception as e:  # noqa: BLE001 —— LLM 挂了降级，不打断循环
                self.log.warning("LLM Planner 失败（%s），降级为基线策略", e)
        return self._decide_baseline(snapshot)

    # ────────────────────────── LLM 模式 ──────────────────────────

    def _decide_llm(self, snapshot):
        user = self._build_context(snapshot)
        plan = self.llm.chat(PLANNER_SYSTEM, user, expect_json=True)
        plan["planner"] = "llm"
        if plan.get("action") == "open":
            plan.setdefault("order_type", "limit_maker")
            self.log.info("Planner(LLM)：开仓计划 %s %s 止损%s（%s）",
                          plan.get("instId"), plan.get("direction"),
                          plan.get("stop_loss"), plan.get("reason", ""))
        else:
            plan.setdefault("action", "hold")
            self.log.info("Planner(LLM)：hold（%s）", plan.get("reason", ""))
        return plan

    def _build_context(self, snapshot):
        lines = [f"账户权益: {snapshot['equity']:.2f} USDT",
                 f"当前持仓: {snapshot['positions'] or '无'}", "市场快照:"]
        for inst_id, m in markets_view(snapshot).items():
            t, f, a = m["ticker"], m["funding"], m["atr"]
            lines.append(
                f"- {inst_id}: 最新 {t['last']}, 买一 {t['bid']}, 卖一 {t['ask']}, "
                f"ATR({self.cfg.ATR_BAR},{self.cfg.ATR_PERIOD})={a:.1f}, "
                f"资金费率 {f['funding_rate']:.4%}"
            )
        lines.append(f"可交易标的: {self.cfg.SYMBOLS}")
        lines.append("请给出本轮决策（JSON）。")
        return "\n".join(lines)

    # ────────────────────────── 基线模式（无 LLM）──────────────────────────

    def _decide_baseline(self, snapshot):
        """基线策略：挑第一个无持仓的标的，按 1H EMA20 趋势方向给计划。"""
        held = {p["instId"] for p in snapshot["positions"]}
        for inst_id in self.cfg.SYMBOLS:
            if inst_id in held:
                continue
            m = markets_view(snapshot).get(inst_id)
            if not m or not m.get("atr"):
                continue
            plan = self._baseline_plan_for(inst_id, m)
            if plan:
                return plan
        return {"action": "hold", "planner": "baseline",
                "reason": "基线策略：所有标的已有持仓或数据不足"}

    def _baseline_plan_for(self, inst_id, m):
        """单标的基线信号：收盘价 vs 1H EMA20 定方向，止损 = 1.5×ATR。"""
        try:
            candles = self.client.get_candles(inst_id, bar=self.cfg.ATR_BAR,
                                              limit=20)
        except Exception as e:  # noqa: BLE001
            self.log.warning("%s 拉 K 线失败：%s", inst_id, e)
            return None
        if len(candles) < 20:
            return None
        closes = [c["close"] for c in candles]
        ema20 = sum(closes) / len(closes)  # 用简单均值近似，基线不需要精确 EMA
        last = candles[-1]["close"]
        atr = m["atr"]
        stop_dist = self.cfg.ATR_STOP_MULT * atr

        if last > ema20:
            direction, stop = "long", last - stop_dist
            why = f"价格 {last:.1f} 在 1H 均线 {ema20:.1f} 上方，趋势偏多"
        elif last < ema20:
            direction, stop = "short", last + stop_dist
            why = f"价格 {last:.1f} 在 1H 均线 {ema20:.1f} 下方，趋势偏空"
        else:
            return None

        plan = {
            "action": "open",
            "instId": inst_id,
            "direction": direction,
            "stop_loss": round(stop, 4),
            "entry_hint": last,
            "order_type": "limit_maker",
            "confidence": 0.5,
            "reason": f"[基线策略] {why}；止损 {self.cfg.ATR_STOP_MULT}×ATR={stop_dist:.1f}",
            "planner": "baseline",
        }
        self.log.info("Planner(基线)：%s %s 止损 %s", inst_id, direction, stop)
        return plan
