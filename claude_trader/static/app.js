// Claude US Stock Simulator - dashboard front-end (vanilla JS, no build step, no CDN).
// Everything written by Claude (summaries, reasons, notes) is untrusted text: always render
// it with textContent, never innerHTML.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const STATE = { status: null, runs: [], trades: null, equity: null, config: null, styles: null, tableView: false };

// ---------- helpers ----------
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
const money = (x, digits = 2) =>
  x === null || x === undefined ? "—" :
  (x < 0 ? "-$" : "$") + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = (x, digits = 2) => (x === null || x === undefined ? "—" : (x > 0 ? "+" : "") + x.toFixed(digits) + "%");
const signClass = (x) => (x > 0 ? "up" : x < 0 ? "down" : "");
const arrow = (x) => (x > 0 ? "▲ " : x < 0 ? "▼ " : "");
const fmtTime = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") : "");
const sideText = (s) => (s === "buy" ? "买入" : s === "sell" ? "卖出" : s);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const ct = res.headers.get("Content-Type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.error) || res.statusText);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

function banner(msg) {
  const b = $("#banner");
  b.hidden = !msg;
  b.textContent = msg || "";
}

// ---------- tabs ----------
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== "tab-" + name));
  try { localStorage.setItem("tab", name); } catch (e) { /* storage may be blocked */ }
  if (name === "overview") drawChart();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- header / overview ----------
function renderHeader() {
  const s = STATE.status;
  const pill = $("#run-pill");
  const btn = $("#run-now");
  if (s.running) {
    pill.textContent = "Claude 正在思考…";
    pill.className = "pill live";
    btn.disabled = true;
  } else {
    pill.className = "pill";
    btn.disabled = false;
    pill.textContent = s.next_run_et
      ? "下次运行 " + fmtTime(s.next_run_et).slice(5) + " ET（本地 " + new Date(s.next_run_et).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) + "）"
      : s.configured ? "自动运行已关闭" : "还没保存设置";
  }
  const cfg = STATE.config;
  if (cfg && STATE.styles) {
    $("#subtitle").textContent = `模拟盘 · 风格：${STATE.styles[cfg.style].label} · ${cfg.backend === "api" ? "API key" : "Claude 订阅"} · ${cfg.model}`;
  }
  $("#version").textContent = " v" + s.version;

  const warn = [];
  if (!s.configured) warn.push("欢迎！先到「设置」页选好本金、风格和调用方式，点「保存设置」之后才会开始自动运行。");
  if (cfg && cfg.backend === "subscription" && !s.cli_found) warn.push("没找到 Claude Code（claude 命令）。订阅模式需要先安装并登录 Claude Code，或者在设置里改用 API key。");
  if (cfg && cfg.backend === "api" && !cfg.api_key_hint) warn.push("API 模式还没有设置 API key。");
  if (s.pending_cash_change) warn.push(`设置里的本金（${money(cfg.starting_cash, 0)}）和当前账户（${money(s.account.starting_cash, 0)}）不一致，在设置页底部重置账户后生效。`);
  if (s.last_result && s.last_result.ok === false) warn.push("上一次运行失败：" + (s.last_result.error || "") + "（详情见 data/errors.log）");
  banner(warn.join("　"));
}

function tile(label, value, delta, cls) {
  return el("div", { class: "tile" },
    el("div", { class: "label", text: label }),
    el("div", { class: "value " + (cls || ""), text: value }),
    delta ? el("div", { class: "delta", text: delta }) : null);
}

function renderTiles() {
  const s = STATE.status, a = s.account, eq = STATE.equity || {};
  const excess = a.benchmark_pnl_pct === null || a.benchmark_pnl_pct === undefined ? null : a.pnl_pct - a.benchmark_pnl_pct;
  const box = $("#tiles");
  box.replaceChildren(
    tile("总资产", money(a.total), "本金 " + money(a.starting_cash, 0)),
    tile("累计收益", arrow(a.pnl) + money(a.pnl), pct(a.pnl_pct), signClass(a.pnl)),
    tile(`对比 ${a.benchmark}`, excess === null ? "—" : arrow(excess) + pct(excess), `${a.benchmark} 同期 ${pct(a.benchmark_pnl_pct)}`, excess === null ? "" : signClass(excess)),
    tile("现金", money(a.cash), a.total ? "占 " + ((a.cash / a.total) * 100).toFixed(1) + "%" : ""),
    tile("最大回撤", eq.max_drawdown_pct === undefined ? "—" : pct(eq.max_drawdown_pct), "按每次运行的净值"),
    tile("运行次数", String(a.run_count), s.started ? "开始于 " + a.created_at.slice(0, 10) : "还没开始"),
  );
}

function renderPositions() {
  const rows = STATE.status.positions;
  const box = $("#positions");
  if (!rows.length) {
    box.replaceChildren(el("div", { class: "empty-state", text: STATE.status.started ? "全部是现金" : "还没有运行过。点右上角「立即运行一次」开始。" }));
    return;
  }
  box.replaceChildren(el("table", {},
    el("thead", {}, el("tr", {}, ["代码", "股数", "成本", "现价", "市值", "占比", "浮盈", "今日"].map((h, i) => el("th", { class: i ? "num" : "", text: h })))),
    el("tbody", {}, rows.map((r) => el("tr", {},
      el("td", {}, el("strong", { text: r.ticker })),
      el("td", { class: "num", text: r.shares.toFixed(4) }),
      el("td", { class: "num", text: r.avg_cost.toFixed(2) }),
      el("td", { class: "num", text: r.price.toFixed(2) + (r.price_missing ? " (旧)" : "") }),
      el("td", { class: "num", text: money(r.value) }),
      el("td", { class: "num", text: r.weight_pct.toFixed(1) + "%" }),
      el("td", { class: "num " + signClass(r.unrealized_pct), text: arrow(r.unrealized_pct) + pct(r.unrealized_pct) }),
      el("td", { class: "num " + signClass(r.day_change_pct || 0), text: pct(r.day_change_pct) }),
    )))));
}

function renderLatest() {
  const run = STATE.runs.find((r) => r.status === "ok");
  const box = $("#latest");
  if (!run) { box.replaceChildren(el("div", { class: "empty-state", text: "还没有决策记录" })); return; }
  $("#latest-time").textContent = fmtTime(run.ts) + " ET";
  box.replaceChildren(
    el("p", { class: "summary", text: run.summary }),
    el("p", { class: "muted", text: run.market_view }),
    run.trades.length ? tradeList(run.trades) : el("p", { class: "muted small", text: "本次没有交易（继续持有）" }),
  );
}

// ---------- equity chart (inline SVG) ----------
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
function niceTicks(min, max, count = 5) {
  const span = max - min || 1;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

function renderLegend() {
  const bm = STATE.status ? STATE.status.account.benchmark : "SPY";
  $("#legend").replaceChildren(
    el("span", {}, el("span", { class: "key", style: "background:var(--series-1)" }), "Claude 组合"),
    el("span", {}, el("span", { class: "key", style: "background:var(--series-2)" }), bm + " 买入持有"),
  );
}

function drawChart() {
  const box = $("#chart");
  if (!box || box.offsetParent === null || !STATE.equity) return;
  const pts = STATE.equity.points;
  const W = box.clientWidth, H = box.clientHeight;
  const m = { t: 12, r: 64, b: 26, l: 46 };
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "收益曲线：Claude 组合对比基准" });
  box.replaceChildren(root);
  if (!pts.length) {
    const t = svg("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty" });
    t.textContent = "运行几次之后这里会出现收益曲线";
    root.append(t);
    return;
  }
  const vals = pts.flatMap((p) => [p.pnl_pct, p.benchmark_pnl_pct]).filter((v) => v !== null && v !== undefined);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = Math.max((hi - lo) * 0.12, 0.5);
  lo -= pad; hi += pad;
  const ticks = niceTicks(lo, hi);
  lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
  const n = pts.length;
  // x is the run index: runs only happen during market hours, so this skips nights and weekends.
  const x = (i) => m.l + (n === 1 ? (W - m.l - m.r) / 2 : (i / (n - 1)) * (W - m.l - m.r));
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);

  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (const t of ticks) {
    grid.append(svg("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
    const lab = svg("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end" });
    lab.textContent = (t > 0 ? "+" : "") + t + "%";
    axis.append(lab);
  }
  // date labels: first run of each day, thinned to fit
  const days = [];
  pts.forEach((p, i) => { const d = p.ts.slice(5, 10); if (!days.length || days[days.length - 1].d !== d) days.push({ d, i }); });
  const maxLabels = Math.max(2, Math.floor((W - m.l - m.r) / 70));
  const every = Math.ceil(days.length / maxLabels);
  days.forEach((dd, k) => {
    if (k % every) return;
    const lab = svg("text", { x: x(dd.i), y: H - 6, "text-anchor": "middle" });
    lab.textContent = dd.d;
    axis.append(lab);
  });
  root.append(grid, axis, svg("line", { class: "zero", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }));

  const series = [
    { key: "pnl_pct", cls: "s1", dot: "dot1", name: "Claude" },
    { key: "benchmark_pnl_pct", cls: "s2", dot: "dot2", name: STATE.status.account.benchmark },
  ];
  const ends = [];
  for (const s of series) {
    const seg = pts.map((p, i) => [i, p[s.key]]).filter(([, v]) => v !== null && v !== undefined);
    if (!seg.length) continue;
    if (seg.length === 1) root.append(svg("circle", { class: s.dot, cx: x(seg[0][0]), cy: y(seg[0][1]), r: 4 }));
    else root.append(svg("path", { class: "line " + s.cls, d: seg.map(([i, v], k) => (k ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1)).join("") }));
    const [li, lv] = seg[seg.length - 1];
    ends.push({ y: y(lv), x: x(li), text: s.name + " " + pct(lv, 1) });
  }
  // direct labels at the line ends, nudged apart so they never overlap
  ends.sort((a, b) => a.y - b.y);
  for (let k = 1; k < ends.length; k++) if (ends[k].y - ends[k - 1].y < 14) ends[k].y = ends[k - 1].y + 14;
  for (const e of ends) {
    const t = svg("text", { class: "endlabel", x: e.x + 8, y: e.y + 4 });
    t.textContent = e.text;
    root.append(t);
  }

  // hover layer: crosshair + tooltip, hit target is the whole plot area
  const cross = svg("line", { class: "cross", y1: m.t, y2: H - m.b, visibility: "hidden" });
  const d1 = svg("circle", { class: "dot1", r: 4, visibility: "hidden" });
  const d2 = svg("circle", { class: "dot2", r: 4, visibility: "hidden" });
  const hit = svg("rect", { x: m.l - 10, y: 0, width: W - m.l - m.r + 20, height: H, fill: "transparent" });
  root.append(cross, d1, d2, hit);
  const tip = $("#tooltip");
  const hide = () => { tip.hidden = true; [cross, d1, d2].forEach((n) => n.setAttribute("visibility", "hidden")); };
  hit.addEventListener("mouseleave", hide);
  hit.addEventListener("pointermove", (ev) => {
    const r = root.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1))));
    const p = pts[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    for (const [dot, v] of [[d1, p.pnl_pct], [d2, p.benchmark_pnl_pct]]) {
      if (v === null || v === undefined) { dot.setAttribute("visibility", "hidden"); continue; }
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(v)); dot.setAttribute("visibility", "visible");
    }
    tip.replaceChildren(
      el("div", { class: "t", text: fmtTime(p.ts) + " ET · Run #" + p.id }),
      el("div", { class: "r" }, el("span", {}, el("span", { class: "sw", style: "background:var(--series-1)" }), "Claude"), el("strong", { text: pct(p.pnl_pct) })),
      el("div", { class: "r" }, el("span", {}, el("span", { class: "sw", style: "background:var(--series-2)" }), STATE.status.account.benchmark), el("strong", { text: pct(p.benchmark_pnl_pct) })),
      el("div", { class: "r muted" }, el("span", { text: "总资产" }), el("span", { text: money(p.total_value) })),
    );
    tip.hidden = false;
    const tx = ev.clientX + 14, ty = ev.clientY + 14;
    tip.style.left = Math.min(tx, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = Math.min(ty, window.innerHeight - tip.offsetHeight - 8) + "px";
  });
}

function renderChartTable() {
  const pts = STATE.equity ? STATE.equity.points : [];
  $("#chart-table").replaceChildren(el("table", {},
    el("thead", {}, el("tr", {}, ["时间 (ET)", "总资产", "Claude", STATE.status.account.benchmark].map((h, i) => el("th", { class: i ? "num" : "", text: h })))),
    el("tbody", {}, pts.slice().reverse().map((p) => el("tr", {},
      el("td", { text: fmtTime(p.ts) }), el("td", { class: "num", text: money(p.total_value) }),
      el("td", { class: "num", text: pct(p.pnl_pct) }), el("td", { class: "num", text: pct(p.benchmark_pnl_pct) }))))));
}
$("#toggle-table").addEventListener("click", () => {
  STATE.tableView = !STATE.tableView;
  $("#chart").hidden = STATE.tableView;
  $("#chart-table").hidden = !STATE.tableView;
  $("#toggle-table").textContent = STATE.tableView ? "图表" : "表格";
  if (!STATE.tableView) drawChart();
});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawChart, 120); });

// ---------- journal ----------
function tradeList(trades) {
  return el("ul", { class: "trade-list" }, trades.map((t) => {
    const ok = t.status === "filled";
    return el("li", {},
      el("span", { class: "tag " + (ok ? t.side : "rejected"), text: sideText(t.side) }),
      el("strong", { text: t.ticker }),
      ok ? el("span", { text: `${t.shares.toFixed(4)} 股 @ ${t.price.toFixed(2)}（${money(t.notional)}）` }) : el("span", { class: "muted", text: "被拒：" + t.note }),
      ok && t.realized_pnl !== null ? el("span", { class: signClass(t.realized_pnl), text: "已实现 " + money(t.realized_pnl) }) : null,
      t.confidence ? el("span", { class: "muted small", text: "信心 " + t.confidence + "/5" }) : null,
      el("span", { class: "why", text: t.reason }),
      ok && t.note ? el("span", { class: "muted small", text: "[" + t.note + "]" }) : null,
    );
  }));
}

function renderJournal() {
  const q = $("#journal-search").value.trim().toLowerCase();
  const onlyTrades = $("#journal-only-trades").checked;
  const showErrors = $("#journal-show-errors").checked;
  const runs = STATE.runs.filter((r) => {
    if (r.status === "error" && !showErrors) return false;
    if (r.status === "running") return false;
    if (onlyTrades && !r.trades.length) return false;
    if (!q) return true;
    const hay = [r.summary, r.market_view, r.reply_to_user, r.error, ...r.trades.map((t) => t.ticker + " " + t.reason)].join(" ").toLowerCase();
    return hay.includes(q);
  });
  const box = $("#journal");
  if (!runs.length) { box.replaceChildren(el("div", { class: "card empty-state", text: STATE.runs.length ? "没有符合条件的记录" : "还没有记录" })); return; }
  box.replaceChildren(...runs.map((r) => {
    if (r.status === "error") {
      return el("article", { class: "card run error" },
        el("div", { class: "run-head" }, el("span", { class: "when", text: fmtTime(r.ts) + " ET" }), el("span", { class: "tag", text: "失败" })),
        el("p", { class: "muted small", text: r.error }));
    }
    return el("article", { class: "card run" },
      el("div", { class: "run-head" },
        el("span", { class: "when", text: fmtTime(r.ts) + " ET" }),
        el("span", { class: signClass(r.pnl_pct), text: money(r.total_value) + "（" + pct(r.pnl_pct) + "）" }),
        el("span", { class: "muted small", text: "基准 " + pct(r.benchmark_pnl_pct) }),
        el("span", { class: "meta", text: [r.style && STATE.styles && STATE.styles[r.style] ? STATE.styles[r.style].label : r.style, r.backend + ":" + r.model,
          (r.input_tokens || 0).toLocaleString() + " / " + (r.output_tokens || 0).toLocaleString() + " tokens", r.duration_sec ? r.duration_sec + " 秒" : "", r.trigger].filter(Boolean).join(" · ") })),
      el("p", { class: "summary", text: r.summary }),
      el("p", { class: "view", text: r.market_view }),
      r.trades.length ? tradeList(r.trades) : el("p", { class: "muted small", text: "没有交易（继续持有）" }),
      r.reply_to_user ? el("div", { class: "reply" }, el("div", { class: "muted small", text: "回复留言：" + (r.user_message || "") }), el("div", { text: r.reply_to_user })) : null,
    );
  }));
}
["input", "change"].forEach((ev) => {
  $("#journal-search").addEventListener(ev, renderJournal);
  $("#journal-only-trades").addEventListener(ev, renderJournal);
  $("#journal-show-errors").addEventListener(ev, renderJournal);
});

// ---------- trades ----------
function renderTrades() {
  const d = STATE.trades;
  if (!d) return;
  const s = d.stats;
  $("#trade-stats").replaceChildren(
    tile("成交笔数", String(s.filled), "被拒 " + s.rejected),
    tile("已平仓", String(s.closed), s.closed ? "胜率 " + Math.round((s.wins / s.closed) * 100) + "%" : ""),
    tile("已实现盈亏", arrow(s.realized) + money(s.realized), "", signClass(s.realized)),
  );
  const box = $("#trades");
  if (!d.trades.length) { box.replaceChildren(el("div", { class: "empty-state", text: "还没有交易" })); return; }
  box.replaceChildren(el("table", {},
    el("thead", {}, el("tr", {}, ["时间 (ET)", "方向", "代码", "股数", "价格", "金额", "已实现", "理由"].map((h, i) => el("th", { class: i >= 3 && i <= 6 ? "num" : "", text: h })))),
    el("tbody", {}, d.trades.map((t) => {
      const ok = t.status === "filled";
      return el("tr", {},
        el("td", { text: fmtTime(t.ts) }),
        el("td", {}, el("span", { class: "tag " + (ok ? t.side : "rejected"), text: sideText(t.side) })),
        el("td", {}, el("strong", { text: t.ticker })),
        el("td", { class: "num", text: ok ? t.shares.toFixed(4) : "—" }),
        el("td", { class: "num", text: ok ? t.price.toFixed(2) : "—" }),
        el("td", { class: "num", text: ok ? money(t.notional) : "—" }),
        el("td", { class: "num " + signClass(t.realized_pnl || 0), text: t.realized_pnl === null ? "" : money(t.realized_pnl) }),
        el("td", { class: "wrap", text: (ok ? "" : "被拒（" + t.note + "）") + t.reason }),
      );
    }))));
}

// ---------- notes & message ----------
function renderNotes() {
  const s = STATE.status;
  $("#notes").textContent = s.strategy_notes || "还没有笔记，第一次运行后会出现。";
  const wl = $("#watchlist");
  wl.replaceChildren(...(s.watchlist.length ? s.watchlist.map((t) => el("span", { class: "chip", text: t })) : [el("span", { class: "muted small", text: "（空）" })]));
  const msg = $("#message");
  if (document.activeElement !== msg && STATE.config) msg.value = STATE.config.message_to_claude || "";
  const replies = STATE.runs.filter((r) => r.reply_to_user).slice(0, 5);
  $("#replies").replaceChildren(...(replies.length ? replies.map((r) => el("div", { class: "run reply" },
    el("div", { class: "muted small", text: fmtTime(r.ts) + " ET · 你说：" + (r.user_message || "") }),
    el("div", { text: r.reply_to_user }))) : [el("p", { class: "muted small", text: "还没有回复" })]));
}
$("#save-message").addEventListener("click", async () => {
  try {
    await post("/api/message", { message: $("#message").value });
    STATE.config.message_to_claude = $("#message").value;
    $("#save-message").textContent = "已保存 ✓";
    setTimeout(() => ($("#save-message").textContent = "保存留言"), 1500);
  } catch (e) { alert("保存失败：" + e.message); }
});

// ---------- settings ----------
const form = $("#settings");
function slotsPreview(first, hours) {
  const [h, mm] = first.split(":").map(Number);
  let t = h * 60 + mm;
  const out = [];
  while (t < 16 * 60) { out.push(String(Math.floor(t / 60)).padStart(2, "0") + ":" + String(Math.round(t % 60)).padStart(2, "0")); t += hours * 60; }
  return out;
}
function updateSettingsHints() {
  const f = form.elements;
  const slots = f.first_run_et.value ? slotsPreview(f.first_run_et.value, Number(f.interval_hours.value)) : [];
  $("#schedule-preview").textContent = f.enabled.checked
    ? `周一到周五，每天 ${slots.length} 次（美东时间）：${slots.join("、")}。周末和节假日自动跳过。`
    : "自动运行已关闭，只能手动点「立即运行一次」。";
  const isApi = f.backend.value === "api";
  $("#api-fields").hidden = !isApi;
  const cfg = STATE.config || {};
  $("#key-hint").textContent = cfg.api_key_from_env ? "正在使用环境变量 ANTHROPIC_API_KEY（" + cfg.api_key_hint + "）"
    : cfg.api_key_hint ? "已保存的 key：" + cfg.api_key_hint + "（保存在本机 config.json，不会上传）" : "还没有设置 key";
  $("#cli-status").textContent = STATE.status ? (STATE.status.cli_found ? "✓ 已检测到 Claude Code" : "✗ 没检测到 claude 命令") : "";
  $("#cli-status").className = "status " + (STATE.status && STATE.status.cli_found ? "up" : "down");
}
function fillSettings() {
  const c = STATE.config, f = form.elements;
  $("#style-grid").replaceChildren(...Object.entries(STATE.styles).map(([k, s]) => el("label", { class: "radio-card" },
    el("input", { type: "radio", name: "style", value: k, checked: c.style === k }),
    el("strong", { text: s.label }),
    el("span", { text: s.summary }),
    el("span", { text: "默认单只上限 " + Math.round(s.max_position_pct * 100) + "%" }))));
  for (const k of ["starting_cash", "benchmark", "custom_style_prompt", "first_run_et", "model", "web_search_max_uses", "effort", "slippage", "commission", "max_turns"]) {
    if (f[k]) f[k].value = c[k];
  }
  f.max_position_pct.value = c.max_position_pct === null ? "" : String(c.max_position_pct);
  f.interval_hours.value = String(c.interval_hours);
  if (f.interval_hours.value !== String(c.interval_hours)) {   // a custom value from config.json
    f.interval_hours.append(el("option", { value: String(c.interval_hours), text: c.interval_hours + " 小时" }));
    f.interval_hours.value = String(c.interval_hours);
  }
  f.enabled.checked = c.enabled;
  f.allow_fractional.checked = c.allow_fractional;
  f.api_key.value = "";
  for (const r of form.querySelectorAll("input[name=backend]")) r.checked = r.value === c.backend;
  updateSettingsHints();
}
form.addEventListener("input", updateSettingsHints);
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = form.elements;
  const data = {
    starting_cash: Number(f.starting_cash.value), benchmark: f.benchmark.value,
    style: f.style.value, custom_style_prompt: f.custom_style_prompt.value,
    max_position_pct: f.max_position_pct.value === "" ? null : Number(f.max_position_pct.value),
    enabled: f.enabled.checked, interval_hours: Number(f.interval_hours.value), first_run_et: f.first_run_et.value,
    backend: f.backend.value, model: f.model.value, web_search_max_uses: Number(f.web_search_max_uses.value),
    effort: f.effort.value, slippage: Number(f.slippage.value), commission: Number(f.commission.value),
    max_turns: Number(f.max_turns.value), allow_fractional: f.allow_fractional.checked,
  };
  if (f.api_key.value.trim()) data.api_key = f.api_key.value.trim();
  const msg = $("#save-msg");
  try {
    const r = await post("/api/config", data);
    STATE.config = r.config;
    msg.textContent = "已保存 ✓ 下一次运行生效";
    msg.className = "up";
    fillSettings();
    refresh();
  } catch (e) {
    msg.textContent = "没保存：" + e.message;
    msg.className = "down";
  }
});
$("#reset").addEventListener("click", async () => {
  const answer = prompt("会把当前账户的所有记录挪到备份目录，按设置里的本金和风格重新开始。\n确认请输入 RESET：");
  if (answer !== "RESET") return;
  try {
    const r = await post("/api/reset", { confirm: "RESET" });
    alert(r.backup ? "已重置。旧数据备份在：" + r.backup : "已重置。");
    refresh();
  } catch (e) { alert("重置失败：" + e.message); }
});

// ---------- run now ----------
$("#run-now").addEventListener("click", async () => {
  if (!confirm("现在就让 Claude 做一次决策吗？\n美股没开盘时，会按最近的收盘价模拟成交。")) return;
  try {
    await post("/api/run-now", { force: true });
    STATE.status.running = true;
    renderHeader();
    pollFast();
  } catch (e) { alert(e.message); }
});

// ---------- data loading ----------
async function refresh() {
  try {
    const [status, runs, trades, equity, conf] = await Promise.all([
      api("/api/status"), api("/api/runs"), api("/api/trades"), api("/api/equity"), api("/api/config"),
    ]);
    const firstConfig = !STATE.config;
    Object.assign(STATE, { status, runs: runs.runs, trades, equity, config: conf.config, styles: conf.styles });
    renderHeader(); renderTiles(); renderLegend(); renderPositions(); renderLatest();
    renderJournal(); renderTrades(); renderNotes();
    if (STATE.tableView) renderChartTable(); else drawChart();
    // Only overwrite the settings form on first load, so a periodic refresh never wipes unsaved edits.
    if (firstConfig) fillSettings(); else updateSettingsHints();
  } catch (e) {
    banner("连不上后台：" + e.message + "。确认 python trader.py start 还在运行。");
  }
}
let fastTimer = null;
function pollFast() {
  clearInterval(fastTimer);
  fastTimer = setInterval(async () => {
    await refresh();
    if (!STATE.status.running) clearInterval(fastTimer);
  }, 4000);
}
$("#toggle-table").addEventListener("click", renderChartTable);

(function init() {
  let tab = "overview";
  try { tab = localStorage.getItem("tab") || tab; } catch (e) { /* ignore */ }
  showTab(document.getElementById("tab-" + tab) ? tab : "overview");
  refresh().then(() => { if (STATE.status && STATE.status.running) pollFast(); });
  setInterval(refresh, 60000);
})();
