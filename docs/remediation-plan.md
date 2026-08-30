# 整改实施方案（Review #6 后）

## 背景

系统当前状态：54 轮认知、1 笔浮盈持仓（SOL long +1.34R）、**0 笔已平仓交易**。
架构完整、风控可审计，但盈利能力零证据。本方案只做两件事：

1. 把"硬层"里剩下的正确性缺陷修掉——它们让本该生效的规则实际没生效；
2. 建立能回答"LLM 层到底值不值"的对照实验，然后不干预地攒样本。

**不包含策略参数调整。** 在 0 笔已平仓样本上调 `MIN_RR` / `ATR_STOP_MULT` /
`SCORE_THRESHOLD` 是给没测量过的东西抛光。

## 已完成，不要重复做

| 项 | 落地位置 | 备注 |
|---|---|---|
| python-okx 签名守卫 | `okx_trader/tests/test_client_signatures.py` | 已上线即抓到 `funding_rate_history` 返回 dict 行的真实 bug（`de89c3e`） |
| 审计链归因 | `exits.open_trade_row(open_round_pk=...)`，`loop.py:470-495` | 补记路径改为指向入场单所属轮次，不再指向巡检轮 |
| 裁判 RR 双口径 | `committee.py:148-151` 裁判提示词 | 明确要求"近位 RR 与远位 RR 都看"，与 R7 的远位口径并存而非互斥 |
| 幻觉核对改为只记录 | `committee.py` `_score_and_aggregate` | 不再扣分；曾导致 4 轮误拒 |
| 巡检恢复计划字段 | `loop.py:476-490` | 从 `risk_verdicts` 回补 `risk_usdt`/`stop`/`target` |

## 优先级总览

| 序 | 动作 | 类型 | 阻塞关系 |
|---|---|---|---|
| P0-1 | 硬规则缺数据一律 fail-closed（R8 为首） | 正确性 | 无 |
| P0-2 | 孤儿成交仓位必须重跑 R1–R8，不过即平 | 正确性 | 无 |
| P1-3 | 无 LLM 对照影子盘 | 对照实验 | 无 |
| P1-4 | 反馈回路最小样本闸门 | 设计修正 | 无 |
| P2-5 | 委员会分化 or 做减法 | 架构决策 | **必须等 P1-3 出结论** |
| P2-6 | 不干预攒样本 + 监控解读预案 | 运营 | 等 P0 全部完成 |

---

## P0-1 · 硬规则缺数据一律 fail-closed

### 证据

`risk.py:350-363` 的 `_same_dir_open_risk` 有三条返回 0 的路径：

```python
if store is None:
    return 0.0                                   # ① 无持久层
row = store.query("SELECT COALESCE(SUM(risk_usdt),0) s FROM trades ...")
return float(row[0]["s"] or 0)                   # ② risk_usdt 全为 NULL
except Exception:
    return 0.0                                   # ③ 查询失败
```

返回 0 意味着"同向已用风险为零" → R8 的 `remaining_frac = cap - 0` 永远等于上限 →
**这一票从未投过**。系统唯一一笔持仓开仓时 R8 恰好走在路径 ②（`risk_usdt` 当时为
NULL），而唯一那张反对票的核心理由正是"已持 BTC 多头，再加 SOL 叠加高相关 beta"。
方向做对了是运气，闸门没生效是事实。

### 改法

**原则：任何 R 规则的输入缺失/不可信，一律按最保守侧处理，而不是按"没有约束"处理。**

1. `_same_dir_open_risk` 改为返回 `(used_usdt, ok)` 二元组：
   - 正常聚合成功 → `(值, True)`
   - `store is None` / 查询异常 → `(None, False)`
   - 聚合成功但**存在 `status='open' AND risk_usdt IS NULL` 的行** → `(None, False)`
     （有未平仓位但风险不可知，比"没有仓位"危险得多）
2. R8 分支：`ok=False` 时**直接 `v.fail("R8: 同向风险不可计算（存在 risk_usdt 缺失的
   未平仓位），按 fail-closed 拒绝开仓")`**，并写 `app_events(kind='rule_fail_closed')`。
3. 例外：`store is None` 只出现在测试 stub 场景。为不破坏既有测试，允许配置
   `RISK_ALLOW_NO_STORE = False`（默认 False = 生产 fail-closed），测试显式置 True。
4. **同批审计其余规则的缺数据分支**，逐条确认默认值取保守侧：
   - R3 `equity <= 0` → 已 `return v.fail`，正确
   - R4 `get_pending_orders()` 抛异常 → 现状需确认；应视为"可能有挂单"→ 拒绝
   - R5 `get_rung()` 读不到 → 应取**最高档**而非 0 档
   - R7 `atr` 为 None → 应拒绝（现状是跳过目标计算）
   逐条在 `risk.py` 里标注 `# fail-closed:` 注释，便于后续审查。

### 验收

`okx_trader/tests/test_risk.py` 新增 4 例：

| 用例 | 期望 |
|---|---|
| 存在 `status='open'` 且 `risk_usdt IS NULL` 的仓位 | `passed=False`，`rule_code='R8'`，failure 文案含"fail-closed" |
| 同向仓位 `risk_usdt` 齐全且已用 1.5% | 本笔预算被压缩到剩余 0.5%（既有行为不变） |
| `store` 查询抛异常 | `passed=False` |
| `RISK_ALLOW_NO_STORE=True` + `store=None` | `passed=True`（测试路径保持可用） |

`app_events` 里应能查到 `kind='rule_fail_closed'` 记录。

### 风险

拒绝率会上升。生产库里那笔 SOL 的 `risk_usdt` 已回填（969.6），所以**不会立刻卡死**；
但任何未来的补记路径若漏填 `risk_usdt`，下一次开仓会被拒——这正是想要的行为，
且 `app_events` 会指名原因。

---

## P0-2 · 孤儿成交仓位必须重跑 R1–R8，不过即平

### 证据

系统唯一一笔持仓的完整时间线：

```
02:53:24  round 27 批准（stop 103.56 / target 107.825 / rr 1.77 / risk 969.61）
02:55:42  post_only 入场单 @105.1 挂出
          → round 27 以 status=error 结束，挂单未在 ORDER_TIMEOUT_SEC(90s) 被撤
~03:0x    交易所成交 @105.06（孤儿单自行成交）
03:08:24  巡检发现裸仓，补挂止损 @103.8
05:44:05  才建 trades 行
```

`loop.py:544` 已有"撤残留孤儿**挂单**"的逻辑，但对**已经成交**的孤儿单没有任何
风控复核——`loop.py:470-495` 直接 `open_trade_row()` 收养。收养 = 事后追认一个
未经完整闸门的仓位：批准它的裁决在成交时已过期约 3 小时，市场可能已经反向。

### 改法

分两处，缺一不可。

**(a) 认知轮非成功退出时清理本轮挂单**（堵住源头）

`_execute_open` 及其调用链外层包 `try/finally`，`finally` 中：

```python
# fail-closed：本轮任何非正常结束，都不能留下未成交的入场单
for o in self.store.query(
        "SELECT exch_ord_id, inst_id FROM orders WHERE round_pk=? "
        "AND kind='entry' AND state IN ('live','partially_filled')", (rw.pk,)):
    try:
        self.client.cancel_order(o["inst_id"], o["exch_ord_id"])
        w.write_event(self.store, self.env.name, "round_cleanup",
                      f"轮次异常结束，撤销残留入场单 {o['exch_ord_id']}", level="warn")
    except OKXAPIError as e:
        # 撤单失败通常意味着刚好成交 —— 交给 (b) 的复核路径
        self.log.warning("残留单撤销失败（可能已成交）：%s", e)
```

**(b) 巡检收养前重跑风控**（堵住漏网）

`_patrol_positions` 里发现"交易所有仓位但库里无 open trade"时，在 `open_trade_row()`
之前插入一次全量复核：

```python
plan = {"instId": inst_id, "direction": p["direction"],
        "stop_loss": recovered_stop, "order_type": "limit_maker",
        "entry_hint": p["avg_px"], "factors": {...当前因子快照...}}
verdict = self.risk.check_open_plan(plan, revalidate=True)
```

`revalidate=True` 需要在 `check_open_plan` 里跳过 **R4 同标的查重**（这个仓位已经
存在，否则必然自我否决）和 **R6 Maker 检查**（已成交），其余 R1/R2/R3/R5/R7/R8 全跑。

- **通过** → 正常 `open_trade_row()` 收养，写 `app_events(kind='orphan_adopted')`
- **不通过** → `close_position_market()` 平掉 + 撤保护单，写
  `app_events(level='critical', kind='orphan_rejected')` + 触发飞书告警，
  message 里带上失败的 `rule_code` 和 `first_failure`

平仓失败时不要静默：连续两个 tick 平不掉就把 `run_state.paused=1` 并告警，等人工。

### 验收

`okx_trader/tests/test_loop_replay.py` 新增 3 例（全部走 `ReplayClient`）：

| 用例 | 期望 |
|---|---|
| 轮次在挂单后抛异常 | `finally` 撤掉入场单；`orders.state='canceled'`；有 `round_cleanup` 事件 |
| 孤儿单已成交且当前数据仍过 R1–R8 | 收养成功，`trades.open_round_pk` = 入场单所属轮，有 `orphan_adopted` 事件 |
| 孤儿单已成交但当前止损距离已超 `MAX_STOP_DIST_PCT` | 调用了 `close_position_market`，有 `orphan_rejected` critical 事件，**不建 trades 行** |

### 风险

`revalidate` 路径会平掉仓位——这是真实的资金动作（模拟盘）。务必先在 replay 上验证
三个用例，再上生产。另外 `check_open_plan` 加 `revalidate` 参数会改变一个被 22 项
测试覆盖的函数签名，用关键字默认参数（`revalidate=False`）保证既有调用不变。

---

## P1-3 · 无 LLM 对照影子盘

### 为什么这是性价比最高的一件事

因子是代码算的，人设只是在机械信号上做一层模糊过滤。所以"LLM 层到底加了什么值"
目前无法回答——学习层度量因子 IC，但没有任何东西度量"委员会通过"与"纯机械规则触发"
的差异。在回答这个问题之前投入精力去分化人设提示词，是本末倒置。

**成本比看起来低一个数量级**：确定性路径已经在代码里了。
`committee.py:550 _baseline_analyst`、`_baseline_judge`，以及 `llm.available` 的三处
分支（`committee.py:384/469/509`）——`LLM_ENDPOINTS` 留空即走基线，不需要新写决策代码。

### 改法

不改主实例，另起一个并行实例：

```
/opt/okx-trader-shadow/          # 同一个 git checkout（或 worktree）
  okx_trader/okx_config.py       # 与主实例的差异见下表
  okx_trader/data/trader.db      # 独立库
```

| 配置 | 主实例 | 影子实例 |
|---|---|---|
| `LLM_ENDPOINTS` / `LLM_ROUTES` | 已配 | **留空**（→ `llm.available=False` → 基线路径） |
| `WEB_PORT` | 8787 | 8788 |
| `TRADING_ENV` | `demo` | `demo`（同一模拟盘账户会互相干扰 → 见下） |
| `SYMBOLS` | BTC/ETH/SOL | 同 |
| R1–R8 全部参数 | — | **必须逐项相同**，否则对照无效 |

**账户冲突问题**：两个实例操作同一个 OKX 模拟盘账户会互相看到对方的持仓，
R4/R8 会交叉干扰，对照就废了。两个选项：

- **推荐**：影子实例用 `TRADING_ENV=paper`（真实行情 + 虚拟账户，不发单）。
  代价是拿不到真实成交/滑点，但对照的目标是**决策分叉**而不是执行质量。
- 或者申请第二个模拟盘 API Key，两个实例各用一个账户。工作量大，收益仅限于
  多一份执行数据，不值得先做。

### 要记录什么

新建 `okx_trader/store/compare.py` + `okxt compare` 子命令，按 `bar_ts` 对齐两个库：

```sql
-- 决策分叉表（离线生成，不进主库）
round_ts | inst | llm_action | llm_analyst | llm_score | baseline_action | 分叉类型
```

分叉类型四象限：

| | 基线开仓 | 基线弃权 |
|---|---|---|
| **LLM 开仓** | 一致（LLM 无增无减） | LLM 独有（增益 or 噪音） |
| **LLM 弃权** | **LLM 过滤掉了**（核心问题：过滤对了还是错了） | 一致 |

关键指标：**"LLM 弃权 / 基线开仓"这一格里的机会，事后 24 小时的收益分布**。
如果这些被过滤掉的机会平均是赚的，LLM 层是减益；如果平均是亏的，它在做有用的过滤。

### 验收

- 影子实例连续运行 ≥ 7 天且 `data_ok` 比例与主实例一致（证明不是数据问题导致分叉）
- `okxt compare` 能输出四象限计数和"被过滤机会"的 24h 前向收益均值
- 主实例吞吐不受影响（两个进程各自独立，不共享 SQLite 写者）

### 风险

影子实例用 `paper` 环境意味着它的"成交"是理想化的（无滑点、无排队）。
读结论时只看**决策分叉**，不要拿它的净值和主实例比。

---

## P1-4 · 反馈回路最小样本闸门

### 证据

`committee.py:412 recent_rounds_summary` 已 JOIN `trades`，`committee.py:460` 会拼出
"按人设战绩"喂进提示词。但目前**已平仓交易 = 0**，唯一那笔是浮盈——喂进去的是单笔
噪声，LLM 会围绕唯一一笔持仓自我强化。

### 改法

1. 战绩统计**只统计 `status='closed'` 且 `r_multiple IS NOT NULL`** 的行（现状需确认）。
2. 新增 `MIN_TRADES_FOR_STATS = 10`（写入 `config.py` `_DEFAULTS`）。
   已平仓样本数 `< MIN_TRADES_FOR_STATS` 时，`recent_rounds_summary` **不输出任何
   战绩段**，改为一行显式声明：

   ```
   按人设战绩：样本不足（已平仓 3 笔 < 10），本轮不提供历史绩效参考
   ```

   说明"没有"比省略更好——否则模型会以为你忘了给。
3. 近期轮次列表（提了什么、状态）不受闸门限制，继续喂——那是事实记录，不是统计推断。

### 验收

`test_factors_zoo.py`（记忆回路那组）新增 2 例：

| 用例 | 期望 |
|---|---|
| 已平仓 3 笔 | 摘要含"样本不足"，**不含** `R` 累计值 |
| 已平仓 12 笔 | 摘要含按人设的胜率与累计 R |

---

## P2-5 · 委员会：分化 or 做减法（等 P1-3 出结论）

### 现状数据

```
均值回归者   open  0 / hold 41      资金哨兵   open 0 / hold 41
趋势猎手     open 18 / hold 23
技术裁判     18 次通过 18，打分区间 [7.0, 8.0]
风控裁判     17 次通过 17，打分区间 [7.0, 8.0]
资金管理裁判 18 次通过 10，打分区间 [4.0, 8.0]   ← 唯一有区分度的
```

`llm_calls` 显示：三个裁判角色各有 11 次由**同一个 `glm-5.3-flash`** 应答且全部成功；
`deepseek-v4-pro` 在所有角色上 `ok=0`（每次失败）。所以"三裁判"在实际运行中
**就是一个模型答了三遍**——回退链存在，但只有一个后端真的能用。同模型换人设，
错误是相关的，"过半通过"不是多数决。

注意：`avg ≥ 6.5` 这道闸门**确实绑定过**（4 轮 `votes=2/3` 过半但 `avg=4.67` 被拒），
不是从未生效——只是那 4 次是幻觉误报造成的，已修。

### 三个待验证问题，按顺序做

**(a) 弃权原因落库**（低成本，先做）

`proposals` 加两列：`abstain_reason_code TEXT`（`condition_unmet` / `held` /
`data_missing` / `model_declined`）、`abstain_detail TEXT`。人设提示词要求弃权时
输出 `{"action":"hold","reason_code":"...","reason":"..."}`。

目的：区分"条件确实没到"和"条件到了但模型不开口"。均值回归者 0/41 很可能不是
bug——1H 上 RSI≥70 同时触上轨本来一周就几次。但现在无法证明。

**(b) 裁判对照实验**（判断橡皮章）

写 `okxt judge-probe` 一次性脚本：构造 5 份**带明显缺陷**的假提案喂给三个裁判，
不入库、只打印分数。缺陷样本：

| 假提案 | 应该被扣到几分 |
|---|---|
| 止损距离 8%（超 `MAX_STOP_DIST_PCT`） | ≤ 3 |
| 盈亏比 0.9（低于 `MIN_RR`） | ≤ 4 |
| 做多但 reason 通篇论证看跌 | ≤ 2 |
| 引用完全不存在的价位 | ≤ 3 |
| 正常提案（对照） | ≥ 7 |

技术裁判和风控裁判如果对前四份仍给 7~8 分，**闸门就是假的**，进入 (c)。

**(c) 二选一，不要维持现状**

- **分化**：裁判提示词改为强制给出区分度（明确"无缺陷 7-8 / 有隐患 4-6 / 危险 0-3"
  并要求指名具体缺陷才能给高分），且**至少一个裁判换异构后端**——前提是先修好
  `deepseek-v4-pro` 那条链（`ok=0` 说明它根本没工作）。
- **做减法**：承认是"一分析师 + 一裁判"，砍掉技术/风控两个裁判，`SCORE_THRESHOLD`
  按单裁判重标定（当前有效否决线是"资管给分 < 4.5"），省 4 次 LLM 调用与故障面。

维持现状是最贵的选项：付 6 次调用的延迟、成本和故障面（孤儿单恰好出在一轮 error 里），
买不到审议本身。

**决策依据**：P1-3 影子盘若显示 LLM 层无增益甚至减益 → 直接走"做减法"，甚至连
分析师层一起简化。所以这一项必须排在影子盘之后。

---

## P2-6 · 不干预攒样本 + 监控解读预案

### 统计现实（必须提前接受）

止损 1.2–1.4%、目标 2–2.6% → RR ≈ 1.43–2.17 → **毛盈亏平衡胜率 31.5%–41.2%**
（典型 RR 1.8 对应 35.7%）。所以：胜率 35% 只是保本，40% 才有正期望（≈+0.12R/笔），
45% 才谈得上稳定盈利（≈+0.26R/笔）。在 1H 双均线+MACD 这种慢信号上，40%+ 不是白送的。

样本量：n=10–15 时即使真实胜率 55–60% 也到不了 5% 显著；n≈20–30 刚过临界；
要按"45% vs 盈亏平衡 35.7%"检出真实 edge，量级在 **n≈200**。按当前吞吐（54 轮 1 笔）
是几个月的事。

**明确否决"并行多实例凑样本"**：多个实例跑同一市场、同一批 1H K 线、同一套因子，
样本高度相关，不是 5 倍独立观测。把它们当独立样本做检验，会重新制造出刚修完的
"重叠窗口不修正导致 t 值虚高"那个完全相同的错误。要加速只有缩短周期（换策略）
或扩标的池（相关性仍高）两条路。

### 分阶段看什么

| 阶段 | 看什么 | 为什么现在就有信息量 |
|---|---|---|
| 前 30 笔 | `exit_reason` 分布、挂单成交率、滑点、OCO 触发延迟、`error` 轮占比 | 都是过程指标，不依赖盈利 |
| 100 笔 | `mean(r_multiple)` 的 t 统计量（Kelly 引擎已在影子模式积累） | 第一次能做推断 |
| 200 笔+ | 才有资格谈"这套系统赚不赚钱" | 之前任何有效性结论都不成立 |

### `exit_reason` 分布的解读预案（提前写死，免得数据来了临时发明结论）

| 观察形态 | 含义 | 对应动作 |
|---|---|---|
| 时间止损占比 > 40% | 目标太远，24h 窗口配不上 2.5×ATR 的目标 | 收窄 `TARGET_ATR_MULT` 或放长 `MAX_HOLD_BARS` |
| 移动止损主导且平均 R 低 | `TRAIL_ATR_MULT=1.0` 太紧，利润跑不出来 | 放宽到 1.5–2.0 |
| 时间止损占比高 + 净值阴跌 | 趋势系统在震荡市的经典死法（当前只剩趋势一个人设，最可能出现） | 需要 regime 层面的开关，不是调参 |
| 止损占比 > 60% | 信号质量问题，不是执行问题 | 回到因子层，看 IC |

### 还该补的一条监控

**"连续 N 笔亏损时系统会做什么"目前没有演练过。** 模拟盘阶段跑到 10% 回撤触发
`flatten + paused=1` 是免费的应急演练机会——真发生时记录：全平是否成功、
保护单是否被正确撤销、`paused` 是否阻止了下一轮开仓、飞书告警是否到达。
别浪费这次机会。

---

## 不做的事（明确记录，避免反复讨论）

| 不做 | 理由 |
|---|---|
| 调 `MIN_RR` / `ATR_STOP_MULT` / `SCORE_THRESHOLD` / `MAX_HOLD_BARS` | 0 笔已平仓，调参是猜 |
| 加新指标因子 | 现有 11 个的 IC 还没测出来；共线性只会抬高假阳性 |
| 随机过程建模 / Hurst / 小波 / HMM / ML | 样本差几个数量级，且都要过同一个闸门 |
| 每轮接搜索引擎 / 新闻情绪 | 无法回放、无法补算 IC、无法过闸门，会毁掉离线验证层 |
| 并行多实例凑样本 | 统计上错误，见 P2-6 |
| 开实盘 | `ALLOW_LIVE_TRADING` 保持 False，直到 200 笔样本给出结论 |

## 验收总表

| 项 | 完成判据 |
|---|---|
| P0-1 | `test_risk.py` 4 例通过；`risk.py` 每个缺数据分支有 `# fail-closed:` 注释 |
| P0-2 | `test_loop_replay.py` 3 例通过；生产运行一周无 `orphan_rejected` 误杀 |
| P1-3 | 影子实例连续 7 天运行；`okxt compare` 输出四象限 + 被过滤机会的 24h 收益均值 |
| P1-4 | 记忆回路 2 例通过；`< 10 笔`时提示词含"样本不足"显式声明 |
| P2-5 | 弃权原因落库；`judge-probe` 结果记录在案；分化或做减法二选一已执行 |
| P2-6 | 30 笔已平仓时按预案表写出第一份 `exit_reason` 分布解读 |

全程保持：`python -m unittest discover -s okx_trader/tests -t .` 全绿、
`compileall` 干净、`/opt/okx-trader` 工作区无未提交改动（线上=仓库）。




