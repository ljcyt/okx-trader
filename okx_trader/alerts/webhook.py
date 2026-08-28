# -*- coding: utf-8 -*-
"""飞书群机器人外发通知器：~40 行换个 URL 就能发 Telegram/DingTalk。

配置 okx_config.py：ALERT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
"""
import requests


def send_feishu(url, title, content, is_at_all=False):
    """飞书自定义机器人 text 消息。失败静默（告警永远不能打断交易）。"""
    if not url:
        return False
    try:
        resp = requests.post(url, json={
            "msg_type": "text",
            "content": {"text": f"[okx-trader] {title}\n{content}"},
            "at": {"isAtAll": is_at_all},
        }, timeout=8)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def make_hook(url, level_floor="warn"):
    """生成 hooks.register 用的回调：级别达到阈值才外发。"""
    order = {"info": 0, "warn": 1, "error": 2, "critical": 3}
    floor = order.get(level_floor, 1)

    def _hook(ctx):
        level = ctx.get("level", "info")
        if order.get(level, 0) >= floor:
            send_feishu(url, f"{ctx.get('kind', 'event')}（{level}）",
                        ctx.get("message", ""))
    return _hook
