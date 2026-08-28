# -*- coding: utf-8 -*-
"""CLI：python -m okx_trader <cmd> ｜ okxt <cmd>

    run-once    跑一轮完整闭环（--env replay|paper|demo，--no-execute 只分析）
    run-loop    定时循环（--serve 在同进程起面板，Phase 4）
    migrate     旧 JSON 记录 → SQLite（幂等）
    check-env   环境自检（唯一碰网络的命令，永不进 CI）
    replay      跑一段脚本化回放（全离线，验证链路）

环境选择：--env 覆盖配置里的 TRADING_ENV；--no-execute 只把 executing 压成 False。
"""
import argparse
import sys


def _build_cfg(args):
    from .config import load_config
    cfg = load_config()
    if getattr(args, "env", None):
        cfg.TRADING_ENV = args.env
    return cfg


def cmd_run_once(args):
    from .config import get_logger
    from .env import make_client, resolve_env
    from .loop import TradingLoop
    cfg = _build_cfg(args)
    env = resolve_env(cfg)
    executing = False if args.no_execute else env.executing
    loop = TradingLoop(cfg=cfg, logger=get_logger(level=cfg.LOG_LEVEL),
                       executing=executing)
    result = loop.run_round()
    status = result.get("status")
    print("\n" + "=" * 60)
    print(f"本轮结果：{status}（env={env.name}，executing={loop.executing}）")
    if result.get("decision"):
        print(f"委员会：{result['decision']}")
    if status == "risk_rejected":
        for f in result.get("failures", []):
            print(f"  风控拒绝：{f}")
    elif status == "planned":
        wd = result.get("execution", {}).get("would", {})
        print(f"  【planned】{wd.get('direction')} {wd.get('contracts')} 张 "
              f"@ {wd.get('maker_px')}，止损 {wd.get('stop_loss')}，"
              f"单笔风险 {wd.get('risk_pct', 0):.2%}")
    elif status == "opened":
        ex = result.get("execution", {})
        print(f"  订单 {ex.get('ord_id')} 成交 {ex.get('filled_contracts')} 张，"
              f"保护单 {ex.get('stop_algo_id')}")
    return 0


def cmd_run_loop(args):
    from .config import get_logger
    from .loop import TradingLoop
    cfg = _build_cfg(args)
    executing = False if args.no_execute else None
    loop = TradingLoop(cfg=cfg, logger=get_logger(level=cfg.LOG_LEVEL),
                       executing=executing)
    if args.serve:
        print("（--serve 将在 Phase 4 提供；当前仅运行交易循环）", file=sys.stderr)
    loop.run(interval_sec=args.interval, max_rounds=args.max_rounds)
    return 0


def cmd_migrate(args):
    from .store.migrate_json import migrate
    result = migrate(dry_run=args.dry_run)
    print(f"迁移完成：记录 {result['records']} 条，导入 {result['imported']} 条 → {result['db']}")
    return 0


def cmd_check_env(args):
    from .check import run_checks
    return run_checks()


def cmd_replay(args):
    """脚本化回放：一段先涨后跌的行情，验证 提案→风控→挂单→成交→保护单 全链路。"""
    from .config import get_logger
    from .loop import TradingLoop

    cfg = _build_cfg(args)
    cfg.TRADING_ENV = "replay"
    cfg.ORDER_TIMEOUT_SEC = 0  # 回放中成交即刻判定，零等待
    loop = TradingLoop(cfg=cfg, logger=get_logger(level="INFO"), executing=False)
    # 行情与脚本：80 根合成K线 + 3 轮（挂单成交 → 持仓巡检 → 触发止损）
    step = {"price": 78000.0, "fill": True}
    loop.client.script = [step,
                          {"price": 78200.0, "fill": False},
                          {"price": 77400.0, "sl_hit": True}]
    result = loop.run_round()
    print(f"\n第 1 轮：{result.get('status')}（env=replay）")
    print(f"决策：{result.get('decision')}")
    if result.get("status") == "risk_rejected":
        for f in result.get("failures", []):
            print(f"  风控拒绝：{f}")
    else:
        print("再推进两轮（行情脚本化）……")
        loop.client.advance()
        r2 = loop.run_round()
        print(f"第 2 轮：{r2.get('status')}")
        loop.client.advance()
        r3 = loop.run_round()
        print(f"第 3 轮：{r3.get('status')}")
        print("回放中的平仓记录：", loop.client.closed_trades)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="okxt", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run-once", help="跑一轮完整闭环")
    p.add_argument("--env", choices=["replay", "paper", "demo"])
    p.add_argument("--no-execute", action="store_true", help="只分析不下单")
    p.set_defaults(fn=cmd_run_once)

    p = sub.add_parser("run-loop", help="定时循环")
    p.add_argument("--env", choices=["replay", "paper", "demo"])
    p.add_argument("--no-execute", action="store_true")
    p.add_argument("--interval", type=int, default=None)
    p.add_argument("--max-rounds", type=int, default=None)
    p.add_argument("--serve", action="store_true", help="同进程起 Web 面板")
    p.set_defaults(fn=cmd_run_loop)

    p = sub.add_parser("migrate", help="旧 JSON 记录 → SQLite")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("check-env", help="环境自检（联网）")
    p.set_defaults(fn=cmd_check_env)

    p = sub.add_parser("replay", help="脚本化回放（全离线）")
    p.add_argument("--env", choices=["replay"], default="replay")
    p.add_argument("--no-execute", action="store_true")
    p.set_defaults(fn=cmd_replay)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
