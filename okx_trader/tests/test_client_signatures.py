# -*- coding: utf-8 -*-
"""python-okx 签名校验：client 发出的每个实参必须与 python-okx 方法形参兼容。

历史事故（同一根因，三次）：
    1. set_leverage 传错关键字 → 运行时 TypeError；
    2. cxlOnClosePos 写成 "cancile"（枚举不存在，文档级错误，签名测不到，
       靠 docstring 记录 + place_stop_loss 防御性降级兜底）；
    3. close_position_market 的 payload 被塞进 place_order 的 stpMode →
       兜底平仓本身崩掉，裸仓靠巡检补挂止损才收场。

根因是给 python-okx 传错关键字只能在运行时发现。本测试在每个 wrapper 的
okx 调用点上装记录器，用 inspect.signature(...).bind() 校验实际实参与
python-okx 真实形参的兼容性——升级 python-okx 后参数被改名/移除时第一时间报警。
"""
import inspect
import os
import sys
import unittest

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.client import OKXAPIError, OKXClient
from okx_trader.config import load_config

INST = "BTC-USDT-SWAP"


class _SignatureTrap:
    """记录实参并按真实签名 bind 校验，然后抛 OKXAPIError 短路（不重试、不打网络）。"""

    def __init__(self, real, label):
        self.real, self.label = real, label
        self.error = None
        self.called = False

    def __call__(self, *args, **kwargs):
        self.called = True
        try:
            inspect.signature(self.real).bind(*args, **kwargs)
        except TypeError as e:
            self.error = f"{self.label}: 实参与 python-okx 签名不兼容 → {e}"
        raise OKXAPIError("TEST", "signature-trap")


def _spec(inst_id=INST):
    """预填合约规格缓存——get_instrument 就不会打网络。"""
    return {"instId": inst_id, "ctVal": 0.01, "ctValCcy": "BTC",
            "settleCcy": "USDT", "lotSz": 1.0, "minSz": 1.0,
            "tickSz": 0.1, "maxLmtSz": 100000.0}


# (用例名, okx 对象名, 方法名, 调用 client wrapper 的 lambda)
CASES = [
    ("set_leverage", "account", "set_leverage",
     lambda cl: cl.set_leverage(INST, 20)),
    ("get_equity", "account", "get_account_balance",
     lambda cl: cl.get_equity()),
    ("get_positions", "account", "get_positions",
     lambda cl: cl.get_positions()),
    ("get_ticker", "market", "get_ticker",
     lambda cl: cl.get_ticker(INST)),
    ("get_candles", "market", "get_candlesticks",
     lambda cl: cl.get_candles(INST, bar="1H", limit=100)),
    ("get_orderbook", "market", "get_orderbook",
     lambda cl: cl.get_orderbook(INST, depth=20)),
    ("get_funding_rate", "public", "get_funding_rate",
     lambda cl: cl.get_funding_rate(INST)),
    ("get_funding_rate_history", "public", "funding_rate_history",
     lambda cl: cl.get_funding_history(INST)),
    ("get_oi_history", "trading_data", "get_open_interest_history",
     lambda cl: cl.get_oi_history(INST, period="1H", limit=2)),
    ("get_long_short_ratio", "trading_data", "get_long_short_ratio",
     lambda cl: cl.get_long_short_ratio("BTC")),
    ("get_taker_volume_ratio", "trading_data", "get_taker_volume",
     lambda cl: cl.get_taker_volume_ratio("BTC")),
    ("place_maker_limit", "trade", "place_order",
     lambda cl: cl.place_maker_limit(INST, "buy", 10.0)),
    ("close_position_market", "trade", "close_positions",
     lambda cl: cl.close_position_market(INST, "long")),
    ("cancel_order", "trade", "cancel_order",
     lambda cl: cl.cancel_order(INST, "123456")),
    ("place_stop_loss_conditional", "trade", "place_algo_order",
     lambda cl: cl.place_stop_loss(INST, "long", 10.0, 77000.0)),
    ("place_stop_loss_oco", "trade", "place_algo_order",
     lambda cl: cl.place_stop_loss(INST, "long", 10.0, 77000.0, tp_px=80000.0)),
    ("cancel_stop_loss", "trade", "cancel_algo_order",
     lambda cl: cl.cancel_stop_loss(INST, "999")),
    ("get_order", "trade", "get_order",
     lambda cl: cl.get_order(INST, "123456")),
    ("get_pending_orders", "trade", "get_order_list",
     lambda cl: cl.get_pending_orders()),
    ("get_pending_stop_losses", "trade", "order_algos_list",
     lambda cl: cl.get_pending_stop_losses(INST)),
    ("get_algo_order_details", "trade", "get_algo_order_details",
     lambda cl: cl.get_algo_order_details("999")),
]


class ClientSignatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cl = OKXClient(load_config(), logger=None)
        cls.cl._instruments[INST] = _spec()
        # 账户模式缓存 + 劫持探测：下单类 wrapper 会先查账户模式，
        # 不劫持就会打真实网络（本地假凭证 → 解析崩溃，到不了 trap）。
        # 真实 get_account_mode 返回 dict（含 posMode 键）
        cls.cl._acct_mode = {"uid": "test", "acctLv": "2", "posMode": "net_mode"}
        cls.cl.get_account_mode = lambda refresh=False: {
            "uid": "test", "acctLv": "2", "posMode": "net_mode"}

    def test_all_payloads_match_python_okx_signatures(self):
        failures = []
        for label, obj_name, meth_name, invoke in CASES:
            api_obj = getattr(self.cl, obj_name)
            if api_obj is None:
                continue                   # TradingData 导入失败的环境跳过
            real = getattr(api_obj, meth_name)
            trap = _SignatureTrap(real, label)
            setattr(api_obj, meth_name, trap)
            try:
                invoke(self.cl)
            except OKXAPIError:
                pass                       # 预期短路
            except Exception as e:  # noqa: BLE001
                # wrapper 到 okx 调用点之前就失败（缺参/逻辑错误）也算问题
                failures.append(f"{label}: wrapper 未到达 okx 调用点 → "
                                f"{type(e).__name__}: {e}")
                continue
            finally:
                setattr(api_obj, meth_name, real)
            if not trap.called:
                failures.append(f"{label}: 未触达 okx 方法（缓存/分支未覆盖？）")
            elif trap.error:
                failures.append(trap.error)
        self.assertEqual(failures, [])

    def test_funding_history_parses_dict_rows(self):
        """python-okx 0.4.3 的 funding_rate_history 返回 dict 行（fundingTime/
        fundingRate），其余行情端点返回数组——曾按数组解析导致 KeyError 被
        except 吞掉，funding_rank 整条链路瞎掉。"""
        self.cl.public.funding_rate_history = lambda *a, **k: {
            "code": "0", "data": [
                {"fundingTime": "1788076800000", "fundingRate": "0.00008"},
                {"fundingTime": "1788048000000", "fundingRate": "0.00004"}]}
        rows = self.cl.get_funding_history("BTC-USDT-SWAP")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ts"], 1788048000000)   # 升序：已 reverse
        self.assertAlmostEqual(rows[0]["rate"], 0.00004, places=9)

    def test_trap_actually_catches_bad_kwargs(self):
        """自证有效性：故意传一个不存在的关键字，trap 必须报错。"""
        real = self.cl.account.set_leverage
        trap = _SignatureTrap(real, "selfcheck")
        try:
            trap(INST, "20", stpMode="cancel_maker")   # stpMode 不属于 set_leverage
        except OKXAPIError:
            pass
        self.assertIsNotNone(trap.error)
        self.assertIn("stpMode", trap.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
