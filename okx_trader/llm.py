# -*- coding: utf-8 -*-
"""LLM 适配层：OpenAI 兼容 chat/completions + 端点 failover + 每次调用记账。

（从被删的 llmcore.py 1200 行里只拿走它唯一值钱的两件事：多端点按序切换、
调用记账——后者把"LLM 静默失败"变成 llm_calls 表里的一行。）

配置：LLM_ENDPOINTS / LLM_API_BASE（单端点简写）/ LLM_API_KEY / LLM_MODEL。
记账：llm.recorder = fn(role, model, ok, err, latency_ms, reply)——由 loop 每轮
     注入，写 llm_calls 表；未注入时静默跳过。
"""
import json
import time

import requests

from .config import SecretStr


class LLMClient:
    def __init__(self, cfg=None, logger=None):
        import logging
        self.log = logger or logging.getLogger("okx_trader")
        # 端点列表：LLM_ENDPOINTS 优先；否则退回单个 LLM_API_BASE
        endpoints = list(getattr(cfg, "LLM_ENDPOINTS", []) or []) if cfg else []
        base = (getattr(cfg, "LLM_API_BASE", "") or "").rstrip("/") if cfg else ""
        if base and base not in endpoints:
            endpoints.append(base)
        self.endpoints = [e.rstrip("/") for e in endpoints if e]
        self.api_key = SecretStr(getattr(cfg, "LLM_API_KEY", "") or "") if cfg else SecretStr("")
        self.model = getattr(cfg, "LLM_MODEL", "") if cfg else ""
        self.temperature = getattr(cfg, "LLM_TEMPERATURE", 0.3) if cfg else 0.3
        self.timeout = getattr(cfg, "LLM_TIMEOUT_SEC", 60) if cfg else 60
        # failover 时轮换起点，避免每次都先撞死端点
        self._rr = 0
        # 记账回调：loop 每轮设置。签名 (role, model, ok, err, latency_ms, raw_reply)
        self.recorder = None
        self.available = bool(self.endpoints and self.api_key.value and self.model)
        if self.available:
            self.log.info("LLM 已启用：%s / %s（%d 个端点）",
                          " | ".join(self.endpoints), self.model, len(self.endpoints))
        else:
            self.log.info("LLM 未配置，Planner/Critic 使用内置基线策略")

    def chat(self, system_prompt, user_prompt, expect_json=True, role="llm"):
        """调用 chat/completions，端点按序 failover。失败抛最后一次异常。
        role 用于 llm_calls 记账（如 "analyst:趋势猎手"）。"""
        if not self.available:
            raise RuntimeError("LLM 未配置")
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
        headers = {
            "Authorization": f"Bearer {self.api_key.value}",
            "Content-Type": "application/json",
        }

        last_err = None
        n = len(self.endpoints)
        for i in range(n):
            url = self.endpoints[(self._rr + i) % n] + "/chat/completions"
            t0 = time.time()
            try:
                content = self._post(url, headers, body, expect_json)
                self._record(role, self.model, True, None,
                             int((time.time() - t0) * 1000), content)
                self._rr = (self._rr + i + 1) % n  # 下次从下一个端点开始
                return _extract_json(content) if expect_json else content
            except Exception as e:  # noqa: BLE001 —— 换下一个端点
                last_err = e
                self.log.warning("LLM 端点 %s 失败（%s），尝试下一个",
                                 url, type(e).__name__)
                self._record(role, self.model, False, f"{type(e).__name__}: {e}",
                             int((time.time() - t0) * 1000), None)
        raise RuntimeError(f"LLM 全部端点失败：{last_err}")

    def _post(self, url, headers, body, expect_json):
        # response_format 有些中转不支持：400 时去掉重试一次
        for attempt in range(2):
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=self.timeout)
            if resp.status_code == 400 and attempt == 0 and expect_json:
                body.pop("response_format", None)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        raise RuntimeError("unreachable")

    def _record(self, role, model, ok, err, latency_ms, raw_reply):
        if self.recorder is None:
            return
        try:
            self.recorder(role, model, ok, err, latency_ms, raw_reply)
        except Exception:  # noqa: BLE001 —— 记账失败不影响交易
            self.log.debug("llm_calls 记账失败", exc_info=True)


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
