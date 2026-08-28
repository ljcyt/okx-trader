# -*- coding: utf-8 -*-
"""factors.py / client.py 清洗逻辑的单元测试（离线，无网络）

覆盖（Phase 1 验证要求）：
    - MACD 符号与手算一致（线性序列 DEA==DIF、柱≈0；对照独立参考实现）
    - confirm 列为主、时间戳启发式兜底的未收盘 K 线丢弃
    - build_factor_report 的过期数据守卫
    - FVG 最新一根形成的缺口也要被识别
"""
import os
import sys
import time
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.factors import macd, fvg_list, build_factor_report, _bar_seconds
from okx_trader.client import OKXClient


def make_candles(n=80, start=100.0, step=1.0, end_ts_ms=None):
    """合成 K 线：线性价格，最后一根 ts=end_ts_ms（默认=刚刚收盘）。"""
    if end_ts_ms is None:
        end_ts_ms = time.time() * 1000 - 3600 * 1000 + 60 * 1000  # 1H 已收盘 1 分钟
    out = []
    for i in range(n):
        c = start + i * step
        out.append({"ts": end_ts_ms - (n - 1 - i) * 3600 * 1000,
                    "open": c - step / 2, "high": c + 0.5,
                    "low": c - 0.5, "close": c, "vol": 10.0})
    return out


def ref_ema(values, period):
    """独立参考 EMA 实现（与 factors._ema_series 同口径），用于交叉验证。"""
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


class MacdTest(unittest.TestCase):
    def test_linear_series_hist_is_zero(self):
        # 线性价格序列：DIF 恒定 → DEA 应等于 DIF、柱应≈0（修复前错位 14 根）
        closes = [100 + i for i in range(80)]
        dif, dea, hist = macd(closes)
        self.assertGreater(dif, 0)                      # 上涨序列 DIF 为正
        self.assertAlmostEqual(dea, dif, places=6)
        self.assertAlmostEqual(hist, 0.0, places=6)

    def test_falling_series_dif_negative(self):
        # 线性下跌：DIF 恒定 → hist≈0 但符号为负的趋势
        closes = [500 - i for i in range(80)]
        dif, dea, hist = macd(closes)
        self.assertLess(dif, 0)
        self.assertAlmostEqual(hist, 0.0, places=6)
        # 加速下跌（凸性）：DIF 越来越负 → DEA（DIF 的平滑）在上方 → hist < 0
        closes_acc = [500 - 0.5 * i * i for i in range(80)]
        dif2, dea2, hist2 = macd(closes_acc)
        self.assertLess(hist2, 0)

    def test_matches_reference_implementation(self):
        # 任意形状序列：macd() 与"手算"（独立参考实现）一致
        closes = [100, 101, 103, 102, 105, 107, 106, 110, 108, 112,
                  111, 115, 114, 118, 117, 121, 119, 123, 122, 126,
                  125, 129, 127, 131, 130, 134, 132, 136, 135, 139,
                  138, 142, 140, 144, 143, 147, 145, 149, 148, 152,
                  150, 154, 153, 157, 155, 159, 158, 162, 160, 164,
                  163, 167, 165, 169, 168, 172, 170, 174, 173, 177,
                  175, 179, 178, 182, 180, 184, 183, 187, 185, 189,
                  188, 192, 190, 194, 193, 197, 195, 199, 198, 202]
        dif, dea, hist = macd(closes)
        d12 = ref_ema(closes, 12)
        d26 = ref_ema(closes, 26)
        off = len(d12) - len(d26)
        dif_hist = [a - b for a, b in zip(d12[off:], d26)]
        dea_ref = ref_ema(dif_hist[-27:], 9)[-1]  # 参考实现返回序列，取末值
        self.assertAlmostEqual(dif, dif_hist[-1], places=9)
        self.assertAlmostEqual(dea, dea_ref, places=9)
        self.assertAlmostEqual(hist, dif_hist[-1] - dea_ref, places=9)


class CandleCleaningTest(unittest.TestCase):
    def test_confirm_column_drops_unclosed(self):
        # OKX 行下标 8 = confirm："1" 已收盘 / "0" 进行中
        rows = [[1000 + i * 3600000, "1", "2", "0.5", "1.5", "10", "0", "0", "1"]
                for i in range(5)]
        rows.append([6000, "1", "2", "0.5", "1.5", "10", "0", "0", "0"])  # 未收盘
        cleaned, dropped = OKXClient._clean_candle_rows(rows, "1H")
        self.assertEqual(len(cleaned), 5)
        self.assertEqual(dropped, 1)
        self.assertEqual(int(cleaned[-1][0]), 1000 + 4 * 3600000)

    def test_timestamp_heuristic_fallback(self):
        # 无 confirm 列（老式响应）→ 时间戳兜底：最后一根未满 1H
        now = 10_000_000
        rows = [[now - 3 * 3600000, "1", "2", "0.5", "1.5", "10"],
                [now - 2 * 3600000, "1", "2", "0.5", "1.5", "10"],
                [now - 3600000, "1", "2", "0.5", "1.5", "10"],
                [now - 600000, "1", "2", "0.5", "1.5", "10"]]  # 才过 10 分钟
        cleaned, dropped = OKXClient._clean_candle_rows(rows, "1H", now_ms=now)
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(dropped, 1)

    def test_confirm_all_closed_drops_nothing(self):
        rows = [[1000 + i * 3600000, "1", "2", "0.5", "1.5", "10", "0", "0", "1"]
                for i in range(5)]
        cleaned, dropped = OKXClient._clean_candle_rows(rows, "1H")
        self.assertEqual((len(cleaned), dropped), (5, 0))


class StalenessGuardTest(unittest.TestCase):
    """build_factor_report 的过期数据守卫：最新已收盘K线 > 2×bar 就抛错。"""

    def setUp(self):
        self.cfg = type("C", (), {"ATR_BAR": "1H", "ATR_PERIOD": 14})()

    def _client(self, candles):
        class FakeClient:
            log = None
            def get_candles(self, inst_id, bar="1H", limit=100):
                return candles
            def get_funding_rate(self, inst_id):
                return {"funding_rate": 0.0001}
            def get_orderbook(self, inst_id, depth=20):
                return None
            def get_oi_history(self, inst_id, period="1H", limit=2):
                return None
            def get_long_short_ratio(self, ccy, period="1H"):
                return None
            def get_taker_volume_ratio(self, ccy, period="1H"):
                return None
        return FakeClient()

    def test_stale_candles_raise(self):
        stale_ts = time.time() * 1000 - 3 * 3600 * 1000  # 3 小时前（> 2×1H）
        candles = make_candles(80, end_ts_ms=stale_ts)
        with self.assertRaises(ValueError) as ctx:
            build_factor_report(self.cfg, self._client(candles), "BTC-USDT-SWAP")
        self.assertIn("过期", str(ctx.exception))

    def test_fresh_candles_pass(self):
        candles = make_candles(80)  # 1 小时前收盘
        report = build_factor_report(self.cfg, self._client(candles), "BTC-USDT-SWAP")
        self.assertAlmostEqual(report["price"], 100 + 79 * 1.0, places=6)
        self.assertIsNotNone(report["rsi14"])
        self.assertIsNotNone(report["atr"])


class FvgTest(unittest.TestCase):
    def test_latest_bar_gap_included(self):
        candles = []
        for i in range(30):
            base = 100 + i * 0.1
            candles.append({"open": base, "high": base + 1, "low": base - 1,
                            "close": base + 0.5, "vol": 1, "ts": i})
        candles[-1]["low"] = candles[-3]["high"] + 2  # 最后一根跳空，无后续K线
        gaps = fvg_list(candles)
        self.assertTrue(any(g["dir"] == "bull" and g["bars_ago"] == 0 for g in gaps),
                        f"gaps={gaps}")

    def test_filled_gap_excluded(self):
        candles = []
        for i in range(30):
            base = 100 + i * 0.1
            candles.append({"open": base, "high": base + 1, "low": base - 1,
                            "close": base + 0.5, "vol": 1, "ts": i})
        candles[10]["low"] = candles[8]["high"] + 2   # 制造缺口
        candles[11]["low"] = candles[8]["high"] - 0.5  # 下一根回补
        gaps = fvg_list(candles)
        self.assertFalse(any(g["dir"] == "bull" and g["bars_ago"] == 19 for g in gaps),
                         f"gaps={gaps}")


class BarSecondsTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(_bar_seconds("1H"), 3600)
        self.assertEqual(_bar_seconds("15m"), 900)
        self.assertEqual(_bar_seconds("4H"), 14400)
        self.assertIsNone(_bar_seconds("bogus"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
