# -*- coding: utf-8 -*-
"""LLM 适配层（第四步交付物之一）

极简 OpenAI 兼容客户端（requests 实现，无额外依赖）。
配置在 okx_config.py 的 LLM_API_BASE / LLM_API_KEY / LLM_MODEL 三项；
任何一项为空 → available=False，交易循环自动降级为基线策略。
"""
import json

import requests


class LLMClient:
    def __init__(self, cfg=None, logger=None):
        import logging
        self.log = logger or logging.getLogger("okx_trader")
        self.api_base = (getattr(cfg, "LLM_API_BASE", "") if cfg else "").rstrip("/")
        self.api_key = getattr(cfg, "LLM_API_KEY", "") if cfg else ""
        self.model = getattr(cfg, "LLM_MODEL", "") if cfg else ""
        self.temperature = getattr(cfg, "LLM_TEMPERATURE", 0.3) if cfg else 0.3
        self.available = bool(self.api_base and self.api_key and self.model)
        if self.available:
            self.log.info("LLM 已启用：%s / %s", self.api_base, self.model)
        else:
            self.log.info("LLM 未配置，Planner/Critic 使用内置基线策略")

    def chat(self, system_prompt, user_prompt, expect_json=True, timeout=60):
        """调用 chat/completions，返回助手文本。失败抛异常（调用方决定降级行为）。"""
        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if expect_json:
            body["response_format"] = {"type": "json_object"}

        last_err = None
        for attempt in range(2):  # response_format 有些中转不支持，失败去掉重试一次
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=timeout)
                if resp.status_code == 400 and attempt == 0 and expect_json:
                    body.pop("response_format", None)  # 不支持 json_object 时降级重试
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                if expect_json:
                    return _extract_json(content)
                return content
            except Exception as e:  # noqa: BLE001 —— LLM 调用失败不应打断交易循环
                last_err = e
                break
        raise RuntimeError(f"LLM 调用失败：{last_err}")


def _extract_json(text):
    """从模型回复里抠出 JSON（容忍 ```json 包裹或前后说明文字）。"""
    text = text.strip()
    if "```" in text:
        for seg in text.split("```"):
            seg = seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{"):
                text = seg
                break
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)
