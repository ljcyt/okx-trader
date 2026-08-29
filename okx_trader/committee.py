# -*- coding: utf-8 -*-
"""多 Agent 委员会（用户确认的架构：3 分析师提案 + 3 裁判打分 + 聚合）

流程：
    1. 三位分析师（不同交易风格人设）各自读因子报告 → 独立提案或弃权
       提案：{instId, direction, stop_loss, confidence, reason}
    2. 三位裁判（技术/风控/资金管理视角）对每份提案打分 0-10 + 通过/否决
    3. 聚合：平均分 ≥ SCORE_THRESHOLD 且 ≥2/3 裁判通过 → 得分最高者胜出
    4. 胜出提案交给硬风控（risk.py）终审 —— 委员会没有下单权

LLM 模式：每个 agent 独立调用模型（同一模型不同人设 prompt）。
基线模式（无 LLM）：每个 agent 用确定性规则模拟，保证链路可先跑通。
"""
import time

from .factors import regime_label
from .llm import LLMClient
from .store import write as w

# 裁判法定人数：有效票不足此数时提案不能胜出（防止 LLM 超时导致单裁判独裁）
MIN_JUDGE_VOTES = 2
# 同一人设连续出现幻觉数字达到该次数 → 降为 observing（只记录意见，不参与授权）
HALLUCINATION_DEMOTE_STREAK = 5


def fmt_score(v):
    return "—" if v is None else f"{float(v):.1f}"


def verify_numbers(reason, report_text, own=(), extra_own=()):
    """防幻觉闸门（代码做，不靠模型自觉）：
    提案 reason 里引用的数字必须能在【全部标的的因子报告原文】里找到
    （LLM 提示词含三份报告，跨标的引用是合法论证）。

    豁免清单：
      own       —— 提案自己算出的字段（止损/入场/置信度）
      extra_own —— 方法论常量（ATR_STOP_MULT、MIN_RR 等，来自配置）；
                    否则"止损放 1.5×ATR"会被当成幻觉误报并实扣 2 分

    容差按量级分档（相对误差）：
        >=1000（价格类）0.05% —— 否则 BTC ±0.5% = ±400，闸门形同虚设
        1~1000（RSI/比率）0.5%
        <1（费率/小占比）5%
    连字符/波浪线数字区间先规约（"2515-2538"、"104~104.58" 各是两个数字）；
    原始值与百分号写法（0.0001 vs 0.0100%）互相兼容。
    返回对不上号的数字列表。"""
    import re

    def _num(s):
        try:
            return float(str(s).rstrip("%"))
        except (ValueError, TypeError):
            return None

    def _tokens(text):
        # 认识科学计数法：factors.py 若用 %g 渲染 ≥1e5 的值会产出 8.027e+04，
        # 旧正则会把它切成 '8.027' 和 '04' 两个垃圾 token——既造成误报
        # （引用真实 EMA 被判幻觉）又造成漏报（幻觉 8.03 撞上垃圾 8.027）
        # 连字符与波浪线区间都规约成独立数字（"2515-2538"、"104~104.58"）
        text = re.sub(r"(?<=\d)\s*[-\u2013~]\s*(?=\d)", "，", str(text or ""))
        return re.findall(r"-?\d+\.?\d*(?:[eE][+-]?\d+)?%?", text)

    def _match(cv, pool):
        for t in pool:
            if abs(cv - t) <= _tol(cv) * max(abs(cv), abs(t), 1e-9):
                return True
            # 原始值 ↔ 百分号写法双向互认：0.0001 ↔ 0.0100%、0.01 ↔ 1。
            # 只对费率量级（|cv|<0.1）启用——对大数启用会让 99.9 撞上
            # 报告里 "1H" 切出来的 1 这类偶然 token，闸门彻底失效
            if abs(cv) < 0.1:
                if abs(cv * 100 - t) <= _tol(cv) * max(abs(cv * 100), abs(t), 1e-9):
                    return True
                if abs(cv / 100 - t) <= _tol(cv) * max(abs(cv / 100), abs(t), 1e-9):
                    return True
        return False

    def _tol(v):
        a = abs(v)
        if a >= 1000:
            return 0.0005
        if a >= 1:
            return 0.005
        return 0.05

    cited = _tokens(reason)
    pool = [_num(t) for t in _tokens(report_text)]
    pool = [p for p in pool if p is not None]
    own_vals = [_num(o) for o in list(own) + list(extra_own)]
    own_vals = [o for o in own_vals if o is not None]
    missing = []
    for c in cited:
        cv = _num(c)
        if cv is None:
            continue
        if _match(cv, pool):
            continue
        if _match(cv, own_vals):  # 自己算出来的字段豁免
            continue
        missing.append(c)
    return missing

# ── 三位分析师：人设与风格 ──────────────────────────────────────────
ANALYSTS = [
    {
        "name": "趋势猎手", "style": "trend",
        "prompt": ("你是「趋势猎手」，一位趋势跟踪交易员。你只在趋势明确时出手："
                   "多头排列+MACD红柱→做多；空头排列+MACD绿柱→做空。"
                   "震荡市和指标矛盾时果断弃权。止损必须放在趋势失效位（如1.5×ATR外）。"),
    },
    {
        "name": "均值回归者", "style": "meanrev",
        "prompt": ("你是「均值回归者」，专做超买超卖的回归。RSI>70 且价格触布林上轨→做空；"
                   "RSI<30 且价格触布林下轨→做多。中位区间一律弃权。"
                   "止损放在区间外缓冲（约1×ATR），方向逆势所以仓位概念上更保守。"),
    },
    {
        "name": "资金哨兵", "style": "funding",
        "prompt": ("你是「资金哨兵」，关注资金费率透露的头寸拥挤度："
                   "费率处于近90期高分位（≥90%分位，多头极度拥挤）→倾向做空；"
                   "低分位（≤10%，空头极度拥挤）→倾向做多；分位中性→弃权。"
                   "你必须同时参考趋势因子，逆势单要求更紧的止损。"),
    },
]

# ── 三位裁判：打分视角 ──────────────────────────────────────────────
JUDGES = [
    {"name": "技术裁判",
     "prompt": ("你是「技术裁判」。审查提案的方向是否与趋势/动量/形态因子自洽："
               "顺趋势且指标配合→8-9分；方向与主趋势相悖→3-4分；理由与数据矛盾→0-2分。"
               "同时检查止损价方向正确、距离合理(0.2%~5%)。")},
    {"name": "风控裁判",
     "prompt": ("你是「风控裁判」。只看风险：止损距离是否落在合理区间；"
               "波动是否极端(ATR/价格>3%要扣分)；资金费率是否异常(|费率|>0.1%要扣分)；"
               "量比异常放大(>3)要扣分。无硬伤→7-8分，有隐患→4-6分，危险→0-3分。")},
    {"name": "资金管理裁判",
     "prompt": ("你是「资金管理裁判」。评估这笔交易值不值得占用风险预算："
               "置信度、盈亏比(止损距离 vs 现实波动)、当前账户是否已有暴露。"
               "高质量出手→8分左右，平庸机会→5-6分，差机会→0-4分。")},
]

SCORE_THRESHOLD_DEFAULT = 6.5


class Committee:
    def __init__(self, cfg, client, llm=None, store=None, env="paper"):
        self.cfg = cfg
        self.client = client
        self.llm = llm or LLMClient(cfg, logger=client.log)
        self.log = client.log
        self.store = store      # 可选：传入后记忆从 SQLite 读
        self.env = env
        self.threshold = getattr(cfg, "SCORE_THRESHOLD", SCORE_THRESHOLD_DEFAULT)

    # ────────────────────────── 主入口 ──────────────────────────

    def decide(self, snapshot):
        """snapshot 需包含 equity / positions / factors（factors.py 的报告）。
        返回完整决策记录（写进 rounds 日志），其中 plan 字段供下游风控/执行使用。
        """
        factor_text = "\n".join(
            format_one(snapshot["factors"][inst])
            for inst in self.cfg.SYMBOLS if snapshot["factors"].get(inst)
        )
        held = {p["instId"] for p in snapshot["positions"]}
        account_ctx = (f"账户权益 {snapshot['equity']:.2f} USDT；"
                       f"当前持仓：{held if held else '无'}；"
                       f"回撤：{snapshot['drawdown']:.1%}")
        memory = self.recent_rounds_summary()
        if memory:
            account_ctx += f"\n近期委员会决策记录（供反思参考，避免连续重复失败思路）：\n{memory}"

        # 1. 分析师提案
        proposals = []
        analyst_log = []
        for slot, a in enumerate(ANALYSTS):
            prop = self._ask_analyst(a, factor_text, account_ctx, held, snapshot)
            prop["slot"] = slot
            analyst_log.append(prop)
            if prop.get("action") == "open":
                proposals.append(prop)
        self.log.info("委员会：分析师产出 %d 份有效提案 / %d 份弃权",
                      len(proposals), len(analyst_log) - len(proposals))
        # regime 标签（代码判定）：人设 × 市况的匹配度在聚合时用作扣分项
        for p in proposals:
            rep = (snapshot.get("factors") or {}).get(p.get("instId"))
            if rep:
                p["regime"] = regime_label(rep, self.cfg)

        if not proposals:
            return {"action": "hold", "mode": self.llm_mode_name(),
                    "analysts": analyst_log,
                    "reason": "所有分析师弃权，本轮不交易"}

        # 2. 裁判打分 + 聚合 + 数字核对 + 修订循环（最多 MAX_REVISIONS 次）
        max_rev = int(getattr(self.cfg, "MAX_REVISIONS", 1) or 0)
        revisions = 0
        judging = self._ask_judges(proposals, factor_text, account_ctx, snapshot)
        self._aggregate(proposals, judging, snapshot)
        while (not [p for p in proposals if p.get("qualify")] and proposals
               and self.llm.available and revisions < max_rev):
            # 修订循环：全部 concerns 回喂给最高分候选，修订或撤回，重打一次分
            revisions += 1
            candidate = max(proposals, key=lambda p: (p["avg_score"],
                                                      _f(p.get("confidence"))))
            concerns = [row["concerns"] for row in judging["rows"]
                        if row["idx"] == proposals.index(candidate)
                        and row.get("concerns")]
            revised = self._ask_revision(candidate, concerns, snapshot,
                                         factor_text, account_ctx, held)
            if not revised or revised.get("action") != "open":
                # 撤回：保留原提案及其打分（审计回溯需要看到"提了什么、为何被拒"）
                break
            revised["analyst"] = candidate["analyst"]
            revised["slot"] = candidate.get("slot")
            # 修订提案继承原提案的 regime/style——否则 _aggregate 的 regime 门控
            # 对修订稿失效，等于"被扣分的提案修订一轮就能免罚重打分"
            revised["regime"] = candidate.get("regime")
            revised["style"] = candidate.get("style")
            proposals = [revised]
            judging = self._ask_judges(proposals, factor_text, account_ctx,
                                       snapshot)
            self._aggregate(proposals, judging, snapshot)

        qualified = [p for p in proposals if p.get("qualify")]
        qualified.sort(key=lambda p: (p["avg_score"], _f(p.get("confidence"))), reverse=True)

        decision = {
            "mode": self.llm_mode_name(),
            "analysts": analyst_log,
            "judging": judging["rows"],
            "revisions": revisions,
            "scoreboard": [
                {"analyst": p["analyst"], "instId": p["instId"],
                 "direction": p["direction"], "avg_score": p["avg_score"],
                 "votes": p["votes"], "qualify": p["qualify"]}
                for p in proposals
            ],
        }
        if qualified:
            win = qualified[0]
            others = [f"{p['analyst']}({p['avg_score']})" for p in qualified[1:]]
            # 把胜出提案对应标的的因子快照（支撑阻力/ATR）附给风控，用于盈亏比检查
            src_report = (snapshot.get("factors") or {}).get(win["instId"]) or {}
            decision["action"] = "open"
            decision["plan"] = {
                "instId": win["instId"],
                "direction": win["direction"],
                "stop_loss": win["stop_loss"],
                "entry_hint": win.get("entry_hint"),
                "order_type": "limit_maker",
                "confidence": win.get("confidence"),
                "reason": (f"[{win['analyst']}] {win['reason']} "
                           f"｜委员会均分 {win['avg_score']}（{win['votes']} 通过）"),
                "committee_score": win["avg_score"],
                "analyst": win["analyst"],
                "factors": {k: src_report[k] for k in ("sr", "atr") if k in src_report},
            }
            decision["reason"] = (
                f"胜出提案：{win['analyst']} → {win['instId']} {win['direction']} "
                f"均分 {win['avg_score']}（{win['votes']} 通过）"
                + (f"；同分候选：{', '.join(others)}" if others else ""))
            self.log.info("委员会：%s", decision["reason"])
        else:
            decision["action"] = "hold"
            decision["reason"] = (
                "有提案但未达标：" +
                ("；".join(f"{p['analyst']} 均分{p['avg_score']}（{p['votes']}）"
                           for p in proposals) or "无"))
            self.log.info("委员会：%s", decision["reason"])
        return decision

    def llm_mode_name(self):
        return "llm" if self.llm.available else "baseline"

    def _aggregate(self, proposals, judging, snapshot):
        """聚合：均分门槛 + 多数决 + 法定票数 + 数字核对扣分。

        数字核对用代码做（不靠模型自觉）：提案 reason 里引用的数字必须能在
        该标的的因子报告原文里找到（±0.5% 相对误差；自己算出的止损/入场价豁免）。
        对不上的写 hallucinated_number 事件并扣分；同一人设连续多次出现幻觉
        自动降为 observing——只记录意见，不再参与授权。
        """
        penalty = float(getattr(self.cfg, "HALLUCINATION_PENALTY", 2.0) or 0)
        # 池子用【全部标的】的报告原文——LLM 收到的提示词含三份报告，
        # 跨标的引用（如引用 ETH 的 FVG 来论证 SOL）是合法论证
        all_reports_text = " NEWLINE_JOIN ".join(
            format_one(r) for r in (snapshot.get("factors") or {}).values() if r)
        all_reports_text = all_reports_text.replace(" NEWLINE_JOIN ", chr(10))
        # 方法论常量白名单："止损放 1.5×ATR" 这类正当表述不是幻觉
        consts = [getattr(self.cfg, k, None) for k in (
            "ATR_STOP_MULT", "TARGET_ATR_MULT", "MIN_RR", "MIN_TARGET_ATR",
            "MAX_RISK_PER_TRADE", "LEVERAGE", "MIN_STOP_DIST_PCT",
            "MAX_STOP_DIST_PCT", "SCORE_THRESHOLD", "MAKER_PRICE_OFFSET")]
        consts = [float(c) for c in consts if c is not None]
        quorum_short = False
        for i, p in enumerate(proposals):
            rows = [row for row in judging["rows"] if row["idx"] == i]
            scores = [row["score"] for row in rows]
            votes = sum(1 for row in rows if row["approved"])
            avg = round(sum(scores) / len(scores), 2) if scores else 0
            p["votes"] = f"{votes}/{len(rows) or len(JUDGES)}"

            missing = verify_numbers(
                p.get("reason"), all_reports_text,
                own=[p.get("stop_loss"), p.get("entry_hint"),
                     p.get("confidence")] + consts)
            if missing:
                # 只记录，不扣分不禁言：派生值（算出的 RR/百分比）会持续误报，
                # 而 −2 分 + 禁言的代价被一次误报实测证伪过（趋势猎手冤案）
                p["hallucinated"] = missing
            if self.store is not None:
                streak_key = f"hallu_streak_{p.get('analyst')}"
                if missing:
                    streak = int(self.store.state_get(self.env, streak_key) or 0) + 1
                    self.store.state_set(self.env, streak_key, streak)
                    w.write_event(
                        self.store, self.env, "hallucinated_number",
                        f"{p.get('analyst')} 提案引用了因子报告里不存在的数字："
                        f"{missing}（连续第 {streak} 次）",
                        level="warn")
                else:
                    self.store.state_set(self.env, streak_key, 0)

            # regime 门控（方向性）：趋势市压均值回归、震荡市压趋势、趋势方向
            # 与提案方向相悖也压——否则强趋势里两个相反人设对同一标的各提一案
            style = p.get("style")
            reg = p.get("regime")
            rp = float(getattr(self.cfg, "REGIME_MISMATCH_PENALTY", 1.0) or 0)
            mismatch = None
            if reg in ("trending_up", "trending_down"):
                if style == "meanrev":
                    mismatch = f"{reg} 市压均值回归 −{rp}"
                elif style == "trend":
                    if reg == "trending_up" and p.get("direction") == "short":
                        mismatch = f"trending_up 市压趋势做空 −{rp}"
                    elif reg == "trending_down" and p.get("direction") == "long":
                        mismatch = f"trending_down 市压趋势做多 −{rp}"
            elif reg == "ranging" and style == "trend":
                mismatch = f"ranging 市压趋势跟随 −{rp}"
            if reg == "high_vol":
                extra = f"高波动统压 −{rp * 0.5}"
                mismatch = (mismatch + "；" + extra) if mismatch else extra
            if mismatch:
                avg = round(avg - rp * (1.0 if reg != "high_vol" else 0.5), 2)
                p["regime_penalty"] = True
                p["reason"] = (p.get("reason") or "") + f"（regime={reg}，{mismatch}）"

            p["avg_score"] = avg
            p["qualify"] = (len(scores) >= MIN_JUDGE_VOTES
                            and avg >= self.threshold
                            and votes * 2 > len(scores)
                            and not p.get("demoted"))
            if len(scores) < MIN_JUDGE_VOTES:
                quorum_short = True

        if quorum_short and self.store is not None:
            w.write_event(self.store, self.env, "judge_quorum",
                          "有效裁判票不足法定人数（2），全部提案不授权",
                          level="warn")

    def _ask_revision(self, candidate, concerns, snapshot, factor_text,
                      account_ctx, held):
        """修订循环：裁判否决后把 concerns 回喂给原分析师一次（修订或撤回）。"""
        if not self.llm.available:
            return None
        persona = next((a for a in ANALYSTS if a["name"] == candidate.get("analyst")),
                       ANALYSTS[0])
        user = (f"{factor_text}\n\n{account_ctx}\n\n"
                f"你此前提出：{candidate.get('instId')} {candidate.get('direction')} "
                f"止损 {candidate.get('stop_loss')}。裁判意见：{concerns}。\n"
                f"请根据意见修订提案（保留原方向就更新止损与理由；意见成立就撤回）。"
                f"reason 仍必须以 'Delta: ' 开头说明你改了什么。只输出 JSON：\n"
                f'修订: {{"action":"open","instId":"...","direction":"long|short",'
                f'"stop_loss":数字,"confidence":0~1,"reason":"Delta: ..."}}\n'
                f'撤回: {{"action":"hold","reason":"Delta: ..."}}')
        try:
            out = self.llm.chat(persona["prompt"], user, expect_json=True,
                                role=f"analyst:{persona['name']}")
        except Exception as e:  # noqa: BLE001
            self.log.warning("修订调用失败：%s", e)
            return None
        if out.get("action") == "open":
            if not (out.get("instId") in self.cfg.SYMBOLS
                    and out.get("direction") in ("long", "short")
                    and float(out.get("stop_loss") or 0) > 0):
                return None
            out["stop_loss"] = float(out["stop_loss"])
            out.setdefault("order_type", "limit_maker")
        out["revised"] = True
        return out

    def recent_rounds_summary(self, n=8):
        """轻量记忆/反思：最近 n 轮的提案**及其结果**（盈亏/R倍数/出场原因），
        加上按人设的累计战绩，回喂给 LLM agent——这是「结果 → 决策」的唯一通路。"""
        if self.store is None:
            return ""
        try:
            rows = self.store.query(
                "SELECT r.ts, r.status, p.analyst, p.inst_id, p.direction, "
                "       p.avg_score, t.realized_pnl, t.r_multiple, t.exit_reason "
                "FROM rounds r "
                "LEFT JOIN proposals p ON p.round_pk = r.id AND p.is_winner = 1 "
                "LEFT JOIN trades    t ON t.open_round_pk = r.id "
                "ORDER BY r.ts DESC LIMIT ?", (n,))
        except Exception:  # noqa: BLE001 —— 记忆只是增益，查不到就空着
            return ""
        lines = []
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r["ts"] or 0))
            what = (f"[{r['analyst']}] {r['inst_id']} {r['direction']}"
                    f" 均分{fmt_score(r['avg_score'])}" if r["inst_id"] else "")
            outcome = ""
            if r["realized_pnl"] is not None:
                rr = r["r_multiple"]
                outcome = (f" → {r['exit_reason'] or 'closed'} "
                           f"{rr:+.1f}R（{r['realized_pnl']:+.2f} U）")
            elif r["status"] in ("opened", "no_fill", "planned"):
                outcome = " → 持仓中"
            lines.append(f"  {ts} 状态={r['status']} {what}{outcome}")
        lines.append(self._analyst_track_record())
        return "\n".join(reversed(lines))

    def _analyst_track_record(self):
        """按人设的累计战绩（与 /api/stats 的 by_analyst 同一查询口径）。"""
        if self.store is None:
            return ""
        try:
            rows = self.store.query(
                "SELECT analyst, COUNT(*) n, "
                "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins, "
                "SUM(r_multiple) sum_r FROM trades "
                "WHERE status='closed' AND analyst IS NOT NULL GROUP BY analyst")
        except Exception:  # noqa: BLE001
            return ""
        if not rows:
            return ""
        parts = [f"{r['analyst']} {r['n']}笔 胜率"
                 f"{(r['wins'] / r['n']) * 100:.0f}% 累计{r['sum_r']:+.1f}R"
                 for r in rows]
        return "  按人设战绩：" + "｜".join(parts)



    # ────────────────────────── 分析师 ──────────────────────────

    def _ask_analyst(self, analyst, factor_text, account_ctx, held, snapshot):
        """单个分析师：LLM 或基线规则。返回统一结构的提案/弃权记录。"""
        base = {"analyst": analyst["name"], "style": analyst["style"]}
        if self.llm.available:
            user = (f"{factor_text}\n\n{account_ctx}\n\n"
                    f"注意：已有持仓的标的不允许再提（当前持仓：{held or '无'}）。"
                    f"reason 必须以 'Delta: ' 开头——先说明相比上一轮你的判断"
                    f"变了什么（或为何维持），再给证据与结论。"
                    f"请以你的人设给出本轮决策，只输出 JSON：\n"
                    f'开仓: {{"action":"open","instId":"...","direction":"long|short",'
                    f'"stop_loss":数字,"confidence":0~1,"reason":"..."}}\n'
                    f'弃权: {{"action":"hold","reason":"..."}}')
            try:
                out = self.llm.chat(analyst["prompt"], user, expect_json=True,
                                    role=f"analyst:{analyst['name']}")
                out.update(base)
                if out.get("action") == "open":
                    out.setdefault("order_type", "limit_maker")
                    # 基本合法性：字段缺失或标的不在白名单，直接降级为弃权
                    if not (out.get("instId") in self.cfg.SYMBOLS
                            and out.get("direction") in ("long", "short")
                            and float(out.get("stop_loss") or 0) > 0):
                        return {**base, "action": "hold",
                                "reason": f"提案不合法被丢弃（标的需在 {self.cfg.SYMBOLS} 内，"
                                          f"且方向/止损齐全）"}
                    out["stop_loss"] = float(out["stop_loss"])
                self.log.info("分析师[%s]（LLM）：%s", analyst["name"],
                              "开仓 %s %s" % (out.get("instId"), out.get("direction"))
                              if out.get("action") == "open" else "弃权")
                return out
            except Exception as e:  # noqa: BLE001 —— 单个 agent 失败不影响委员会
                self.log.warning("分析师[%s] LLM 失败：%s", analyst["name"], e)
                return {**base, "action": "hold", "reason": f"调用失败：{e}"}
        ctx = {"cfg": self.cfg,
               "reports": snapshot.get("factors") or {},
               "held": held}
        return {**_baseline_analyst(analyst["style"], ctx), **base}

    # ────────────────────────── 裁判 ──────────────────────────

    def _ask_judges(self, proposals, factor_text, account_ctx, snapshot):
        """所有裁判对所有提案打分。返回 {"rows": [{idx, judge, score, approved, concerns}]}。"""
        rows = []
        if self.llm.available:
            props_text = "\n".join(
                f"提案{i}（来自{p['analyst']}）：{p['instId']} {p['direction']} "
                f"止损 {p['stop_loss']} 置信度 {p.get('confidence')}；理由：{p['reason']}"
                for i, p in enumerate(proposals))
            for j in JUDGES:
                user = (f"因子报告：\n{factor_text}\n\n{account_ctx}\n\n候选提案：\n{props_text}\n\n"
                        f"请以你的人设给每份提案打分，只输出 JSON："
                        f'{{"scores":[{{"idx":0,"score":0~10,"approved":true/false,'
                        f'"concerns":"一句话"}}]}}')
                try:
                    out = self.llm.chat(j["prompt"], user, expect_json=True,
                                    role=f"judge:{j['name']}")
                    for row in out.get("scores", []):
                        idx = int(row.get("idx", -1))
                        if 0 <= idx < len(proposals):
                            rows.append({"idx": idx, "judge": j["name"],
                                         "score": float(row.get("score", 0)),
                                         "approved": bool(row.get("approved")),
                                         "concerns": row.get("concerns", "")})
                except Exception as e:  # noqa: BLE001
                    self.log.warning("裁判[%s] LLM 失败，该票作废：%s", j["name"], e)
        else:
            # 基线裁判：确定性规则打分
            for j in JUDGES:
                for i, p in enumerate(proposals):
                    score, concerns = _baseline_judge(j["name"], p, snapshot, self.cfg)
                    rows.append({"idx": i, "judge": j["name"], "score": score,
                                 "approved": score >= 6, "concerns": concerns})
        return {"rows": rows}


# ────────────────────────── 基线（无 LLM）确定性规则 ──────────────────────────

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _baseline_analyst(style, ctx):
    """ctx: {"reports": {instId: factor_report}, "held": set}

    横截面选优：遍历【全部】标的收集候选、按信号强度取最优——
    旧写法第一个命中就 return，结构上永远偏向 SYMBOLS 列表头部的 BTC。"""
    reports, held, cfg = ctx["reports"], ctx["held"], ctx["cfg"]
    stop_mult = cfg.ATR_STOP_MULT
    best = None

    for inst_id in cfg.SYMBOLS:
        r = reports.get(inst_id)
        if not r or inst_id in held:
            continue
        price, atr = r["price"], r["atr"]
        cand = None

        if style == "trend":
            mtf = r.get("mtf") or {}
            t4h = (mtf.get("4H") or {}).get("trend", "")
            if "多头排列" in r["trend"] and r["rsi14"] < 75 and "下降" not in t4h:
                cand = {"action": "open", "instId": inst_id, "direction": "long",
                        "stop_loss": round(price - stop_mult * atr, 4),
                        "confidence": 0.65, "order_type": "limit_maker",
                        "reason": f"多头排列+MACD{r['macd']['state']}，4H不逆势，趋势做多"}
            elif "空头排列" in r["trend"] and r["rsi14"] > 25 and "上升" not in t4h:
                cand = {"action": "open", "instId": inst_id, "direction": "short",
                        "stop_loss": round(price + stop_mult * atr, 4),
                        "confidence": 0.65, "order_type": "limit_maker",
                        "reason": f"空头排列+MACD{r['macd']['state']}，4H不逆势，趋势做空"}
        elif style == "meanrev":
            if r["rsi14"] >= 70 and "上轨" in r["price_vs_boll"]:
                cand = {"action": "open", "instId": inst_id, "direction": "short",
                        "stop_loss": round(price + 1.0 * atr, 4),
                        "confidence": 0.5, "order_type": "limit_maker",
                        "reason": f"RSI {r['rsi14']:.0f} 超买且触布林上轨，回归做空"}
            elif r["rsi14"] <= 30 and "下轨" in r["price_vs_boll"]:
                cand = {"action": "open", "instId": inst_id, "direction": "long",
                        "stop_loss": round(price - 1.0 * atr, 4),
                        "confidence": 0.5, "order_type": "limit_maker",
                        "reason": f"RSI {r['rsi14']:.0f} 超卖且触布林下轨，回归做多"}
        elif style == "funding":
            fr = r["funding_rate"]
            rank = r.get("funding_rank")
            # 分位优先（近90期滚动），绝对阈值兜底——主流币费率长期贴 0.01%，绝对阈值不可达
            if rank is not None and rank >= 0.9:
                cand = {"action": "open", "instId": inst_id, "direction": "short",
                        "stop_loss": round(price + 1.2 * atr, 4),
                        "confidence": 0.45 + 0.1 * (rank - 0.9) * 10,
                        "order_type": "limit_maker",
                        "reason": f"资金费率 {fr:+.3%} 处于 {rank:.0%} 分位（多头拥挤），逆向做空"}
            elif rank is not None and rank <= 0.1:
                cand = {"action": "open", "instId": inst_id, "direction": "long",
                        "stop_loss": round(price - 1.2 * atr, 4),
                        "confidence": 0.45 + 0.1 * (0.1 - rank) * 10,
                        "order_type": "limit_maker",
                        "reason": f"资金费率 {fr:+.3%} 处于 {rank:.0%} 分位（空头拥挤），逆向做多"}
            elif abs(fr) >= 0.0005:
                d = "short" if fr > 0 else "long"
                side_px = price + (1.2 * atr if fr > 0 else -1.2 * atr)
                cand = {"action": "open", "instId": inst_id, "direction": d,
                        "stop_loss": round(side_px, 4),
                        "confidence": 0.45, "order_type": "limit_maker",
                        "reason": f"资金费率 {fr:+.3%} 极端（无分位数据），逆向"}

        if cand and (best is None or cand["confidence"] > best["confidence"]):
            best = cand
    return best or {"action": "hold", "reason": "无符合人设信号"}


def _baseline_judge(judge_name, p, snapshot, cfg):
    """基线裁判打分（0-10）。p 为提案，snapshot 用于取该标的因子。"""
    r = (snapshot.get("factors") or {}).get(p.get("instId")) or {}
    score, concerns = 8.0, []
    direction, stop = p.get("direction"), _f(p.get("stop_loss"))
    entry = p.get("entry_hint") or r.get("price") or 0

    if judge_name == "技术裁判":
        if r:
            if direction == "long" and "空头" in r["trend"]:
                score, concerns = 3.5, ["做多逆主趋势"]
            elif direction == "short" and "多头" in r["trend"]:
                score, concerns = 3.5, ["做空逆主趋势"]
    elif judge_name == "风控裁判":
        if entry > 0 and stop > 0:
            dist = abs(entry - stop) / entry
            if dist < cfg.MIN_STOP_DIST_PCT:
                score, concerns = 5.0, [f"止损过近 {dist:.2%}"]
            elif dist > cfg.MAX_STOP_DIST_PCT:
                score, concerns = 2.0, [f"止损过远 {dist:.2%}"]
        else:
            score, concerns = 2.0, ["止损字段异常"]
        if r and abs(r.get("funding_rate", 0)) > 0.001:
            score -= 1.5
            concerns.append("资金费率异常")
        if r and r.get("atr_pct", 0) > 0.03:
            score -= 1.0
            concerns.append("波动极端")
    elif judge_name == "资金管理裁判":
        conf = _f(p.get("confidence"))
        score = 8.0 if conf >= 0.6 else 6.5 if conf >= 0.45 else 5.0
        if r and r.get("vol_ratio", 1) and r["vol_ratio"] > 3:
            score -= 1.0
            concerns.append("异常放量")
    score = max(0.0, min(10.0, score))
    return round(score, 1), "；".join(concerns) or "无异议"


def format_one(report):
    from .factors import format_factor_report
    return format_factor_report(report)
