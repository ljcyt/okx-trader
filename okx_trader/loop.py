# -*- coding: utf-8 -*-
"""自主交易循环（多 agent 委员会版）

每轮流程（1H K线，默认每小时一轮）：
    1. 快照：账户权益 + 持仓 + 各标的行情与因子报告（factors.py 代码计算）
    2. 委员会决策（committee.py）：3 分析师提案 → 3 裁判打分 → 聚合胜出
    3. 硬风控终审（risk.py 一票否决，AI 不可越过）
    4. 执行：
       - DRY_RUN=True：纸面模拟——记录"将挂什么单/挂什么止损"，不碰交易所
       - DRY_RUN=False：Maker 限价挂单 → 等成交 → 交易所止损（失败则市价平仓兜底）
    5. 全程落盘 rounds 日志

运行模式说明：
    - 没填 OKX Key 时自动进入「纸面模式」：行情/因子真实，账户用 PAPER_EQUITY 虚拟权益，
      只分析不交易 —— 用来先验证决策质量。
    - 填好 Key 且 DRY_RUN=False 才会真实下单到 OKX 模拟盘。

入口：
    python okx_trader/run_once.py      # 单轮（调试/手动触发）
    python okx_trader/run_loop.py      # 定时循环（LOOP_INTERVAL_SEC 间隔）
"""
import importlib.util
import json
import os
import time
import traceback

from client import OKXDemoClient, OKXAPIError, load_config, get_logger
from committee import Committee
from factors import build_factor_report, format_factor_report
from risk import RiskManager
from state import StateStore

HERE = os.path.dirname(os.path.abspath(__file__))
ROUNDS_DIR = os.path.join(HERE, "data", "rounds")

# 订单状态轮询间隔（秒）
FILL_POLL_SEC = 2


class PaperOKXClient(OKXDemoClient):
    """纸面模式客户端：行情/因子走真实公开接口，
    账户状态用本地虚拟数据（不调任何需要鉴权的接口）。"""

    def __init__(self, cfg, logger=None):
        # "-1" 是 python-okx 约定的不签名标记。显式传参而不是改写 cfg——
        # 就地改写会把共享 cfg 对象里的真实密钥抹掉，影响其他客户端
        super().__init__(cfg, logger=logger,
                         api_key="-1", api_secret_key="-1", passphrase="-1")
        self.paper_equity = float(getattr(cfg, "PAPER_EQUITY", 10000.0))

    def get_equity(self):
        return {"total_eq": self.paper_equity, "usdt_eq": self.paper_equity,
                "usdt_avail": self.paper_equity, "raw": {"paper": True}}

    def get_positions(self, inst_id=""):
        return []

    def get_pending_orders(self, inst_id=""):
        return []  # 纸面从不持有真实挂单（风控 R4 查重用）

    def set_leverage(self, inst_id, lever):
        self.log.info("（纸面）set_leverage %s %sx", inst_id, lever)

    def get_account_mode(self, refresh=False):
        return {"uid": "paper", "acctLv": "5", "posMode": "net_mode"}


class TradingLoop:
    def __init__(self, cfg=None, logger=None, force_dry_run=False):
        # 凭证缺失 + 允许纸面时，降级为纸面模式而不是报错退出
        try:
            self.cfg = cfg or load_config()
            self.creds_ok = True
        except (FileNotFoundError, RuntimeError) as e:
            if force_dry_run:
                raise
            self.cfg = _load_raw_config()
            self.creds_ok = False
            self.cfg.DRY_RUN = True
            get_logger().warning("未配置 API Key，进入纸面模式（原因：%s）", e)

        self.log = logger or get_logger(level=getattr(self.cfg, "LOG_LEVEL", "INFO"))
        self.dry_run = bool(getattr(self.cfg, "DRY_RUN", True)) or not self.creds_ok

        client_cls = OKXDemoClient if self.creds_ok else PaperOKXClient
        self.client = client_cls(self.cfg, logger=self.log)
        if not self.creds_ok:
            self.log.warning("== 纸面模式 ==：真实行情 + 虚拟账户（%.0f U），不会真实下单",
                             self.cfg.PAPER_EQUITY)
        elif self.dry_run:
            self.log.warning("== DRY_RUN ==：使用真实模拟盘账户，但只分析不下单")

        self.state = StateStore(mode="paper" if not self.creds_ok else "live")
        self.risk = RiskManager(self.cfg, self.client, self.state)
        self.committee = Committee(self.cfg, self.client)
        self.round_seq = 0

    # ────────────────────────── 快照 ──────────────────────────

    def take_snapshot(self):
        """账户 + 持仓 + 各标的因子报告。单标因子失败不影响其他标的。"""
        equity_info = self.client.get_equity()
        equity = equity_info["total_eq"]
        hwm, drawdown = self.risk.update_equity_hwm(equity)
        positions = self.client.get_positions()

        factors = {}
        factor_errors = {}
        for inst_id in self.cfg.SYMBOLS:
            try:
                factors[inst_id] = build_factor_report(self.cfg, self.client, inst_id)
            except Exception as e:  # noqa: BLE001
                # 单标的失败记入 factor_errors——"没数据"绝不能伪装成"没信号"
                self.log.warning("%s 因子计算失败：%s", inst_id, e)
                factors[inst_id] = None
                factor_errors[inst_id] = f"{type(e).__name__}: {e}"

        symbols_total = len(self.cfg.SYMBOLS)
        symbols_ok = sum(1 for f in factors.values() if f is not None)
        snapshot = {
            "ts": time.time(),
            "equity": equity,
            "usdt_avail": equity_info["usdt_avail"],
            "hwm": hwm,
            "drawdown": drawdown,
            "positions": positions,
            "factors": factors,
            "factor_errors": factor_errors,
            "data_ok": symbols_ok > 0,
            "symbols_ok": symbols_ok,
            "symbols_total": symbols_total,
        }
        self.log.info("快照：权益 %.2f U（%s），高水位 %.2f，回撤 %.2f%%，持仓 %d 个，因子 %d/%d",
                      equity, "真实" if self.creds_ok else "纸面",
                      hwm, drawdown * 100, len(positions), symbols_ok, symbols_total)
        for inst_id, f in factors.items():
            if f:
                self.log.info("因子：%s", format_factor_report(f).replace("\n", " | "))
        return snapshot

    # ────────────────────────── 持仓巡检 / 崩溃对账 ──────────────────────────

    def _patrol_positions(self, snap):
        """每轮开跑前对账（只有连接真实模拟盘账户时才动账）：
        1. 撤掉残留的普通挂单（上轮崩溃留下的孤儿 Maker 单）
        2. 清理已平仓位的元数据（记录离场）
        3. 给没有任何交易所保护单的持仓补挂止损 —— 保证"有仓必有止损"在崩溃后仍成立
        """
        meta = self.state.get_positions_meta()
        live = {p["instId"]: p for p in snap["positions"]}

        # 已离场的仓位：清理元数据
        changed = False
        for inst_id in list(meta.keys()):
            if inst_id not in live:
                self.log.info("巡检：%s 仓位已离场，清理元数据（止盈/止损/超时离场以交易所记录为准）",
                              inst_id)
                meta.pop(inst_id)
                changed = True
        if changed:
            self.state.set_positions_meta(meta)

        if not (self.creds_ok and not self.dry_run):
            return  # 纸面/DRY_RUN 不碰真实账户

        # 残留挂单：本策略的挂单要么成交要么被超时撤单，开局还挂着的都是崩溃残留
        try:
            for o in self.client.get_pending_orders():
                if o["instId"] in self.cfg.SYMBOLS:
                    try:
                        self.client.cancel_order(o["instId"], o["ordId"])
                        self.log.warning("巡检：撤销残留挂单 %s（%s）", o["ordId"][:8], o["instId"])
                    except OKXAPIError as e:
                        self.log.warning("巡检：撤单失败 %s：%s", o["ordId"][:8], e)
        except OKXAPIError as e:
            self.log.warning("巡检：读取挂单失败：%s", e)

        # 止损缺失检测：交易所列表（conditional+oco 合并）为空时，先用元数据里的
        # algo_id 对账（防瞬时查询异常误判裸仓），确认没了才补挂；
        # 补挂时透传 meta 里的 target，避免把 OCO 静默降级成纯止损
        for inst_id, p in live.items():
            try:
                existing = self.client.get_pending_stop_losses(inst_id)
            except OKXAPIError as e:
                self.log.error("巡检：读取 %s 保护单失败（不据此判定裸仓）：%s", inst_id, e)
                continue
            if existing:
                continue
            m = meta.get(inst_id) or {}
            recorded_algo = m.get("algo_id")
            if recorded_algo:
                try:
                    d = self.client.get_algo_order_details(recorded_algo)
                    if d and str(d.get("state")) in ("live", "effective", "running", "pause"):
                        self.log.info("巡检：%s 列表为空但 algoId=%s 状态=%s 仍有效，不重复补挂",
                                      inst_id, recorded_algo, d.get("state"))
                        continue
                except OKXAPIError as e:
                    self.log.warning("巡检：%s 对账 algoId=%s 失败（%s），按缺失处理",
                                     inst_id, recorded_algo, e)
            stop = m.get("stop")
            if not stop:
                atr = ((snap.get("factors") or {}).get(inst_id) or {}).get("atr")
                if not atr:
                    self.log.error("巡检：%s 止损缺失且无法估算（无元数据/无ATR），请人工检查！", inst_id)
                    continue
                stop = (p["avg_px"] - 1.5 * atr if p["direction"] == "long"
                        else p["avg_px"] + 1.5 * atr)
            tp = m.get("target")
            try:
                algo_id = self.client.place_stop_loss(inst_id, p["direction"],
                                                      p["contracts"], stop, tp_px=tp)
                meta[inst_id] = {**m, "algo_id": algo_id, "stop": stop, "target": tp}
                changed = True
                self.log.warning("巡检：%s 保护单缺失，已补挂 %s 止损@%.4g%s",
                                 inst_id, p["direction"], stop,
                                 f" 止盈@{tp:.4g}" if tp else "")
            except OKXAPIError as e:
                self.log.error("巡检：%s 补挂止损失败：%s —— 该仓位当前无保护，请人工处理！",
                               inst_id, e)
        if changed:
            self.state.set_positions_meta(meta)

    # ────────────────────────── 单轮 ──────────────────────────

    def run_round(self):
        """跑一轮完整闭环，返回并落盘 round 记录。任何异常都记录后吞掉，循环不中断。"""
        t0 = time.time()
        self.round_seq += 1
        round_id = time.strftime("%Y%m%d_%H%M%S") + f"_{self.round_seq:03d}"
        record = {"round_id": round_id, "ts": t0,
                  "mode": "paper" if not self.creds_ok else ("dry_run" if self.dry_run else "live")}

        try:
            # 1. 快照（含因子报告）
            snap = self.take_snapshot()
            # 1.5 持仓巡检与对账（崩溃恢复：撤残留挂单、补挂丢失的止损）
            self._patrol_positions(snap)
            record["account"] = {
                "equity": snap["equity"], "hwm": snap["hwm"],
                "drawdown": snap["drawdown"], "usdt_avail": snap["usdt_avail"],
            }
            record["positions"] = snap["positions"]
            record["factors"] = {
                k: ({kk: f[kk] for kk in ("price", "trend", "rsi14", "atr", "atr_pct",
                                          "funding_rate", "pattern", "vol_ratio",
                                          "price_vs_boll")}
                    if f else None)
                for k, f in snap["factors"].items()
            }
            record["data_ok"] = snap["data_ok"]
            record["symbols_ok"] = snap["symbols_ok"]
            record["symbols_total"] = snap["symbols_total"]

            # 数据降级短路：全部标的因子失败 → 不跑委员会。
            # 跑了只会制造"所有分析师都弃权"的误导性记录——没数据 ≠ 没信号。
            if not snap["data_ok"]:
                record["status"] = "data_unavailable"
                record["decision_reason"] = "全部标的因子获取失败，本轮不跑委员会"
                record["factor_errors"] = snap["factor_errors"]
                self.log.error("数据降级：%s —— 本轮记为 data_unavailable，不跑委员会",
                               snap["factor_errors"])
                record["duration_sec"] = round(time.time() - t0, 2)
                return self._save_round(record)

            # 2. 委员会决策
            decision = self.committee.decide(snap)
            record["committee"] = decision
            plan = decision.get("plan") if decision.get("action") == "open" else None
            if plan is None:
                record["status"] = "no_action"
                record["decision_reason"] = decision.get("reason")
                record["duration_sec"] = round(time.time() - t0, 2)
                return self._save_round(record)

            # 3. 硬风控终审
            verdict = self.risk.check_open_plan(plan)
            record["risk"] = {
                "passed": verdict.passed,
                "failures": verdict.failures,
                "warnings": verdict.warnings,
                "sized": verdict.sized,
            }
            if not verdict.passed:
                record["status"] = "risk_rejected"
                record["duration_sec"] = round(time.time() - t0, 2)
                return self._save_round(record)

            # 4. 执行（纸面 / 真实）
            if self.dry_run:
                record["execution"] = self._dry_run_execute(verdict.sized)
                record["status"] = "dry_run_planned"
            else:
                record["execution"] = self._execute_open(verdict.sized)
                record["status"] = record["execution"]["status"]
        except Exception as e:  # noqa: BLE001 —— 单轮失败不影响下一轮
            self.log.error("本轮异常：%s\n%s", e, traceback.format_exc())
            record["status"] = "error"
            record["error"] = f"{type(e).__name__}: {e}"

        record["duration_sec"] = round(time.time() - t0, 2)
        return self._save_round(record)

    # ────────────────────────── 执行 ──────────────────────────

    def _dry_run_execute(self, sized):
        """纸面执行：计算并记录将要挂的单，不触碰交易所下单接口。"""
        inst_id, direction = sized["instId"], sized["direction"]
        side = "buy" if direction == "long" else "sell"
        try:
            px = self.client.maker_price(inst_id, side)
        except OKXAPIError as e:
            px = None
            self.log.warning("纸面计算委托价失败：%s", e)
        plan = {
            "instId": inst_id, "direction": direction,
            "contracts": sized["contracts"],
            "maker_px": px,
            "stop_loss": sized["stop_loss"],
            "risk_usdt": sized["risk_usdt"], "risk_pct": sized["risk_pct"],
            "notional_usdt": sized["notional_usdt"],
        }
        self.log.info(
            "【DRY-RUN】将挂 Maker %s 单：%s %s 张 @ %s；成交后将挂交易所止损 @ %s"
            "（名义 %.0f U，单笔风险 %.2f%%）",
            side, inst_id, plan["contracts"], px, plan["stop_loss"],
            plan["notional_usdt"], plan["risk_pct"] * 100)
        return {"status": "simulated", "would": plan}

    def _execute_open(self, sized):
        """真实执行：限价挂单 → 等成交 → 挂止损。"""
        inst_id = sized["instId"]
        direction = sized["direction"]
        contracts = sized["contracts"]
        stop = sized["stop_loss"]
        side = "buy" if direction == "long" else "sell"

        # 开仓前设置杠杆（幂等）
        try:
            self.client.set_leverage(inst_id, self.cfg.LEVERAGE)
        except OKXAPIError as e:
            self.log.warning("设置杠杆失败（继续，可能已是目标杠杆）：%s", e)

        # Maker 限价挂单（post_only，价格由 client 按盘口偏移自动定）
        ord_id = self.client.place_maker_limit(inst_id, side, contracts)
        if not ord_id:
            # post_only 被交易所拒（挂单瞬间会吃单）—— 竞争失败，按未成交处理
            self.log.info("post_only 挂单被拒，本轮按未成交处理")
            return {"status": "no_fill", "note": "post_only rejected",
                    "contracts_planned": contracts, "stop_px": stop,
                    "direction": direction}
        self.log.info("已挂 Maker 限价单 %s %s %s 张 @订单 %s", inst_id, side, contracts, ord_id)
        execution = {"ord_id": ord_id, "contracts_planned": contracts,
                     "stop_px": stop, "direction": direction}

        # 等待成交
        deadline = time.time() + self.cfg.ORDER_TIMEOUT_SEC
        order = None
        while time.time() < deadline:
            time.sleep(FILL_POLL_SEC)
            order = self.client.get_order(inst_id, ord_id)
            if order["state"] in ("filled", "canceled"):
                break
            if order["state"] == "partially_filled" and order["acc_fill_sz"] >= contracts:
                break

        if order is None:
            order = self.client.get_order(inst_id, ord_id)

        execution["fill_state"] = order["state"]
        execution["filled_contracts"] = order["acc_fill_sz"]
        execution["avg_fill_px"] = order["avg_px"]

        filled = order["acc_fill_sz"]
        if filled <= 0:
            # 完全未成交 → 撤单走人（下轮再说）
            try:
                self.client.cancel_order(inst_id, ord_id)
                execution["canceled"] = True
            except OKXAPIError as e:
                # 可能刚好成交了导致撤单失败，查一次状态
                order = self.client.get_order(inst_id, ord_id)
                filled = order["acc_fill_sz"]
                execution["fill_state"] = order["state"]
                execution["filled_contracts"] = filled
                execution["cancel_error"] = str(e)
                if filled <= 0:
                    execution["status"] = "no_fill"
                    return execution
        else:
            # 有成交 → 撤掉剩余部分（部分成交时）
            if order["state"] in ("live", "partially_filled"):
                try:
                    self.client.cancel_order(inst_id, ord_id)
                except OKXAPIError:
                    pass  # 可能已全部成交

        if filled <= 0:
            execution["status"] = "no_fill"
            self.log.info("限价单未成交，已撤单")
            return execution

        # 成交了 → 立刻挂交易所保护单（有结构位目标时挂 OCO 止盈+止损，否则纯止损）
        tp = sized.get("target")
        execution["tp_px"] = tp
        execution["status"] = self._attach_stop_loss(
            inst_id, direction, filled, stop, execution, tp_px=tp)
        if execution["status"] == "opened":
            meta = self.state.get_positions_meta()
            meta[inst_id] = {
                "direction": direction, "stop": stop, "target": tp,
                "contracts": filled, "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "algo_id": execution.get("stop_algo_id"),
            }
            self.state.set_positions_meta(meta)
        return execution

    def _attach_stop_loss(self, inst_id, direction, contracts, stop_px, execution,
                          tp_px=None):
        """挂交易所保护单（止损 [+ 止盈]）；失败则市价平仓兜底。返回状态字符串。"""
        try:
            algo_id = self.client.place_stop_loss(inst_id, direction, contracts,
                                                  stop_px, tp_px=tp_px)
            execution["stop_algo_id"] = algo_id
            self.log.info("交易所保护单已挂：%s %s %s 张 止损@%s%s（algoId=%s）",
                          inst_id, direction, contracts, stop_px,
                          f" 止盈@{tp_px:.4g}" if tp_px else "", algo_id)
            return "opened"
        except OKXAPIError as e:
            # 绝不允许无止损仓位：平仓兜底
            self.log.error("挂止损失败（%s），市价平仓兜底！", e)
            try:
                self.client.close_position_market(inst_id, direction)
                execution["emergency_closed"] = True
            except OKXAPIError as e2:
                self.log.critical("兜底平仓也失败：%s —— 请人工处理！", e2)
                execution["emergency_close_failed"] = True
            return "stop_failed_closed"

    # ────────────────────────── rounds 日志 ──────────────────────────

    def _save_round(self, record):
        """每轮决策落盘：汇总 JSONL 为主存储（加简单轮转）；
        有实际动作/异常的轮次额外写单文件 JSON，方便人工查看。"""
        os.makedirs(ROUNDS_DIR, exist_ok=True)

        jsonl_path = os.path.join(ROUNDS_DIR, "rounds.jsonl")
        try:
            if os.path.getsize(jsonl_path) > 5 * 1024 * 1024:  # 5MB 轮转，保留一代
                os.replace(jsonl_path, jsonl_path + ".1")
        except OSError:
            pass
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        single_path = None
        if record.get("status") not in ("no_action",):
            single_path = os.path.join(ROUNDS_DIR, f"round_{record['round_id']}.json")
            with open(single_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2, default=str)

        self.log.info("round %s：%s（%.1fs）已记录",
                      record["round_id"], record.get("status"),
                      record.get("duration_sec", 0))
        record["log_path"] = single_path or jsonl_path
        return record

    # ────────────────────────── 循环 ──────────────────────────

    def run(self, interval_sec=None, max_rounds=None):
        """定时循环。max_rounds 用于调试限次；None 表示一直跑。"""
        interval = interval_sec or getattr(self.cfg, "LOOP_INTERVAL_SEC", 3600)
        self.log.info("交易循环启动：每 %ds 一轮%s，模式=%s/%s",
                      interval,
                      f"，共 {max_rounds} 轮" if max_rounds else "，持续运行",
                      "纸面" if not self.creds_ok else ("DRY_RUN" if self.dry_run else "实盘模拟"),
                      self.decision_mode)
        n = 0
        try:
            while max_rounds is None or n < max_rounds:
                n += 1
                self.run_round()
                if max_rounds is not None and n >= max_rounds:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            self.log.info("收到中断，交易循环停止")


def _load_raw_config():
    """跳过凭证检查加载 okx_config.py（纸面模式用）。"""
    path = os.path.join(HERE, "okx_config.py")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"找不到配置文件 {path}；请先 cp okx_trader/okx_config_template.py okx_trader/okx_config.py")
    spec = importlib.util.spec_from_file_location("okx_config", path)
    cfg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg)
    return cfg
