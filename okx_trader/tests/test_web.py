# -*- coding: utf-8 -*-
"""test_web.py：用手工 WSGI environ 驱动 bottle app，CI 里不开 socket。

覆盖（Phase 4 验证要求）：
    - 无 cookie GET /api/state → 401
    - 错密码 ×6，第 6 次 → 429（5 次失败即锁定）
    - 对密码 → 200 且带 Set-Cookie: okxt_sid=…; HttpOnly; SameSite=Lax
    - 带 cookie 的 /api/rounds 分页与 /api/rounds/<id> 详情（R7 原文）
    - WEB_HOST=0.0.0.0 且 WEB_PASSWORD 空 → 守卫拒绝启动
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse

sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from okx_trader.config import load_config
from okx_trader.web import server as ws
from okx_trader.web import auth as auth  # noqa: F401
from okx_trader.store.db import Store


def make_cfg(tmp, **kw):
    cfg = load_config()
    cfg.TRADING_ENV = "paper"
    cfg.WEB_HOST = "127.0.0.1"
    cfg.WEB_PORT = 8787
    cfg.WEB_PASSWORD = "secret-pw"
    cfg._loaded_from = os.path.join(tmp, "okx_config.py")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def call_wsgi(app, method, path, body=None, cookie=None):
    """手工构造 WSGI environ（不开 socket），返回 (status, headers, body_bytes)。"""
    parsed = urllib.parse.urlsplit(path)
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": parsed.path,
        "QUERY_STRING": parsed.query,
        "SERVER_NAME": "localhost", "SERVER_PORT": "8787",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0), "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body.encode() if body else b""),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False, "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(body.encode()) if body else 0),
        "CONTENT_TYPE": "application/json",
        "REMOTE_ADDR": "127.0.0.1",
    }
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    status_headers = {}

    def start_response(status, headers, exc_info=None):
        status_headers["status"] = status
        status_headers["headers"] = headers

    chunks = app(environ, start_response)
    data = b"".join(chunks)
    return status_headers["status"], status_headers["headers"], data


def get_cookie(headers):
    """返回完整 Set-Cookie 值（含属性段），供断言 HttpOnly/SameSite。"""
    for k, v in headers:
        if k.lower() == "set-cookie" and v.startswith("okxt_sid="):
            return v
    return None


class WebTest(unittest.TestCase):
    def setUp(self):
        auth.SESSIONS.clear()
        auth._FAILS.clear()
        self.tmp = tempfile.mkdtemp(prefix="okxt-web-")
        self.cfg = make_cfg(self.tmp)
        self.store = Store(os.path.join(self.tmp, "trader.db"))
        self.app = ws.create_app(self.cfg, self.store, loop=None)
        # 造 3 条 rounds + 1 条 R7 verdict（像迁移后的形状）
        from okx_trader.store.write import RoundWriter
        for rid in ("r1", "r2", "r3"):
            rw = RoundWriter.open(self.store, rid, 1000.0 + len(rid), "paper", 0,
                                  "baseline")
            rw.store.execute(
                "INSERT INTO risk_verdicts(round_pk, passed, rule_code, "
                "first_failure, failures_json, warnings_json) VALUES (?,?,?,?,?,?)",
                (rw.pk, 0, "R7", "R7: 盈亏比不足 RR=0.31", "[]", "[]"))
            rw.finish("risk_rejected" if rid != "r3" else "no_action")

    def _login(self, password="secret-pw"):
        status, headers, body = call_wsgi(
            self.app, "POST", "/api/login",
            body=json.dumps({"password": password}))
        return status, headers, body

    def test_unauthenticated_state_is_401(self):
        status, _, body = call_wsgi(self.app, "GET", "/api/state")
        self.assertTrue(status.startswith("401"), status)
        self.assertEqual(json.loads(body)["error"], "unauthenticated")

    def test_lockout_on_repeated_failures(self):
        for i in range(5):
            status, _, _ = self._login("wrong")
            self.assertTrue(status.startswith("401"), f"attempt {i}: {status}")
        status, _, _ = self._login("wrong")     # 第 6 次 → 已锁定
        self.assertTrue(status.startswith("429"), status)
        status, _, _ = self._login("secret-pw")  # 密码对了也被锁
        self.assertTrue(status.startswith("429"), status)

    def test_login_sets_session_cookie(self):
        status, headers, _ = self._login()
        self.assertTrue(status.startswith("200"), status)
        cookie = get_cookie(headers)
        self.assertIsNotNone(cookie)
        self.assertIn("okxt_sid=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("samesite=lax", cookie.lower())

    def test_rounds_pagination_and_detail(self):
        _, headers, _ = self._login()
        cookie = get_cookie(headers)
        status, _, body = call_wsgi(self.app, "GET",
                                    "/api/rounds?page=1&size=2", cookie=cookie)
        data = json.loads(body)
        self.assertTrue(status.startswith("200"))
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["items"]), 2)
        rid = data["items"][0]["round_id"]
        status, _, body = call_wsgi(self.app, "GET", f"/api/rounds/{rid}",
                                    cookie=cookie)
        d = json.loads(body)
        self.assertTrue(status.startswith("200"))
        self.assertEqual(d["risk"]["rule_code"], "R7")
        self.assertIn("RR=0.31", d["risk"]["first_failure"])

    def test_bind_guard_blocks_lan_without_password(self):
        with self.assertRaises(RuntimeError) as ctx:
            ws.check_bind_guard("0.0.0.0", "")
        self.assertIn("WEB_PASSWORD", str(ctx.exception))
        # 回环 + 空密码允许；局域网 + 有密码允许
        ws.check_bind_guard("127.0.0.1", "")
        ws.check_bind_guard("0.0.0.0", "pw")


if __name__ == "__main__":
    unittest.main(verbosity=2)
