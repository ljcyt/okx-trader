# -*- coding: utf-8 -*-
"""极简事件钩子：register(event, fn) / trigger(event, ctx) / discover_and_load(dir)。

（移植自被删的 plugins/hooks.py——正适合 round_done / order_filled /
risk_rejected / data_degraded / circuit_breaker / naked_position 这些交易事件，
让告警变成可插拔文件。）
"""
import glob
import importlib.util
import os

_registry = {}


def register(event, fn):
    _registry.setdefault(event, []).append(fn)


def trigger(event, ctx=None):
    """任何监听者的异常都不影响主流程。"""
    ctx = ctx or {}
    for fn in _registry.get(event, []):
        try:
            fn(ctx)
        except Exception:  # noqa: BLE001
            pass


def discover_and_load(directory):
    """加载目录下的 hook_*.py（如 alerts/alert_hook.py 的注册文件）。"""
    for path in sorted(glob.glob(os.path.join(directory, "hook_*.py"))):
        spec = importlib.util.spec_from_file_location(path[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return _registry
