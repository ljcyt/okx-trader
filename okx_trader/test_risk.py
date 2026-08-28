# -*- coding: utf-8 -*-
"""risk.py 单元测试（纯逻辑，用 Stub 客户端，不触网、不需要 API Key）

运行：python okx_trader/test_risk.py
"""
import sys
import os

try:  # GBK 控制台兜底；包在 try 里以便 pytest 收集时不炸
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ── Stub：替代 OKXDemoClient 的最小接口 ─────────────────────────────
class StubClient:
    def __init__(self, equity=10000.0, positions=None):
        import logging
        self.log = logging.getLogger("test")
        self.log.addHandler(logging.NullHandler())
        self._equity = equity
        self._positions = positions or []

    def get_equity(self):
        return {"total_eq": self._equity, "usdt_eq": self._equity, "usdt_avail": self._equity}

    def get_positions(self):
        return list(self._positions)

    def get_instrument(self, inst_id):
        return {"instId": inst_id, "ctVal": 0.01, "lotSz": 0.01, "minSz": 0.01,
                "tickSz": 0.1, "settleCcy": "USDT"}

    def get_ticker(self, inst_id):
        return {"instId": inst_id, "last": 80000.0, "ask": 80000.5, "bid": 79999.5}

    def compute_atr(self, inst_id, period=14, bar="1H"):
        return 400.0  # BTC 1H ATR 假设 400 U

    def round_size(self, sz, lot_sz, min_sz):
        from client import OKXDemoClient
        return OKXDemoClient.round_size(sz, lot_sz, min_sz)


class StubState:
    def __init__(self, hwm=0.0):
        self._hwm = hwm

    def get_hwm(self):
        return self._hwm

    def update_hwm(self, equity):
        self._hwm = max(self._hwm, equity)
        dd = (self._hwm - equity) / self._hwm if self._hwm else 0
        return self._hwm, dd


def make_plan(**kw):
    plan = {
        "instId": "BTC-USDT-SWAP",
        "direction": "long",
        "stop_loss": 79200.0,          # 距入场 80000 约 1% 的止损
        "order_type": "limit_maker",
        "reason": "测试计划",
    }
    plan.update(kw)
    return plan


def main():
    from risk import RiskManager

    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(f"{'[通过]' if cond else '[失败]'} {name} {detail}")

    # 1. 正常计划 → 通过，且仓位满足 1% 风险约束
    rm = RiskManager(type("C", (), {
        "MAX_RISK_PER_TRADE": 0.01, "MAX_TOTAL_LEVERAGE": 3.0,
        "MAX_OPEN_POSITIONS": 3, "MAX_DRAWDOWN": 0.10,
        "ATR_PERIOD": 14, "ATR_BAR": "1H", "ATR_STOP_MULT": 1.5,
        "MIN_STOP_DIST_PCT": 0.002, "MAX_STOP_DIST_PCT": 0.05, "MIN_RR": 1.5,
    })(), StubClient(), StubState())
    v = rm.check_open_plan(make_plan())
    print(v)
    sized = v.sized
    # 有效止损距离 = max(800, 1.5*400=600) = 800 → 预算 100U → 12.5 张
    check("R3 正常计划通过", v.passed)
    check("R3 张数符合预算", abs(sized["contracts"] - 12.5) < 1e-9,
          f"contracts={sized['contracts']}")
    check("R3 单笔风险≤1%", sized["risk_pct"] <= 0.01001,
          f"risk={sized['risk_pct']:.3%}")
    check("R4 加仓后总杠杆合理", sized["leverage_after"] <= 3.0,
          f"lev={sized['leverage_after']:.2f}x")

    # 2. 无止损 → 拒绝
    v = rm.check_open_plan(make_plan(stop_loss=None))
    check("R1 无止损拒绝", not v.passed and any("止损" in f for f in v.failures))

    # 3. 多仓止损在上方 → 拒绝
    v = rm.check_open_plan(make_plan(stop_loss=81000.0))
    check("R1 止损方向错误拒绝", not v.passed and any("低于" in f for f in v.failures))

    # 4. 止损距离过远（10%）→ 拒绝
    v = rm.check_open_plan(make_plan(stop_loss=71000.0))
    check("R2 止损过远拒绝", not v.passed and any("过远" in f for f in v.failures))

    # 5. 止损过近（0.05%）→ 有效距离按 ATR 下限 600 处理：仓位 100/(600*0.01)=16.66 张，
    #    实际止损在 40 距离处，实际风险远小于预算（保守方向正确）
    v = rm.check_open_plan(make_plan(stop_loss=79960.0))  # 0.05% 距离
    check("R2 止损过近按ATR下限计算仓位", v.passed and abs(v.sized["contracts"] - 16.66) < 0.01,
          f"contracts={v.sized.get('contracts')}")
    check("R2 过近止损实际风险仍≤1%", v.passed and v.sized["risk_pct"] <= 0.01001,
          f"risk={v.sized['risk_pct']:.3%}")

    # 6. 非Maker单 → 拒绝
    v = rm.check_open_plan(make_plan(order_type="market"))
    check("R6 非Maker拒绝", not v.passed and any("Maker" in f for f in v.failures))

    # 7. 同标的已有持仓 → 拒绝
    rm2 = RiskManager(rm.cfg, StubClient(positions=[{
        "instId": "BTC-USDT-SWAP", "direction": "long", "contracts": 1.0,
        "avg_px": 79000.0, "mark_px": 80000.0, "upl": 10.0,
    }]), StubState())
    v = rm2.check_open_plan(make_plan())
    check("R4 同标的重复开仓拒绝", not v.passed and any("已有持仓" in f for f in v.failures))

    # 8. 持仓数量达到上限 → 拒绝
    pos3 = [{"instId": f"{c}-USDT-SWAP", "direction": "long", "contracts": 1.0,
             "avg_px": 1000.0, "mark_px": 1000.0, "upl": 0.0}
            for c in ("ETH", "SOL", "XRP")]
    rm3 = RiskManager(rm.cfg, StubClient(positions=pos3), StubState())
    v = rm3.check_open_plan(make_plan(instId="DOGE-USDT-SWAP"))
    check("R4 持仓数上限拒绝", not v.passed and any("上限" in f for f in v.failures))

    # 9. 总杠杆超限 → 拒绝（stub ctVal=0.01：2900 张 × 0.01 × 1000 = 29000 = 2.9x 权益）
    rm4 = RiskManager(rm.cfg, StubClient(equity=10000.0, positions=[{
        "instId": "ETH-USDT-SWAP", "direction": "long", "contracts": 2900.0,
        "avg_px": 1000.0, "mark_px": 1000.0, "upl": 0.0,
    }]), StubState())
    v = rm4.check_open_plan(make_plan())
    check("R4 总杠杆超限拒绝", not v.passed and any("总杠杆" in f for f in v.failures))

    # 10. 回撤熔断 → 拒绝
    rm5 = RiskManager(rm.cfg, StubClient(equity=8800.0), StubState(hwm=10000.0))
    v = rm5.check_open_plan(make_plan())
    check("R5 回撤熔断拒绝", not v.passed and any("熔断" in f for f in v.failures))

    # 11. 空头方向正常计算
    v = rm.check_open_plan(make_plan(direction="short", stop_loss=80800.0))
    check("R3 空头计划通过", v.passed, f"contracts={v.sized.get('contracts')}")

    # 12. R7 盈亏比：止损距离 1000U（79000），最近阻力 81000 只有 1000U 空间 → RR=1.0 < 1.5 拒绝
    v = rm.check_open_plan(make_plan(stop_loss=79000.0,
                                     factors={"atr": 400.0, "sr": {
                                         "supports": [78000.0], "resistances": [81000.0]}}))
    check("R7 盈亏比不足拒绝", not v.passed and any("盈亏比" in f for f in v.failures),
          f"failures={v.failures}")

    # 13. R7 盈亏比：止损 400U（1×ATR），阻力在 81000（1000U 空间）→ RR=2.5 通过并记录
    v = rm.check_open_plan(make_plan(stop_loss=79600.0,
                                     factors={"atr": 400.0, "sr": {
                                         "supports": [78000.0], "resistances": [81000.0]}}))
    check("R7 盈亏比达标通过", v.passed and v.sized.get("rr", 0) >= 1.5,
          f"rr={v.sized.get('rr')}")

    print()
    failed = [n for n, ok in results if not ok]
    if failed:
        print(f"共 {len(results)} 项，失败 {len(failed)} 项：{failed}")
        sys.exit(1)
    print(f"共 {len(results)} 项测试全部通过 ✔")


if __name__ == "__main__":
    main()
