# -*- coding: utf-8 -*-
"""风控硬约束模块（第三步交付物）

核心原则：所有规则用【代码强制】执行，模型（Planner/Critic）只能提计划，
开仓必须先通过本模块的 check_open_plan，任何一条不过就一票否决。

规则清单（对应需求）：
    R1 止损必填        —— 计划必须带止损价，且方向正确（多仓止损在下方，空仓在上方）
    R2 止损距离合理    —— |入场-止损|/入场 ∈ [MIN_STOP_DIST_PCT, MAX_STOP_DIST_PCT]
                          （太近会被噪声扫掉，太远风险失控）
    R3 单笔风险 ≤ 1%   —— 仓位大小由本模块反推：
                          张数 = (权益 × MAX_RISK_PER_TRADE) / (有效止损距离 × 每张面值)
                          有效止损距离 = max(计划止损距离, ATR_STOP_MULT × ATR)
                          —— 这一步同时实现了「波动率目标仓位」：波动越大仓位越小
    R4 总杠杆/总仓位上限 —— (现有持仓名义 + 新仓名义) / 权益 ≤ MAX_TOTAL_LEVERAGE；
                          持仓数 < MAX_OPEN_POSITIONS；同一标的不允许重复开仓
                          （组合理论总风险 ≤ 持仓数 × 单笔 1%，隐式满足"总风险有上限"）
    R5 回撤熔断        —— 权益距高水位回撤超过 MAX_DRAWDOWN 时禁止开新仓
    R6 Maker 优先      —— 计划的 order_type 必须是 "limit_maker"（执行层也只走限价）

使用方式（交易循环里）：
    verdict = risk.check_open_plan(plan)
    if verdict.passed:
        用 verdict.sized["contracts"] 下单   # 张数以风控计算结果为准，不接受计划自带
"""
import time

from .client import OKXAPIError


class RiskVerdict:
    """风控结论。passed=True 才允许下单；failures 是硬性拒绝原因。"""

    def __init__(self):
        self.passed = True
        self.failures = []   # 硬性失败（一票否决）
        self.warnings = []   # 提示（不拦截）
        self.sized = {}      # 风控计算出的仓位参数

    def fail(self, reason):
        self.passed = False
        self.failures.append(reason)
        return self

    def warn(self, msg):
        self.warnings.append(msg)
        return self

    def __str__(self):
        tag = "通过" if self.passed else "拒绝"
        lines = [f"风控结论：{tag}"]
        lines += [f"  ✗ {f}" for f in self.failures]
        lines += [f"  ⚠ {w}" for w in self.warnings]
        return "\n".join(lines)


class RiskManager:
    def __init__(self, cfg, client, state_store):
        self.cfg = cfg
        self.client = client
        self.state = state_store
        self.log = client.log

    # ────────────────────────── 主入口 ──────────────────────────

    def check_open_plan(self, plan):
        """审查一份开仓计划。plan 字段：
            instId     str   必填，如 "BTC-USDT-SWAP"
            direction  str   必填，"long" / "short"
            stop_loss  float 必填，止损触发价（R1）
            entry_hint float 可选，参考入场价；不填用当前最新价
            order_type str   必须为 "limit_maker"（R6）
            reason     str   计划理由（写日志用）
        返回 RiskVerdict；通过时 verdict.sized 里带：
            contracts（张数，以此为准）、entry_ref、stop_loss、notional_usdt、
            risk_usdt、risk_pct、leverage_after
        """
        v = RiskVerdict()
        t_start = time.time()
        self.last_kelly = None

        # ── 基础字段检查 ──
        inst_id = plan.get("instId", "")
        direction = str(plan.get("direction", "")).lower()
        try:
            stop = float(plan.get("stop_loss") or 0)
        except (TypeError, ValueError):
            stop = 0.0

        if not inst_id:
            v.fail("R1: 缺少 instId")
        if direction not in ("long", "short"):
            v.fail("R1: direction 必须是 long/short")
        if stop <= 0:
            v.fail("R1: 未提供止损价（stop_loss）—— 无止损不允许开仓")
        if str(plan.get("order_type", "")) != "limit_maker":
            v.fail("R6: 开仓只允许 Maker 限价单（order_type=limit_maker）")

        # 前置字段已失败就不用再拉行情了
        if not v.passed:
            return v

        # ── 拉取账户上下文 ──
        equity = self.client.get_equity()["total_eq"]
        positions = self.client.get_positions()
        inst = self.client.get_instrument(inst_id)
        ticker = self.client.get_ticker(inst_id)

        entry_ref = float(plan.get("entry_hint") or ticker["last"])
        if entry_ref <= 0:
            return v.fail("行情异常：最新价 <= 0，拒绝开仓")

        # ── R5 回撤阶梯（分级；未配置 DRAWDOWN_LADDER 时退回二元熔断）──
        rung, tier = self._ladder_tier()
        if tier is not None:
            if not tier.get("allow_open", True):
                hwm = self.state.get_hwm()
                dd = (hwm - equity) / hwm if hwm > 0 else 0
                return v.fail(
                    f"R5: 回撤阶梯第 {rung} 档生效——回撤 {dd:.1%} 禁止开新仓"
                    f"（风险预算 ×{tier.get('risk_mult', 0)}；恢复需回撤降至 "
                    f"{tier.get('dd', 0) * 0.8:.1%} 以下）"
                )
            if tier.get("risk_mult", 1) < 1:
                v.warn(f"R5: 回撤阶梯第 {rung} 档——风险预算 ×{tier['risk_mult']}")
        elif self.is_drawdown_breach(equity):
            hwm = self.state.get_hwm()
            dd = (hwm - equity) / hwm if hwm > 0 else 0
            return v.fail(
                f"R5: 回撤熔断生效——权益 {equity:.2f} 距高水位 {hwm:.2f} "
                f"回撤 {dd:.1%} ≥ 阈值 {self.cfg.MAX_DRAWDOWN:.0%}，禁止开新仓"
            )

        # ── R1 止损方向 ──
        if direction == "long" and stop >= entry_ref:
            v.fail(f"R1: 多仓止损价 {stop} 必须低于参考入场价 {entry_ref}")
        if direction == "short" and stop <= entry_ref:
            v.fail(f"R1: 空仓止损价 {stop} 必须高于参考入场价 {entry_ref}")
        # 计划过期守卫：提案基于已收盘K线，快照到执行之间行情可能穿过止损价——
        # 现价已经不满足止损方向时，这笔交易天然不合理（开仓即浮亏、止损秒触发）
        live_last = ticker["last"]
        if direction == "long" and live_last <= stop:
            v.fail(f"R1: 现价 {live_last} 已不高于止损价 {stop}——计划过期，拒绝")
        if direction == "short" and live_last >= stop:
            v.fail(f"R1: 现价 {live_last} 已不低于止损价 {stop}——计划过期，拒绝")

        # ── R2 止损距离 ──
        stop_dist = abs(entry_ref - stop)
        stop_dist_pct = stop_dist / entry_ref
        if v.passed:  # 方向对了距离才有意义
            if stop_dist_pct < self.cfg.MIN_STOP_DIST_PCT:
                v.warn(
                    f"R2: 止损距离 {stop_dist_pct:.3%} 过近（< {self.cfg.MIN_STOP_DIST_PCT:.1%}），"
                    f"按 ATR 下限处理"
                )
            if stop_dist_pct > self.cfg.MAX_STOP_DIST_PCT:
                v.fail(
                    f"R2: 止损距离 {stop_dist_pct:.2%} 过远（> {self.cfg.MAX_STOP_DIST_PCT:.1%}），"
                    f"风险失控，拒绝"
                )

        # ── R4 组合层约束 ──
        same_pos = [p for p in positions if p["instId"] == inst_id]
        if same_pos:
            v.fail(f"R4: {inst_id} 已有持仓（{same_pos[0]['direction']} "
                   f"{same_pos[0]['contracts']} 张），当前版本不允许加仓")
        # 挂着的入场单也算暴露：已有未成交挂单时不重复提交
        # （真实环境查交易所；paper/replay 的 client 返回各自维护的挂单）
        try:
            pending_entries = self.client.get_pending_orders(inst_id)
        except OKXAPIError:
            pending_entries = []
        if pending_entries:
            v.fail(f"R4: {inst_id} 已有未成交的入场挂单"
                   f"（ordId={pending_entries[0].get('ordId', '?')}），不重复提交")

        if len(positions) >= self.cfg.MAX_OPEN_POSITIONS:
            v.fail(f"R4: 持仓数量已达上限 {self.cfg.MAX_OPEN_POSITIONS}，禁止再开")

        # ── R3 波动率目标仓位计算 ──
        if v.passed:
            # ATR 单一来源：优先用委员会看过的那份（plan.factors.atr），
            # 缺失时才自己去拉——两个 ATR 口径不一致会让风控按没见过的数字定仓位
            atr = (plan.get("factors") or {}).get("atr")
            if not atr:
                try:
                    atr = self.client.compute_atr(
                        inst_id, period=self.cfg.ATR_PERIOD, bar=self.cfg.ATR_BAR
                    )
                except OKXAPIError as e:
                    return v.fail(f"R3: 无法计算 ATR：{e}")

            atr_dist = self.cfg.ATR_STOP_MULT * atr
            # 有效止损距离取「计划距离」与「ATR 距离」的较大者：
            #   计划止损太近 → 用 ATR 距离算仓位（实际止损仍在计划价，只会更保守）
            #   波动放大 → ATR 变大 → 仓位自动变小（波动率目标）
            eff_dist = max(stop_dist, atr_dist)
            risk_budget = equity * self.cfg.MAX_RISK_PER_TRADE \
                * (tier.get("risk_mult", 1.0) if tier else 1.0)
            # ── Kelly 系数（影子模式）：mult 无条件计算 + 入库（校准曲线先积累）；
            #    KELLY_ENABLED=true 时才乘进预算。LLM 置信度永远不进这个公式——
            #    校准只用 trades 表的真实成交。
            kstore = getattr(self.state, "store", None)
            kelly = {"mult": 1.0, "n": 0, "p": None, "b": None,
                     "note": "无持久层 → 中性 1.0"}
            if kstore is not None:
                from .kelly import mult_for
                kelly = mult_for(kstore, plan.get("analyst"), self.cfg)
            if getattr(self.cfg, "KELLY_ENABLED", False) \
                    and abs(kelly["mult"] - 1.0) > 1e-9:
                risk_budget *= kelly["mult"]
                v.warn(f"Kelly: {plan.get('analyst')} mult="
                       f"{kelly['mult']}（n={kelly['n']}，{kelly['note']}）")
            self.last_kelly = kelly
            # ── R8 同向风险聚合：BTC/ETH/SOL 相关性 ~0.8-0.9，同向持仓本质是
            #    一笔放大 beta 押注——同向已用风险从本笔预算中扣除，聚合超限直接拒绝
            cap = float(getattr(self.cfg, "SAME_DIRECTION_RISK_CAP", 0.0) or 0)
            if cap <= 0:
                cap = self.cfg.MAX_RISK_PER_TRADE * 2
            used_usdt = self._same_dir_open_risk(direction)
            used_frac = used_usdt / equity if equity > 0 else 0.0
            remaining_frac = cap - used_frac
            if remaining_frac <= 0:
                return v.fail(
                    f"R8: 同向({direction})风险预算耗尽——已有同向持仓风险 "
                    f"{used_usdt:.0f}U（{used_frac:.2%}）≥ 上限 {cap:.0%}，"
                    f"一次不利波动会同时打掉所有止损"
                )
            if remaining_frac < self.cfg.MAX_RISK_PER_TRADE:
                shrunk = equity * remaining_frac
                v.warn(f"R8: 同向({direction})风险已用 {used_frac:.2%}，"
                       f"本笔预算压缩至 {shrunk:.0f}U（剩 {remaining_frac:.2%}）")
                risk_budget = min(risk_budget, shrunk)

            # 每张合约在有效止损距离下的亏损（USDT 本位）= eff_dist × ctVal
            contracts_raw = risk_budget / (eff_dist * inst["ctVal"])
            contracts = self.client.round_size(
                contracts_raw, inst["lotSz"], inst["minSz"]
            )
            if contracts <= 0:
                return v.fail(
                    f"R3: 按风险预算算出的仓位不足最小下单量"
                    f"（理论 {contracts_raw:.4f} 张 < minSz {inst['minSz']} 张），拒绝"
                )

            # R4 续：总杠杆上限
            notional_new = contracts * inst["ctVal"] * entry_ref
            notional_exist = sum(
                p["contracts"] * self.client.get_instrument(p["instId"])["ctVal"]
                * (p["mark_px"] or p["avg_px"])
                for p in positions
            )
            leverage_after = (notional_exist + notional_new) / equity if equity > 0 else 999
            if leverage_after > self.cfg.MAX_TOTAL_LEVERAGE:
                v.fail(
                    f"R4: 总杠杆超限——加仓后名义/权益 = {leverage_after:.2f}x "
                    f"> 上限 {self.cfg.MAX_TOTAL_LEVERAGE}x"
                )

            # 实际单笔风险复核（用计划止损价，不是 ATR 距离）
            actual_risk = contracts * inst["ctVal"] * stop_dist
            risk_pct = actual_risk / equity if equity > 0 else 999
            if risk_pct > self.cfg.MAX_RISK_PER_TRADE * 1.001:  # 留浮点余量
                v.fail(
                    f"R3: 实际单笔风险 {risk_pct:.3%} 超过预算 "
                    f"{self.cfg.MAX_RISK_PER_TRADE:.1%}"
                )

            v.sized = {
                "instId": inst_id,
                "direction": direction,
                "contracts": contracts,          # 张数（交易所单位），下单以此为准
                "entry_ref": entry_ref,          # 参考入场价
                "stop_loss": stop,               # 止损触发价（Planner 给的，风控只校验）
                "notional_usdt": notional_new,
                "risk_usdt": actual_risk,
                "risk_pct": risk_pct,
                "atr": atr,
                "leverage_after": leverage_after,
            }
            v.warn(
                f"仓位 {contracts} 张（名义 {notional_new:.0f} U，单笔风险 "
                f"{risk_pct:.2%}，加仓后总杠杆 {leverage_after:.2f}x，ATR={atr:.1f}）"
            )

            # ── R7 盈亏比检查（RR ≥ MIN_RR 才出手；无条件执行，永远算出 target/rr）──
            # 目标选择：按距离升序找"最近的、值得去的"结构位——
            #   1) 距入场 < MIN_TARGET_ATR×ATR 的位直接过滤（贴脸的位是噪声，不是目标；
            #      旧逻辑取"最近位"曾被 0.41×ATR 外的贴脸阻力以 RR=0.31 否决过本可通过的交易）
            #   2) 取第一个 RR 达标的位（一个近位不再否决有远位支撑的交易）
            #   3) 都没有 → TARGET_ATR_MULT×ATR 兜底（target_source='atr_multiple'），
            #      突破单在结构位方向没有位时也逃不过 RR 检查
            factor_snap = plan.get("factors") or {}
            sr = factor_snap.get("sr") or {}
            atr_val = v.sized.get("atr")
            if not atr_val:
                v.warn("R7 跳过：无 ATR（不该发生，因子快照缺失）")
            else:
                sign = 1 if direction == "long" else -1
                levels = [x for x in (sr.get("resistances", []) if direction == "long"
                                      else sr.get("supports", []))
                          if sign * (x - entry_ref) > 0]
                levels.sort(key=lambda x: abs(x - entry_ref))
                min_target_dist = self.cfg.MIN_TARGET_ATR * atr_val
                target, target_source = None, None
                for lv in levels:
                    dist = abs(lv - entry_ref)
                    if dist < min_target_dist:
                        continue
                    if dist / stop_dist >= self.cfg.MIN_RR:
                        target, target_source = lv, "structure"
                        break
                if target is None:
                    target = entry_ref + sign * self.cfg.TARGET_ATR_MULT * atr_val
                    target_source = "atr_multiple"
                rr = abs(target - entry_ref) / stop_dist
                if rr < self.cfg.MIN_RR:
                    v.fail(
                        f"R7: 盈亏比不足——目标 {target:.4g}（{target_source}），"
                        f"止损距离 {stop_dist:.4g}，RR={rr:.2f} < {self.cfg.MIN_RR}"
                    )
                else:
                    v.sized["target"] = round(target, 6)
                    v.sized["target_source"] = target_source
                    v.sized["rr"] = round(rr, 2)
                    v.warn(f"盈亏比 RR={rr:.2f}（目标 {target:.4g}，来源 {target_source}）")
                    # 双口径显式化：最近结构位（不过滤贴脸位）的 RR 单独报——
                    # 批准 RR 依赖"跳过近位取远位"的取舍，裁判与审计都看得到，
                    # 而不是软硬两层各算各的（资管裁判曾按最近位算出 0.33 并
                    # 四次正确提示同向叠加，而 R7 按 107.825 批出 1.77）
                    if levels:
                        rr_nearest = abs(levels[0] - entry_ref) / stop_dist
                        if rr_nearest < self.cfg.MIN_RR:
                            v.warn(
                                f"最近结构位 {levels[0]:.4g} 的 RR 仅 "
                                f"{rr_nearest:.2f}（< {self.cfg.MIN_RR}，"
                                f"距入场 {abs(levels[0] - entry_ref):.4g}）——"
                                f"批准依赖更远目标，价格可能先在近位受阻"
                            )

        self.log.info(
            "风控审查 %s %s：%s（%.0fms）",
            inst_id, direction, "通过" if v.passed else "拒绝",
            (time.time() - t_start) * 1000,
        )
        for f in v.failures:
            self.log.warning("风控拒绝原因：%s", f)
        return v

    def _same_dir_open_risk(self, direction):
        """同方向未平仓位的 risk_usdt 之和（来自 trades 表）。
        无持久层（测试 stub）时返回 0。"""
        store = getattr(self.state, "store", None)
        if store is None:
            return 0.0
        try:
            row = store.query(
                "SELECT COALESCE(SUM(risk_usdt),0) s FROM trades "
                "WHERE status='open' AND direction=?", (direction,))
            return float(row[0]["s"] or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    # ────────────────────────── 回撤熔断 ──────────────────────────

    def _ladder_tier(self):
        """当前回撤档位：rung 从 run_state 读取（由 tick 维护）。
        未配置阶梯或 state 不支持时返回 (0, None)。"""
        ladder = list(getattr(self.cfg, "DRAWDOWN_LADDER", []) or [])
        if not ladder:
            return 0, None
        try:
            rung = int(self.state.get_rung() or 0)
        except AttributeError:  # 测试 stub 没有 rung 概念
            return 0, None
        if 0 < rung <= len(ladder):
            return rung, ladder[rung - 1]
        return rung, None

    def is_drawdown_breach(self, equity):
        """权益距高水位回撤是否超过 MAX_DRAWDOWN。"""
        hwm = self.state.get_hwm()
        if hwm <= 0:  # 首次运行还没有高水位
            return False
        return (hwm - equity) / hwm > self.cfg.MAX_DRAWDOWN

    def update_equity_hwm(self, equity):
        """每轮循环结束调用：抬升高水位并返回当前回撤。"""
        hwm, dd = self.state.update_hwm(equity)
        self.log.debug("权益 %.2f / 高水位 %.2f / 回撤 %.2f%%", equity, hwm, dd * 100)
        return hwm, dd
