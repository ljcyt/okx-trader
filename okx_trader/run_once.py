# -*- coding: utf-8 -*-
"""单轮交易入口（调试/手动触发用）

用法（仓库根目录）：
    python okx_trader/run_once.py

跑一轮完整闭环：快照+因子 → 委员会(3分析师+3裁判) → 硬风控 → （纸面/真实）执行 → 落盘记录。
（single 单Planner模式已删除，决策一律走委员会）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loop import TradingLoop  # noqa: E402


def main():
    loop = TradingLoop()
    record = loop.run_round()
    status = record.get("status")

    print("\n" + "=" * 60)
    print(f"本轮结果：{status}（模式：{record.get('mode')}）")

    if record.get("committee"):
        c = record["committee"]
        print(f"委员会：{c.get('reason')}")
        for row in c.get("scoreboard", []):
            mark = "✓" if row["qualify"] else "✗"
            print(f"  {mark} {row['analyst']} → {row['instId']} {row['direction']} "
                  f"均分 {row['avg_score']}（{row['votes']} 通过）")
        for a in c.get("analysts", []):
            if a.get("action") != "open":
                print(f"  - {a['analyst']} 弃权：{a.get('reason')}")

    if status == "risk_rejected":
        for f in record.get("risk", {}).get("failures", []):
            print(f"  风控拒绝：{f}")
    elif status == "dry_run_planned":
        w = record.get("execution", {}).get("would", {})
        print(f"  【纸面】将挂 {w.get('direction')} 限价单 {w.get('contracts')} 张 @ {w.get('maker_px')}，"
              f"止损 {w.get('stop_loss')}，单笔风险 {w.get('risk_pct'):.2%}")
    elif status == "opened":
        ex = record.get("execution", {})
        print(f"  订单 {ex.get('ord_id')} 成交 {ex.get('filled_contracts')} 张 @"
              f"{ex.get('avg_fill_px')}，止损单 {ex.get('stop_algo_id')}（触发价 {ex.get('stop_px')}）")

    print(f"记录文件：{record.get('log_path', 'okx_trader/data/rounds/rounds.jsonl')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
