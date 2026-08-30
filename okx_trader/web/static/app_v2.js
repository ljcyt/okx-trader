/* okx-trader 单页面板 · 无框架 · 所有插值一律过 esc()（LLM reason 是不可信文本） */
"use strict";

const $ = sel => document.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = (v, d = 2) => (v === null || v === undefined) ? "—" :
  Number(v).toLocaleString("zh-CN", { maximumFractionDigits: d });
const pct = v => (v === null || v === undefined) ? "—" : (v * 100).toFixed(2) + "%";
const ts = v => v ? new Date(v * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";

async function api(path, opts = {}) {
  const resp = await fetch(path, Object.assign({ headers: {} }, opts));
  if (resp.status === 401) { location.hash = "#/login"; throw new Error("unauthenticated"); }
  const data = await resp.json();
  if (!data.ok && !data.authed) throw new Error(data.error || resp.status);
  return data;
}
const post = path => api(path, { method: "POST" });

let LIMITS = {};

/* ── 术语 → 人话（状态、出场原因都翻成大白话）─────────────────── */
const STATUS_CN = {
  running: "进行中", steady: "不交易", no_action: "观望",
  opened: "已开仓", deploy: "已下单", place: "准备下单",
  planned: "已挂单", working: "挂单中", no_fill: "未成交",
  risk_rejected: "风控未过", data_unavailable: "行情中断", error: "异常",
  open: "持仓中", closed: "已平仓", stop: "止损离场", target: "止盈离场",
  time_stop: "到时离场", stop_failed_closed: "强制平仓",
};
const cn = s => STATUS_CN[s] || s;
const chipCN = s => `<span class="chip ${esc(s)}">${esc(cn(s))}</span>`;

/* ── 登录 ─────────────────────────────────────────────── */
let LOGIN_HTML = null;   // 初始登录表单（renderers 会覆盖 #app，需要能恢复）
function renderLogin(err) {
  if (LOGIN_HTML === null) LOGIN_HTML = $("#app").innerHTML;
  $("#app").innerHTML = LOGIN_HTML;
  $("#login-error").textContent = err || "";
  // 登录接口的 401 是"密码错误"，不能走 api() 的统一 401 跳转，否则真实原因被遮住
  $("#login-form").onsubmit = async e => {
    e.preventDefault();
    try {
      const resp = await fetch("/api/login", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: $("#login-password").value }) });
      const data = await resp.json().catch(() => ({}));
      if (resp.status === 429) {
        $("#login-error").textContent = data.error || "尝试过多，请 60 秒后再试";
        return;
      }
      if (resp.status !== 200 || !data.ok) {
        $("#login-error").textContent = data.error || "密码错误";
        return;
      }
      location.hash = "#/overview";
      route();
    } catch (ex) { $("#login-error").textContent = "网络错误：" + ex.message; }
  };
}

/* ── 总览 ─────────────────────────────────────────────── */
async function renderOverview() {
  const [state, latest] = await Promise.all([api("/api/state"),
                                             api("/api/rounds?size=1")]);
  LIMITS = state.limits || {};
  const a = state.account, lh = state.data_health;
  const badge = $("#env-badge");
  badge.textContent = state.env.toUpperCase();
  badge.className = "badge " + state.env.toUpperCase();
  $("#exec-badge").textContent = state.executing ? "executing" : "观察模式";
  let html = "";
  if (state.circuit_breaker && state.circuit_breaker.tripped)
    html += `<div class="banner">⛔ 亏得太多触发了保护机制：${esc(state.circuit_breaker.reason)}——暂时不开新仓</div>`;
  if (lh && lh.data_ok === 0)
    html += `<div class="healthbar bad">⚠ 行情数据拿不到——这轮先不讨论，等下一轮再试</div>`;
  html += `<div class="tiles">
    <div class="card tile"><div class="v">${fmt(a.equity)}</div><div class="k">账户权益（USDT）</div></div>
    <div class="card tile"><div class="v ${a.total_return >= 0 ? "up" : "down"}">${a.total_return == null ? "—" : (a.total_return >= 0 ? "+" : "") + fmt(a.total_return)}</div><div class="k">累计赚了（含还没平的）</div></div>
    <div class="card tile"><div class="v ${a.total_upl >= 0 ? "up" : "down"}">${a.total_upl == null ? "—" : (a.total_upl >= 0 ? "+" : "") + fmt(a.total_upl)}</div><div class="k">手里持仓的浮动盈亏</div></div>
    <div class="card tile"><div class="v ${a.realized_pnl >= 0 ? "up" : "down"}">${(a.realized_pnl >= 0 ? "+" : "") + fmt(a.realized_pnl || 0)}</div><div class="k">已经落袋的盈亏</div></div>
    <div class="card tile"><div class="v ${a.drawdown > 0 ? "down" : "up"}">${pct(a.drawdown)}</div><div class="k">离最近高点回撤</div></div>
    <div class="card tile"><div class="v">${(state.positions || []).length}</div><div class="k">正在持有</div></div>
  </div>`;

  // 最新动态：系统每轮在干嘛，说人话
  const lr = (latest.items || [])[0];
  if (lr) {
    const mins = Math.max(0, Math.round(Date.now() / 1000 - lr.ts) / 60);
    const ago = mins < 1 ? "刚刚" : mins < 60 ? `${Math.round(mins)} 分钟前` :
      `${Math.round(mins / 60)} 小时前`;
    html += `<div class="card"><h3>最新动态</h3>
      <div><b>${esc(cn(lr.status))}</b> <span class="muted">· ${ago}开的会（${ts(lr.ts)}）</span></div>
      ${lr.reason ? `<div class="wrap" style="margin-top:6px">${esc(lr.reason)}</div>` : ""}
      <div class="muted" style="margin-top:8px">委员会每小时讨论一次：三位分析师先提案，三位裁判打分，风控把关后才下单。
      <a href="#/rounds/${esc(lr.round_id)}">看这轮的完整讨论 →</a></div></div>`;
  }

  // 风控状态：正常时不占版面，只在降档时冒出来说明
  const rung = state.dd_rung || {level: 0, ladder: []};
  if (rung.level > 0) {
    const mult = rung.ladder && rung.ladder[rung.level - 1]
      ? rung.ladder[rung.level - 1].risk_mult : null;
    html += `<div class="healthbar bad">⚠ 最近亏得有点多，风控自动收缩：每笔投入降到原来的 ${pct(mult ?? 1)}</div>`;
  }

  html += `<div class="card"><h3>当前持仓</h3>`;
  const pos = state.positions || [];
  if (!pos.length) html += `<div class="muted">空仓中——委员会没找到值得出手的机会，等着就好</div>`;
  else {
    html += `<table><thead><tr><th>标的</th><th>方向</th><th>数量</th><th>买入价</th>
      <th>现价</th><th>浮动盈亏</th></tr></thead><tbody>`;
    for (const p of pos)
      html += `<tr><td>${esc(p.instId)}</td><td>${p.direction === "long" ? "做多" : "做空"}</td>
        <td class="num">${fmt(p.contracts)}</td><td class="num">${fmt(p.avg_px)}</td>
        <td class="num">${fmt(p.mark_px)}</td>
        <td class="num ${p.upl >= 0 ? "up" : "down"}">${fmt(p.upl)}</td></tr>`;
    html += `</tbody></table>`;
    html += `<div class="muted" style="margin-top:8px">每笔持仓在交易所都挂了止损单，跌破自动离场，不需要人盯着。</div>`;
  }
  html += `</div>`;

  // 参考信息默认折叠，总览只留决策相关内容
  html += `<details class="card"><summary>风控限额（只读）</summary><div class="grid3 mono" style="margin-top:10px">`;
  for (const [k, v] of Object.entries(LIMITS))
    html += `<div>${esc(k)} = ${esc(JSON.stringify(v))}</div>`;
  html += `</div></details>`;
  $("#app").innerHTML = html;
  $("#pause-btn").textContent = state.loop.paused ? "▶ 恢复" : "⏸ 暂停";
}

/* ── 权益曲线（内联 SVG，零依赖）──────────────────────── */
async function renderEquity() {
  const data = await api("/api/equity");
  const pts = data.points || [];
  let html = `<div class="card"><h3>权益曲线（${pts.length} 个点）</h3>`;
  if (pts.length < 2) { html += `<div class="muted">数据不足（需要 ≥2 轮）</div></div>`; }
  else {
    const W = 1100, H = 320, PL = 60, PR = 10, PT = 10, PB = 24;
    const xs = pts.map(p => p[0]), es = pts.map(p => p[1]), hs = pts.map(p => p[2]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const ymin = Math.min(...es, ...hs), ymax = Math.max(...es, ...hs);
    const pad = (ymax - ymin) * 0.08 || 1;
    const X = t => PL + (t - x0) / ((x1 - x0) || 1) * (W - PL - PR);
    const Y = v => PT + (1 - (v - (ymin - pad)) / ((ymax + pad) - (ymin - pad))) * (H - PT - PB);
    const line = arr => arr.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    const eqPath = line(pts);
    const hwmPath = line(pts.map(p => [p[0], p[2]]));
    const ddArea = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`)
      .join("") + pts.slice().reverse().map(p => `L${X(p[0]).toFixed(1)},${Y(p[2]).toFixed(1)}`).join("") + "Z";
    let grid = "", labels = "";
    for (let i = 0; i <= 4; i++) {
      const v = ymin - pad + (ymax + pad - (ymin - pad)) * i / 4;
      grid += `<line x1="${PL}" x2="${W - PR}" y1="${Y(v)}" y2="${Y(v)}" stroke="rgba(16,24,40,0.07)"/>`;
      labels += `<text x="${PL - 6}" y="${Y(v) + 4}" fill="#8B92A3" font-size="11" text-anchor="end">${fmt(v, 0)}</text>`;
    }
    html += `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      ${grid}${labels}
      <path d="${ddArea}" fill="rgba(220,61,67,0.07)"/>
      <path d="${hwmPath}" fill="none" stroke="#98A1B2" stroke-width="1" stroke-dasharray="4 3"/>
      <path d="${eqPath}" fill="none" stroke="#2F5FE3" stroke-width="1.6"/>
      <text x="${PL}" y="${H - 6}" fill="#8B92A3" font-size="11">${ts(x0)}</text>
      <text x="${W - PR}" y="${H - 6}" fill="#8B92A3" font-size="11" text-anchor="end">${ts(x1)}</text>
    </svg>
    <div class="muted">蓝=权益 · 灰虚=高水位 · 红域=回撤</div></div>`;
  }
  $("#app").innerHTML = html;
}

/* ── 决策历史 ─────────────────────────────────────────── */
async function renderRounds(params) {
  const qs = new URLSearchParams({ page: params.page || 1, size: 15,
    status: params.status || "", inst: params.inst || "" });
  for (const [k, v] of Object.entries(qs)) if (!v) qs.delete(k);
  const data = await api("/api/rounds?" + qs.toString());
  let html = `<div class="card"><h3>决策历史（${data.total} 轮）</h3>
    <div class="filters">
      <select id="f-status"><option value="">全部状态</option></select>
      <select id="f-inst"><option value="">全部标的</option></select>
      <button class="ghost" id="f-go">筛选</button>
    </div>
    <table><thead><tr><th>时间</th><th>这轮结果</th><th>谁提议的</th><th>风控</th></tr></thead><tbody>`;
  for (const it of data.items) {
    const wnr = it.winner;
    const win = wnr ? `${esc(wnr.analyst)} 建议买${wnr.direction === "long" ? "入" : "出"} ${esc((wnr.inst_id || "").replace("-SWAP", ""))}` : `<span class="muted">没人提议，继续等</span>`;
    html += `<tr onclick="location.hash='#/rounds/${esc(it.round_id)}'">
      <td>${ts(it.ts)}</td>
      <td>${chipCN(it.status)}</td>
      <td>${win}</td>
      <td>${it.risk ? (it.risk.passed ? '<span class="chip opened">过关，下单</span>' : '<span class="chip risk_rejected">没过关</span>') : '<span class="muted">—</span>'}</td></tr>`;
  }
  html += `</tbody></table>
    <div class="pager">
      <button class="ghost" ${data.page <= 1 ? "disabled" : ""} onclick="location.hash='#/rounds?${qs}&page=${data.page - 1}'">‹ 上一页</button>
      第 ${data.page} / ${Math.max(1, Math.ceil(data.total / data.size))} 页
      <button class="ghost" ${data.page >= Math.ceil(data.total / data.size) ? "disabled" : ""} onclick="location.hash='#/rounds?${qs}&page=${data.page + 1}'">下一页 ›</button>
    </div></div>`;
  $("#app").innerHTML = html;
  const st = $("#f-status"), inst = $("#f-inst");
  for (const [v, label] of [["", "全部结果"], ["no_action", "观望"], ["opened", "已开仓"],
                            ["planned", "已挂单"], ["risk_rejected", "风控没过"],
                            ["no_fill", "没成交"], ["error", "异常"],
                            ["data_unavailable", "行情中断"]])
    st.add(new Option(label, v));
  for (const s of (LIMITS.SYMBOLS || []))
    inst.add(new Option(s.replace("-SWAP", ""), s));
  st.value = params.status || ""; inst.value = params.inst || "";
  $("#f-go").onclick = () => {
    const p = new URLSearchParams({ status: st.value, inst: inst.value, page: 1 });
    for (const [k, v] of Object.entries(p)) if (!v) p.delete(k);
    location.hash = "#/rounds" + (p.toString() ? "?" + p : "");
  };
}

const R7_NOTES = {
  R1: "无止损 / 止损方向错误 / 计划已过期", R2: "止损距离不合理（太近或太远）",
  R3: "仓位与风险预算问题", R4: "组合约束（已有持仓/挂单、数量或总杠杆上限）",
  R5: "回撤熔断生效", R6: "非 Maker 单", R7: "目标空间相对止损距离太小（盈亏比不足）",
};

/* ── 轮次详情：六步漏斗 ───────────────────────────────── */
async function renderRoundDetail(rid) {
  const d = await api("/api/rounds/" + encodeURIComponent(rid));
  const rd = d.round;
  let html = `<div class="card"><h3>这一轮的讨论
    ${chipCN(rd.status)}</h3>
    <div class="muted">${ts(rd.ts)} · ${esc(rd.env) === "demo" ? "模拟盘" : esc(rd.env)} ·
      权益 ${fmt(rd.equity)} · 回撤 ${pct(rd.drawdown)}</div>
    ${rd.reason ? `<div class="wrap" style="margin-top:8px">${esc(rd.reason)}</div>` : ""}</div>`;

  html += `<div class="step"><h4>Step 0 · 输入（因子快照 = 分析师实际看到的数字）</h4><div class="grid3">`;
  for (const f of d.factors) {
    html += f.ok ? `<div class="card"><b>${esc(f.inst_id)}</b>
      <div>价格 ${fmt(f.price)} · RSI ${fmt(f.rsi14, 1)} · ATR ${fmt(f.atr)}（${pct(f.atr_pct)}）</div>
      <div>${esc(f.trend)} / ${esc(f.structure)}</div>
      <div>资金费率 ${pct(f.funding_rate)} · 末根形态 ${esc(f.pattern)}</div>
      <details><summary>逐字因子报告（喂给 LLM 的原文）</summary><pre>${esc(f.report_text)}</pre></details>
      <details><summary>完整 JSON（支撑阻力/多周期/FVG/OI/订单簿）</summary><pre>${esc(JSON.stringify(JSON.parse(f.report_json || "{}"), null, 1))}</pre></details>
      </div>`
      : `<div class="card"><b>${esc(f.inst_id)}</b> <span class="chip data_unavailable">失败</span>
         <div class="error">${esc(f.err || "")}</div>
         ${f.report_json && JSON.parse(f.report_json || "{}")._partial ? '<div class="muted">本轮完整因子未留存（旧记录）</div>' : ""}</div>`;
  }
  html += `</div></div>`;

  html += `<div class="step"><h4>Step 1 · 三位分析师（独立提案或弃权）</h4><div class="grid3">`;
  for (const p of d.proposals) {
    html += `<div class="card analyst"><div class="big">${esc(p.analyst)}
      <span class="chip ${p.action === "open" ? "opened" : "no_action"}">${p.action === "open" ? "OPEN" : "ABSTAIN"}</span></div>`;
    if (p.action === "open")
      html += `<div>${esc(p.inst_id)} ${p.direction === "long" ? "做多" : "做空"} · 止损 ${fmt(p.stop_loss)} · 置信度 ${fmt(p.confidence)}</div>`;
    html += `<div class="wrap muted">${esc(p.reason)}</div>`;
    if (p.regime_penalty)
      html += `<div class="chip data_unavailable">regime 扣分</div> <div class="wrap muted">${esc(p.regime_note || "")}</div>`;
    if (p.hallucinated)
      html += `<div class="chip data_unavailable">数字存疑</div> <div class="wrap muted">${esc(JSON.stringify(p.hallucinated))}</div>`;
    html += `</div>`;
  }
  html += `</div></div>`;

  html += `<div class="step"><h4>Step 2 · 三位裁判打分（0-10，✓=通过票）</h4>`;
  const openProps = d.proposals.filter(p => p.action === "open");
  if (openProps.length) {
    html += `<table class="judge-matrix"><thead><tr><th>裁判</th>
      ${openProps.map(p => `<th>${esc(p.analyst)}<br><span class="muted">${esc(p.inst_id)}</span></th>`).join("")}</tr></thead><tbody>`;
    for (const j of d.judges_of ? d.judges_of : judgeRows(d)) {
      html += `<tr><td>${esc(j.judge)}</td>`;
      for (const p of openProps) {
        const cell = (p.judges || []).find(x => x.judge === j.judge);
        html += cell ? `<td>${fmt(cell.score)} <span class="${cell.approved ? "ok" : "no"}">${cell.approved ? "✓" : "✗"}</span>
          <details><summary>意见</summary><div class="wrap muted">${esc(cell.concerns)}</div></details></td>`
          : `<td class="muted">缺席</td>`;
      }
      html += `</tr>`;
    }
    html += `</tbody></table><div class="wrap">`;
    for (const p of openProps) {
      const v = (p.votes_for ?? "?") + "/" + (p.votes_total ?? "?");
      html += `<div>• ${esc(p.analyst)}：均分 ${fmt(p.avg_score)} ${p.avg_score >= (LIMITS.SCORE_THRESHOLD ?? 6.5) ? "≥" : "<"} 阈值 ${fmt(LIMITS.SCORE_THRESHOLD ?? 6.5)}
        ${p.avg_score >= (LIMITS.SCORE_THRESHOLD ?? 6.5) ? "✓" : "✗"}；通过票 ${esc(v)} ${p.qualify ? "✓" : "✗"} → ${p.qualify ? "达标" : "未达标"}</div>`;
    }
    html += `</div>`;
  } else html += `<div class="muted">无开仓提案，裁判未介入</div>`;
  html += `</div>`;

  const winner = d.proposals.find(p => p.is_winner);
  html += `<div class="step"><h4>Step 3 · ${winner ? "胜出提案" : "未胜出"}</h4>`;
  html += winner
    ? `<div class="card"><b>${esc(winner.analyst)} → ${esc(winner.inst_id)} ${winner.direction === "long" ? "做多" : "做空"}</b>
       <div class="wrap">${esc(winner.reason)}</div></div>`
    : `<div class="muted">有提案但未达标，或全部弃权</div>`;
  html += `</div>`;

  html += `<div class="step"><h4>Step 4 · 硬风控（代码一票否决）</h4>`;
  if (!d.risk) html += `<div class="muted">未走到风控（委员会未通过）</div>`;
  else if (d.risk.passed) {
    const v = d.risk;
    html += `<span class="chip opened">PASS</span><table><tbody>
      <tr><td>张数</td><td class="num">${fmt(v.contracts)}</td><td>参考入场</td><td class="num">${fmt(v.entry_ref)}</td>
      <td>止损</td><td class="num">${fmt(v.stop_loss)}</td><td>目标</td><td class="num">${fmt(v.target)}
      <span class="chip ${v.target_source === "structure" ? "opened" : "no_action"}">${esc(v.target_source || "—")}</span></td></tr>
      <tr><td>RR</td><td class="num">${fmt(v.rr)}</td><td>单笔风险</td><td class="num">${pct(v.risk_pct)}</td>
      <td>名义</td><td class="num">${fmt(v.notional_usdt)}</td><td>加仓后杠杆</td><td class="num">${fmt(v.leverage_after)}x</td></tr>
      </tbody></table>`;
  } else {
    for (const f of d.risk.failures) {
      const code = (f.match(/^(R\d)/) || [])[1];
      html += `<div class="wrap"><span class="chip risk_rejected">${esc(code || "")}</span>
        ${esc(f)}${code && R7_NOTES[code] ? ` <span class="muted">（${esc(R7_NOTES[code])}）</span>` : ""}</div>`;
    }
  }
  html += `</div>`;

  html += `<div class="step"><h4>Step 5 · 执行</h4>`;
  if (!d.orders.length) html += `<div class="muted">${rd.status === "planned" ? "非执行环境：只记录了意图" : "没有真实订单（纸面/未成交/被否决）"}</div>`;
  else {
    html += `<table><thead><tr><th>类型</th><th>方式</th><th>方向</th><th>价格</th><th>触发</th><th>状态</th><th>备注</th></tr></thead><tbody>`;
    for (const o of d.orders)
      html += `<tr><td>${esc(o.kind)}</td><td>${esc(o.ord_type)}</td><td>${esc(o.side)}</td>
        <td class="num">${fmt(o.px)}</td><td class="num">${fmt(o.sl_trigger_px)}${o.tp_trigger_px ? " / TP " + fmt(o.tp_trigger_px) : ""}</td>
        <td>${esc(o.state)}</td><td class="wrap muted">${esc(o.note || "")}</td></tr>`;
    html += `</tbody></table>`;
  }
  html += `</div>`;

  html += `<div class="step"><h4>页脚 · LLM 调用（静默失败在这里现形）</h4>`;
  if (!d.llm_calls.length) html += `<div class="muted">本轮无 LLM 调用（基线模式）</div>`;
  else {
    html += `<table><thead><tr><th>角色</th><th>模型</th><th>结果</th><th>延迟</th></tr></thead><tbody>`;
    for (const c of d.llm_calls)
      html += `<tr><td>${esc(c.role)}</td><td>${esc(c.model)}</td>
        <td>${c.ok ? '<span class="ok">ok</span>' : `<span class="no">${esc(c.err || "fail")}</span>`}</td>
        <td class="num">${fmt(c.latency_ms, 0)}ms</td></tr>`;
    html += `</tbody></table>`;
  }
  html += `</div><button class="ghost" onclick="location.hash='#/rounds'">‹ 返回列表</button>`;
  $("#app").innerHTML = html;
}

function judgeRows(d) {
  const seen = new Map();
  for (const p of d.proposals) for (const j of (p.judges || []))
    if (!seen.has(j.judge)) seen.set(j.judge, j.judge);
  return [...seen.keys()].map(name => ({ judge: name }));
}

/* ── 交易 ─────────────────────────────────────────────── */
async function renderTrades() {
  const [data, stats] = await Promise.all([
    api("/api/trades?size=100"), api("/api/stats")]);
  const net = stats.net_pnl !== undefined ? stats.net_pnl :
    (stats.sum_pnl || 0) - (stats.llm_cost_total || 0);
  let html = `<div class="tiles">
    <div class="card tile"><div class="v ${stats.sum_pnl >= 0 ? "up" : "down"}">${fmt(stats.sum_pnl)}</div><div class="k">交易盈亏 USDT（已平）</div></div>
    <div class="card tile"><div class="v down">−${fmt(stats.llm_cost_total, 3)}</div><div class="k">模型成本 USD${stats.llm_cost_total === 0 ? "（未计价模型不累计）" : ""}</div></div>
    <div class="card tile"><div class="v ${net >= 0 ? "up" : "down"}">${fmt(net)}</div><div class="k">净额（盈亏 − 模型成本）</div></div>
    <div class="card tile"><div class="v">${stats.win_rate === null ? "—" : pct(stats.win_rate)}</div><div class="k">胜率 · 平均 ${fmt(stats.avg_r)}R</div></div>
  </div>`;
  html += `<div class="card"><h3>交易记录（${data.total}）</h3>`;
  if (!data.items.length) html += `<div class="muted">还没有交易——纸面/回放模式不产生真实成交</div>`;
  else {
    html += `<table><thead><tr><th>开仓时间</th><th>标的</th><th>方向</th><th>买入价</th><th>卖出价</th>
      <th>盈亏</th><th>结果</th><th>谁提议的</th></tr></thead><tbody>`;
    for (const t of data.items)
      html += `<tr onclick="location.hash='#/trades/${t.id}'">
        <td>${ts(t.opened_ts)}</td><td>${esc((t.inst_id || "").replace("-SWAP", ""))}</td>
        <td>${t.direction === "long" ? "买入" : "卖出"}</td>
        <td class="num">${fmt(t.entry_px)}</td><td class="num">${fmt(t.exit_px)}</td>
        <td class="num ${t.realized_pnl >= 0 ? "up" : "down"}">${fmt(t.realized_pnl)}</td>
        <td>${chipCN(t.exit_reason || t.status)}</td>
        <td>${esc(t.analyst || "—")}</td></tr>`;
    html += `</tbody></table>`;
  }
  html += `</div>`;
  for (const [title, bucket] of [["按标的", stats.by_symbol], ["按分析师（哪个人设真的赚钱）", stats.by_analyst]]) {
    html += `<div class="card"><h3>${title}</h3><table><thead><tr><th>名称</th><th>笔数</th><th>盈亏</th><th>胜率</th></tr></thead><tbody>`;
    const keys = Object.keys(bucket || {});
    if (!keys.length) html += `<tr><td colspan="4" class="muted">暂无已平仓数据</td></tr>`;
    for (const k of keys) {
      const b = bucket[k];
      html += `<tr><td>${esc(k)}</td><td class="num">${b.n}</td>
        <td class="num ${b.pnl >= 0 ? "up" : "down"}">${fmt(b.pnl)}</td>
        <td class="num">${pct(b.wins / b.n)}</td></tr>`;
    }
    html += `</tbody></table></div>`;
  }
  $("#app").innerHTML = html;
}

async function renderTradeDetail(id) {
  const d = await api("/api/trades/" + encodeURIComponent(id));
  const t = d.trade;
  let html = `<div class="card"><h3>${esc((t.inst_id || "").replace("-SWAP", ""))} ${t.direction === "long" ? "买入" : "卖出"}
    ${chipCN(t.exit_reason || t.status)}</h3>
    <div>买入价 ${fmt(t.entry_px)} → 卖出价 ${fmt(t.exit_px)} ·
    盈亏 <b class="${t.realized_pnl >= 0 ? "up" : "down"}">${fmt(t.realized_pnl)}</b> ·
    提议人 ${esc(t.analyst || "—")}</div>
    ${d.open_round_id ? `<div style="margin-top:6px"><a href="#/rounds/${esc(d.open_round_id)}">看当时委员会为什么买它 →</a></div>` : ""}</div>`;
  html += `<div class="card"><h3>订单</h3><table><tbody>`;
  for (const o of d.orders)
    html += `<tr><td>${esc(o.kind)}</td><td>${esc(o.ord_type)}</td><td class="num">${fmt(o.px)}</td>
      <td>${esc(o.state)}</td><td class="wrap muted">${esc(o.note || "")}</td></tr>`;
  html += `</tbody></table></div>`;
  $("#app").innerHTML = html;
}

/* ── 事件流 ───────────────────────────────────────────── */
async function renderEvents() {
  const data = await api("/api/events?size=100");
  let html = `<div class="card"><h3>系统事件（${data.total}）</h3>
    <table><thead><tr><th>时间</th><th>级别</th><th>类型</th><th>内容</th></tr></thead><tbody>`;
  for (const e of data.items)
    html += `<tr><td>${ts(e.ts)}</td><td><span class="chip ${esc(e.level)}">${esc(e.level)}</span></td>
      <td>${esc(e.kind)}</td><td class="wrap">${esc(e.message)}</td></tr>`;
  html += `</tbody></table></div>`;
  $("#app").innerHTML = html;
}

/* ── 因子动物园 ───────────────────────────────────────── */
async function renderFactors() {
  const data = await api("/api/factors");
  const c = data.counts;
  let html = `<div class="card"><h3>因子动物园 · core ${c.core || 0} · observing ${c.observing || 0} ·
    trial ${c.trial || 0} · active ${c.active || 0} · retired ${c.retired || 0} · rejected ${c.rejected || 0}</h3>
    <div class="muted">这些"信号"要先连续多日验证确实有效，才会被拿来做决策——没验证过的绝不影响下单。</div>
    <table style="margin-top:10px"><thead><tr><th>因子</th><th>族</th><th>层</th><th>状态</th>
    <th>IC</th><th>rank-IC</th><th>命中率</th><th>观测数</th><th>tracked 天数</th></tr></thead><tbody>`;
  for (const f of data.items) {
    html += `<tr><td><b>${esc(f.name)}</b></td><td>${esc(f.family)}</td><td>${esc(f.tier)}</td>
      <td><span class="chip ${f.status === "active" || f.status === "trial" ? "opened" : f.status === "rejected" || f.status === "retired" ? "risk_rejected" : "no_action"}">${esc(f.status)}</span></td>
      <td class="num">${fmt(f.ic, 3)}</td>
      <td class="num">${fmt(f.rank_ic, 3)}</td>
      <td class="num">${f.hit_rate === null ? "—" : pct(f.hit_rate)}</td>
      <td class="num">${fmt(f.n_obs, 0)}</td>
      <td class="num">${fmt(f.days_tracked, 0)}</td></tr>`;
  }
  if (!data.items.length)
    html += `<tr><td colspan="9" class="muted">尚无观测——跑几轮 run-loop 后回来</td></tr>`;
  html += `</tbody></table></div>`;
  $("#app").innerHTML = html;
}

/* ── 路由 ─────────────────────────────────────────────── */
function parseHash() {
  const h = (location.hash || "#/overview").slice(2);
  const [path, qs] = h.split("?");
  const parts = path.split("/").filter(Boolean);
  return { parts, query: Object.fromEntries(new URLSearchParams(qs || "")) };
}

async function route() {
  const { parts, query } = parseHash();
  document.querySelectorAll("nav a").forEach(a =>
    a.classList.toggle("active", a.dataset.tab === (parts[0] || "overview")));
  if (parts[0] === "login" || parts.length === 0 && location.hash === "#/login") return renderLogin();
  try {
    if (!parts.length || parts[0] === "overview") return await renderOverview();
    if (parts[0] === "equity") return await renderEquity();
    if (parts[0] === "rounds" && parts[1]) return await renderRoundDetail(parts[1]);
    if (parts[0] === "rounds") return await renderRounds(query);
    if (parts[0] === "trades" && parts[1]) return await renderTradeDetail(parts[1]);
    if (parts[0] === "trades") return await renderTrades();
    if (parts[0] === "factors") return await renderFactors();
    if (parts[0] === "events") return await renderEvents();
    return await renderOverview();
  } catch (e) {
    if (e.message === "unauthenticated") return;
    $("#app").innerHTML = `<div class="banner">加载失败：${esc(e.message)}</div>`;
  }
}

/* ── 顶栏动作 ─────────────────────────────────────────── */
window.addEventListener("hashchange", route);

document.addEventListener("DOMContentLoaded", async () => {
  LOGIN_HTML = $("#app").innerHTML;
  $("#logout-btn").onclick = async () => { await post("/api/logout"); location.hash = "#/login"; route(); };
  $("#pause-btn").onclick = async () => {
    const state = await api("/api/state");
    await post(state.loop.paused ? "/api/loop/resume" : "/api/loop/pause");
    route();
  };
  $("#runnow-btn").onclick = async () => {
    try { await post("/api/loop/run-now"); } catch (e) { alert(e.message); }
    route();
  };
  // 登录态探测：未登录时显示登录表单
  const me = await fetch("/api/me").then(r => r.json()).catch(() => ({ authed: false }));
  if (!me.authed && (location.hash === "" || location.hash === "#/" || !location.hash.startsWith("#/login"))) {
    location.hash = "#/login";
    renderLogin();
    return;
  }
  if (location.hash === "#/login" && me.authed) location.hash = "#/overview";
  route();
});
