# 回测（okxt backtest）

在历史 K 线上回放**确定性（无 LLM）**决策路径，分钟级回答：
"当前 R1–R8 参数与出场机制的组合，历史上表现如何"。

> ⚠ 回测只覆盖机械路径（因子 + 基线人设/裁判 + 风控 + 出场规则），
> **不代表 LLM 路径的表现**。LLM 路径不确定、有成本，无法回测——它是否
> 增值由影子盘对照实验（`okxt compare`）回答。

## 抓数据

```bash
# 与回测解耦，可独立重试（OKX 部分网络不可达）
python -m okx_trader backtest-fetch --from 2026-06-01 --to 2026-08-30 --bar 1H
```

缓存到 `okx_trader/backtest/cache/<inst>_<bar>.jsonl`（一行一根，ts 升序去重）。
二次运行只补缺口不重抓。

## 跑回测

```bash
python -m okx_trader backtest \
  --from 2026-06-01 --to 2026-08-30 --bar 1H \
  --symbols BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
  --fill-model touch --json out.json
```

## 报告字段

- **meta**：bar 数、轮次数、成交模型、假设成交率（成交单数 / 挂单数，用于和
  线上真实成交率对照）
- **params**：实际生效的 R1–R8 参数快照（与 `_DEFAULTS` 不一致的项须人工核对）
- **trades**：笔数、胜率、mean R / median R、最大单笔盈/亏（R）、总 R、
  单样本 t 值与 p 值、样本量是否达 30/100/200
- **exit_reason_distribution**：`stop` / `target` / `trailing` / `time_stop` /
  `backtest_end` 各自笔数、占比、平均 R
- **risk**：各 `rule_code` 否决次数 + "提案 → 过风控 → 成交"漏斗
- **equity**：期末权益、最大回撤
- **by_inst**：分标的交易统计

## 三个成交模型（已知偏差方向）

| 值 | 规则 | 偏差 |
|---|---|---|
| `touch`（默认） | 挂单当根 bar 的 low<=px（买）/high>=px（卖）则成交，否则收盘撤单 | **乐观**（真实 post_only 只挂 90 秒） |
| `strict` | 需 bar 极值穿过挂单价至少 1 tickSz | 略保守 |
| `always` | 挂即成交 | 最乐观（仅对齐旧测试） |

## 关键实现约定

1. **虚拟时钟**：交易路径时间戳（`rounds.ts`/`trades.opened_ts`/时间止损的
   `held_bars`）读 `TradingLoop.now()`，回测注入固定时钟。不做这步，回测
   几分钟走完几个月 → 时间止损恒不触发。
2. **盘中触发**：`advance()` 用 bar 的 high/low 判定止损/止盈，同一根 K 线内
   两者都在区间 → 保守判**止损先成交**（坏消息优先）。
3. **前视保护**：`get_candles` 只返回 ts 严格早于当前 bar 的已收盘 K 线，
   决策绝不偷看决定成交/止损的那根 bar。
4. **出场原因**：以 ReplayClient 平仓记录为准（回填 trades 表），巡检的
   re-infer 因回测一 bar 滞后会误判成 unknown。

## 完成后不要做的事

拿到 `exit_reason` 分布后**不要顺手调参**。参数调整是基于分布的独立决策，
需单独评审。本工具的产出是测量结果，不是参数优化。
