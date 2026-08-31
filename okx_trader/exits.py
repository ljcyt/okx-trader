# -*- coding: utf-8 -*-
"""退出与生命周期（Phase 3）。

每轮对每个持仓跑 manage_open_positions()：
    - 移动止损：浮盈达 1R 先推保本，其后按 TRAIL_ATR_MULT×ATR 跟随极值（撤单重挂）
    - 时间止损：持有超过 MAX_HOLD_BARS 根且 |PnL| < 0.3R → 市价平掉
    - 目标：由入场 OCO 承担，这里只确认它还在（巡检负责补挂）

持仓消失时（巡检发现）reconcile_closed_trade() 从元数据/价格推断出场，
回填 trades 行的 exit_px/realized_pnl/r_multiple/exit_reason——
没有这一步，"哪个人设真的赚钱"永远无数据可查。
每个动作都写 orders + app_events，让面板能把出场讲出来。
"""
import time

from .hooks import trigger as hook_trigger
from .store import write as w


def _bar_seconds(bar):
    import re
    m = re.match(r"^(\d+)([mHdD])$", str(bar))
    if not m:
        return 3600
    return int(m.group(1)) * {"m": 60, "H": 3600, "D": 86400}[m.group(2)]


def _open_trades_by_inst(store):
    rows = store.query("SELECT * FROM trades WHERE status='open'")
    return {r["inst_id"]: r for r in rows}


def manage_open_positions(loop, snap, rw):
    """移动止损 + 时间止损 + regime 逆转收紧。只在会真实下单的环境跑。"""
    if not loop.executing:
        return
    client = loop.client
    cfg = loop.cfg
    meta_all = loop.risk.state.get_positions_meta()
    open_trades = _open_trades_by_inst(loop.store)
    changed_meta = False
    regime_row = loop.store.query_one(
        "SELECT regime FROM rounds WHERE regime IS NOT NULL "
        "ORDER BY id DESC LIMIT 1")
    last_regime = regime_row["regime"] if regime_row else None

    for p in snap["positions"]:
        inst = p["instId"]
        m = meta_all.get(inst) or {}
        tr = open_trades.get(inst)
        entry = (tr["entry_px"] if tr else None) or p["avg_px"]
        stop0 = (tr["stop_px"] if tr else None) or m.get("stop")
        if not stop0 or not entry:
            continue
        ct = client.get_instrument(inst)["ctVal"]
        direction = p["direction"]
        sign = 1 if direction == "long" else -1
        r_price = abs(entry - stop0)
        if r_price <= 0:
            continue
        mark = p["mark_px"] or entry
        r_now = sign * (mark - entry) / r_price
        atr = ((snap.get("factors") or {}).get(inst) or {}).get("atr")
        current_stop = m.get("stop") or stop0

        # ── 移动止损 ──
        if atr and r_now >= 1.0:
            tick = client.get_instrument(inst)["tickSz"]
            if direction == "long":
                desired = max(current_stop, entry, mark - cfg.TRAIL_ATR_MULT * atr)
            else:
                desired = min(current_stop, entry, mark + cfg.TRAIL_ATR_MULT * atr)
            desired = client.round_price(desired, tick)
            if abs(desired - current_stop) >= tick:
                old_algo = m.get("algo_id")
                if old_algo:
                    try:
                        client.cancel_stop_loss(inst, old_algo)
                    except Exception:  # noqa: BLE001 —— 可能已触发
                        pass
                try:
                    algo_id = client.place_stop_loss(
                        inst, direction, p["contracts"], desired, tp_px=m.get("target"))
                    meta_all[inst] = {**m, "algo_id": algo_id, "stop": desired}
                    changed_meta = True
                    if tr:
                        loop.store.execute(
                            "UPDATE trades SET stop_px=? WHERE id=?",
                            (desired, tr["id"]))
                    rw.write_order(loop.env.name, inst, "protect",
                                   "oco" if m.get("target") else "conditional",
                                   exch_algo_id=str(algo_id),
                                   side="sell" if direction == "long" else "buy",
                                   sz=p["contracts"], sl_trigger_px=desired,
                                   tp_trigger_px=m.get("target"), state="live",
                                   note=f"移动止损 {current_stop:.4g}→{desired:.4g}")
                    w.write_event(loop.store, loop.env.name, "trailing_stop",
                                  f"{inst} 止损上移 {current_stop:.4g}→{desired:.4g}"
                                  f"（浮盈 {r_now:.2f}R）",
                                  inst_id=inst, round_pk=rw.pk)
                    hook_trigger("trailing_stop", {"kind": "trailing_stop", "level": "info",
                                 "inst_id": inst,
                                 "message": f"止损 {current_stop:.4g} → {desired:.4g}"
                                            f"（浮盈 {r_now:.2f}R）"})
                    loop.log.info("移动止损：%s %.4g → %.4g（%.2fR）",
                                  inst, current_stop, desired, r_now)
                except Exception as e:  # noqa: BLE001
                    loop.log.error("移动止损失败 %s：%s（保留原止损 %.4g）",
                                   inst, e, current_stop)

        # ── regime 逆转收紧：市况翻转逆向后一次性收紧止损 ──
        # regime 门控原本只在入场时生效，持仓期间翻转（long 遇 trending_down）
        # 无人处理。翻转 → 止损收到保本（浮盈≥0）或止损-入场中点（浮亏，
        # 且保持距现价 ≥0.1R 防即时触发）。meta 记录已处置的 regime，
        # 同一段逆向市况只收一次，防每 tick 连续推进。
        adverse = ((direction == "long" and last_regime == "trending_down")
                   or (direction == "short" and last_regime == "trending_up"))
        # r_now ≥1 交给上面的 trailing（保本+跟随已更优），避免两规则
        # 各持一份 current_stop 互相倒退
        if adverse and r_now < 1.0 and m.get("regime_gate") != last_regime:
            tick_sz = client.get_instrument(inst)["tickSz"]
            if r_now >= 0:
                desired = max(current_stop, entry)
            else:
                desired = min((current_stop + entry) / 2.0, mark - 0.1 * r_price)
            desired = client.round_price(desired, tick_sz)
            improved = desired > current_stop if direction == "long" \
                else desired < current_stop
            if improved:
                old_algo = m.get("algo_id")
                if old_algo:
                    try:
                        client.cancel_stop_loss(inst, old_algo)
                    except Exception:  # noqa: BLE001 —— 可能已触发
                        pass
                try:
                    algo_id = client.place_stop_loss(
                        inst, direction, p["contracts"], desired,
                        tp_px=m.get("target"))
                    meta_all[inst] = {**m, "algo_id": algo_id, "stop": desired,
                                      "regime_gate": last_regime}
                    changed_meta = True
                    if tr:
                        loop.store.execute("UPDATE trades SET stop_px=? WHERE id=?",
                                           (desired, tr["id"]))
                    rw.write_order(loop.env.name, inst, "protect",
                                   "oco" if m.get("target") else "conditional",
                                   exch_algo_id=str(algo_id),
                                   side="sell" if direction == "long" else "buy",
                                   sz=p["contracts"], sl_trigger_px=desired,
                                   tp_trigger_px=m.get("target"), state="live",
                                   note=f"regime 逆转收紧 "
                                        f"{current_stop:.4g}→{desired:.4g}")
                    w.write_event(loop.store, loop.env.name, "regime_gate",
                                  f"{inst} {direction} 遇 {last_regime}"
                                  f"——止损收紧 {current_stop:.4g}→{desired:.4g}",
                                  level="warn", inst_id=inst, round_pk=rw.pk)
                    loop.log.warning("regime 逆转：%s %s 遇 %s，止损 %.4g→%.4g",
                                     inst, direction, last_regime,
                                     current_stop, desired)
                except Exception as e:  # noqa: BLE001
                    loop.log.error("regime 收紧失败 %s：%s（保留原止损）", inst, e)
            else:
                # 收不出更好的价（如深亏到中点仍在现价下方不到 0.1R）——
                # 记录已处置，交给原止损，不反复尝试
                meta_all[inst] = {**m, "regime_gate": last_regime}
                changed_meta = True
                loop.log.info("regime 逆转：%s 遇 %s，无可收紧空间（%.2fR）",
                              inst, last_regime, r_now)

        # ── 时间止损 ──
        opened_ts = (tr["opened_ts"] if tr else None) or _parse_ts(m.get("opened_at"))
        if opened_ts:
            held_bars = (loop.now() - opened_ts) / _bar_seconds(cfg.ATR_BAR)
            if held_bars >= cfg.MAX_HOLD_BARS and abs(r_now) < 0.3:
                loop.log.warning("时间止损：%s 持有 %.1f 根、浮盈 %.2fR——市价平掉",
                                 inst, held_bars, r_now)
                try:
                    client.close_position_market(inst, direction)
                    rw.write_order(loop.env.name, inst, "exit", "market",
                                   side="sell" if direction == "long" else "buy",
                                   sz=p["contracts"], state="filled",
                                   note=f"时间止损：{held_bars:.1f} 根，{r_now:.2f}R")
                    w.write_event(loop.store, loop.env.name, "time_stop",
                                  f"{inst} 持有 {held_bars:.1f} 根且 |PnL|<0.3R，市价平仓",
                                  level="warn", inst_id=inst, round_pk=rw.pk)
                    hook_trigger("time_stop", {"kind": "time_stop", "level": "warn",
                                 "inst_id": inst,
                                 "message": f"持有 {held_bars:.1f} 根、{r_now:+.2f}R，市价平仓"})
                    if tr:
                        reconcile_trade(loop.store, tr, exit_px=mark,
                                        reason="time_stop", close_round_pk=rw.pk,
                                        ct_val=ct, closed_ts=loop.now())
                    meta_all.pop(inst, None)
                    changed_meta = True
                except Exception as e:  # noqa: BLE001
                    loop.log.error("时间止损平仓失败 %s：%s", inst, e)

    if changed_meta:
        loop.risk.state.set_positions_meta(meta_all)


def reconcile_closed_trade(loop, snap, rw, inst, meta):
    """仓位消失时调用：推断出场并回填 trades 行（取代旧的 meta.pop 销毁）。"""
    tr = _open_trades_by_inst(loop.store).get(inst)
    meta.pop(inst, None)
    if not tr:
        return
    inst_id = tr["inst_id"]
    atr = ((snap.get("factors") or {}).get(inst_id) or {}).get("atr") or 0
    last = ((snap.get("factors") or {}).get(inst_id) or {}).get("price") \
        or tr["entry_px"]
    stop, target = tr["stop_px"], tr["target_px"]
    # 出场原因推断：最后一根K线价格贴近谁就是谁（±0.25×ATR 容差）
    reason, exit_px = "unknown", last
    tol = 0.25 * atr if atr else 0
    if tr["direction"] == "long":
        if target and last >= (target - tol):
            reason, exit_px = "target", target
        elif last <= (stop + tol):
            reason, exit_px = "stop", stop
    else:
        if target and last <= (target + tol):
            reason, exit_px = "target", target
        elif last >= (stop - tol):
            reason, exit_px = "stop", stop
    reconcile_trade(loop.store, tr, exit_px=exit_px, reason=reason,
                    close_round_pk=rw.pk,
                    ct_val=tr["ct_val"], closed_ts=loop.now())
    w.write_event(loop.store, loop.env.name, "trade_closed",
                  f"{inst_id} 离场：{reason}，exit≈{exit_px:.4g}",
                  level="info", inst_id=inst_id, round_pk=rw.pk)
    hook_trigger("trade_closed", {"kind": "trade_closed", "level": "info",
                 "inst_id": inst_id, "exit_reason": reason,
                 "realized_pnl": tr["realized_pnl"],
                 "r_multiple": tr["r_multiple"],
                 "message": f"出场 {exit_px:.4g}（{reason}）"})


def reconcile_trade(store, tr, exit_px, reason, close_round_pk, ct_val,
                    closed_ts=None):
    sign = 1 if tr["direction"] == "long" else -1
    pnl = sign * (exit_px - tr["entry_px"]) * tr["contracts"] * ct_val
    r_mult = (pnl / tr["risk_usdt"]) if tr["risk_usdt"] else None
    store.execute(
        "UPDATE trades SET closed_ts=?, exit_px=?, realized_pnl=?, r_multiple=?, "
        "exit_reason=?, close_round_pk=?, status='closed' WHERE id=?",
        (time.time() if closed_ts is None else closed_ts,
         exit_px, pnl, r_mult, reason, close_round_pk, tr["id"]))


def _parse_ts(s):
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


def open_trade_row(loop, rw, sized, filled, avg_px, open_round_pk=None):
    """成交即建 trades 行（status='open'）。返回 trade_pk。

    open_round_pk：批准这笔交易的轮次。补记路径必须传入入场单所属的
    round_pk（orders.round_pk）——默认用当前巡检轮会把审计链指到
    一个 no_action 轮次上，面板的"决策 → 结果"追溯就断了。"""
    inst = sized["instId"]
    ct = loop.client.get_instrument(inst)["ctVal"]
    return loop.store.execute(
        "INSERT INTO trades(env, inst_id, direction, open_round_pk, opened_ts, "
        "contracts, ct_val, entry_px, stop_px, target_px, planned_rr, risk_usdt, "
        "analyst, committee_score, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
        (loop.env.name, inst, sized["direction"],
         open_round_pk if open_round_pk is not None else rw.pk,
         loop.now(),
         filled, ct, avg_px, sized.get("stop_loss"), sized.get("target"),
         sized.get("rr"), sized.get("risk_usdt"), sized.get("analyst"),
         sized.get("committee_score")))
