# -*- coding: utf-8 -*-
"""回测驱动器：在历史 K 线上回放确定性（无 LLM）决策路径。

约定（重要）：
- script 由 candles 生成，script[i] == candle[i]。
- 每根 bar：run_round（因子用 bars[0..i-1]，前视保护）→ risk_tick
  （移动/时间止损）→ advance（游标到 i+1，按下一根 bar 的 high/low 判定
  止损/止盈）。缺 risk_tick 则 trailing/time_stop 两类出场永远不会出现。
- 成交模型 touch 是乐观口径（假设挂单存活整根 bar），strict 更保守，
  always 仅用于对齐旧测试。报告的"假设成交率"用于和线上真实成交率对照。
"""
import copy
import os
import tempfile
import time

from ..loop import TradingLoop
from ..replay import ReplayClient
from ..store.db import Store
from ..exits import reconcile_trade


def _copy_cfg(cfg):
    try:
        return copy.deepcopy(cfg)
    except Exception:  # noqa: BLE001 —— SecretStr 等可能不可深拷
        import types
        out = types.SimpleNamespace()
        for k, v in vars(cfg).items():
            try:
                setattr(out, k, copy.deepcopy(v))
            except Exception:  # noqa: BLE001
                setattr(out, k, v)
        out.credential = cfg.credential
        return out


def _bars_to_script(bars):
    return [{"ts": b["ts"], "open": b["open"], "high": b["high"],
             "low": b["low"], "close": b["close"], "price": b["close"],
             "vol": b.get("vol", 0.0)} for b in bars]


def run_backtest(cfg, candles, *, bar="1H", fill_model="touch",
                 store=None, collect_factors=False, progress=None,
                 warmup=80):
    """回放历史 K 线。candles: {inst_id: [candle dict 升序]}。返回统计 dict。

    store=None 时用内存临时库（:memory: 不可跨线程，这里单线程可直接用）。
    """
    cfg = _copy_cfg(cfg)
    # 强制走确定性基线 + 回放执行
    cfg.TRADING_ENV = "replay"
    cfg.LLM_ENDPOINTS = {}
    cfg.LLM_ROUTES = {}
    cfg.LLM_API_BASE = ""
    cfg.ORDER_TIMEOUT_SEC = 0
    cfg.COLLECT_FACTORS = bool(collect_factors)

    store = store or Store(db_path=os.path.join(
        tempfile.mkdtemp(prefix="okxbt-"), "bt.db"))
    loop = TradingLoop(cfg=cfg, env_name="replay", executing=True, store=store)

    # 对齐所有 symbol 的 bar 数（取最短的，避免不同标的长度不一致越界）
    syms = [s for s in cfg.SYMBOLS if s in candles and candles[s]]
    if not syms:
        raise RuntimeError("回测无任何标的的 K 线数据")
    n_bars = min(len(candles[s]) for s in syms)

    script_by_inst = {}
    for s in syms:
        script_by_inst[s] = _bars_to_script(candles[s][:n_bars])

    # ReplayClient 用第一个标的的 script 驱动游标（其余标的按同一时间轴）
    primary = syms[0]
    loop.client = ReplayClient(cfg, logger=loop.log, candles=candles,
                               script=script_by_inst[primary],
                               fill_model=fill_model)
    # 关键：替换 client 后同步 risk/committee 引用，否则风控去打真实网络
    loop.risk.client = loop.client
    loop.committee.client = loop.client

    # warmup：前 warmup 根不决策，只推进游标
    for _ in range(min(warmup, n_bars)):
        loop.client.advance()

    n_placed = 0
    n_filled = 0
    for i in range(warmup, n_bars):
        bar_ts = candles[primary][i]["ts"]
        loop._clock = lambda ts=bar_ts: ts  # noqa: B023 —— 每轮固定该 bar 时刻
        loop.run_round()
        loop.risk_tick()
        loop.client.advance()
        if progress and (i - warmup) % 500 == 0:
            progress(i - warmup, n_bars - warmup,
                     len(loop.client.closed_trades))

    # 收尾：未平仓位按最后一根收盘价强制平仓
    last_px = {s: candles[s][n_bars - 1]["close"] for s in syms}
    for inst in list(loop.client.positions):
        loop.client._close(inst, last_px[inst], "backtest_end")
    for tr in store.query("SELECT * FROM trades WHERE status='open'"):
        reconcile_trade(store, tr, exit_px=last_px.get(tr["inst_id"],
                                                       tr["entry_px"]),
                        reason="backtest_end", close_round_pk=None,
                        ct_val=tr["ct_val"], closed_ts=bar_ts)

    # 成交率（ReplayClient.orders 只含入场单：place_maker_limit 写入，
    # 保护单在 algos 里）
    n_placed = len(loop.client.orders)
    n_filled = sum(1 for o in loop.client.orders.values()
                   if o["state"] == "filled")

    # 修正出场原因：巡检 reconcile_closed_trade 用当前因子价 re-infer 出场，
    # 因回测的一 bar 滞后会把 stop/target 误判成 unknown。这里用 ReplayClient
    # 平仓记录（真实触发原因）回填 trades 表的 exit_reason/exit_px。
    closed_by_inst = {}
    for ct in loop.client.closed_trades:
        closed_by_inst.setdefault(ct["instId"], []).append(ct)
    for inst, cts in closed_by_inst.items():
        rows = store.query(
            "SELECT * FROM trades WHERE inst_id=? AND status='closed' "
            "ORDER BY closed_ts", (inst,))
        for tr in rows:
            match = next((c for c in cts
                          if abs(c["avg_px"] - tr["entry_px"]) < 1e-6
                          and abs(c["contracts"] - tr["contracts"]) < 1e-9),
                         None)
            if match and match["exit_reason"] != (tr["exit_reason"] or "unknown"):
                store.execute(
                    "UPDATE trades SET exit_px=?, exit_reason=? WHERE id=?",
                    (match["exit_px"], match["exit_reason"], tr["id"]))

    # 汇总统计交给 report.py
    from .report import build_report
    return build_report(store, cfg, bar=bar, fill_model=fill_model,
                        n_bars=n_bars, warmup=warmup, n_placed=n_placed,
                        n_filled=n_filled)
