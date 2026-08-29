# -*- coding: utf-8 -*-
"""飞书群机器人通知：交互卡片格式（挂单/成交/平仓/仓位速览/风控告警）。

一个 requests.post 就够，不需要 SDK。卡片 schema 见飞书开放平台
"发送消息卡片"（msg_type=interactive）。所有发送失败静默——告警永远
不能打断交易循环。
"""
import time

import requests


def _post(url, payload):
    if not url:
        return False
    try:
        resp = requests.post(url, json=payload, timeout=8)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def send_feishu(url, title, content, is_at_all=False):
    """旧版纯文本通知（保留兼容）。"""
    if not url:
        return False
    payload = {"msg_type": "text",
               "content": {"text": f"[okx-trader] {title}\n{content}"},
               "at": {"isAtAll": is_at_all}}
    return _post(url, payload)


# 卡片头颜色：按事件语义映射（飞书 template 取值）
_COLOR = {"info": "blue", "warn": "orange", "error": "red", "critical": "red",
          "profit": "green", "loss": "red"}

_TITLE = {
    "order_placed":  "📋 挂单",
    "order_filled":  "✅ 成交",
    "trade_closed":  "💰 平仓",
    "time_stop":     "⏱ 时间止损平仓",
    "trailing_stop": "📈 移动止损上移",
    "round_done":    "📊 轮次速览 · 持仓与盈亏",
    "circuit_breaker": "⛔ 回撤熔断",
    "naked_position":  "🚨 裸仓告警",
    "data_degraded":   "⚠️ 行情数据中断",
    "risk_rejected":   "🛑 风控否决",
    "judge_quorum":    "⚖️ 裁判缺席",
    "hallucinated_number": "🔮 数字核对未通过",
    "factor_status":   "🧪 因子状态变更",
    "env_switch":      "⚙️ 运行环境",
    "paused":          "⏸ 循环已暂停",
    "resumed":         "▶️ 循环已恢复",
}

# ctx 字段 → 卡片字段中文名（按此顺序展示；未列出的忽略）
_FIELD_ZH = [
    ("inst_id", "标的"), ("direction", "方向"), ("contracts", "张数"),
    ("px", "委托价"), ("avg_px", "成交均价"), ("stop", "止损"),
    ("target", "止盈"), ("rr", "盈亏比"), ("risk_usdt", "单笔风险(U)"),
    ("notional_usdt", "名义价值(U)"), ("equity", "账户权益(U)"),
    ("drawdown", "当前回撤"), ("realized_pnl", "已实现盈亏(U)"),
    ("r_multiple", "R倍数"), ("exit_reason", "出场原因"),
    ("upl", "浮动盈亏(U)"), ("positions_n", "持仓数"),
    ("regime", "市况"), ("score", "委员会评分"),
]


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.4g}"
    return str(v)


def event_card(ctx):
    """把 hook ctx 渲染成飞书交互卡片 dict。"""
    kind = ctx.get("kind", "event")
    level = ctx.get("level", "info")
    color = _COLOR.get(level, "blue")
    if kind == "trade_closed":
        pnl = ctx.get("realized_pnl")
        if pnl is not None:
            color = "profit" if pnl >= 0 else "loss"
    title = _TITLE.get(kind, kind)
    if ctx.get("inst_id"):
        title += f" · {ctx['inst_id']}"

    elements = []
    fields = []
    for key, zh in _FIELD_ZH:
        if key in ctx and ctx[key] is not None:
            v = ctx[key]
            if key == "direction":
                v = "做多 🟢" if v == "long" else "做空 🔴"
            fields.append((zh, _fmt(v)))
    if fields:
        for i in range(0, len(fields), 2):
            elements.append({"tag": "div", "fields": [
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": f"**{zh}**\n{v}"}}
                for zh, v in fields[i:i + 2]]})
    if ctx.get("message"):
        elements.append({"tag": "div", "text": {
            "tag": "lark_md", "content": ctx["message"]}})
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
        "content": "okx-trader · "
                   + time.strftime("%Y-%m-%d %H:%M:%S") + " · 模拟盘"}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color,
                   "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def send_card(url, ctx, level_floor="info"):
    """按 ctx 渲染卡片并发送。级别低于阈值的直接跳过。"""
    if not url:
        return False
    order = {"info": 0, "warn": 1, "error": 2, "critical": 3}
    if order.get(ctx.get("level", "info"), 0) < order.get(level_floor, 0):
        return False
    return _post(url, {"msg_type": "interactive",
                       "card": event_card(ctx)})


def make_hook(url, level_floor="info"):
    """hooks.register 用的回调：达到级别阈值才外发。"""
    def _hook(ctx):
        send_card(url, ctx, level_floor=level_floor)
    return _hook
