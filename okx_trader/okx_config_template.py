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

# 模拟盘标志："1" = 模拟盘（Demo Trading）；"0" = 实盘（真实资金，慎改！）。
# 客户端会把它放进请求头 x-simulated-trading。
OKX_FLAG = "1"

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
MAX_TOTAL_LEVERAGE = 3.0      # 总杠杆上限：全部仓位名义市值之和 / 账户权益
MAX_OPEN_POSITIONS = 3        # 最多同时持有的仓位数量
MAX_DRAWDOWN = 0.10           # 回撤熔断：权益自历史高点回撤超过 10% 禁止开新仓

# 订单参数
MAKER_PRICE_OFFSET = 0.0005   # Maker 限价单相对买一/卖一的偏移比例（0.05%）
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
LEVERAGE = 3                  # 单合约杠杆（开仓前自动设置；总杠杆另受 MAX_TOTAL_LEVERAGE 约束）

# ── 运行模式 ─────────────────────────────────────────────────────────────────
# DRY_RUN = True：只分析、只记录，不下真单（无 Key 时自动进入纸面模式）
# DRY_RUN = False：真实在 OKX 模拟盘下单（需要填好上面的凭证并通过 check_env）
DRY_RUN = True
PAPER_EQUITY = 10000.0        # 纸面模式（无 Key）使用的虚拟权益

# ── 决策模式（第四步/多agent委员会）──────────────────────────────────────────
SCORE_THRESHOLD = 6.5         # Judge 平均分 ≥ 此值 且 多数通过，提案才胜出
LOOP_INTERVAL_SEC = 3600      # 交易循环每轮间隔（秒）—— 建议≈K线周期（1H）

# ── Planner / Critic 的 LLM 配置（可选）──────────────────────────────────────
# 使用 OpenAI 兼容的 chat/completions 接口。三项都填才启用 LLM 决策；
# 不填则自动降级为内置基线策略（趋势跟随，不调用模型），方便先跑通流程。
LLM_API_BASE = ""             # 例如 "https://api.openai.com/v1" 或任意兼容中转
LLM_API_KEY = ""              # 对应 API Key
LLM_MODEL = ""                # 例如 "gpt-4o"、"glm-4.7" 等

# 日志与数据
LOG_LEVEL = "INFO"            # DEBUG / INFO / WARNING / ERROR
LOG_DIR = "logs"              # 运行日志目录（相对 okx_trader/，已 gitignore）
ROUNDS_DIR = "data/rounds"    # 每轮决策记录目录（对应 rounds 页面，已 gitignore）
STATE_DIR = "data/state"      # 权益高水位等运行状态（已 gitignore）
