# -*- coding: utf-8 -*-
"""运行状态持久化（按模式分区）

风控需要跨进程记忆的量（权益高水位、持仓元数据）保存在 data/state/ 下：
    equity_hwm    —— 账户权益历史最高点，回撤熔断的基准
    positions_meta —— 每笔持仓的保护单参数（止损/目标），用于崩溃后对账补挂

⚠ 高水位按模式分文件：纸面模式（state_paper.json）与实盘模拟（state_live.json）
   互不污染 —— 纸面的 10000 U 高水位不能在切换到真实模拟盘后立刻触发回撤熔断。
重置状态直接删掉对应 state_*.json。
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "data", "state")


class StateStore:
    """简单的 JSON 状态存储，读写都是全量替换（状态很小，够用）。

    mode: "paper"（纸面）/ "live"（真实模拟盘账户），决定用哪个状态文件。
    """

    def __init__(self, mode="live", path=None):
        self.path = path or os.path.join(STATE_DIR, f"state_{mode}.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}  # 文件损坏时从头开始，宁可重新计量也不让风控失效

    def save(self, state):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)  # 原子替换，防止写一半崩掉

    # ── 权益高水位 / 回撤 ──────────────────────────────────────────────

    def get_hwm(self):
        return float(self.load().get("equity_hwm") or 0)

    def update_hwm(self, equity):
        """把权益高水位抬升到 max(旧值, equity)，返回 (hwm, drawdown)。
        drawdown = 权益距高水位的回撤比例（0~1）。
        """
        state = self.load()
        hwm = max(float(state.get("equity_hwm") or 0), float(equity))
        state["equity_hwm"] = hwm
        state["hwm_updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save(state)
        dd = (hwm - float(equity)) / hwm if hwm > 0 else 0.0
        return hwm, dd

    # ── 持仓元数据（崩溃对账用）───────────────────────────────────────

    def get_positions_meta(self):
        return self.load().get("positions_meta") or {}

    def set_positions_meta(self, meta):
        state = self.load()
        state["positions_meta"] = meta
        self.save(state)
