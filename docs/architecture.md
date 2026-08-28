# 架构

## 每轮数据流

    数据层  get_candles(丢未收盘 confirm 行)/orderbook/OI/多空比/资金费率
      ↓
    因子层  factors.py（纯代码）：EMA/MACD/RSI/布林/ATR/量比/结构/支撑阻力/FVG/形态
      ↓                                  → report_json + report_text（逐字喂 LLM）
    委员会  3 分析师（LLM/基线人设）提案 → 3 裁判打分 → 均分≥阈值 且 ≥2 票通过 → 胜出
      ↓
    硬风控  risk.py R1–R7（一票否决，张数由风控反推，AI 不可越过）
      ↓
    执行    post_only Maker 限价 → 等成交（超时撤）→ OCO 保护单（止损+止盈）
      ↓
    退出    exits.py 移动止损（1R 保本 → 1×ATR 跟随）/ 时间止损；仓位消失 → 对账回填
      ↓
    落库    SQLite（WAL）：rounds / round_factors / proposals / judge_scores /
            risk_verdicts / trades / orders / fills / equity_curve / llm_calls / app_events

## 环境开关

`env.py` 的 TradingEnv 是唯一环境判断点：`paper / demo / replay / live`。
OKX_FLAG 由 env 派生，配置翻不动；live 需要两处独立改动；`--no-execute`
正交压缩 executing。`make_client()` 里那个 if 是全库唯一的环境分支。

## 关键不变量

- 有仓必有止损：挂止损失败 → 市价平仓兜底；每轮巡检补挂缺失保护单
- 单一数据源：账本只在 SQLite；JSONL 已退休
- run_state 按 env 分区：纸面高水位永远不会污染真实账户熔断器
- 幂等重放：orders 上 (env, exch_ord_id)/(env, exch_algo_id) partial unique index
