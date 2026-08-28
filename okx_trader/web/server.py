# -*- coding: utf-8 -*-
"""Web 层：bottle 单页面板 API + 静态资源。

进程模型（run-loop --serve）：交易循环在主线程，bottle 在守护线程——
只有一个 SQLite 写者；serve 单独跑时对 DB 只读，控制端点返回 409。

安全：
    - 非回环绑定必须设 WEB_PASSWORD，否则拒绝启动（让"已暴露且无认证"不可表示）
    - 永不 bottle.run(debug=...)，traceback 不到局域网客户端
    - 刻意不提供任何下单/撤单/切环境端点：cookie 被偷的影响半径是"能看、能暂停"
"""
import json
import os
import threading
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler
from socketserver import ThreadingMixIn

import bottle

import okx_trader as _pkg
from . import auth
from ..store import read as r
from ..store import write as w

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

# 手写白名单——绝不是 vars(cfg)，那会把 OKX_SECRET_KEY 发到浏览器
_LIMIT_KEYS = ("SYMBOLS", "MAX_RISK_PER_TRADE", "MAX_TOTAL_LEVERAGE",
               "MAX_OPEN_POSITIONS", "MAX_DRAWDOWN", "MIN_RR", "MIN_TARGET_ATR",
               "TARGET_ATR_MULT", "ATR_BAR", "ATR_STOP_MULT", "LEVERAGE",
               "TRAIL_ATR_MULT", "MAX_HOLD_BARS", "SCORE_THRESHOLD",
               "LOOP_INTERVAL_SEC", "RISK_TICK_SEC", "DRAWDOWN_LADDER",
               "FACTOR_GATE")


def _ok(**kw):
    bottle.response.content_type = "application/json; charset=utf-8"
    return json.dumps({"ok": True, **kw}, ensure_ascii=False, default=str)


def _err(msg, code=400):
    bottle.response.content_type = "application/json; charset=utf-8"
    bottle.response.status = code
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def _query_args(*names):
    out = {}
    for n in names:
        v = bottle.request.query.get(n)
        if v not in (None, ""):
            out[n] = v
    return out


def create_app(cfg, store, loop=None):
    """loop=None 时为只读 serve 模式：/api/loop/* 返回 409。"""
    app = bottle.Bottle()
    app.config["okxt.cfg"] = cfg
    app.config["okxt.store"] = store
    app.config["okxt.loop"] = loop

    # ── 静态与登录（免认证）────────────────────────────────────────

    @app.route("/")
    def index():
        return bottle.static_file("index.html", root=STATIC)

    @app.route("/static/<path:path>")
    def static(path):
        return bottle.static_file(path, root=STATIC)

    @app.post("/api/login")
    def login():
        try:
            password = (bottle.request.json or {}).get("password", "")
        except Exception:  # noqa: BLE001
            return _err("bad request", 400)
        ip = bottle.request.remote_addr or "?"
        try:
            token = auth.login(password, cfg, ip)
        except PermissionError:
            w.write_event(store, getattr(loop, "env", None) and loop.env.name or "paper",
                          "login", "登录被限流", level="warn")
            return _err("尝试过多，请 60 秒后再试", 429)
        if not token:
            w.write_event(store, getattr(loop, "env", None) and loop.env.name or "paper",
                          "login", "登录失败", level="warn")
            return _err("密码错误", 401)
        w.write_event(store, getattr(loop, "env", None) and loop.env.name or "paper",
                      "login", "登录成功")
        bottle.response.set_cookie(
            auth.COOKIE_NAME, token, path="/", httponly=True, samesite="Lax",
            max_age=auth.TTL_SEC, secure=bool(getattr(cfg, "WEB_TLS", False)))
        return _ok()

    @app.post("/api/logout")
    def logout():
        token = bottle.request.get_cookie(auth.COOKIE_NAME)
        if token:
            auth.logout(token)
        bottle.response.delete_cookie(auth.COOKIE_NAME, path="/")
        return _ok()

    @app.get("/api/me")
    def me():
        token = bottle.request.get_cookie(auth.COOKIE_NAME)
        authed = bool(token and auth.validate(token))
        env_name = loop.env.name if loop else cfg.TRADING_ENV
        return _ok(authed=authed, env=env_name,
                   executing=bool(loop.executing) if loop else False,
                   version=_pkg.__version__)

    # ── 认证闸门：白名单之外全部 401 ────────────────────────────────

    @app.hook("before_request")
    def _auth_gate():
        path = bottle.request.path
        if path == "/" or path.startswith("/static/") or path == "/api/login":
            return
        token = bottle.request.get_cookie(auth.COOKIE_NAME)
        if not (token and auth.validate(token)):
            # 钩子的返回值会被 bottle 忽略——必须抛 HTTPError 才拦得住
            raise bottle.HTTPError(
                status=401,
                body=json.dumps({"ok": False, "error": "unauthenticated"}),
                headers={"Content-Type": "application/json; charset=utf-8"})

    # ── HTTPError 一律以 JSON 信封输出（bottle 默认是 HTML 模板）──────

    @app.error(400)
    @app.error(401)
    @app.error(404)
    @app.error(409)
    @app.error(429)
    @app.error(500)
    def _json_error(err):
        bottle.response.content_type = "application/json; charset=utf-8"
        try:
            payload = json.loads(err.body) if err.body else None
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps({"ok": False, "error": err.body or err.status_line},
                          ensure_ascii=False)

    # ── 状态与数据 ─────────────────────────────────────────────────

    @app.get("/api/state")
    def state():
        last = (loop.last_snapshot if loop else None) or _last_equity_row(store)
        drawdown = (last or {}).get("drawdown") or 0
        tripped = bool(drawdown > (cfg.MAX_DRAWDOWN or 1))
        positions = (last or {}).get("positions") or []
        pending = store.query(
            "SELECT * FROM orders WHERE state='live' AND kind='entry' "
            "ORDER BY created_ts DESC LIMIT 20")
        out = {
            "env": loop.env.name if loop else cfg.TRADING_ENV,
            "executing": bool(loop.executing) if loop else False,
            "dd_rung": {"level": loop.risk.state.get_rung() if loop else 0,
                        "ladder": getattr(cfg, "DRAWDOWN_LADDER", [])},
            "risk_ticks": int(store.state_get(
                cfg.TRADING_ENV, "risk_ticks") or 0),
            "last_risk_tick_ts": store.state_get(cfg.TRADING_ENV,
                                                 "last_risk_tick_ts"),
            "loop": {
                "attached": loop is not None,
                "running": bool(loop and loop.last_snapshot),
                "paused": bool(loop.paused) if loop else False,
                "step": getattr(loop, "current_step", None) if loop else None,
                "rounds_done": loop.round_seq if loop else 0,
                "next_round_ts": getattr(loop, "next_round_ts", None),
                "last_round_id": getattr(loop, "last_round_id", None),
            },
            "account": {
                "equity": (last or {}).get("equity"),
                "hwm": (last or {}).get("hwm"),
                "drawdown": drawdown,
                "usdt_avail": (last or {}).get("usdt_avail"),
            },
            "positions": positions,
            "pending_orders": [dict(o) for o in pending],
            "circuit_breaker": {"tripped": tripped,
                                "reason": f"回撤 {drawdown:.1%} ≥ {cfg.MAX_DRAWDOWN:.0%}"
                                if tripped else None},
            "data_health": {"data_ok": (last or {}).get("data_ok", 1),
                            "symbols_ok": (last or {}).get("symbols_ok", 0),
                            "symbols_total": (last or {}).get("symbols_total", 0)},
            "limits": {k: getattr(cfg, k, None) for k in _LIMIT_KEYS},
        }
        if loop and getattr(cfg, "DECISION_IGNORED", None) is None:
            out["limits"]["llm_mode"] = ("llm" if loop.committee.llm.available
                                         else "baseline")
        return _ok(**out)

    @app.get("/api/equity")
    def equity():
        args = _query_args("env", "from", "to")
        sql = "SELECT ts, equity, hwm FROM equity_curve WHERE 1=1"
        params = []
        for col, key in (("env", "env"), ("ts", "from"), ("ts", "to")):
            pass
        if "env" in args:
            sql += " AND env=?"
            params.append(args["env"])
        if "from" in args:
            sql += " AND ts>=?"
            params.append(float(args["from"]))
        if "to" in args:
            sql += " AND ts<=?"
            params.append(float(args["to"]))
        sql += " ORDER BY ts ASC LIMIT 5000"
        rows = store.query(sql, params)
        return _ok(points=[[row["ts"], row["equity"], row["hwm"]] for row in rows])

    @app.get("/api/stats")
    def stats():
        args = _query_args("env", "from", "to")
        return _ok(**r.stats(store, env=args.get("env"),
                             frm=args.get("from") and float(args["from"]),
                             to=args.get("to") and float(args["to"])))

    @app.get("/api/rounds")
    def rounds():
        args = _query_args("page", "size", "status", "env", "inst", "from", "to")
        page = int(args.get("page", 1))
        size = min(int(args.get("size", 20)), 200)
        data = r.rounds_page(store, page=page, size=size,
                             status=args.get("status"), env=args.get("env"),
                             inst=args.get("inst"),
                             frm=args.get("from") and float(args["from"]),
                             to=args.get("to") and float(args["to"]))
        return _ok(**data)

    @app.get("/api/rounds/<round_id>")
    def round_detail_route(round_id):
        d = r.round_detail(store, round_id)
        if not d:
            return _err("not found", 404)
        return _ok(**d)

    @app.get("/api/trades")
    def trades():
        args = _query_args("page", "size", "status", "inst")
        page = int(args.get("page", 1))
        size = min(int(args.get("size", 50)), 500)
        where, params = [], []
        if args.get("status"):
            where.append("status=?")
            params.append(args["status"])
        if args.get("inst"):
            where.append("inst_id=?")
            params.append(args["inst"])
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        total = store.query_one(f"SELECT COUNT(*) c FROM trades {wsql}", params)["c"]
        rows = store.query(
            f"SELECT * FROM trades {wsql} ORDER BY opened_ts DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size])
        return _ok(total=total, page=page, size=size, items=[dict(x) for x in rows])

    @app.get("/api/trades/<tid>")
    def trade_detail(tid):
        row = store.query_one("SELECT * FROM trades WHERE id=?", (tid,))
        if not row:
            return _err("not found", 404)
        orders = store.query("SELECT * FROM orders WHERE trade_pk=?", (tid,))
        fills = store.query(
            "SELECT f.* FROM fills f JOIN orders o ON o.id=f.order_pk "
            "WHERE o.trade_pk=? ORDER BY f.ts", (tid,))
        open_round = store.query_one(
            "SELECT round_id FROM rounds WHERE id=?", (row["open_round_pk"],))
        return _ok(trade=dict(row), orders=[dict(o) for o in orders],
                   fills=[dict(f) for f in fills],
                   open_round_id=open_round["round_id"] if open_round else None)

    @app.get("/api/orders")
    def orders():
        args = _query_args("page", "size")
        page = int(args.get("page", 1))
        size = min(int(args.get("size", 50)), 500)
        total = store.query_one("SELECT COUNT(*) c FROM orders")["c"]
        rows = store.query(
            "SELECT * FROM orders ORDER BY created_ts DESC LIMIT ? OFFSET ?",
            [size, (page - 1) * size])
        return _ok(total=total, page=page, size=size, items=[dict(x) for x in rows])

    # ── 因子动物园 / 模型路由（Phase 7/9）────────────────────────────

    @app.get("/api/factors")
    def factors():
        gate = getattr(cfg, "FACTOR_GATE", {}) or {}
        counts = {"observing": 0, "trial": 0, "active": 0,
                  "retired": 0, "rejected": 0}
        items = []
        for d in store.query("SELECT * FROM factor_defs ORDER BY name"):
            counts[d["status"]] = counts.get(d["status"], 0) + 1
            s = store.query_one(
                "SELECT * FROM factor_scores WHERE factor=? "
                "ORDER BY computed_ts DESC LIMIT 1", (d["name"],))
            items.append({
                "name": d["name"], "family": d["family"], "tier": d["tier"],
                "status": d["status"],
                "ic": s["ic"] if s else None,
                "rank_ic": s["rank_ic"] if s else None,
                "ic_t": s["ic_t"] if s else None,
                "hit_rate": s["hit_rate"] if s else None,
                "n_obs": s["n_obs"] if s else 0,
                "days_tracked": s["days_tracked"] if s else 0,
                "gate_passed": bool(s["gate_passed"]) if s else False,
            })
        return _ok(counts=counts, gate=gate, items=items)

    @app.get("/api/factors/<name>")
    def factor_detail(name):
        d = store.query_one("SELECT * FROM factor_defs WHERE name=?", (name,))
        if not d:
            return _err("not found", 404)
        horizon = bottle.request.query.get("horizon") or "1b"
        series = store.query(
            "SELECT * FROM factor_scores WHERE factor=? AND horizon=? "
            "ORDER BY computed_ts ASC", (name, horizon))
        recent = store.query(
            "SELECT bar_ts, value, fwd_ret_1b FROM factor_obs WHERE factor=? "
            "ORDER BY bar_ts DESC LIMIT 30", (name,))
        return _ok(defn=dict(d), horizon=horizon,
                   series=[dict(s) for s in series],
                   recent=[dict(o) for o in recent])

    @app.get("/api/routes")
    def routes():
        out = []
        if loop:
            llm = loop.committee.llm
            for role in sorted(set(list(getattr(cfg, "LLM_ROUTES", {}) or {})
                                   + list(llm.backends))):
                prefix = role.split(":")[0] + "%"
                last = store.query_one(
                    "SELECT model FROM llm_calls WHERE role LIKE ? AND ok=1 "
                    "ORDER BY id DESC LIMIT 1", (prefix,))
                fails = store.query_one(
                    "SELECT COUNT(*) c FROM llm_calls WHERE role LIKE ? AND ok=0 "
                    "AND id > (SELECT COALESCE(MAX(id),0) FROM llm_calls "
                    "WHERE role LIKE ? AND ok=1)", (prefix, prefix))
                out.append({"role": role, "chain": llm.chain_for(role),
                            "last_answered_by": last["model"] if last else None,
                            "fail_streak": fails["c"]})
        return _ok(routes=out)

    @app.get("/api/events")
    def events():
        args = _query_args("page", "size", "kind", "level")
        page = int(args.get("page", 1))
        size = min(int(args.get("size", 50)), 500)
        where, params = [], []
        if args.get("kind"):
            where.append("kind=?")
            params.append(args["kind"])
        if args.get("level"):
            where.append("level=?")
            params.append(args["level"])
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        total = store.query_one(f"SELECT COUNT(*) c FROM app_events {wsql}", params)["c"]
        rows = store.query(
            f"SELECT * FROM app_events {wsql} ORDER BY ts DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size])
        return _ok(total=total, page=page, size=size, items=[dict(x) for x in rows])

    # ── 控制端点（只看/能暂停，永远不能交易）────────────────────────

    @app.post("/api/loop/run-now")
    def run_now():
        if not loop:
            return _err("循环未随本进程运行（只读 serve 模式）", 409)
        if getattr(loop, "_busy", False):
            return _err("有轮次正在运行", 409)
        loop.request_run_now()
        return _ok()

    @app.post("/api/loop/pause")
    def pause():
        if not loop:
            return _err("循环未随本进程运行（只读 serve 模式）", 409)
        loop.set_paused(True)
        return _ok(paused=True)

    @app.post("/api/loop/resume")
    def resume():
        if not loop:
            return _err("循环未随本进程运行（只读 serve 模式）", 409)
        loop.set_paused(False)
        return _ok(paused=False)

    return app


def _last_equity_row(store):
    row = store.query_one(
        "SELECT ts, equity, hwm, drawdown, usdt_avail, "
        "0 AS positions FROM equity_curve ORDER BY ts DESC LIMIT 1")
    if not row:
        return None
    d = dict(row)
    d["positions"] = []
    d["data_ok"] = 1
    d["symbols_ok"] = 0
    d["symbols_total"] = 0
    return d


class _Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _Quiet(WSGIRequestHandler):
    def log_request(self, *a):  # 静音访问日志
        pass


def serve(app, host, port):
    """照抄 TMWebDriver.py:114-121 的 bottle 服务体（线程化 wsgiref）。"""
    make_server(host, port, app, server_class=_Threaded,
                handler_class=_Quiet).serve_forever()


def check_bind_guard(host, password):
    """非回环绑定 + 空密码 = 拒绝启动（不可表示的安全状态）。"""
    if host not in ("127.0.0.1", "localhost", "::1") and not password:
        raise RuntimeError(
            "WEB_HOST 指向非回环地址但 WEB_PASSWORD 为空——"
            "拒绝启动：请设置 WEB_PASSWORD，或把 WEB_HOST 改回 127.0.0.1")
