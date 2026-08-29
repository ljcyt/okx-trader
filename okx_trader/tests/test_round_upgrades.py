# -*- coding: utf-8 -*-
"""Phase 10 验证：数字核对（扣分+事件）、修订循环、regime 标签。"""
import os
import sys
import tempfile
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.committee import Committee, verify_numbers
from okx_trader.factors import overall_regime, regime_label
from okx_trader.store.db import Store


def make_report():
    """能喂给 format_factor_report 的最小完整报告。"""
    return {"instId": "BTC-USDT-SWAP", "bar": "1H", "ts": 1000, "time": "01-01 00:00",
            "price": 78000.0, "ema20": 78100.0, "ema60": 77900.0,
            "trend": "多头排列（价>EMA20>EMA60）", "structure": "上升结构（HH+HL）",
            "macd": {"dif": 10.0, "dea": 8.0, "hist": 2.0, "state": "金叉红柱"},
            "rsi14": 55.0, "atr": 400.0, "atr_pct": 0.005,
            "boll": {"mid": 78000.0, "upper": 79200.0, "lower": 76800.0,
                     "width_pct": 0.03},
            "price_vs_boll": "轨道内", "vol_ratio": 1.2,
            "funding_rate": 0.0001, "pattern": "常规", "patterns": [],
            "sr": {"supports": [77000.0], "resistances": [79500.0]},
            "fvg": [], "mtf": {}, "obi": 0.55, "oi": None,
            "oi_delta_pct": None, "ls_ratio": 1.1, "taker_ratio": None}


def make_committee(store=None):
    cfg = type("C", (), {"SYMBOLS": ["BTC-USDT-SWAP"], "SCORE_THRESHOLD": 6.5,
                         "MAX_REVISIONS": 1,
                         "ATR_BAR": "1H"})()
    cm = Committee.__new__(Committee)
    cm.cfg, cm.threshold = cfg, 6.5
    cm.store = store
    cm.env = "replay"
    cm.log = __import__("logging").getLogger("t")
    cm.llm = type("L", (), {"available": False})()
    return cm


SNAP = {"equity": 10000.0, "positions": [], "drawdown": 0.0,
        "factors": {"BTC-USDT-SWAP": make_report()}}


class VerifyNumbersTest(unittest.TestCase):
    def test_cited_number_found(self):
        m = verify_numbers("RSI14 55.0 高于均值且 ATR 400", "RSI14 55.0；ATR 400")
        self.assertEqual(m, [])

    def test_hallucinated_number_flagged(self):
        m = verify_numbers("RSI 暴涨到 99.9", "RSI14 55.0；ATR 400",
                           own=[77000.0])
        self.assertEqual(m, ["99.9"])

    def test_own_computed_fields_exempt(self):
        m = verify_numbers("止损 77000.0 置信度 0.6", "RSI14 55.0",
                           own=[77000.0, 0.6])
        self.assertEqual(m, [])

    def test_tolerance_half_percent(self):
        # 报告里是 400，引用 401（+0.25%）应算命中
        m = verify_numbers("ATR 401", "ATR 400")
        self.assertEqual(m, [])

    def test_price_tier_tight_tolerance(self):
        # 中：价格量级（>=1000）容差收紧到 0.05% —— 0.375% 的幻觉必须被抓
        m = verify_numbers("价格 80300 突破", "价格 80000")
        self.assertEqual(m, ["80300"])
        # 报告 80000，引用 80030（0.0375%）算命中
        m = verify_numbers("价格 80030 站稳", "价格 80000")
        self.assertEqual(m, [])

    def test_hyphen_range_not_misread(self):
        # 中：'阻力 2515-2538' 是区间，不能切出 -2538 记成幻觉
        m = verify_numbers("阻力 2515-2538 一带压制", "阻力位 2515、2538")
        self.assertEqual(m, [])

    def test_scientific_notation_pool(self):
        """复查遗留：%g 渲染 ≥1e5 会产出 8.027e+04——tokenize 必须认识
        科学计数法，否则引用真实 EMA 被误报、幻觉的 8.03 反而漏报。"""
        report = ("价格 80300；EMA20 8.027e+04 / EMA60 7.988e+04 → 多头排列；"
                  "ATR 402.1（0.50%）")
        # 引用真实的 EMA60 → 不能误报
        self.assertEqual(verify_numbers("EMA60 79880.2 附近有支撑", report), [])
        # 幻觉的 8.03 不能撞上垃圾 token 通过（旧正则会漏报）
        self.assertEqual(verify_numbers("EMA20 已到 8.03", report), ["8.03"])
        # 原始十进制引用同一数值也命中
        self.assertEqual(verify_numbers("EMA20 80270 站稳", report), [])


SNAP_UP = {'equity': 10000.0, 'positions': [], 'drawdown': 0.0,
           'factors': {'BTC-USDT-SWAP': make_report()}}


class HallucinationRecordOnlyTest(unittest.TestCase):
    """幻觉数字只记录（事件+标记可见），不扣分不禁言——
    派生值误报（跨标的引用、方法论常量、算出的 RR）曾实测冤枉唯一会开口的分析师。"""

    def test_recorded_without_penalty(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        cm = make_committee(store)
        # style=funding：ranging 市不触发 regime 门控，测试只验证幻觉行为
        cm._ask_analyst = lambda *a, **k: {
            "action": "open", "analyst": "X", "style": "funding",
            "instId": "BTC-USDT-SWAP", "direction": "long",
            "stop_loss": 77000.0, "confidence": 0.6,
            "reason": "RSI 暴涨到 99.9 所以做多"}
        cm._ask_judges = lambda *a, **k: {"rows": [
            {"idx": 0, "judge": f"J{i}", "score": 9.0, "approved": True,
             "concerns": ""} for i in range(3)]}
        d = cm.decide(SNAP)
        p = next(a for a in d["analysts"] if a["analyst"] == "X")
        self.assertTrue(p.get("hallucinated"))
        self.assertEqual(d["scoreboard"][0]["avg_score"], 9.0)   # 不扣分
        kinds = [r["kind"] for r in store.query("SELECT kind FROM app_events")]
        self.assertIn("hallucinated_number", kinds)


class RevisionLoopTest(unittest.TestCase):
    def _make_fake(self, judge_script):
        """按角色分发的 fake：分析师第 1 次给首提案、第 2 次给修订稿；
        judge_script 每次「单个裁判调用」给一个分（3 裁判 = 3 次调用/轮）。"""
        state = {"analyst": 0, "judge": 0}

        def fake_chat(system, user, expect_json=True, role="llm"):
            if role == "analyst:趋势猎手":
                state["analyst"] += 1
                if state["analyst"] == 1:
                    return {"action": "open", "instId": "BTC-USDT-SWAP",
                            "direction": "long", "stop_loss": 77000.0,
                            "confidence": 0.6, "reason": "Delta: 首次提案"}
                return {"action": "open", "instId": "BTC-USDT-SWAP",
                        "direction": "long", "stop_loss": 77500.0,
                        "confidence": 0.7, "reason": "Delta: 收紧止损"}
            if role.startswith("analyst"):
                return {"action": "hold", "reason": "Delta: 无信号"}  # 其他人设弃权
            state["judge"] += 1
            s, c = judge_script[min(state["judge"] - 1, len(judge_script) - 1)]
            return {"scores": [{"idx": 0, "score": s, "approved": s >= 6,
                                "concerns": c or ""}]}

        return fake_chat, state

    def test_veto_then_revise_once(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        cm = make_committee(store)
        cm.llm = type("L", (), {"available": True})()
        judge_script = [(3.0, "逆势")] * 3 + [(8.0, "")] * 3   # 3 否决 + 3 通过
        fake_chat, state = self._make_fake(judge_script)
        cm.llm.chat = fake_chat
        d = cm.decide(SNAP)
        self.assertEqual(d.get("revisions"), 1)
        self.assertEqual(d["action"], "open")
        self.assertEqual(state["judge"], 6)        # 两轮 × 3 裁判
        self.assertEqual(state["analyst"], 2)      # 首提 + 修订

    def test_max_revisions_respected(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
        cm = make_committee(store)
        cm.llm = type("L", (), {"available": True})()
        judge_script = [(3.0, "差")] * 6           # 修订后仍全部否决
        fake_chat, state = self._make_fake(judge_script)
        cm.llm.chat = fake_chat
        d = cm.decide(SNAP)
        self.assertEqual(d.get("revisions"), 1)
        self.assertEqual(d["action"], "hold")      # 第二次仍不过 → hold
        self.assertEqual(state["judge"], 6)        # 不会有第三轮打分
        self.assertEqual(state["analyst"], 2)


class RegimeTest(unittest.TestCase):
    cfg = type("C", (), {"HIGH_VOL_ATR_PCT": 0.03, "TREND_THRESHOLD": 1.0})()

    def test_high_vol(self):
        r = {"atr_pct": 0.05, "ema20": 100.0, "ema60": 100.2, "atr": 1.0}
        self.assertEqual(regime_label(r, self.cfg), "high_vol")

    def test_trending_up(self):
        # EMA20 在上、gap 2.0 ≥ 阈值 1.0 → trending_up
        r = {"atr_pct": 0.01, "ema20": 102.0, "ema60": 100.0, "atr": 1.0}
        self.assertEqual(regime_label(r, self.cfg), "trending_up")

    def test_trending_down(self):
        # EMA20 在下、gap 2.0 → trending_down（方向性）
        r = {"atr_pct": 0.01, "ema20": 100.0, "ema60": 102.0, "atr": 1.0}
        self.assertEqual(regime_label(r, self.cfg), "trending_down")

    def test_ranging(self):
        r = {"atr_pct": 0.01, "ema20": 100.1, "ema60": 100.0, "atr": 1.0}
        self.assertEqual(regime_label(r, self.cfg), "ranging")

    def test_overall_vote(self):
        f = {"A": {"atr_pct": 0.01, "ema20": 102, "ema60": 100, "atr": 1},
             "B": {"atr_pct": 0.01, "ema20": 102.2, "ema60": 100, "atr": 1},
             "C": {"atr_pct": 0.05, "ema20": 1, "ema60": 1, "atr": 1}}
        self.assertEqual(overall_regime(f, self.cfg), "trending_up")   # 2:1 投票


if __name__ == "__main__":
    unittest.main(verbosity=2)
