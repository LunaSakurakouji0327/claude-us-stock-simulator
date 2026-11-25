"""One full trading run, plus the scheduler."""

import json
import os
import threading
import time as _time
import traceback
from datetime import datetime, timedelta

from . import config as C
from .brain import ask, build_user_prompt, fmt_money
from .market import ET, get_quotes, market_is_open, normalize_symbol, now_et
from .portfolio import db_connect, execute_trades, load_state, save_state, valuation
from .styles import STYLES

CONTEXT_TICKERS = ["SPY", "QQQ", "IWM", "TLT", "^VIX"]
_THREAD_LOCK = threading.Lock()
STATUS = {"running": False, "started_at": None, "last_result": None}


def log(msg):
    print(f"[{now_et():%Y-%m-%d %H:%M:%S} ET] {msg}", flush=True)


def log_error(context):
    C.DATA.mkdir(parents=True, exist_ok=True)
    with open(C.ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n===== {now_et().isoformat()} | {context} =====\n")
        f.write(traceback.format_exc())
    log(f"出错：{context}（详情见 {C.ERROR_LOG}）")


# ------------------------------------------------------------
# Cross-process lock: keeps a web "run now" and a scheduled run from overlapping.
# ------------------------------------------------------------
def _acquire_file_lock():
    C.DATA.mkdir(parents=True, exist_ok=True)
    if C.LOCK_PATH.exists() and _time.time() - C.LOCK_PATH.stat().st_mtime > 3600:
        C.LOCK_PATH.unlink(missing_ok=True)          # a lock older than 1 hour is treated as stale
    try:
        fd = os.open(C.LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def is_running():
    return STATUS["running"] or (C.LOCK_PATH.exists() and _time.time() - C.LOCK_PATH.stat().st_mtime < 3600)


# ------------------------------------------------------------
# Journal (markdown for humans; the web UI and Claude read the database instead)
# ------------------------------------------------------------
def append_journal(state, after, decision, results, bm_pnl, usage, cfg):
    pnl_pct = (after["total"] / state["starting_cash"] - 1) * 100
    L = [f"\n## Run #{state['run_count']} · {now_et():%Y-%m-%d %H:%M} ET",
         f"**总资产 {fmt_money(after['total'])}**（{pnl_pct:+.2f}%）· 现金 {fmt_money(after['cash'])} · "
         f"{state['benchmark']} 同期 " + (f"{bm_pnl:+.2f}%" if bm_pnl is not None else "n/a"),
         "", f"**一句话：** {decision.get('summary', '')}", "",
         f"**市场判断：** {decision.get('market_view', '')}", ""]
    if results:
        L.append("**交易：**")
        for r in results:
            if r["status"] == "filled":
                extra = f"，已实现 {fmt_money(r['realized'])}" if r["realized"] is not None else ""
                L.append(f"- {'买入' if r['side'] == 'buy' else '卖出'} {r['ticker']} {r['shares']:.4f} 股 @ "
                         f"{r['price']:.2f}（{fmt_money(r['notional'])}{extra}）— {r['reason']}"
                         + (f"  _[{r['note']}]_" if r["note"] else ""))
            else:
                L.append(f"- ~~{r['side']} {r['ticker']}~~ 被拒：{r['note']} — {r['reason']}")
    else:
        L.append("**交易：** 无（继续持有）")
    if decision.get("reply_to_user"):
        L += ["", f"**回复你的留言：** {decision['reply_to_user']}"]
    if after["rows"]:
        L += ["", "| 持仓 | 股数 | 成本 | 现价 | 市值 | 占比 | 浮盈 |", "|---|---|---|---|---|---|---|"]
        for r in after["rows"]:
            L.append(f"| {r['ticker']} | {r['shares']:.4f} | {r['avg_cost']:.2f} | {r['price']:.2f} | "
                     f"{fmt_money(r['value'])} | {r['weight_pct']:.1f}% | {r['unrealized_pct']:+.2f}% |")
    L.append(f"\n<sub>{cfg['backend']}:{cfg['model']} · tokens in/out {usage['input_tokens']:,}/{usage['output_tokens']:,}"
             f" · 搜索 {usage['web_searches']} 次 · {usage.get('duration_sec', 0)} 秒</sub>")
    with open(C.JOURNAL_PATH, "a", encoding="utf-8") as f:
        if f.tell() == 0:
            f.write(f"# Claude 美股实时操作模拟器 · 交易日记\n\n起始资金 {fmt_money(state['starting_cash'])}，开始于 "
                    f"{state['created_at'][:10]}，风格 {STYLES[cfg['style']]['label']}，基准 {state['benchmark']}。\n")
        f.write("\n".join(L) + "\n")


# ------------------------------------------------------------
# One full run
# ------------------------------------------------------------
def run_once(force=False, trigger="manual"):
    """Return the run_id, or None when skipped or failed."""
    cfg = C.load()
    if not force and not market_is_open():
        log("美股现在没开盘（周末/节假日/盘外），跳过。")
        return None
    if not _THREAD_LOCK.acquire(blocking=False):
        log("已经有一次运行在进行中，跳过。")
        return None
    if not _acquire_file_lock():
        _THREAD_LOCK.release()
        log("另一个进程正在运行，跳过。")
        return None
    STATUS.update(running=True, started_at=now_et().isoformat())
    try:
        return _run(cfg, trigger)
    except Exception:
        log_error("run_once 异常")
        STATUS["last_result"] = {"ok": False, "error": traceback.format_exc(limit=1)}
        return None
    finally:
        STATUS["running"] = False
        C.LOCK_PATH.unlink(missing_ok=True)
        _THREAD_LOCK.release()


def _run(cfg, trigger):
    state = load_state(cfg)
    con = db_connect()
    bm = state["benchmark"]
    tickers = list(state["positions"]) + state.get("watchlist", []) + CONTEXT_TICKERS + [bm]
    quotes = get_quotes(tickers)
    if state["benchmark_start_price"] is None and quotes.get(bm, {}).get("price"):
        state["benchmark_start_price"] = quotes[bm]["price"]

    before = valuation(state, quotes)
    prompt = build_user_prompt(state, cfg, before, quotes, con)
    user_msg = (cfg.get("message_to_claude") or "").strip()
    log(f"开始 Run #{state['run_count'] + 1}（{trigger}），总资产 {fmt_money(before['total'])}，"
        f"风格 {STYLES[cfg['style']]['label']}，询问 {cfg['backend']}:{cfg['model']} …")

    try:
        decision, usage = ask(state, cfg, prompt, con)
    except Exception as e:
        log_error("调用 Claude 失败")
        con.execute("INSERT INTO runs(ts, status, trigger, cash, positions_value, total_value, pnl_pct, benchmark_price, "
                    "style, backend, model, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now_et().isoformat(), "error", trigger, before["cash"], before["positions_value"], before["total"],
                     (before["total"] / state["starting_cash"] - 1) * 100, quotes.get(bm, {}).get("price"),
                     cfg["style"], cfg["backend"], cfg["model"], str(e)[:2000]))
        con.commit()
        save_state(state)
        STATUS["last_result"] = {"ok": False, "error": str(e)[:500]}
        return None

    cur = con.execute("INSERT INTO runs(ts, status, trigger, style, backend, model) VALUES (?,?,?,?,?,?)",
                      (now_et().isoformat(), "running", trigger, cfg["style"], cfg["backend"], cfg["model"]))
    run_id = cur.lastrowid
    results = execute_trades(state, decision.get("trades") or [], run_id, con, before["total"], cfg)

    state["strategy_notes"] = (decision.get("strategy_notes") or state["strategy_notes"])[:4000]
    wl = [normalize_symbol(t) for t in (decision.get("watchlist") or []) if normalize_symbol(t)]
    state["watchlist"] = list(dict.fromkeys(wl))[:20]
    state["run_count"] += 1

    quotes = get_quotes(list(state["positions"]) + [bm])
    after = valuation(state, quotes)
    bm_price = quotes.get(bm, {}).get("price")
    bm_pnl = (bm_price / state["benchmark_start_price"] - 1) * 100 if bm_price and state["benchmark_start_price"] else None
    pnl_pct = (after["total"] / state["starting_cash"] - 1) * 100

    con.execute("UPDATE runs SET status='ok', cash=?, positions_value=?, total_value=?, pnl_pct=?, benchmark_price=?, "
                "benchmark_pnl_pct=?, market_view=?, summary=?, strategy_notes=?, reply_to_user=?, user_message=?, "
                "input_tokens=?, output_tokens=?, web_searches=?, duration_sec=? WHERE id=?",
                (after["cash"], after["positions_value"], after["total"], pnl_pct, bm_price, bm_pnl,
                 decision.get("market_view", ""), decision.get("summary", ""), state["strategy_notes"],
                 decision.get("reply_to_user", ""), user_msg,
                 usage["input_tokens"], usage["output_tokens"], usage["web_searches"], usage.get("duration_sec"), run_id))
    ts = now_et().isoformat()
    for r in after["rows"]:
        con.execute("INSERT INTO holdings VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, ts, r["ticker"], r["shares"], r["avg_cost"], r["price"], r["value"], r["unrealized_pct"]))
    con.commit()
    save_state(state)
    append_journal(state, after, decision, results, bm_pnl, usage, cfg)

    filled = [r for r in results if r["status"] == "filled"]
    log(f"完成 Run #{state['run_count']}：成交 {len(filled)} 笔，总资产 {fmt_money(after['total'])} "
        f"({pnl_pct:+.2f}%)，{bm} " + (f"{bm_pnl:+.2f}%" if bm_pnl is not None else "n/a"))
    log(f"Claude：{decision.get('summary', '')}")
    STATUS["last_result"] = {"ok": True, "run_id": run_id, "summary": decision.get("summary", "")}
    return run_id


# ------------------------------------------------------------
# Scheduling: starting at first_run_et, every interval_hours, during market hours
# ------------------------------------------------------------
def slots_for_day(cfg, day):
    h, m = map(int, cfg["first_run_et"].split(":"))
    t = datetime(day.year, day.month, day.day, h, m, tzinfo=ET)
    close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)
    out = []
    while t < close:
        out.append(t)
        t += timedelta(hours=cfg["interval_hours"])
    return out


def next_slot(cfg, now=None):
    now = now or now_et()
    for d in range(0, 10):
        day = (now + timedelta(days=d)).date()
        if day.weekday() >= 5:
            continue
        for t in slots_for_day(cfg, day):
            if t > now:
                return t
    return None


def due_slot(cfg, now=None, grace_min=30):
    """Return a slot that is due but has not run yet (tolerates grace_min minutes of delay, e.g. after sleep)."""
    now = now or now_et()
    if now.weekday() >= 5:
        return None
    past = [t for t in slots_for_day(cfg, now.date()) if t <= now < t + timedelta(minutes=grace_min)]
    if not past:
        return None
    slot = past[-1].isoformat()
    try:
        done = json.loads(C.SCHED_PATH.read_text(encoding="utf-8")).get("last_slot")
    except Exception:
        done = None
    return None if done == slot else slot


def mark_slot(slot):
    C.DATA.mkdir(parents=True, exist_ok=True)
    C.SCHED_PATH.write_text(json.dumps({"last_slot": slot, "at": now_et().isoformat()}), encoding="utf-8")


def tick():
    """Run once if a slot is due. Meant to be called by cron / Windows Task Scheduler every 5-15 minutes."""
    # Never trade on defaults: wait until the owner has saved settings once (config.json exists).
    if not C.CONFIG_PATH.exists():
        return None
    cfg = C.load()
    if not cfg["enabled"]:
        return None
    slot = due_slot(cfg)
    if not slot:
        return None
    mark_slot(slot)       # mark first, so a failed market-open check does not retry in a loop
    return run_once(force=False, trigger="schedule")


def scheduler_loop(stop_event=None):
    log(f"调度已启动，数据目录 {C.DATA}")
    last_announce = None
    while not (stop_event and stop_event.is_set()):
        try:
            cfg = C.load()
            configured = C.CONFIG_PATH.exists()
            nxt = next_slot(cfg) if cfg["enabled"] and configured else None
            if (nxt, configured) != last_announce:
                if not configured:
                    log("还没有保存过设置：打开网页面板的「设置」页保存一次之后才会自动运行。")
                else:
                    log(f"下一次运行：{nxt:%Y-%m-%d %H:%M} ET（本地 {nxt.astimezone():%m-%d %H:%M}）" if nxt
                        else "自动运行已关闭（在设置里打开）")
                last_announce = (nxt, configured)
            tick()
        except Exception:
            log_error("调度异常")
        for _ in range(30):
            if stop_event and stop_event.is_set():
                return
            _time.sleep(1)
