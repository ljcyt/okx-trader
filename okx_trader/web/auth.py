# -*- coding: utf-8 -*-
"""认证：表单登录 + 签名 httponly 会话 cookie，会话只存内存。

设计取舍（为什么不用仓库旧代码的两种方案）：
    - HTTP Basic：无登出、每个请求重放密码、被浏览器凭据库缓存
    - ?t=<token>：长期静态密钥进 URL/Referer/shell 历史，且不过期
本实现各取其好：恒定时间比较 + httponly/SameSite cookie + TTL。

约束：
    - 同一 IP 连续 5 次失败 → 封 60 秒（恒定时间比较挡不住对人选密码的爆破）
    - 会话只在内存：重启即登出对单人工具是特性，还少一个持久化面
    - 明文 HTTP 的现实（局域网）在 docs/dashboard.md 里直说
"""
import secrets
import threading
import time

COOKIE_NAME = "okxt_sid"
TTL_SEC = 7 * 24 * 3600          # 7 天（单人局域网工具）
MAX_FAILS = 5
LOCKOUT_SEC = 60

SESSIONS = {}                    # token -> {"exp": ts, "ip": ip}
_FAILS = {}                      # ip -> [count, locked_until]
_lock = threading.Lock()


def check_password(password, cfg) -> bool:
    expected = getattr(cfg, "WEB_PASSWORD", "") or ""
    return secrets.compare_digest(str(password), str(expected))


def login(password, cfg, ip):
    """成功返回 token；失败返回 None（含锁定判定，锁定抛 PermissionError 由路由转 429）。"""
    with _lock:
        fails = _FAILS.get(ip, [0, 0.0])
        now = time.time()
        if fails[1] > now:
            raise PermissionError("locked")
        if not check_password(password, cfg):
            fails[0] += 1
            if fails[0] >= MAX_FAILS:
                fails = [0, now + LOCKOUT_SEC]
            _FAILS[ip] = fails
            return None
        _FAILS.pop(ip, None)
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = {"exp": now + TTL_SEC, "ip": ip}
        return token


def logout(token):
    with _lock:
        SESSIONS.pop(token, None)


def validate(token, ip=None):
    with _lock:
        s = SESSIONS.get(token)
        if not s or s["exp"] < time.time():
            SESSIONS.pop(token, None)
            return False
        return True


def sweep():
    """清理过期会话（loop 线程每轮顺手调即可）。"""
    with _lock:
        now = time.time()
        for t in [t for t, s in SESSIONS.items() if s["exp"] < now]:
            SESSIONS.pop(t, None)
