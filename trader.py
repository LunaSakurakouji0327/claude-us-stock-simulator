#!/usr/bin/env python3
"""Claude US Stock Simulator - command-line entry point.

  python trader.py start          web dashboard + scheduler (recommended)
  python trader.py web            web dashboard only, no automatic runs
  python trader.py scheduler      scheduler only, no web dashboard
  python trader.py tick           run if a slot is due (for cron / Windows Task Scheduler)
  python trader.py once [--force] run once now (--force ignores market hours)
  python trader.py report         print performance and export CSV
  python trader.py reset          back up and clear the account, restart with the configured capital

  Tools Claude itself calls in subscription mode (they print JSON):
  python trader.py quote SPY AAPL
  python trader.py history NVDA 6mo 1d
  python trader.py records 20 50 [TICKER]
"""

import argparse
import json
import sys


def _setup_output(cmd):
    # pythonw / Task Scheduler has no console: write output to data/run.log
    if sys.stdout is None and cmd not in ("quote", "history", "records"):
        from claude_trader import config as C
        C.DATA.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(C.RUN_LOG, "a", encoding="utf-8", buffering=1)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def report():
    import csv
    from claude_trader import config as C
    from claude_trader.brain import fmt_money
    from claude_trader.portfolio import db_connect, load_state
    from claude_trader.styles import STYLES

    if not C.DB_PATH.exists():
        print("还没有任何运行记录。")
        return
    cfg = C.load()
    con = db_connect()
    state = load_state(cfg)
    runs = con.execute("SELECT * FROM runs WHERE status='ok' ORDER BY id").fetchall()
    trades = con.execute("SELECT * FROM trades ORDER BY id").fetchall()
    if not runs:
        print("还没有成功的运行记录。")
        return
    values = [r["total_value"] for r in runs]
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    last = runs[-1]
    filled = [t for t in trades if t["status"] == "filled"]
    sells = [t for t in filled if t["side"] == "sell" and t["realized_pnl"] is not None]
    wins = [t for t in sells if t["realized_pnl"] > 0]

    print("=" * 56)
    print(f" Claude 模拟炒股报告   {runs[0]['ts'][:10]} → {last['ts'][:16]}")
    print("=" * 56)
    print(f" 风格          {STYLES[cfg['style']]['label']}    调用方式 {cfg['backend']}:{cfg['model']}")
    print(f" 起始资金      {fmt_money(state['starting_cash'])}")
    print(f" 当前总资产    {fmt_money(last['total_value'])}   ({last['pnl_pct']:+.2f}%)")
    if last["benchmark_pnl_pct"] is not None:
        print(f" {state['benchmark']} 同期     {last['benchmark_pnl_pct']:+.2f}%   "
              f"超额 {last['pnl_pct'] - last['benchmark_pnl_pct']:+.2f} 个百分点")
    print(f" 最大回撤      {mdd * 100:.2f}%（按每次运行的净值）")
    print(f" 运行次数      {len(runs)}    成交 {len(filled)} 笔（被拒 {len(trades) - len(filled)}）")
    if sells:
        print(f" 已平仓胜率    {len(wins)}/{len(sells)} = {len(wins) / len(sells) * 100:.0f}%   "
              f"已实现盈亏 {fmt_money(sum(t['realized_pnl'] for t in sells))}")
    print(f" 累计 tokens   in {sum(r['input_tokens'] or 0 for r in runs):,} / "
          f"out {sum(r['output_tokens'] or 0 for r in runs):,}")
    print(f" 当前现金      {fmt_money(state['cash'])}")
    for sym, p in state["positions"].items():
        print(f"   {sym:<6} {p['shares']:.4f} 股  成本 {p['avg_cost']:.2f}")
    print(f"\n 最新一句话：{last['summary']}")
    for name, rows in (("runs.csv", runs), ("trades.csv", trades)):
        with open(C.DATA / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(rows[0].keys() if rows else [])
            w.writerows([tuple(r) for r in rows])
    print(f"\n 已导出 {C.DATA / 'runs.csv'} 和 {C.DATA / 'trades.csv'}")


def main():
    ap = argparse.ArgumentParser(description="Claude US Stock Simulator / Claude 美股实时操作模拟器")
    ap.add_argument("cmd", choices=["start", "web", "scheduler", "tick", "once", "report", "reset",
                                    "quote", "history", "records"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--force", action="store_true", help="once：不检查是否开盘")
    ap.add_argument("--host", help="网页监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, help="网页端口（默认 8765）")
    a = ap.parse_args()
    _setup_output(a.cmd)

    if a.cmd == "quote":
        from claude_trader.market import get_quotes
        print(json.dumps(get_quotes(a.args[:20])))
    elif a.cmd == "history":
        from claude_trader.market import get_history
        x = a.args + [None, None, None]
        print(json.dumps(get_history(x[0] or "", x[1] or "3mo", x[2] or "1d")))
    elif a.cmd == "records":
        from claude_trader.portfolio import db_connect, records
        x = a.args + [None, None, None]
        print(json.dumps(records(db_connect(), int(x[0] or 20), int(x[1] or 50), x[2]), default=str))
    elif a.cmd in ("start", "web"):
        from claude_trader.web import serve
        serve(with_scheduler=(a.cmd == "start"), host=a.host, port=a.port)
    elif a.cmd == "scheduler":
        from claude_trader.engine import scheduler_loop
        try:
            scheduler_loop()
        except KeyboardInterrupt:
            pass
    elif a.cmd == "tick":
        from claude_trader.engine import log_error, tick
        try:
            tick()
        except Exception:
            log_error("tick 异常")
            sys.exit(1)
    elif a.cmd == "once":
        from claude_trader.engine import run_once
        sys.exit(0 if run_once(force=a.force, trigger="cli") else 1)
    elif a.cmd == "report":
        report()
    elif a.cmd == "reset":
        from claude_trader.portfolio import reset_account
        ok = input("会把 data/ 挪到备份目录、账户从头开始。输入 RESET 确认：") if sys.stdin and sys.stdin.isatty() else ""
        if ok.strip() != "RESET":
            print("已取消。")
            return
        dest = reset_account()
        print(f"已备份到 {dest}" if dest else "没有数据，无需重置。")


if __name__ == "__main__":
    main()
