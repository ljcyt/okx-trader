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


def _acquire_loop_lock(port=8777):
    """绑 127.0.0.1:8777 做单例锁：第二个循环进程直接退出。

    两个循环同时操作一个账户是真实的资金 bug（重复开仓/重复挂止损）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(0)
        return s  # 进程退出时自动释放
    except OSError:
        print(f"已有一个交易循环在运行（单例锁 127.0.0.1:{port}），拒绝启动第二个。",
              file=sys.stderr)
        sys.exit(2)


def cmd_run_loop(args):
    import threading

    _acquire_loop_lock()

    from .config import get_logger
    from .loop import TradingLoop
    from .web import server as web_server

    cfg = _build_cfg(args)
    executing = False if args.no_execute else None
    loop = TradingLoop(cfg=cfg, logger=get_logger(level=cfg.LOG_LEVEL),
                       executing=executing)
    if args.serve:
        web_server.check_bind_guard(cfg.WEB_HOST, cfg.WEB_PASSWORD)
        app = web_server.create_app(cfg, loop.store, loop=loop)
        t = threading.Thread(target=web_server.serve,
                             args=(app, cfg.WEB_HOST, cfg.WEB_PORT),
                             daemon=True, name="okxt-web")
        t.start()
        loop.log.info("Web 面板：http://%s:%d（局域网明文 HTTP，见 docs/dashboard.md）",
                      cfg.WEB_HOST, cfg.WEB_PORT)
    loop.run(interval_sec=args.interval, max_rounds=args.max_rounds)
    return 0


def cmd_serve(args):
    """只读 serve：面板对 DB 只读；控制端点 409。"""
    import threading

    from .config import get_logger, load_config
    from .store.db import Store
    from .web import server as web_server

    cfg = _build_cfg(args)
    web_server.check_bind_guard(cfg.WEB_HOST, cfg.WEB_PASSWORD)
    store = Store(web_server_store_path(cfg))
    app = web_server.create_app(cfg, store, loop=None)
    get_logger(level=cfg.LOG_LEVEL).info(
        "Web 面板（只读）：http://%s:%d", cfg.WEB_HOST, cfg.WEB_PORT)
    t = threading.Thread(target=web_server.serve,
                         args=(app, cfg.WEB_HOST, cfg.WEB_PORT),
                         daemon=True)
    t.start()
    try:
        threading.Event().wait()  # 常驻
    except KeyboardInterrupt:
        return 0
    return 0


def web_server_store_path(cfg):
    from .env import db_path
    return db_path()


def cmd_migrate(args):
    from .store.migrate_json import migrate
    result = migrate(dry_run=args.dry_run)
    print(f"迁移完成：记录 {result['records']} 条，导入 {result['imported']} 条 → {result['db']}")
    return 0


def cmd_backfill(args):
    from .config import load_config
    from .env import make_client, resolve_env
    from .store.db import Store
    from .store.factors_zoo import backfill_returns
    from .env import db_path
    cfg = _build_cfg(args)
    store = Store(db_path())
    client = make_client(resolve_env(cfg), cfg)
    n = backfill_returns(store, client, bar=cfg.ATR_BAR)
    print(f"回填完成：{n} 个前向收益")
    return 0


def cmd_score_factors(args):
    import json as _json
    from .config import load_config
    from .store.db import Store
    from .store.factors_zoo import score_factors
    from .env import db_path
    cfg = _build_cfg(args)
    store = Store(db_path())
    rows = score_factors(store, getattr(cfg, "FACTOR_GATE", {}), bar=cfg.ATR_BAR)
    for r in rows:
        print(f"{r['factor']:>14} {r['horizon']:>4} n={r['n_obs']:>5} "
              f"ic={_fmtv(r['ic'])} rank_ic={_fmtv(r['rank_ic'])} "
              f"days={r['days_tracked']} gate={'PASS' if r['gate_passed'] else '—'}")
    return 0


def _fmtv(v):
    return f"{v:+.4f}" if v is not None else "  —   "


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

    p = sub.add_parser("backfill-returns", help="因子观测的前向收益回填")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("score-factors", help="因子 IC 打分与晋级判定")
    p.set_defaults(fn=cmd_score_factors)

    p = sub.add_parser("check-env", help="环境自检（联网）")
    p.set_defaults(fn=cmd_check_env)

    p = sub.add_parser("serve", help="只读 Web 面板（循环跑在别处时用）")
    p.add_argument("--env", choices=["replay", "paper", "demo"])
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("replay", help="脚本化回放（全离线）")
    p.add_argument("--env", choices=["replay"], default="replay")
    p.add_argument("--no-execute", action="store_true")
    p.set_defaults(fn=cmd_replay)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
