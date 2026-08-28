# -*- coding: utf-8 -*-
"""单一环境开关。

全代码库关于"这是哪种交易环境"的判断只允许出现在 make_client 这一个 if 里：
    - okx_config.py 的 TRADING_ENV 是选择环境的唯一位置
    - OKX_FLAG 由 env 派生，不再从配置读取（配置里手滑写 OKX_FLAG="0" 什么也翻不动）
    - 开实盘需要两处独立改动：TRADING_ENV="live" 且 ALLOW_LIVE_TRADING=True，
      且 ALLOW_LIVE_TRADING 不出现在模板里，必须手打
    - --no-execute 把 env.executing 压成 False：demo 可以只观察，而不变成另一个环境
"""
import os
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class TradingEnv:
    name: str                # replay | paper | demo | live
    okx_flag: str | None     # "1" 模拟盘 / "0" 实盘 / None 离线
    client_factory: str      # ReplayClient | PaperClient | OKXClient
    executing: bool          # 会真的下单吗
    needs_creds: bool
    requires_optin: bool = False


ENVS = {
    "replay": TradingEnv("replay", None, "ReplayClient", executing=False, needs_creds=False),
    "paper": TradingEnv("paper", "1", "PaperClient", executing=False, needs_creds=False),
    "demo": TradingEnv("demo", "1", "OKXClient", executing=True, needs_creds=True),
    "live": TradingEnv("live", "0", "OKXClient", executing=True, needs_creds=True,
                       requires_optin=True),
}


def resolve_env(cfg) -> TradingEnv:
    """从配置解析环境。TRADING_ENV 非法 / live 未显式 opt-in 都在这里拦下。"""
    env = ENVS[str(getattr(cfg, "TRADING_ENV", "paper")).lower()]
    if env.requires_optin and not getattr(cfg, "ALLOW_LIVE_TRADING", False):
        raise RuntimeError("live 环境需要 ALLOW_LIVE_TRADING=True；本版本默认关闭")
    return env


def make_client(env: TradingEnv, cfg, logger=None):
    """环境 → 客户端实例。全代码库唯一一个关于环境的 if。"""
    if env.needs_creds and not (cfg.has_credential("OKX_API_KEY")
                                and cfg.has_credential("OKX_SECRET_KEY")
                                and cfg.has_credential("OKX_PASSPHRASE")):
        raise RuntimeError(
            f"env={env.name} 需要真实凭证：请在 okx_trader/okx_config.py 填入"
            f"【模拟盘专属】API Key（OKX 网页 → 交易 → 模拟交易 → API 创建）")
    if env.client_factory == "ReplayClient":
        from .replay import ReplayClient
        return ReplayClient(cfg, logger=logger)
    if env.client_factory == "PaperClient":
        from .paper import PaperClient
        return PaperClient(cfg, logger=logger)
    from .client import OKXClient
    return OKXClient(cfg, logger=logger, flag=env.okx_flag)


def db_path(cfg=None) -> str:
    """SQLite 单文件位置（data/ 已 gitignore）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "trader.db")
