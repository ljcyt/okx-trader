# 开发任务：机械路径回测驱动器

> 本文档是**自包含**的交接说明，假设你没有相关对话上下文。请先读完第 3 节的前置清单再动手。

## 1. 任务

新增 `okxt backtest` 子命令：在**历史 K 线**上回放确定性（无 LLM）决策路径，
输出 `exit_reason` 分布、`mean R`、t 统计量等，用来在**分钟级**内回答
"当前 R1–R8 参数与出场机制的组合，历史上表现如何"。

## 2. 背景（为什么需要它）

系统当前只有 **1 笔已平仓交易**。前向积累的吞吐是每小时 1 轮、多数轮次弃权，
攒到能做统计推断的 30–200 笔需要数月。

但**机械路径是完全确定性的**：因子由代码算（`factors.py`），基线人设与裁判是
规则（`committee.py` 的 `_baseline_analyst` / `_baseline_judge`），R1–R8 是纯判定
（`risk.py`），出场三条腿也是规则（`exits.py`）。给定同一段 K 线，输出可复现。

所以同一个问题可以在历史数据上几分钟跑完，而不是等几个月。
**LLM 路径不在本任务范围内**（不确定 + 有成本，无法回测）。

## 3. 前置阅读（必须，按顺序）

| 文件 | 读什么 |
|---|---|
| `okx_trader/env.py` | `TradingEnv` / `ENVS` / `resolve_env` / `make_client`。注意 `ENVS["replay"].executing = False` |
| `okx_trader/replay.py` | **重点**。`ReplayClient` 已实现完整 OKXClient 接口。看 `__init__(cfg, logger, candles, script)`、`advance()`、`current_step()`、`_close()`、`get_candles()` |
| `okx_trader/loop.py` | `TradingLoop.__init__(cfg, logger, env_name, executing, store)`、`run_round()`、`take_snapshot()`、`_execute_open()`、`_patrol_positions()` |
| `okx_trader/exits.py` | `manage_open_positions()` / `reconcile_closed_trade()` / `open_trade_row()`。**特别看第 172-175 行的时间止损** |
| `okx_trader/risk.py` | `check_open_plan()` 的 R1–R8 |
| `okx_trader/cli.py` | `cmd_replay`（第 173 行，你的起点）、`_build_cfg`、`main()` 的子命令注册方式 |
| `okx_trader/store/schema.sql` | `rounds` / `trades` / `orders` / `risk_verdicts` / `equity_curve` 表结构 |
| `okx_trader/tests/test_loop_replay.py` | 现有 ReplayClient 用法范例 |

## 4. 范围

**做：**
- 历史 K 线抓取 + 本地缓存
- `ReplayClient` 的三处扩展（见 6.2）
- 虚拟时钟注入（见 6.3，**不做这个回测结果就是错的**）
- 回测驱动器 + CLI 子命令 + 报告输出
- 配套单测

**不做（明确排除，不要顺手改）：**
- 不碰 LLM 路径、不改任何人设/裁判提示词
- **不调任何策略参数**（`MIN_RR` / `ATR_STOP_MULT` / `SCORE_THRESHOLD` /
  `MAX_HOLD_BARS` / `TARGET_ATR_MULT` 等一律不动）。本任务只提供测量工具，
  调参是拿到分布之后的独立决策
- 不改 `web/`、不改生产配置、不动 `/opt/okx-trader`
- 不引入新的第三方依赖（只用 `requests` + 标准库；项目现有依赖见 `pyproject.toml`）

## 5. 现有可复用零件（精确签名）

```python
# okx_trader/replay.py
class ReplayClient:
    def __init__(self, cfg, logger=None, candles=None, script=None)
    #   candles: {inst_id: [ {ts, open, high, low, close, vol}, ... ]}  ← 直接喂历史数据
    #   script:  [ {"price": px, "fill": bool, "sl_hit": bool, "tp_hit": bool}, ... ]
    #            每一步 = 一轮行情；cursor 指向当前步
    def advance(self)                 # 游标 +1，并按 step 价格判定 SL/TP 触发
    def current_step(self)            # 越界时钳在最后一步
    def get_candles(self, inst_id, bar="1H", limit=100, after=None)   # 只返回游标之前的已收盘部分
    self.positions / self.orders / self.algos / self.closed_trades     # 状态机

# okx_trader/loop.py
TradingLoop(cfg=None, logger=None, env_name=None, executing=None, store=None)
loop.run_round() -> dict            # {"status": ..., "decision": ..., "failures": [...]}
loop.client                          # 可直接替换/改 .script

# okx_trader/exits.py
manage_open_positions(loop, snap, rw)          # 移动止损 / 时间止损 / 目标确认
reconcile_closed_trade(loop, snap, rw, inst, meta)
reconcile_trade(store, tr, exit_px, reason, close_round_pk, ct_val)
open_trade_row(loop, rw, sized, filled, avg_px, open_round_pk=None)

# okx_trader/store/db.py
Store(db_path)                       # 独立库文件即可，schema 自动迁移
```

`cmd_replay`（`cli.py:173`）是最接近的现成范例——它构造 3 步脚本跑 3 轮。
你要做的本质上是"把 3 步换成 N 千步，每步来自真实 K 线"。

## 6. 实现规格

### 6.1 历史 K 线获取与缓存

新增 `okx_trader/backtest/data.py`：

```python
def fetch_history(client, inst_id, bar, start_ms, end_ms, cache_dir) -> list[dict]
```

- 用现有 `client.get_candles(inst_id, bar=bar, limit=300, after=<cursor>)` 分页向更旧翻
  （`after` 语义 = 返回比该 ts 更旧的记录，`client.py` 已把 limit 钳到 300）
- 缓存到 `okx_trader/backtest/cache/<inst>_<bar>.jsonl`，一行一根 K 线，按 ts 升序、去重
- 二次运行只补缺口，不重抓
- 抓取失败必须 `log.warning` 并抛出，**不要静默返回空**（本项目有过静默空转的事故）
- 新增 `okxt backtest-fetch --from --to --bar` 子命令单独跑抓取，与回测解耦
  （OKX 从部分网络不可达，抓取要能独立重试）

### 6.2 `ReplayClient` 三处扩展

**(a) 用 bar 的 high/low 判定触发，不要用单一 price**

现状 `advance()` 只看 `step["price"]`，等于只用收盘价判定止损——**会系统性漏掉盘中
被打掉的止损**，让回测结果乐观。改为 step 携带 OHLC，判定用：

- long：`low <= stop` → 止损；`high >= target` → 止盈
- short：`high >= stop` → 止损；`low <= target` → 止盈

**同一根 K 线内止损与止盈都在区间内时，一律判止损先成交**（保守约定）。
这条必须写进代码注释，并有专门单测。

**(b) Maker 成交规则要显式，且默认保守**

现状 `fill=True` 即刻全量成交。真实系统是 `post_only` 平齐买一/卖一 +
`ORDER_TIMEOUT_SEC=90`——90 秒只占 1H bar 的 2.5%。提供 `--fill-model`：

| 值 | 规则 | 偏差方向 |
|---|---|---|
| `touch`（默认） | 挂单当根 bar 的 `low <= px`（买）/ `high >= px`（卖）则成交，否则收盘撤单 | **乐观**（真实只挂 90 秒） |
| `strict` | 需 bar 极值**穿过**挂单价至少 1 个 tickSz 才成交 | 略保守 |
| `always` | 挂即成交（仅用于和现有 3 步脚本测试对齐） | 最乐观 |

报告里**必须打印假设成交率**（成交单数 / 挂单数），以便和线上真实成交率对照。

**(c) 由 bar 驱动，而非手写 script**

驱动器按 bar 生成 `script`：`{"price": close, "high": high, "low": low, "open": open, "ts": ts}`。
保留 `sl_hit`/`tp_hit` 字段以兼容现有测试（显式置位时优先于价格判定）。

**保持 `get_candles` 的前视保护**：只返回游标之前的已收盘 bar。加一条单测断言
"游标在第 i 根时，`get_candles` 返回的最大 ts < script[i].ts"。

### 6.3 虚拟时钟注入 —— 不做这一步回测结果就是错的

`exits.py:172-175` 的时间止损是这么算的：

```python
opened_ts = (tr["opened_ts"] if tr else None) or _parse_ts(m.get("opened_at"))
held_bars = (time.time() - opened_ts) / _bar_seconds(cfg.ATR_BAR)
if held_bars >= cfg.MAX_HOLD_BARS and abs(r_now) < 0.3:
```

回测在几分钟内走完几个月的 bar，**墙上时间几乎不动** → `held_bars` 恒约 0 →
**时间止损永远不触发**。而 `exit_reason` 分布正是本任务的核心产出，缺一条腿就无意义。

`loop.py` 里有 22 处 `time.time()`，同样会污染 `rounds.ts`、`trades.opened_ts`、
`equity_curve.ts`（全部塞成同一个真实时刻，报表和查询都会错）。

**要求：**

1. 在 `TradingLoop` 上加 `self._clock = time.time`（构造参数可注入），
   并加 `def now(self): return self._clock()`
2. 把**交易路径**上的 `time.time()` 换成 `self.now()`：`run_round` 的 `round_id`/`ts`、
   `open_trade_row`、`reconcile_trade`、`write_equity`、`_patrol_positions` 的时间戳。
   `exits.py` 的函数签名已带 `loop`，改用 `loop.now()`
3. **日志时间戳不要改**（那应该是真实时间）
4. 回测驱动器每推进一根 bar 就把 `loop._clock` 设为该 bar 的收盘时刻（秒）
5. 加单测：注入一个固定时钟，断言 `trades.opened_ts` 等于注入值

改动点多但机械。改完后 `python -m unittest discover -s okx_trader/tests -t .`
必须全绿——现有测试依赖真实时间的地方要么保持默认时钟，要么显式注入。

### 6.4 回测驱动器

新增 `okx_trader/backtest/runner.py`：

```python
def run_backtest(cfg, candles, *, bar, fill_model="touch",
                 store=None, progress=None) -> dict
```

流程：

```
1. cfg = 深拷贝传入配置，强制：
      TRADING_ENV = "replay"
      LLM_ENDPOINTS = {}   LLM_ROUTES = {}   LLM_API_BASE = ""   ← 保证走确定性基线
      ORDER_TIMEOUT_SEC = 0
2. loop = TradingLoop(cfg=cfg, env_name="replay", executing=True, store=Store(<回测库>))
      ↑ 注意：ENVS["replay"].executing 默认 False，必须显式传 True，
        否则 _execute_open 走 dry-run 分支（loop.py:568），不会产生订单与成交
3. loop.client = ReplayClient(cfg, candles=candles, script=<由 bar 生成>)
   loop.risk.client = loop.client          ← 见第 7 节陷阱 ①
   loop.committee.client = loop.client
4. warmup：前 80 根不决策（EMA60/MACD/布林都需要历史），只推进游标
5. for each bar in candles[80:]:
       loop._clock = lambda ts=bar_ts_sec: ts
       loop.run_round()
       loop.client.advance()
6. 收尾：对未平仓位按最后一根收盘价强制平仓，reason='backtest_end'
7. 返回统计
```

**进度输出**：每 500 根打一行（`已处理 N/M 根，交易 K 笔`），否则长回测看不出是否卡死。

### 6.5 CLI 与报告

```
okxt backtest --from 2026-06-01 --to 2026-08-30 --bar 1H \
              --symbols BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP \
              --fill-model touch --db okx_trader/backtest/bt.db --json out.json
```

报告必须包含（缺任何一项算未完成）：

| 分组 | 字段 |
|---|---|
| 总体 | 回测区间、bar 数、轮次数、决策次数、假设成交率 |
| 交易 | 笔数、胜率、`mean R`、`median R`、最大单笔盈/亏（R）、总 R |
| 统计 | `mean R` 的单样本 t 值与 p 值（`t = mean/(std/√n)`，正态近似）、样本量是否达 30/100/200 |
| **`exit_reason` 分布** | `stop` / `target` / `trailing` / `time_stop` / `backtest_end` 各自笔数、占比、平均 R |
| 风控 | 各 `rule_code` 的否决次数（`R1`–`R8`），以及"提案数 → 过闸数 → 成交数"漏斗 |
| 权益 | 期末权益、最大回撤、回撤阶梯各档触发次数 |
| 分标的 | 按 `inst_id` 拆分上述交易与 exit 分布 |

`--json` 输出结构化结果，便于以后对比不同参数组（**但本任务不做参数扫描**）。

## 7. 已知陷阱（本代码库特有，都真实发生过）

**① 替换 `loop.client` 时必须同步 `loop.risk.client` 和 `loop.committee.client`**

`RiskManager` 和 `Committee` 各自持有自己的 client 引用。只换 `loop.client` 会让风控层
继续用原来的客户端——如果原来是 `PaperClient`，它会**去打真实 OKX 网络**。
这个坑在 `test_loop_replay.py` 和 `test_tick.py` 里已修，**`test_loop_data.py` 里还在**
（表现为测试结果随行情漂移、时而通过时而失败）。

**② 不要模仿 `test_loop_data.FakeLiveClient.get_pending_stop_losses` 的写法**

它是 `self._sl_results.pop(0)` 的一次性队列。巡检每轮会多次调用该方法，队列语义会把
正确答案发给错误的调用者，造成"保护单重复补挂"的假象。**stub 要返回稳定值**
（真实 client 每次调用返回同一份列表）。

**③ 本机 `okx_trader/okx_config.py` 会覆盖 `config.py` 的 `_DEFAULTS`**

例：`_DEFAULTS["MAX_TOTAL_LEVERAGE"] = 3.0`，而某台机器的本地配置是 `20.0`。
**回测必须显式构造 cfg 并打印实际生效的全部风控参数**，不能依赖环境里的配置文件，
否则同一段代码在两台机器上给出不同结果且无从察觉。报告开头要打印参数快照。

**④ R8 现在是 fail-closed**

`trades.risk_usdt` 为 NULL 时 R8 会**拒绝开仓**（这是刻意的）。回测里 `open_trade_row`
必须把 `sized["risk_usdt"]` 写进去，否则第二笔同向交易起就全被拒，你会误以为策略不出手。

**⑤ `sr_levels` 可能返回空的一侧**

价格跌破全部已识别支撑后，`supports` 为空。R7 有 `TARGET_ATR_MULT` 兜底所以能算出目标，
但**基线裁判 `_baseline_judge` 看到的是原始 `sr` 列表**。若你发现空头提案被大量否决，
先确认是不是这个原因，**如实记录在报告里，不要为了让回测好看而改判定逻辑**。

**⑥ 因子采集会让库膨胀**

每轮每标的写约 11 行 `factor_obs`。2 年 1H × 3 标的 ≈ 57 万行。加 `--no-factors` 开关
（默认关闭采集）：回测只要交易统计，不需要 IC 面板。

**⑦ 提交纪律**

- 生产部署与仓库必须一致（`/opt/okx-trader` 里 `git status` 要干净），
  历史上出现过"手工把改动贴到生产"导致无法追溯
- 不要提交 `okx_trader/backtest/cache/`（加进 `.gitignore`）
- 不要提交 `.db` 文件

## 8. 验收标准

全部满足才算完成：

| # | 判据 | 怎么验 |
|---|---|---|
| 1 | 现有测试不回归 | `python -m unittest discover -s okx_trader/tests -t .` 全绿（当前基线 125 项，`test_loop_data` 的 stub 队列语义与 `risk.client` 同步已在 review-7 修复，不再是已知失败） |
| 2 | 前视保护 | 单测：游标在第 i 根时 `get_candles` 返回的最大 ts < `script[i]["ts"]` |
| 3 | 盘中触发 | 单测：构造一根"收盘价未破止损但最低价破了"的 bar，断言产生 `exit_reason='stop'`；同一根同时含止损与止盈时断言判为 `stop` |
| 4 | 虚拟时钟 | 单测：注入固定时钟，断言 `trades.opened_ts` 等于注入值；构造持有超过 `MAX_HOLD_BARS` 根且 \|R\|<0.3 的场景，断言产生 `exit_reason='time_stop'` |
| 5 | 成交模型 | 单测：`touch` / `strict` / `always` 三种模型在同一根 bar 上给出预期不同的成交结果 |
| 6 | 确定性 | 同一份缓存 K 线跑两次，报告 JSON **逐字节相同** |
| 7 | 端到端 | `okxt backtest` 在 ≥3 个月真实 1H 数据上跑通，输出第 6.5 节全部字段，且 `exit_reason` 四类都出现过至少一次 |
| 8 | 参数快照 | 报告开头打印实际生效的 R1–R8 参数与 `fill_model`，与 `_DEFAULTS` 不一致的项要标出 |

## 9. 交付

1. 新增：`okx_trader/backtest/{__init__,data,runner,report}.py`、
   `okx_trader/tests/test_backtest.py`
2. 修改：`replay.py`（三处扩展）、`loop.py` + `exits.py`（时钟注入）、`cli.py`（两个子命令）、
   `.gitignore`（cache 与 db）
3. 一个提交，提交信息里列明：改了哪些既有文件、时钟注入涉及多少个调用点、
   已知偏差（成交模型的乐观方向、`sr_levels` 空侧的影响）
4. 在 `docs/` 下补一页 `backtest.md`：怎么抓数据、怎么跑、报告各字段含义、
   以及**明确写出"回测只覆盖机械路径，不代表 LLM 路径表现"**

## 10. 完成后不要做的事

拿到 `exit_reason` 分布后**不要顺手调参**。分布交回给我/项目负责人，
参数调整是基于分布的独立决策，需要单独评审。本任务的产出是**测量工具和第一份测量结果**，
不是参数优化。



