# -*- coding: utf-8 -*-
"""配置加载与密钥掩码。

okx_config.py（gitignored，用户手填）是唯一配置来源；本模块负责：
    1. 把它加载成 Config 对象（含默认值兜底）
    2. 用 SecretStr 包住 4 个敏感字段——repr/traceback 里只见 "***"，不泄密钥
    3. 环境选择不在本模块——那是 env.py（TradingEnv）的唯一职责
"""
import importlib.util
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))

# 敏感字段：加载后一律包成 SecretStr
_SECRET_FIELDS = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "LLM_API_KEY")
_PLACEHOLDER = "在此填入"


class SecretStr:
    """掩码字符串：repr/str 永远是 "***"，真实值只从 .value 拿。
    （借鉴 memory/keychain.py 的思路但只保留值得保留的部分——掩码，而非 XOR 混淆。）"""

    def __init__(self, value):
        self.value = "" if value is None else str(value)

    def __repr__(self):
        return "SecretStr('***')"

    __str__ = __repr__

    def __bool__(self):
        return bool(self.value)

    def __eq__(self, other):
        return isinstance(other, SecretStr) and self.value == other.value

    def __hash__(self):
        return hash(self.value)


def _masked(v):
    return v if isinstance(v, SecretStr) else SecretStr(v)


class Config(SimpleNamespace):
    """okx_config.py 的加载结果；未写的字段取默认值。"""

    def __getattr__(self, name):
        # SimpleNamespace 已有 __getattr__ 语义；这里只为可读性兜底
        raise AttributeError(name)

    def credential(self, field):
        """取敏感字段的明文（仅 client/llm 内部使用；日志请勿打印返回值）。"""
        v = getattr(self, field)
        return v.value if isinstance(v, SecretStr) else str(v or "")

    def has_credential(self, field):
        v = getattr(self, field, None)
        s = v.value if isinstance(v, SecretStr) else str(v or "")
        return bool(s) and _PLACEHOLDER not in s


# 用户可写可不写的字段 → 默认值（与 okx_config_template.py 对齐）
_DEFAULTS = {
    "TRADING_ENV": "paper",
    "OKX_PROXY": "",
    "SYMBOLS": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
    "MAX_RISK_PER_TRADE": 0.01,
    "MAX_TOTAL_LEVERAGE": 3.0,
    "MAX_OPEN_POSITIONS": 3,
    "MAX_DRAWDOWN": 0.10,
    "MAKER_PRICE_OFFSET": 0.0005,
    "ORDER_TIMEOUT_SEC": 90,
    "ATR_PERIOD": 14,
    "ATR_BAR": "1H",
    "ATR_STOP_MULT": 1.5,
    "MIN_STOP_DIST_PCT": 0.002,
    "MAX_STOP_DIST_PCT": 0.05,
    "MIN_RR": 1.5,
    "MIN_TARGET_ATR": 0.5,
    "TARGET_ATR_MULT": 2.5,
    "LEVERAGE": 3,
    "LOOP_INTERVAL_SEC": 3600,
    "SCORE_THRESHOLD": 6.5,
    "FACTOR_GATE": {"scored_days": 15, "days_tracked": 30,
                    "require_positive_rank_ic": True, "min_obs": 100},
    "RISK_TICK_SEC": 300,
    "DRAWDOWN_LADDER": [
        {"dd": 0.04, "risk_mult": 0.5, "allow_open": True},
        {"dd": 0.07, "risk_mult": 0.25, "allow_open": False},
        {"dd": 0.10, "risk_mult": 0.0, "allow_open": False, "flatten": True},
    ],
    "HIGH_VOL_ATR_PCT": 0.03,
    "TREND_THRESHOLD": 0.45,
    "HALLUCINATION_PENALTY": 2.0,
    "MAX_REVISIONS": 1,
    "PAPER_EQUITY": 10000.0,
    "TRAIL_ATR_MULT": 1.0,
    "MAX_HOLD_BARS": 24,
    "LLM_API_BASE": "",
    "LLM_MODEL": "",
    "LLM_TEMPERATURE": 0.3,
    "LLM_ENDPOINTS": {},
    "LLM_ROUTES": {},
    "LLM_PRICES": {},
    "LLM_TIMEOUT_SEC": 60,
    "ALERT_WEBHOOK_URL": "",
    "WEB_HOST": "127.0.0.1",
    "WEB_PORT": 8787,
    "WEB_PASSWORD": "",
    "LOG_LEVEL": "INFO",
}


def config_path():
    return os.path.join(HERE, "okx_config.py")


def load_config():
    """加载 okx_config.py。文件不存在或字段没填都**不抛错**——
    需要凭证与否是 TradingEnv 的事（env.needs_creds），这里只忠实加载。"""
    path = config_path()
    raw = {}
    if not os.path.exists(path):
        raw = {}
    else:
        spec = importlib.util.spec_from_file_location("okx_config", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        raw = {k: v for k, v in vars(mod).items() if k.isupper()}

    data = dict(_DEFAULTS)
    data.update(raw)
    for f in _SECRET_FIELDS:
        data[f] = _masked(data.get(f, ""))
    cfg = Config(**data)
    cfg._loaded_from = path
    return cfg


def get_logger(name="okx_trader", level="INFO"):
    """统一日志：控制台 + logs/okx_client.log（滚动，5MB×3 份）。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    log_dir = os.path.join(HERE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(os.path.join(log_dir, "okx_client.log"),
                             maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger
