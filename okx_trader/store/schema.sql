-- okx-trader 单文件库。设计规则：面板要筛选、排序、聚合的字段就是真列；其余进 JSON text。
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT NOT NULL UNIQUE,          -- '20260828_232427_001'
  ts REAL NOT NULL,
  env TEXT NOT NULL,                      -- replay|paper|demo|live
  executing INTEGER NOT NULL,             -- 0=只分析 1=会真下单
  llm_mode TEXT NOT NULL,                 -- llm|baseline
  status TEXT NOT NULL,                   -- data_unavailable|no_action|risk_rejected
                                          -- |no_fill|opened|stop_failed_closed|error
  action TEXT, reason TEXT,
  data_ok INTEGER NOT NULL DEFAULT 1,     -- 0 = 全标的因子失败（"没数据"≠"没信号"）
  symbols_ok INTEGER NOT NULL DEFAULT 0, symbols_total INTEGER NOT NULL DEFAULT 0,
  equity REAL, hwm REAL, drawdown REAL, usdt_avail REAL,
  open_positions INTEGER NOT NULL DEFAULT 0, duration_sec REAL, error TEXT,
  round_type TEXT,                        -- cognition|event|evolution
  intent TEXT,                            -- deploy|steady|place|hold
  final_action TEXT,                      -- deploy|steady|place|revise
  regime TEXT,                            -- trending|ranging|high_vol
  advisor_endorsed TEXT,                  -- '2/3'
  revisions INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rounds_ts     ON rounds(ts DESC);
CREATE INDEX IF NOT EXISTS idx_rounds_status ON rounds(status, ts DESC);
CREATE INDEX IF NOT EXISTS idx_rounds_env_ts ON rounds(env, ts DESC);

-- 每轮每标的完整因子快照 —— LLM 实际看到的东西
CREATE TABLE IF NOT EXISTS round_factors (
  round_pk INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
  inst_id TEXT NOT NULL, ok INTEGER NOT NULL, err TEXT,
  bar TEXT, bar_ts INTEGER,               -- 已收盘K线时间戳(ms)
  price REAL, ema20 REAL, ema60 REAL, rsi14 REAL, atr REAL, atr_pct REAL,
  macd_dif REAL, macd_dea REAL, macd_hist REAL, funding_rate REAL, vol_ratio REAL,
  trend TEXT, structure TEXT, price_vs_boll TEXT, pattern TEXT,
  obi REAL, oi REAL, oi_delta_pct REAL, ls_ratio REAL, taker_ratio REAL,
  report_json TEXT NOT NULL,   -- 完整 build_factor_report()：sr/mtf/fvg/patterns…
  report_text TEXT,            -- format_factor_report() 原文 = 字面喂给 LLM 的输入
  PRIMARY KEY (round_pk, inst_id)
);
CREATE INDEX IF NOT EXISTS idx_rf_inst ON round_factors(inst_id, bar_ts DESC);

-- 分析师提案（含弃权，三位全入库）
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_pk INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL,                  -- ANALYSTS 中的序号（含弃权者）
  analyst TEXT NOT NULL, style TEXT, action TEXT NOT NULL,   -- open|hold
  inst_id TEXT, direction TEXT, stop_loss REAL, entry_hint REAL,
  confidence REAL, reason TEXT,
  avg_score REAL, votes_for INTEGER, votes_total INTEGER,
  qualify INTEGER, is_winner INTEGER NOT NULL DEFAULT 0,
  UNIQUE (round_pk, slot)
);
CREATE INDEX IF NOT EXISTS idx_prop_round ON proposals(round_pk);
CREATE INDEX IF NOT EXISTS idx_prop_inst  ON proposals(inst_id, id DESC);

CREATE TABLE IF NOT EXISTS judge_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_pk INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
  proposal_pk INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
  judge TEXT NOT NULL, score REAL, approved INTEGER, concerns TEXT
);
CREATE INDEX IF NOT EXISTS idx_js_round ON judge_scores(round_pk);
CREATE INDEX IF NOT EXISTS idx_js_judge ON judge_scores(judge, id DESC);

CREATE TABLE IF NOT EXISTS risk_verdicts (
  round_pk INTEGER PRIMARY KEY REFERENCES rounds(id) ON DELETE CASCADE,
  proposal_pk INTEGER REFERENCES proposals(id) ON DELETE SET NULL,
  passed INTEGER NOT NULL,
  rule_code TEXT,        -- 'R1'..'R7'，从 failures[0] 抽取 → "为什么从不交易"一行 GROUP BY
  first_failure TEXT,
  failures_json TEXT NOT NULL DEFAULT '[]', warnings_json TEXT NOT NULL DEFAULT '[]',
  inst_id TEXT, direction TEXT, contracts REAL, entry_ref REAL, stop_loss REAL,
  target REAL, rr REAL,
  target_source TEXT,    -- 'structure' | 'atr_multiple' —— R7 的目标从哪来
  notional_usdt REAL, risk_usdt REAL, risk_pct REAL, atr REAL, leverage_after REAL,
  kelly_mult REAL, edge_p REAL, edge_b REAL, kelly_n INTEGER, kelly_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_rv_rule ON risk_verdicts(rule_code, round_pk DESC);

CREATE TABLE IF NOT EXISTS trades (            -- 先建，orders 引用它
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env TEXT NOT NULL, inst_id TEXT NOT NULL, direction TEXT NOT NULL,
  open_round_pk  INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
  close_round_pk INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
  opened_ts REAL NOT NULL, closed_ts REAL,
  contracts REAL NOT NULL, ct_val REAL NOT NULL,
  entry_px REAL NOT NULL, exit_px REAL,
  stop_px REAL, target_px REAL, planned_rr REAL, risk_usdt REAL,
  realized_pnl REAL, fees REAL,
  r_multiple REAL,       -- realized_pnl / risk_usdt —— 策略质量唯一诚实的指标
  exit_reason TEXT,      -- stop|target|trailing|time_stop|manual|emergency|unknown
  analyst TEXT,          -- 胜出提案的作者 → "哪个人设真的赚钱"
  committee_score REAL, status TEXT NOT NULL   -- open|closed
);
CREATE INDEX IF NOT EXISTS idx_trade_status  ON trades(status, opened_ts DESC);
CREATE INDEX IF NOT EXISTS idx_trade_inst    ON trades(inst_id, opened_ts DESC);
CREATE INDEX IF NOT EXISTS idx_trade_analyst ON trades(analyst, closed_ts DESC);

CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_pk INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
  trade_pk INTEGER REFERENCES trades(id) ON DELETE SET NULL,
  env TEXT NOT NULL, inst_id TEXT NOT NULL,
  kind TEXT NOT NULL,        -- entry|protect|exit
  ord_type TEXT NOT NULL,    -- post_only|conditional|oco|market
  exch_ord_id TEXT, exch_algo_id TEXT, cl_ord_id TEXT,
  side TEXT, pos_side TEXT,
  px REAL, sz REAL, sl_trigger_px REAL, tp_trigger_px REAL,
  state TEXT, filled_sz REAL DEFAULT 0, avg_px REAL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL, note TEXT, raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ord_trade ON orders(trade_pk);
CREATE INDEX IF NOT EXISTS idx_ord_state ON orders(state, created_ts DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ord_exch ON orders(env, exch_ord_id)  WHERE exch_ord_id  IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ord_algo ON orders(env, exch_algo_id) WHERE exch_algo_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_pk INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  trade_pk INTEGER REFERENCES trades(id) ON DELETE SET NULL,
  ts REAL NOT NULL, px REAL NOT NULL, sz REAL NOT NULL,
  fee REAL, fee_ccy TEXT, exch_trade_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fill_exch ON fills(exch_trade_id) WHERE exch_trade_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS equity_curve (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  env TEXT NOT NULL, ts REAL NOT NULL,
  round_pk INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
  equity REAL NOT NULL, hwm REAL NOT NULL, drawdown REAL NOT NULL,
  usdt_avail REAL, upl REAL, open_positions INTEGER
);
CREATE INDEX IF NOT EXISTS idx_eq_env_ts ON equity_curve(env, ts);   -- ASC：图表按时间升序读区间
CREATE UNIQUE INDEX IF NOT EXISTS uq_eq_round ON equity_curve(round_pk) WHERE round_pk IS NOT NULL;

CREATE TABLE IF NOT EXISTS run_state (         -- 取代 state_*.json
  env TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_ts REAL NOT NULL,
  PRIMARY KEY (env, key)                      -- 跨环境污染在结构上不可能
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_pk INTEGER REFERENCES rounds(id) ON DELETE CASCADE,
  role TEXT NOT NULL,        -- 'analyst:趋势猎手' | 'judge:风控裁判'
  model TEXT, ok INTEGER NOT NULL, err TEXT,
  latency_ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
  raw_reply TEXT,            -- 原始回复：JSON 解析失败时唯一的线索
  cost_usd REAL              -- 按价目表折算；缺价模型为 NULL（不能拿 0 当已知）
);
CREATE INDEX IF NOT EXISTS idx_llm_round ON llm_calls(round_pk);

CREATE TABLE IF NOT EXISTS app_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, env TEXT NOT NULL,
  level TEXT NOT NULL,       -- info|warn|error|critical
  kind TEXT NOT NULL,        -- data_degraded|circuit_breaker|stop_reattached
                             -- |naked_position|env_switch|login|paused|resumed
  inst_id TEXT, round_pk INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
  message TEXT NOT NULL, detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_kind ON app_events(kind, ts DESC);

-- ── 因子动物园（Phase 7）：unproven edge never touches the book ──────────────

CREATE TABLE IF NOT EXISTS factor_defs (
  name        TEXT PRIMARY KEY,      -- 'rsi14' / 'funding_rate' ...
  family      TEXT NOT NULL,         -- momentum|reversal|breakout|carry|volatility|microstructure
  tier        TEXT NOT NULL,         -- core|derived
  status      TEXT NOT NULL,         -- candidate|observing|trial|active|retired|rejected
  source      TEXT NOT NULL,         -- 'builtin' —— 本期只有内置因子
  created_ts  REAL NOT NULL,
  status_ts   REAL NOT NULL,
  status_note TEXT
);

CREATE TABLE IF NOT EXISTS factor_obs (
  factor     TEXT NOT NULL REFERENCES factor_defs(name) ON DELETE CASCADE,
  inst_id    TEXT NOT NULL,
  bar_ts     INTEGER NOT NULL,       -- 已收盘 K 线时间戳(ms)，对齐的唯一依据
  round_pk   INTEGER REFERENCES rounds(id) ON DELETE SET NULL,
  value      REAL NOT NULL,
  fwd_ret_1b REAL, fwd_ret_4b REAL, fwd_ret_24b REAL,   -- 回填
  filled_ts  REAL,
  PRIMARY KEY (factor, inst_id, bar_ts)                 -- 天然幂等，重跑不产生重复
);
CREATE INDEX IF NOT EXISTS idx_fo_pending ON factor_obs(factor, filled_ts) WHERE filled_ts IS NULL;
CREATE INDEX IF NOT EXISTS idx_fo_bar     ON factor_obs(bar_ts);

CREATE TABLE IF NOT EXISTS factor_scores (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  factor       TEXT NOT NULL REFERENCES factor_defs(name) ON DELETE CASCADE,
  horizon      TEXT NOT NULL,        -- '1b'|'4b'|'24b'
  computed_ts  REAL NOT NULL,
  n_obs        INTEGER NOT NULL,     -- 参与计算的观测数
  n_eff        REAL,                 -- 重叠修正后的有效样本量
  scored_days  INTEGER NOT NULL,     -- 有前向收益的自然日数
  days_tracked INTEGER NOT NULL,     -- 从 created_ts 起的自然日数
  ic           REAL,                 -- Pearson(value, fwd_ret)
  rank_ic      REAL,                 -- Spearman
  ic_t         REAL,                 -- ic * sqrt(n_eff - 2) / sqrt(1 - ic^2)
  hit_rate     REAL,                 -- sign(value)==sign(fwd_ret) 的占比
  gate_passed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fs_latest ON factor_scores(factor, horizon, computed_ts DESC);
