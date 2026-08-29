# ══════════════════════════════════════════════════════════════════════════════
#  OKX 模拟盘自主交易 — 配置模板
# ══════════════════════════════════════════════════════════════════════════════
#  用法：把本文件复制为同目录下的 okx_config.py，填入你的【模拟盘】API 凭证。
#        okx_config.py 已被 .gitignore 忽略，绝不会提交到 git。
#
#  ⚠ 模拟盘 API Key 必须是「模拟盘（Demo Trading）专门创建」的 Key，
#     实盘 Key 用于模拟盘会报错（50102/51000 等），反之亦然。
#     创建入口：OKX 网页 → 顶部「交易」→「模拟交易」→ 头像 → API → 创建 API Key
#     权限勾选：读取 + 交易（不需要提现权限！）
# ══════════════════════════════════════════════════════════════════════════════

# ── 凭证区（必填）─────────────────────────────────────────────────────────────
OKX_API_KEY     = "在此填入模拟盘APIKey"
OKX_SECRET_KEY  = "在此填入模拟盘SecretKey"
OKX_PASSPHRASE  = "在此填入模拟盘Passphrase"

# 交易环境（唯一开关，见 okx_trader/env.py）：
#   paper  = 纸面（真实行情+虚拟账户，不需要 Key）
#   demo   = OKX 模拟盘真实下单（需要模拟盘 Key）
#   replay = 全离线回放（测试用）
# 实盘（live）需要 TRADING_ENV="live" 且手打 ALLOW_LIVE_TRADING=True 两处独立改动。
# OKX_FLAG 不再从配置读取——由环境派生，配置里写了也无效。
TRADING_ENV = "paper"

# 可选：HTTP(S) 代理，例如 "http://127.0.0.1:7890"；留空表示直连。
# 直连不通时（国内网络常见），先在 Clash/V2Ray 等工具里确认代理端口并填到这里。
# 注意：留空时 httpx 会尝试读 Windows 系统代理，可能指向一个路由不了 okx.com 的端口，
# 出现 "Server disconnected" 就说明当前代理通不了 OKX，换一个端口试试。
OKX_PROXY = ""

# ── 交易参数区（有默认值，按需修改）───────────────────────────────────────────
# 交易标的：永续合约（SWAP）
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

# 全账户风控上限（由 okx_trader/risk.py 用代码强制执行，见第三步）
MAX_RISK_PER_TRADE = 0.01     # 单笔最大亏损 ≤ 账户权益的 1%
MAX_TOTAL_LEVERAGE = 3
TRAIL_ATR_MULT = 1.0          # 移动止损：跟在价格极值后 1×ATR（浮盈 1R 前先推保本）
MAX_HOLD_BARS = 24            # 时间止损：持有超过 24 根 K 线且 |PnL|<0.3R 平仓
MAX_OPEN_POSITIONS = 3        # 最多同时持有的仓位数量
MAX_DRAWDOWN = 0.10           # 回撤熔断：权益自历史高点回撤超过 10% 禁止开新仓
SAME_DIRECTION_RISK_CAP = 0.02
REGIME_MISMATCH_PENALTY = 1.0  # 人设×市况错配扣分（趋势市压均值回归等）

# ── Kelly 仓位系数（默认影子模式：只算不改仓位）─────────────────────────────
KELLY_ENABLED = False          # true 后 kelly_mult 才作用于 R3 预算
KELLY_FRACTION = 0.5           # 分数 Kelly（半 Kelly）
KELLY_MIN_MULT = 0.25          # 系数地板
KELLY_MIN_SAMPLES = 30         # 每人设最少已平仓样本（不足 → 中性 1.0）
KELLY_SIG_LEVEL = 0.05         # 二项检验显著性水平  # 人设×市况错配扣分（趋势市压均值回归等）  # 同方向持仓聚合风险上限（BTC/ETH/SOL 高相关，同向=放大beta）

# 订单参数
MAKER_PRICE_OFFSET = 0.0     # 0 = 平齐买一/卖一挂单（post_only 保证 maker）；0.0005 = 前方 0.05%
ORDER_TIMEOUT_SEC = 90        # 限价单最长等待成交时间，超时未成交自动撤单

# 波动率 / 止损参数（第三步风控模块用）
ATR_PERIOD = 14               # ATR 回看周期（根 K 线）
ATR_BAR = "1H"                # ATR 使用的 K 线周期
ATR_STOP_MULT = 1.5           # 止损距离下限 = 1.5 × ATR（防止止损太近被噪声扫掉）
MIN_STOP_DIST_PCT = 0.002     # 止损距离相对入场价的最小比例（0.2%）
MAX_STOP_DIST_PCT = 0.05      # 止损距离相对入场价的最大比例（5%，太远视为风险失控）
MIN_RR = 1.5                  # 盈亏比下限：目标空间 / 止损距离
MIN_TARGET_ATR = 0.5          # 结构位距入场至少 0.5×ATR 才算目标（贴脸的位是噪声不是目标）
TARGET_ATR_MULT = 2.5         # 无可用结构位时的 ATR 兜底目标倍数

# 杠杆
LEVERAGE = 3

PAPER_EQUITY = 10000.0        # 纸面模式（无 Key）使用的虚拟权益

# ── 决策模式（第四步/多agent委员会）──────────────────────────────────────────
SCORE_THRESHOLD = 6.5         # Judge 平均分 ≥ 此值 且 多数通过，提案才胜出

# 因子晋级闸门（照抄 trader.gaagent.ai；未晋级因子只观测、永不影响下单）
FACTOR_GATE = {"scored_days": 15, "days_tracked": 30,
               "require_positive_rank_ic": True, "min_obs": 100}         # Judge 平均分 ≥ 此值 且 多数通过，提案才胜出
LOOP_INTERVAL_SEC = 3600      # 交易循环每轮间隔（秒）—— 建议≈K线周期（1H）

# ── Planner / Critic 的 LLM 配置（可选）──────────────────────────────────────
# 使用 OpenAI 兼容的 chat/completions 接口。三项都填才启用 LLM 决策；
# 不填则自动降级为内置基线策略（趋势跟随，不调用模型），方便先跑通流程。
LLM_API_BASE = ""             # 单后端简写；多后端用 LLM_ENDPOINTS
LLM_API_KEY = ""              # 对应 API Key
LLM_MODEL = ""                # 例如 "gpt-4o"、"glm-4.7" 等
# 按角色的后端路由（Phase 9）：每个后端一份凭证；裁判首选与分析师刻意不同
LLM_ENDPOINTS = {}
# LLM_ENDPOINTS = {
#   "gpt": {"api_base": "https://api.openai.com/v1", "api_key": "sk-...", "model": "gpt-4o"},
#   "glm": {"api_base": "...", "api_key": "...", "model": "glm-4.7"},
# }
LLM_ROUTES = {}
# LLM_ROUTES = {"analyst:趋势猎手": ["gpt", "glm"], "judge": ["glm", "gpt"]}
# 模型价目表（USD / 1M tokens）：未列出的模型 cost 记 NULL，面板显示"未计价"
LLM_PRICES = {}
# LLM_PRICES = {"gpt-4o": {"in": 2.5, "out": 10.0}}
LLM_TIMEOUT_SEC = 60

# 日志与数据
LOG_LEVEL = "INFO"            # DEBUG / INFO / WARNING / ERROR
LOG_DIR = "logs"              # 运行日志目录（相对 okx_trader/，已 gitignore）
ROUNDS_DIR = "data/rounds"    # 每轮决策记录目录（对应 rounds 页面，已 gitignore）
STATE_DIR = "data/state"      # 权益高水位等运行状态（已 gitignore）

# ── Web 面板（Phase 4 起）────────────────────────────────────────────────────
WEB_HOST = "127.0.0.1"        # 局域网暴露是显式 opt-in（"0.0.0.0"），且必须设密码
WEB_PORT = 8787
WEB_PASSWORD = ""             # 非回环绑定 + 空密码 = 拒绝启动

# ── 告警（Phase 7）───────────────────────────────────────────────────────────
ALERT_WEBHOOK_URL = ""        # 飞书群机器人 webhook；留空则不发告警
