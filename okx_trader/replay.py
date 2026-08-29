# -*- coding: utf-8 -*-
"""ReplayClient：完整实现 OKXClient 交易接口的全离线回放客户端。

- 行情：K线来自构造参数（测试里合成，或 tests/fixtures/candles_*.json），
  get_candles 只返回游标之前的已收盘部分
- 账户/订单：带游标的状态机。script 的每一步是一轮行情：
    {"price": px, "fill": bool(入场单是否成交), "sl_hit": bool, "tp_hit": bool}
  连续调用 run_round 会推进游标（loop 每轮结束调 advance() 由 cli/tests 驱动）
- 成交在挂单时即刻判定（fill=True → 立即全部成交），保证离线测试零等待
"""
import time


class ReplayClient:
    def __init__(self, cfg, logger=None, candles=None, script=None):
        import logging
        from .config import get_logger
        self.cfg = cfg
        self.log = logger or get_logger(level=getattr(cfg, "LOG_LEVEL", "INFO"))
        self.symbols = list(cfg.SYMBOLS)
        # inst → list[candle dict]；缺省给每个 symbol 生成一段平缓序列
        self.candles = candles or {s: self._synth(s) for s in self.symbols}
        self.script = list(script or [])
        self.cursor = 0                      # 当前处于 script 的第几步
        self.equity = float(getattr(cfg, "PAPER_EQUITY", 10000.0))
        self.positions = {}                  # inst → dict(direction, contracts, avg_px, stop, target)
        self.orders = {}                     # ord_id → dict(...)
        self.algos = {}                      # algo_id → dict(...)
        self._seq = 0
        self.closed_trades = []              # 平仓记录（Phase 3 对账用）
        self._instruments = {}

    # ── 游标 ──────────────────────────────────────────────────────

    def advance(self):
        self.cursor += 1
        step = self.current_step()
        # SL/TP 触发判定：用本轮价格
        if step and self.positions:
            px = float(step.get("price") or self._last_close())
            for inst in list(self.positions):
                p = self.positions[inst]
                if p["direction"] == "long" and (step.get("sl_hit") or px <= p["stop"]):
                    self._close(inst, p["stop"], "stop")
                elif p["direction"] == "long" and (step.get("tp_hit") or px >= p["target"]):
                    self._close(inst, p["target"], "target")
                elif p["direction"] == "short" and (step.get("sl_hit") or px >= p["stop"]):
                    self._close(inst, p["stop"], "stop")
                elif p["direction"] == "short" and (step.get("tp_hit") or px <= p["target"]):
                    self._close(inst, p["target"], "target")

    def current_step(self):
        return self.script[self.cursor] if self.cursor < len(self.script) else \
            (self.script[-1] if self.script else {})

    def _close(self, inst, exit_px, reason):
        p = self.positions.pop(inst)
        ct_val = self.get_instrument(inst)["ctVal"]
        sign = 1 if p["direction"] == "long" else -1
        pnl = sign * (exit_px - p["avg_px"]) * p["contracts"] * ct_val
        self.equity += pnl
        self.closed_trades.append({
            "instId": inst, "direction": p["direction"], "contracts": p["contracts"],
            "avg_px": p["avg_px"], "exit_px": exit_px, "realized_pnl": pnl,
            "exit_reason": reason, "risk_usdt": p.get("risk_usdt"),
            "analyst": p.get("analyst"),
        })
        # 保护单随仓位消失
        for aid in [a for a, v in self.algos.items() if v["instId"] == inst]:
            self.algos[aid]["state"] = "canceled"

    # ── 行情 ──────────────────────────────────────────────────────

    def _synth(self, inst):
        base = 80000 if inst.startswith("BTC") else 2500 if inst.startswith("ETH") else 100
        n = 80
        out = []
        t0 = int(time.time() * 1000) - n * 3600 * 1000
        for i in range(n):
            c = base + i * base * 0.0004
            out.append({"ts": t0 + i * 3600 * 1000, "open": c - 5, "high": c + 20,
                        "low": c - 20, "close": c, "vol": 1000.0})
        return out

    def current_price_hint(self, inst):
        step = self.current_step()
        return step.get("price") if step else None

    def _last_close(self):
        step = self.current_step()
        return float(step.get("price") or 0) or None

    def get_ticker(self, inst_id):
        px = self._last_close() or 100.0
        return {"instId": inst_id, "last": px, "bid": px - 0.5, "ask": px + 0.5,
                "ask_sz": 10.0, "bid_sz": 10.0, "vol24h_quote": 1e7, "ts": int(time.time() * 1000)}

    def get_funding_rate(self, inst_id):
        return {"instId": inst_id, "funding_rate": 0.0001, "next_rate": 0.0001,
                "next_time": 0}

    def get_orderbook(self, inst_id, depth=20):
        return None

    def get_oi_history(self, inst_id, period="1H", limit=2):
        return None

    def get_long_short_ratio(self, ccy, period="1H"):
        return None

    def get_taker_volume_ratio(self, ccy, period="1H"):
        return None

    def get_candles(self, inst_id, bar="1H", limit=100, after=None):
        candles = list(self.candles.get(inst_id) or [])
        # 追加当前轮的"最新已收盘"K线（随游标推进），保持价格路径与因子一致
        px = self._last_close()
        if px:
            prev = candles[-1]["close"] if candles else px
            candles = candles + [{"ts": candles[-1]["ts"] + 3600 * 1000,
                                  "open": prev, "high": max(px, prev) + 10,
                                  "low": min(px, prev) - 10, "close": px,
                                  "vol": 1000.0}]
        return candles[-int(limit):]

    def get_instrument(self, inst_id, refresh=False):
        if inst_id not in self._instruments:
            self._instruments[inst_id] = {
                "instId": inst_id, "ctVal": 0.01, "ctValCcy": "BTC",
                "settleCcy": "USDT", "lotSz": 0.01, "minSz": 0.01,
                "tickSz": 0.1, "maxLmtSz": 0}
        return self._instruments[inst_id]

    def compute_atr(self, inst_id, period=14, bar="1H"):
        candles = self.get_candles(inst_id, bar=bar, limit=period + 2)
        trs = []
        for prev, cur in zip(candles, candles[1:]):
            trs.append(max(cur["high"] - cur["low"],
                           abs(cur["high"] - prev["close"]),
                           abs(cur["low"] - prev["close"])))
        return sum(trs) / len(trs) if trs else 0.0

    # ── 账户 ──────────────────────────────────────────────────────

    def get_equity(self):
        upl = 0.0
        px = self._last_close() or 0
        for p in self.positions.values():
            sign = 1 if p["direction"] == "long" else -1
            upl += sign * (px - p["avg_px"]) * p["contracts"] * \
                self.get_instrument(p["instId"])["ctVal"]
        return {"total_eq": self.equity + upl, "usdt_eq": self.equity + upl,
                "usdt_avail": self.equity, "raw": {"replay": True}}

    def get_positions(self, inst_id=""):
        out = []
        for inst, p in self.positions.items():
            out.append({"instId": inst, "direction": p["direction"],
                        "contracts": p["contracts"], "avg_px": p["avg_px"],
                        "mark_px": self._last_close() or p["avg_px"],
                        "upl": 0.0, "lever": 3.0, "liq_px": 0.0,
                        "mgnMode": "cross", "raw": {}})
        return out

    def get_pending_orders(self, inst_id=""):
        return [o for o in self.orders.values() if o["state"] == "live"]

    def set_leverage(self, inst_id, lever):
        pass

    def get_account_mode(self, refresh=False):
        return {"uid": "replay", "acctLv": "5", "posMode": "net_mode"}

    # ── 交易 ──────────────────────────────────────────────────────

    @staticmethod
    def round_price(px, tick_sz):
        return round(float(px) / float(tick_sz)) * float(tick_sz)

    @staticmethod
    def round_size(sz, lot_sz, min_sz):
        lots = int(float(sz) / float(lot_sz))
        out = lots * float(lot_sz)
        return out if out >= float(min_sz) else 0.0

    def maker_price(self, inst_id, side, px=None, price_offset_ratio=None):
        if px is None:
            ratio = price_offset_ratio
            if ratio is None:
                ratio = getattr(self.cfg, "MAKER_PRICE_OFFSET", 0.0005)
            t = self.get_ticker(inst_id)
            ref = t["bid"] if side == "buy" else t["ask"]
            px = ref * (1 - ratio) if side == "buy" else ref * (1 + ratio)
        return self.round_price(px, self.get_instrument(inst_id)["tickSz"])

    def place_maker_limit(self, inst_id, side, contracts, px=None,
                          price_offset_ratio=None, cl_ord_id="", reduce_only=False):
        step = self.current_step()
        px = self.maker_price(inst_id, side, px=px,
                              price_offset_ratio=price_offset_ratio)
        self._seq += 1
        ord_id = f"replay-ord-{self._seq}"
        fill = bool(step.get("fill"))
        self.orders[ord_id] = {
            "instId": inst_id, "ordId": ord_id, "side": side, "px": px,
            "sz": contracts, "state": "filled" if fill else "live",
            "acc_fill_sz": contracts if fill else 0.0,
            "avg_px": px if fill else None,
        }
        if fill:
            self._open(inst_id, side, contracts, px)
        self.log.info("（回放）挂单 %s %s %s 张 @%s → %s",
                      inst_id, side, contracts, px, self.orders[ord_id]["state"])
        return ord_id

    def _open(self, inst_id, side, contracts, px):
        direction = "long" if side == "buy" else "short"
        self.positions[inst_id] = {"instId": inst_id, "direction": direction,
                                   "contracts": contracts, "avg_px": px,
                                   "stop": 0.0, "target": None, "risk_usdt": None,
                                   "analyst": None}

    def cancel_order(self, inst_id, ord_id):
        if ord_id in self.orders:
            self.orders[ord_id]["state"] = "canceled"

    def get_order(self, inst_id, ord_id):
        o = self.orders.get(str(ord_id)) or {}
        return {"instId": inst_id, "ordId": str(ord_id),
                "state": o.get("state", "canceled"), "side": o.get("side"),
                "px": o.get("px"), "sz": float(o.get("sz") or 0),
                "acc_fill_sz": float(o.get("acc_fill_sz") or 0),
                "avg_px": o.get("avg_px"), "raw": o}

    def place_stop_loss(self, inst_id, direction, contracts, stop_px, tp_px=None,
                        cl_ord_id=""):
        if inst_id in self.positions:
            self.positions[inst_id]["stop"] = float(stop_px)
            self.positions[inst_id]["target"] = float(tp_px) if tp_px else None
        self._seq += 1
        algo_id = f"replay-algo-{self._seq}"
        self.algos[algo_id] = {"instId": inst_id, "algoId": algo_id,
                               "ord_type": "oco" if tp_px else "conditional",
                               "state": "live", "sl": stop_px, "tp": tp_px}
        return algo_id

    def get_pending_stop_losses(self, inst_id=""):
        return [dict(v) for v in self.algos.values()
                if v["instId"] == inst_id and v["state"] == "live"]

    def get_algo_order_details(self, algo_id):
        return self.algos.get(str(algo_id))

    def cancel_stop_loss(self, inst_id, algo_id):
        if str(algo_id) in self.algos:
            self.algos[str(algo_id)]["state"] = "canceled"

    def close_position_market(self, inst_id, direction=""):
        if inst_id in self.positions:
            self._close(inst_id, self._last_close() or 0, "manual")
