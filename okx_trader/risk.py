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

from client import OKXAPIError


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

        # ── R5 回撤熔断（先查，熔断时不用算后面）──
        if self.is_drawdown_breach(equity):
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

        if len(positions) >= self.cfg.MAX_OPEN_POSITIONS:
            v.fail(f"R4: 持仓数量已达上限 {self.cfg.MAX_OPEN_POSITIONS}，禁止再开")

        # ── R3 波动率目标仓位计算 ──
        if v.passed:
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
            risk_budget = equity * self.cfg.MAX_RISK_PER_TRADE

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

            # ── R7 盈亏比检查（借鉴调研中的 Risk Manager 实践：RR ≥ 1.5 才出手）──
            # 目标空间：优先用最近的逆向结构位（做多看阻力、做空看支撑），
            # 没有结构位时用 2.5×ATR 作为默认目标。因子快照由委员会附在 plan["factors"]。
            factor_snap = plan.get("factors") or {}
            sr = factor_snap.get("sr") or {}
            atr_val = v.sized.get("atr")
            if atr_val and (sr.get("resistances") or sr.get("supports")):
                if direction == "long":
                    ups = [x for x in sr.get("resistances", []) if x > entry_ref]
                    target = min(ups) if ups else entry_ref + 2.5 * atr_val
                else:
                    downs = [x for x in sr.get("supports", []) if x < entry_ref]
                    target = max(downs) if downs else entry_ref - 2.5 * atr_val
                rr = abs(target - entry_ref) / stop_dist
                if rr < self.cfg.MIN_RR:
                    v.fail(
                        f"R7: 盈亏比不足——目标 {target:.4g}（结构位），止损距离 "
                        f"{stop_dist:.4g}，RR={rr:.2f} < {self.cfg.MIN_RR}"
                    )
                else:
                    v.sized["target"] = round(target, 6)
                    v.sized["rr"] = round(rr, 2)
                    v.warn(f"盈亏比 RR={rr:.2f}（目标 {target:.4g}）")

        self.log.info(
            "风控审查 %s %s：%s（%.0fms）",
            inst_id, direction, "通过" if v.passed else "拒绝",
            (time.time() - t_start) * 1000,
        )
        for f in v.failures:
            self.log.warning("风控拒绝原因：%s", f)
        return v

    # ────────────────────────── 回撤熔断 ──────────────────────────

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
