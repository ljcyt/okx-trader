# okx-trader

**多 agent 委员会加密货币交易 Agent**（OKX 模拟盘）+ **Web 决策透明面板**。

> Multi-agent committee trading agent for OKX demo trading, with a web dashboard
> that makes every decision auditable — factor snapshots, analyst proposals,
> judge scores, hard-risk vetoes, and realized PnL per persona.

## 核心理念

1. **AI 只有提名权和打分权，没有下单权。** 每一轮：3 位不同交易风格的分析师
   独立提案 → 3 位裁判打分 → 均分门槛 + 多数决选出胜者 → **代码硬风控 R1–R7
   一票否决** → Maker 限价单 + 交易所侧止盈止损。
2. **因子由代码计算，不由模型编造。** EMA/MACD/RSI/布林/ATR/量比/资金费率/
   市场结构/支撑阻力/FVG/OI/订单簿失衡，全部是确定性 Python，LLM 只做解读。
3. **每个决策可审计。** 每轮完整落 SQLite：分析师实际看到的逐字因子报告、
   每份提案、每张裁判票、每条风控拒绝原因、每笔订单与盈亏（按分析师归因）。
4. **风险是预算制。** 单笔最大亏损 ≤ 权益 1%、总杠杆 ≤ 3x、最多 3 仓、
   回撤 >10% 熔断、盈亏比 ≥1.5、开仓后绝不允许无止损仓位（崩溃自动补挂）。

## 委员会构成

| 角色 | 人设/视角 | 提案/打分逻辑 |
|---|---|---|
| 分析师 · 趋势猎手 | 趋势跟踪 | 多/空头排列 + MACD 状态，4H 不逆势 |
| 分析师 · 均值回归者 | 超买超卖 | RSI 极值 + 布林带触轨 |
| 分析师 · 资金哨兵 | 衍生品情绪 | 资金费率极端拥挤时逆向 |
| 裁判 · 技术裁判 | 技术一致性 | 方向与因子自洽性 |
| 裁判 · 风控裁判 | 风险审查 | 止损质量、波动/费率/量能异常 |
| 裁判 · 资金管理裁判 | 资金效率 | 置信度、盈亏比、暴露 |

分析师与裁判由 LLM 驱动（同一模型不同人设，支持多端点 failover）；
未配置 LLM 时自动降级为确定性规则模拟，整条链路依然完整可跑。

## 快速开始

    pip install -e .
    cp okx_trader/okx_config_template.py okx_trader/okx_config.py

    # 1) 零配置：纸面模式（真实行情 + 虚拟账户，不需要任何 Key）
    python -m okx_trader run-once                 # 跑一轮，看委员会决策
    python -m okx_trader run-loop --serve         # 每小时一轮 + Web 面板

    # 2) 全离线验证（不碰网络）
    python -m okx_trader replay

    # 3) OKX 模拟盘真实下单：创建模拟盘专属 API Key 填入 okx_config.py，
    #    TRADING_ENV="demo"（详见 docs/setup.md），然后
    python -m okx_trader check-env
    python -m okx_trader run-loop --serve

打开 http://127.0.0.1:8787 （密码 = WEB_PASSWORD）。

## Web 面板

- **总览** — 环境徽章、权益/回撤/持仓、熔断横幅、数据健康条
- **权益** — 内联 SVG 权益曲线（高水位 + 回撤带）
- **决策历史** — 每轮状态、胜出提案、风控规则码，点进是**六步漏斗**：
  因子快照（逐字喂给 LLM 的原文）→ 分析师提案 → 裁判矩阵与聚合算式 →
  胜出者 → 硬风控 → 执行
- **交易** — 按标的与**按分析师**的真实盈亏（回答"哪个人设真的赚钱"）
- **事件** — 熔断、数据降级、止损补挂、环境切换

安全设计：默认只绑 127.0.0.1；局域网暴露必须设密码；**没有任何下单端点**——
cookie 被盗的影响半径是"能看、能暂停"，不是"能交易"。

## 环境

| env | 说明 | 下单 | 需要 Key |
|---|---|---|---|
| paper | 真实行情 + 虚拟账户 | 否 | 否 |
| demo | OKX 模拟盘（x-simulated-trading: 1） | 是 | 模拟盘专属 Key |
| replay | 全离线回放（测试） | 否 | 否 |
| live | 实盘 | 是 | 需要 TRADING_ENV="live" 且手打 ALLOW_LIVE_TRADING=True |

`--no-execute` 把 demo 压成"只观察"，不需要发明第五种环境。

## 测试

    python -m unittest discover -s okx_trader/tests -v
    # 或 pytest okx_trader/tests -q

42 项测试，全部离线：风控规则、因子计算（MACD/形态/FVG/K线清洗）、
迁移幂等、三场景回放（止损/止盈/时间止损 + 移动止损）、Web 认证与分页。

## 文档

- docs/setup.md — OKX 模拟盘 Key 申请与配置
- docs/architecture.md — 数据流与表结构
- docs/dashboard.md — 面板与安全模型
- docs/code_style.md — 代码风格

## 上游声明

本项目是 lsdefine/GenericAgent 的硬分叉，已移除全部通用 agent 框架代码，
仅保留 MIT 许可要求的原始署名（见 NOTICE.md）。与上游无关联、无向后兼容承诺。

## English summary

Deterministic factor layer -> 3 LLM analyst personas propose trades -> 3 judge
personas score them (mean threshold + majority quorum) -> code-enforced risk
gate (1% per trade, 3x leverage cap, drawdown circuit breaker, R:R >= 1.5,
no position without an exchange-side stop) -> post-only maker entry + OCO
protection. SQLite records everything including verbatim LLM prompts; the
dashboard exposes the full decision funnel and per-persona realized PnL.
Paper/replay/demo environments with a single switch. Works fully offline
(replay + synthetic fixtures, 42 tests).
