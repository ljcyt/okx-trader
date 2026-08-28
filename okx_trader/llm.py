# -*- coding: utf-8 -*-
"""LLM 适配层：按角色路由 + 端点 failover + 调用记账（含成本）。

（从被删的 llmcore.py 1200 行里只拿走它唯一值钱的三件事：多端点按序切换、
按角色的回退链、调用记账——第三件把"LLM 静默失败"和"模型烧了多少钱"
变成 llm_calls 表里的一行。）

配置（okx_config.py）：
    LLM_ENDPOINTS = {                     # 每个后端一份凭证
      "gpt":  {"api_base": "...", "api_key": "...", "model": "..."},
      "glm":  {...},
    }
    LLM_ROUTES = {                        # 角色 → 按序尝试的后端；裁判首选与分析师不同
      "analyst:趋势猎手": ["gpt", "glm"],
      "judge":            ["glm", "gpt"],
    }
    LLM_PRICES  = {"<model>": {"in": USD/1M, "out": USD/1M}}   # 缺价 → cost NULL

记账：loop 每轮注入 llm.recorder；每次尝试（含失败）写一行 llm_calls。
"""
import json
import time

import requests

from .config import SecretStr


class LLMClient:
    def __init__(self, cfg=None, logger=None):
        import logging
        self.log = logger or logging.getLogger("okx_trader")
        cfg = cfg
        # 后端表：dict {name: {api_base, api_key, model}}；兼容旧的 list[str] 简写
        raw = getattr(cfg, "LLM_ENDPOINTS", {}) or {} if cfg else {}
        self.backends = {}
        if isinstance(raw, dict):
            for name, spec in raw.items():
                self.backends[str(name)] = {
                    "api_base": str(spec.get("api_base", "")).rstrip("/"),
                    "api_key": SecretStr(spec.get("api_key", "")),
                    "model": str(spec.get("model", "")),
                }
        else:  # 旧式 list of base url（共用顶层 key/model）
            for i, base in enumerate(raw):
                self.backends[f"ep{i}"] = {
                    "api_base": str(base).rstrip("/"),
                    "api_key": SecretStr(getattr(cfg, "LLM_API_KEY", "")),
                    "model": str(getattr(cfg, "LLM_MODEL", "")),
                }
        self.routes = {str(k): list(v) for k, v in
                       (getattr(cfg, "LLM_ROUTES", {}) or {}).items()} if cfg else {}
        self.prices = getattr(cfg, "LLM_PRICES", {}) or {} if cfg else {}
        self.temperature = getattr(cfg, "LLM_TEMPERATURE", 0.3) if cfg else 0.3
        self.timeout = getattr(cfg, "LLM_TIMEOUT_SEC", 60) if cfg else 60
        # 记账回调：loop 每轮设置
        self.recorder = None
        self.available = bool(self.backends) and any(
            b["api_base"] and b["api_key"].value and b["model"]
            for b in self.backends.values())
        if self.available:
            self.log.info("LLM 已启用：%d 个后端（%s），路由 %s",
                          len(self.backends), list(self.backends), self.routes)
        else:
            self.log.info("LLM 未配置，Planner/Critic 使用内置基线策略")

    # ── 角色路由 ─────────────────────────────────────────────────

    def chain_for(self, role):
        """角色 → 按序后端名：精确匹配 → 冒号前缀 → 其余全部。"""
        if role in self.routes:
            chain = list(self.routes[role])
        else:
            prefix = role.split(":", 1)[0]
            chain = list(self.routes.get(prefix, []))
        for name in self.backends:
            if name not in chain:
                chain.append(name)          # 兜底：没配到的后端排最后
        return [c for c in chain if c in self.backends]

    def chat(self, system_prompt, user_prompt, expect_json=True, role="llm"):
        """按角色的回退链调用。每次尝试都记账；全部失败抛最后一次异常。"""
        if not self.available:
            raise RuntimeError("LLM 未配置")
        body = {
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if expect_json:
            body["response_format"] = {"type": "json_object"}

        last_err = None
        for name in self.chain_for(role):
            backend = self.backends[name]
            url = backend["api_base"] + "/chat/completions"
            model = backend["model"]
            payload = dict(body, model=model)
            headers = {
                "Authorization": f"Bearer {backend['api_key'].value}",
                "Content-Type": "application/json",
            }
            t0 = time.time()
            try:
                content, usage = self._post(url, headers, payload, expect_json)
                latency = int((time.time() - t0) * 1000)
                cost = self._cost(model, usage)
                self._record(role, model, True, None, latency, content,
                             usage.get("prompt_tokens"),
                             usage.get("completion_tokens"), cost)
                return _extract_json(content) if expect_json else content
            except Exception as e:  # noqa: BLE001 —— 换下一个后端
                last_err = e
                self._record(role, model, False, f"{type(e).__name__}: {e}",
                             int((time.time() - t0) * 1000), None, None, None,
                             None)
                self.log.warning("LLM 后端 %s 失败（%s），尝试下一个",
                                 name, type(e).__name__)
        raise RuntimeError(f"LLM 全部后端失败：{last_err}")

    def _post(self, url, headers, body, expect_json):
        # response_format 有些中转不支持：400 时去掉重试一次
        usage = {}
        for attempt in range(2):
            resp = requests.post(url, headers=headers, json=body,
                                 timeout=self.timeout)
            if resp.status_code == 400 and attempt == 0 and expect_json:
                body.pop("response_format", None)
                continue
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage") or {}
            return data["choices"][0]["message"]["content"], usage
        raise RuntimeError("unreachable")

    def _cost(self, model, usage):
        """按价目表折算；缺价模型返回 None——不能拿 0 当已知。"""
        p = self.prices.get(model)
        if not p or not usage:
            return None
        try:
            return (usage.get("prompt_tokens") or 0) / 1e6 * p.get("in", 0) \
                + (usage.get("completion_tokens") or 0) / 1e6 * p.get("out", 0)
        except (TypeError, KeyError):
            return None

    def _record(self, role, model, ok, err, latency_ms, raw_reply,
                prompt_tokens, completion_tokens, cost_usd):
        if self.recorder is None:
            return
        try:
            self.recorder(role, model, ok, err, latency_ms, raw_reply,
                          prompt_tokens, completion_tokens, cost_usd)
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
