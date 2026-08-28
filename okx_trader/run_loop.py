# -*- coding: utf-8 -*-
"""定时交易循环入口

用法（仓库根目录）：
    python okx_trader/run_loop.py              # 按 LOOP_INTERVAL_SEC 间隔持续运行
    python okx_trader/run_loop.py 60 10        # 也可传参：每 60s 一轮，跑 10 轮后停
    Ctrl+C 停止。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loop import TradingLoop  # noqa: E402


def main():
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else None
    max_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else None
    loop = TradingLoop()
    loop.run(interval_sec=interval, max_rounds=max_rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
