"""Talking to Claude: prompts plus two backends.

  subscription  Claude Code headless mode (claude -p) on this machine; uses your Claude plan, no API key
  api           the anthropic Python SDK with an API key, billed per token
"""

import json
import os
import shutil
import subprocess
import time as _time
from pathlib import Path

from . import config as C
from .market import get_history, get_quotes, now_et
from .portfolio import records
from .styles import STYLES, position_cap, style_prompt

PY = "python" if shutil.which("python") else "python3"


# ============================================================
# Prompts
# ============================================================
def system_prompt(state, cfg, tools_text):
    cap = position_cap(cfg)
    return f"""You are managing a simulated (paper-trading) US equity portfolio as a long-running experiment.
The account owner wants to see how well you do when you follow what you genuinely believe is the best
strategy within the investment style below. Every decision and reason you give is logged to a journal
the owner reads in a web dashboard.

Setup
- Starting capital: ${state['starting_cash']:,.2f}, started {state['created_at'][:10]}.
- You are invoked about every {cfg['interval_hours']:g} hours during US market hours. Between invocations you
  remember nothing except what is shown to you: your account, recent runs and trades, and your own
  notebook (strategy_notes). You can look further back with the records tool.
- Objective: the best result you can achieve within the style, measured against buy-and-hold {state['benchmark']}.

{style_prompt(cfg)}

Rules (enforced by the script)
- Long only: US-listed common stocks and ETFs. No margin, no shorting, no options, no crypto.
- Max single position: {cap:.0%} of total portfolio value (larger buys are scaled down).
- Market orders fill at the current price with {cfg['slippage'] * 100:.2f}% slippage; commission ${cfg['commission']:.2f}/trade.
- Fractional shares: {'allowed' if cfg['allow_fractional'] else 'NOT allowed (whole shares only)'}.
- Buys larger than available cash are scaled down. Sells larger than the position are capped.
- Sells are executed before buys within the same run.

How to work
- Holding is a valid decision. Unnecessary turnover and reacting to noise usually hurt returns.
- Use strategy_notes as your notebook: overall strategy, thesis and exit plan for each position,
  and what to check next time. Revise it deliberately; don't flip-flop without a reason.
- Be efficient with research: a few targeted lookups and searches are usually enough.
- If the owner left you a message, answer it in reply_to_user.
- Write all free-text fields (market_view, reasons, strategy_notes, summary, reply_to_user) in Simplified Chinese.
{tools_text}"""


def decision_schema():
    return {"type": "object", "properties": {
        "market_view": {"type": "string", "description": "Your read of the market right now, 2-5 sentences."},
        "trades": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["buy", "sell"]},
            "ticker": {"type": "string"},
            "amount_usd": {"type": "number", "description": "Dollar amount to buy/sell. Use this OR shares."},
            "shares": {"type": "number", "description": "Number of shares."},
            "sell_all": {"type": "boolean", "description": "Sell only: close the entire position."},
            "reason": {"type": "string"},
            "confidence": {"type": "integer", "minimum": 1, "maximum": 5}},
            "required": ["action", "ticker", "reason"]}},
        "strategy_notes": {"type": "string",
                           "description": "Your full updated notebook. Replaces the previous one. Keep it under ~2000 characters."},
        "watchlist": {"type": "array", "items": {"type": "string"},
                      "description": "Tickers to auto-quote for you next run (max 20)."},
        "summary": {"type": "string", "description": "One-sentence summary of this run."},
        "reply_to_user": {"type": "string", "description": "Reply to the owner's message, if any. Empty otherwise."}},
        "required": ["market_view", "trades", "strategy_notes", "summary"]}


def fmt_money(x):
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def build_user_prompt(state, cfg, val, quotes, con):
    start_val = state["starting_cash"]
    pnl = val["total"] - start_val
    bm = state["benchmark"]
    bm_price = quotes.get(bm, {}).get("price")
    bm_pnl = (bm_price / state["benchmark_start_price"] - 1) * 100 if bm_price and state["benchmark_start_price"] else None

    L = [f"Now: {now_et():%A %Y-%m-%d %H:%M} ET. Run #{state['run_count'] + 1}. Style: {STYLES[cfg['style']]['label']} ({cfg['style']}).", "",
         "## Account",
         f"- Cash: {fmt_money(val['cash'])}",
         f"- Positions value: {fmt_money(val['positions_value'])}",
         f"- Total value: {fmt_money(val['total'])}  (P&L since start: {fmt_money(pnl)}, {pnl / start_val * 100:+.2f}%)",
         f"- {bm} since start: " + (f"{bm_pnl:+.2f}%" if bm_pnl is not None else "n/a (first run)"), "",
         "## Positions"]
    if val["rows"]:
        L.append("ticker | shares | avg_cost | price | value | weight | unrealized | today")
        for r in val["rows"]:
            day = f"{r['day_change_pct']:+.2f}%" if r["day_change_pct"] is not None else "n/a"
            L.append(f"{r['ticker']} | {r['shares']:.4f} | {r['avg_cost']:.2f} | {r['price']:.2f}"
                     f"{' (stale)' if r['price_missing'] else ''} | {fmt_money(r['value'])} | "
                     f"{r['weight_pct']:.1f}% | {r['unrealized_pct']:+.2f}% | {day}")
    else:
        L.append("(none — all cash)")

    L += ["", "## Market snapshot (auto-fetched)"]
    for sym, q in quotes.items():
        if "error" in q:
            L.append(f"- {sym}: error {q['error']}")
        else:
            day = f"{q['day_change_pct']:+.2f}%" if q["day_change_pct"] is not None else "n/a"
            L.append(f"- {sym}: {q['price']} ({day} today, as of {q['as_of']})")

    L += ["", "## Your notebook (strategy_notes from last run)", state["strategy_notes"] or "(empty)"]

    msg = (cfg.get("message_to_claude") or "").strip()
    if msg:
        L += ["", "## Message from the account owner (answer in reply_to_user)", msg]

    runs = con.execute("SELECT ts, total_value, pnl_pct, summary FROM runs WHERE status='ok' "
                       "ORDER BY id DESC LIMIT 8").fetchall()
    L += ["", "## Recent runs (newest first)"]
    L += [f"- {r['ts'][:16]} | {fmt_money(r['total_value'])} ({r['pnl_pct']:+.2f}%) | {r['summary']}" for r in runs] or ["(none)"]

    trades = con.execute("SELECT ts, side, ticker, shares, price, notional, realized_pnl, status, reason, note "
                         "FROM trades ORDER BY id DESC LIMIT 15").fetchall()
    L += ["", "## Recent trades (newest first)"]
    for t in trades:
        extra = f", realized {fmt_money(t['realized_pnl'])}" if t["realized_pnl"] else ""
        if t["status"] == "filled":
            L.append(f"- {t['ts'][:16]} {t['side'].upper()} {t['ticker']} {t['shares']:.4f} @ {t['price']:.2f}"
                     f" ({fmt_money(t['notional'])}{extra}) — {t['reason']}")
        else:
            L.append(f"- {t['ts'][:16]} {t['side'].upper()} {t['ticker']} REJECTED ({t['note']}) — {t['reason']}")
    if not trades:
        L.append("(none)")
    L += ["", "Research as needed, then submit your decisions."]
    return "\n".join(L)


def run_tool(name, args, con):
    """Local tools shared by both backends."""
    if name == "get_quotes":
        return get_quotes((args.get("tickers") or [])[:20])
    if name == "get_price_history":
        return get_history(args.get("ticker", ""), args.get("period", "3mo"), args.get("interval", "1d"))
    if name == "get_my_records":
        return records(con, int(args.get("runs", 20)), int(args.get("trades", 50)), args.get("ticker"))
    return {"error": f"unknown tool {name}"}


# ============================================================
# Backend 1: API key (anthropic SDK)
# ============================================================
API_TOOLS_TEXT = """
Tools: get_quotes, get_price_history, get_my_records (your older runs/trades), web_search (news, earnings, macro).
Finish by calling submit_decisions exactly once (with an empty trades list if you don't trade)."""


def api_tool_specs(cfg):
    model_id = C.API_MODEL_IDS[cfg["model"]]
    search_type = "web_search_20250305" if model_id.startswith("claude-haiku") else "web_search_20260209"
    tools = []
    if cfg["web_search_max_uses"] > 0:
        tools.append({"type": search_type, "name": "web_search", "max_uses": cfg["web_search_max_uses"]})
    tools += [
        {"name": "get_quotes",
         "description": "Latest price, previous close and day change % for up to 20 tickers "
                        "(US stocks/ETFs; index symbols like ^VIX allowed for reference only).",
         "input_schema": {"type": "object", "properties": {
             "tickers": {"type": "array", "items": {"type": "string"}, "maxItems": 20}}, "required": ["tickers"]}},
        {"name": "get_price_history",
         "description": "Price history for one ticker: stats (return, high/low, SMA20/50/200, annualized vol) plus the last 60 bars.",
         "input_schema": {"type": "object", "properties": {
             "ticker": {"type": "string"},
             "period": {"type": "string", "enum": ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y"]},
             "interval": {"type": "string", "enum": ["1h", "1d", "1wk"]}}, "required": ["ticker"]}},
        {"name": "get_my_records",
         "description": "Your own past runs (summary, market view, value) and trades from the journal database, newest first.",
         "input_schema": {"type": "object", "properties": {
             "runs": {"type": "integer", "minimum": 1, "maximum": 200},
             "trades": {"type": "integer", "minimum": 1, "maximum": 500},
             "ticker": {"type": "string", "description": "Only trades for this ticker."}}}},
        {"name": "submit_decisions",
         "description": "Submit this run's final decisions. Call exactly once, at the end, even if you make no trades.",
         "input_schema": decision_schema()},
    ]
    return tools


def ask_api(state, cfg, user_prompt, con):
    import anthropic
    key = C.api_key(cfg)
    if not key:
        raise RuntimeError("API 模式需要 API key：在网页设置里填，或设置环境变量 ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=key)
    model_id = C.API_MODEL_IDS[cfg["model"]]
    use_fallback = not model_id.startswith("claude-haiku")
    system = system_prompt(state, cfg, API_TOOLS_TEXT)
    tools = api_tool_specs(cfg)
    messages = [{"role": "user", "content": user_prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    nudged = False

    for _ in range(cfg["max_turns"]):
        kwargs = dict(model=model_id, max_tokens=16000, system=system, tools=tools, messages=messages,
                      thinking={"type": "adaptive"}, output_config={"effort": cfg["effort"]})
        if use_fallback:
            # If a safety classifier declines, the server continues on a fallback model (no model list to maintain).
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            resp = client.messages.create(**kwargs)
        u = resp.usage
        usage["input_tokens"] += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        usage["web_searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0

        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude 拒绝了这次请求（stop_reason=refusal）")
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":       # server-side search still running: send the turn back to continue
            continue

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            if nudged:
                break
            messages.append({"role": "user", "content": "请调用 submit_decisions 提交本次决策（不交易也要提交）。"})
            nudged = True
            continue

        decision, results = None, []
        for tu in tool_uses:
            if tu.name == "submit_decisions":
                decision = tu.input
                content = "received"
            else:
                try:
                    content = json.dumps(run_tool(tu.name, tu.input or {}, con), ensure_ascii=False)
                except Exception as e:
                    content = f"tool error: {type(e).__name__}: {e}"
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content})
        if decision is not None:
            return decision, usage
        messages.append({"role": "user", "content": results})

    raise RuntimeError("Claude 没有在限定轮数内提交决策")


# ============================================================
# Backend 2: Claude subscription (Claude Code headless mode, claude -p)
# ============================================================
CLI_TOOLS_TEXT = """
Tools available in this session. Run these from the current directory exactly as shown (they print JSON):
- Quotes:   {py} trader.py quote SPY AAPL ^VIX          (up to 20 tickers)
- History:  {py} trader.py history NVDA 6mo 1d          (period 5d/1mo/3mo/6mo/1y/2y/5y; interval 1h/1d/1wk)
- Records:  {py} trader.py records 20 50 [TICKER]       (your older runs and trades, newest first)
- WebSearch / WebFetch for news, earnings and macro events (at most ~{n} searches).
Do not run any other commands and do not edit any files.
When you are done, return your decisions as the structured output."""


def find_claude():
    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:   # Windows npm install: call the native exe directly to avoid .cmd argument quoting issues
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    for p in candidates:
        if p.exists():
            return str(p)
    for name in ("claude.exe", "claude"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("找不到 claude 命令。订阅模式需要先安装 Claude Code 并登录：npm i -g @anthropic-ai/claude-code，然后运行 claude 登录")


def cli_available():
    try:
        return find_claude()
    except RuntimeError:
        return None


def ask_cli(state, cfg, user_prompt, con=None):
    sysp = system_prompt(state, cfg, CLI_TOOLS_TEXT.format(py=PY, n=cfg["web_search_max_uses"]))
    allowed = []
    for sub in ("quote", "history", "records"):
        allowed += [f"Bash({PY} trader.py {sub}:*)", f"PowerShell({PY} trader.py {sub}:*)"]
    if cfg["web_search_max_uses"] > 0:
        allowed += ["WebSearch", "WebFetch"]
    cmd = [find_claude(), "-p",
           "--output-format", "json",
           "--json-schema", json.dumps(decision_schema()),
           "--model", cfg["model"],
           "--max-turns", str(cfg["max_turns"]),
           "--system-prompt", sysp,
           "--permission-mode", "dontAsk",          # deny every tool that is not on the allowlist
           "--allowedTools", *allowed,
           "--disallowedTools", "Edit", "Write", "NotebookEdit", "Agent"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, input=user_prompt, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(C.CODE_DIR), timeout=cfg["timeout_sec"], creationflags=flags)
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else {}
    except json.JSONDecodeError:
        data = {}
    if not data:
        raise RuntimeError(f"claude -p 没有返回 JSON（exit {proc.returncode}）：{(proc.stderr or out)[:800]}")
    if data.get("is_error") or data.get("subtype") != "success":
        raise RuntimeError(f"claude -p 失败：{data.get('subtype')} {str(data.get('result'))[:800]}")
    decision = data.get("structured_output")
    if not decision:
        try:
            decision = json.loads(data.get("result") or "")
        except Exception:
            raise RuntimeError(f"没拿到结构化决策：{str(data.get('result'))[:800]}")
    u = data.get("usage") or {}
    usage = {
        "input_tokens": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        + (u.get("cache_read_input_tokens") or 0),
        "output_tokens": u.get("output_tokens") or 0,
        "web_searches": (u.get("server_tool_use") or {}).get("web_search_requests") or 0,
    }
    return decision, usage


def ask(state, cfg, user_prompt, con):
    t0 = _time.time()
    fn = ask_cli if cfg["backend"] == "subscription" else ask_api
    decision, usage = fn(state, cfg, user_prompt, con)
    usage["duration_sec"] = round(_time.time() - t0, 1)
    return decision, usage
