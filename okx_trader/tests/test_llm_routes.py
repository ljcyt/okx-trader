# -*- coding: utf-8 -*-
"""Phase 9 验证：按角色路由 + failover + 成本记账 + 法定票数事件。全部离线（mock HTTP）。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.llm import LLMClient
from okx_trader.store.db import Store


def make_cfg():
    cfg = type("C", (), {
        "LLM_ENDPOINTS": {
            "a": {"api_base": "https://a.example", "api_key": "ka",
                  "model": "model-a"},
            "b": {"api_base": "https://b.example", "api_key": "kb",
                  "model": "model-b"},
        },
        "LLM_ROUTES": {"judge": ["b", "a"]},
        "LLM_PRICES": {"model-a": {"in": 2.0, "out": 8.0}},   # model-b 未计价
        "LLM_TEMPERATURE": 0.2, "LLM_TIMEOUT_SEC": 5,
    })()
    return cfg


def make_client():
    c = LLMClient(make_cfg())
    c.recorder = lambda *args, **kw: None
    return c


class _FakeResp:
    def __init__(self, content, status=200):
        self.status_code = status
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


class RouteChainTest(unittest.TestCase):
    def test_chain_order(self):
        c = make_client()
        # 裁判首选与分析师不同
        self.assertEqual(c.chain_for("judge:技术裁判"), ["b", "a"])
        self.assertEqual(c.chain_for("analyst:趋势猎手"), ["a", "b"])
        self.assertEqual(c.chain_for("anything"), ["a", "b"])

    def test_failover_records_both_attempts(self):
        c = make_client()
        calls = []

        def fake_post(url, headers=None, json=None, timeout=None):
            if "a.example" in url:
                raise ConnectionError("boom")
            return _FakeResp('{"action":"hold"}')

        c.recorder = lambda role, model, ok, err, lat, raw, pt=None, ct=None, \
            cost=None: calls.append((role, model, ok))
        with mock.patch("okx_trader.llm.requests.post", side_effect=fake_post):
            out = c.chat("sys", "user", role="analyst:x")   # 分析师链 a → b
        self.assertEqual(out, {"action": "hold"})
        self.assertEqual(len(calls), 2)                    # 一失败一成功
        self.assertEqual([ok for _, _, ok in calls], [False, True])

    def test_all_backends_fail_raises(self):
        c = make_client()
        with mock.patch("okx_trader.llm.requests.post",
                        side_effect=ConnectionError("down")):
            with self.assertRaises(RuntimeError):
                c.chat("sys", "user", role="analyst:x")

    def test_cost_priced_and_unpriced(self):
        c = make_client()
        cost = c._cost("model-a", {"prompt_tokens": 1_000_000,
                                   "completion_tokens": 500_000})
        self.assertAlmostEqual(cost, 2.0 + 4.0)            # in 2/1M + out 8/1M
        self.assertIsNone(c._cost("model-b", {"prompt_tokens": 1000,
                                              "completion_tokens": 10}))


class QuorumEventTest(unittest.TestCase):
    def test_single_judge_vote_writes_quorum_event(self):
        from okx_trader.committee import Committee
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        cfg = type("C", (), {"SYMBOLS": ["BTC-USDT-SWAP"],
                             "SCORE_THRESHOLD": 6.5,
                             "HALLUCINATION_PENALTY": 2.0})()
        cm = Committee.__new__(Committee)
        cm.cfg, cm.threshold, cm.store, cm.env = cfg, 6.5, store, "replay"
        cm.llm = type("L", (), {"available": False})()
        cm.log = __import__("logging").getLogger("t")
        snap = {"equity": 10000.0, "positions": [], "drawdown": 0.0, "factors": {}}
        cm._ask_analyst = lambda *a, **k: {
            "action": "open", "analyst": "X", "style": "trend",
            "instId": "BTC-USDT-SWAP", "direction": "long",
            "stop_loss": 79000.0, "confidence": 0.6, "reason": "t"}
        cm._ask_judges = lambda *a, **k: {"rows": [
            {"idx": 0, "judge": "J", "score": 9.0, "approved": True,
             "concerns": ""}]}
        d = cm.decide(snap)
        self.assertEqual(d["action"], "hold")              # 法定人数不足 → hold
        kinds = [r["kind"] for r in store.query(
            "SELECT kind FROM app_events")]
        self.assertIn("judge_quorum", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
