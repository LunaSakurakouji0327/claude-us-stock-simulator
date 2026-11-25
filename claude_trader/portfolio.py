"""Account state, database, valuation and simulated order fills."""

import json
import math
import os
import shutil
import sqlite3
from datetime import datetime

from . import config as C
from .market import get_quotes, is_tradable_symbol, normalize_symbol, now_et
from .styles import position_cap

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, status TEXT, trigger TEXT,
  cash REAL, positions_value REAL, total_value REAL, pnl_pct REAL,
  benchmark_price REAL, benchmark_pnl_pct REAL,
  market_view TEXT, summary TEXT, strategy_notes TEXT, reply_to_user TEXT, user_message TEXT,
  style TEXT, backend TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, web_searches INTEGER, duration_sec REAL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, ts TEXT, ticker TEXT, side TEXT,
  shares REAL, price REAL, notional REAL, realized_pnl REAL,
  reason TEXT, confidence INTEGER, status TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS holdings(
  run_id INTEGER, ts TEXT, ticker TEXT, shares REAL, avg_cost REAL,
  price REAL, value REAL, unrealized_pct REAL
);
"""


def default_state(cfg):
    return {
        "created_at": now_et().isoformat(),
        "starting_cash": cfg["starting_cash"],
        "cash": cfg["starting_cash"],
        "positions": {},
        "benchmark": cfg["benchmark"],
        "benchmark_start_price": None,
        "strategy_notes": "(This is the first run. No notes yet.)",
        "watchlist": [],
        "run_count": 0,
    }


def load_state(cfg):
    C.DATA.mkdir(parents=True, exist_ok=True)
    if C.STATE_PATH.exists():
        with open(C.STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    s = default_state(cfg)
    save_state(s)
    return s


def save_state(state):
    C.DATA.mkdir(parents=True, exist_ok=True)
    tmp = C.STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, C.STATE_PATH)


def db_connect():
    C.DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(C.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def reset_account():
    """Move data/ to a backup folder so the next run starts over with the configured capital. Returns the backup path."""
    if not C.DATA.exists():
        return None
    if C.LOCK_PATH.exists():
        raise RuntimeError("正在运行中，等这次结束再重置")
    dest = C.ROOT / f"data_backup_{datetime.now():%Y%m%d_%H%M%S}"
    shutil.move(str(C.DATA), str(dest))
    return dest


def valuation(state, quotes):
    rows, pos_value = [], 0.0
    for sym, p in state["positions"].items():
        q = quotes.get(sym, {})
        price = q.get("price")
        stale = price is None
        if stale:
            price = p["avg_cost"]
        value = p["shares"] * price
        pos_value += value
        rows.append({
            "ticker": sym, "shares": p["shares"], "avg_cost": p["avg_cost"], "price": price,
            "value": value, "unrealized_pct": (price / p["avg_cost"] - 1) * 100 if p["avg_cost"] else 0.0,
            "day_change_pct": q.get("day_change_pct"), "price_missing": stale,
            "opened_at": p.get("opened_at"),
        })
    total = state["cash"] + pos_value
    for r in rows:
        r["weight_pct"] = r["value"] / total * 100 if total else 0
    rows.sort(key=lambda r: -r["value"])
    return {"cash": state["cash"], "positions_value": pos_value, "total": total, "rows": rows}


def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError):
        return None


def execute_trades(state, trades, run_id, con, total_value, cfg):
    """Simulate fills for Claude's orders. Sells run before buys; buys are scaled down to available cash and the position cap."""
    ts = now_et().isoformat()
    slip, fee = cfg["slippage"], cfg["commission"]
    cap = position_cap(cfg)
    results = []

    def record(t, side, sym, status, shares=0.0, price=0.0, notional=0.0, realized=None, note=""):
        conf = t.get("confidence")
        conf = int(conf) if isinstance(conf, (int, float)) else None
        con.execute("INSERT INTO trades(run_id, ts, ticker, side, shares, price, notional, realized_pnl, reason, "
                    "confidence, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, ts, sym, side, shares, price, notional, realized, t.get("reason", ""),
                     conf, status, note))
        results.append({"side": side, "ticker": sym, "status": status, "shares": shares, "price": price,
                        "notional": notional, "realized": realized, "reason": t.get("reason", ""), "note": note})

    ordered = [t for t in trades if t.get("action") == "sell"] + [t for t in trades if t.get("action") == "buy"]
    ordered += [t for t in trades if t.get("action") not in ("buy", "sell")]
    for t in ordered:
        side = t.get("action")
        sym = normalize_symbol(t.get("ticker", ""))
        if side not in ("buy", "sell"):
            record(t, str(side), sym, "rejected", note="action 必须是 buy 或 sell")
            continue
        if not is_tradable_symbol(sym):
            record(t, side, sym, "rejected", note="不支持的代码（只允许美股/ETF）")
            continue
        if side == "sell" and sym not in state["positions"]:
            record(t, side, sym, "rejected", note="没有该持仓")
            continue
        q = get_quotes([sym], fresh=True).get(sym, {})
        price = q.get("price")
        if not price:
            record(t, side, sym, "rejected", note=f"拿不到价格：{q.get('error', 'unknown')}")
            continue

        if side == "sell":
            pos = state["positions"][sym]
            fill = price * (1 - slip)
            if t.get("sell_all"):
                shares = pos["shares"]
            elif _num(t.get("shares")):
                shares = _num(t["shares"])
            elif _num(t.get("amount_usd")):
                shares = _num(t["amount_usd"]) / fill
            else:
                record(t, side, sym, "rejected", note="没给数量")
                continue
            note = ""
            if shares > pos["shares"] or pos["shares"] - shares < 1e-6:
                note = "超过持仓，按全部卖出" if shares > pos["shares"] + 1e-9 else ""
                shares = pos["shares"]
            if not cfg["allow_fractional"] and shares != pos["shares"]:
                shares = math.floor(shares)
            if shares <= 0 or (shares * fill < cfg["min_trade_usd"] and shares != pos["shares"]):
                record(t, side, sym, "rejected", note="数量太小")
                continue
            proceeds = shares * fill - fee
            realized = (fill - pos["avg_cost"]) * shares - fee
            state["cash"] += proceeds
            pos["shares"] -= shares
            if pos["shares"] < 1e-6:
                del state["positions"][sym]
            record(t, side, sym, "filled", shares, fill, shares * fill, realized, note)

        else:  # buy
            fill = price * (1 + slip)
            if _num(t.get("shares")):
                shares = _num(t["shares"])
            elif _num(t.get("amount_usd")):
                shares = _num(t["amount_usd"]) / fill
            else:
                record(t, side, sym, "rejected", note="没给数量")
                continue
            notes = []
            if cap < 1.0:
                existing = state["positions"].get(sym, {}).get("shares", 0) * price
                room = max(0.0, cap * total_value - existing)
                if shares * fill > room:
                    shares = room / fill
                    notes.append(f"受单只上限 {cap:.0%} 限制缩小")
            avail = state["cash"] - fee
            if shares * fill > avail:
                shares = max(0.0, avail / fill)
                notes.append("现金不足，按可用现金缩小")
            if not cfg["allow_fractional"]:
                shares = math.floor(shares)
            shares = math.floor(shares * 1e6) / 1e6
            if shares <= 0 or shares * fill < cfg["min_trade_usd"]:
                record(t, side, sym, "rejected", note="；".join(notes + ["金额太小或现金不够"]))
                continue
            cost = shares * fill
            state["cash"] -= cost + fee
            pos = state["positions"].get(sym)
            if pos:
                new_shares = pos["shares"] + shares
                pos["avg_cost"] = (pos["avg_cost"] * pos["shares"] + cost + fee) / new_shares
                pos["shares"] = new_shares
            else:
                state["positions"][sym] = {"shares": shares, "avg_cost": (cost + fee) / shares, "opened_at": ts}
            record(t, side, sym, "filled", shares, fill, cost, None, "；".join(notes))

    if abs(state["cash"]) < 1e-6:
        state["cash"] = 0.0
    return results


def records(con, n_runs=20, n_trades=50, ticker=None):
    """Past runs and trades, newest first (read by Claude and by the web UI)."""
    runs = con.execute("SELECT id, ts, status, total_value, pnl_pct, benchmark_pnl_pct, summary, market_view, "
                       "reply_to_user, error FROM runs ORDER BY id DESC LIMIT ?", (n_runs,)).fetchall()
    if ticker:
        trades = con.execute("SELECT * FROM trades WHERE ticker=? ORDER BY id DESC LIMIT ?",
                             (normalize_symbol(ticker), n_trades)).fetchall()
    else:
        trades = con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n_trades,)).fetchall()
    return {"runs": [dict(r) for r in runs], "trades": [dict(t) for t in trades]}
