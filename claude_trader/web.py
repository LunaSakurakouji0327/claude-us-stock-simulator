"""Web dashboard on the standard-library http.server (no extra dependencies). Listens on 127.0.0.1 by default."""

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import config as C
from .brain import cli_available
from .engine import STATUS, is_running, log, next_slot, run_once, scheduler_loop
from .market import get_quotes, now_et
from .portfolio import db_connect, default_state, reset_account, valuation
from .styles import catalog

STATIC = Path(__file__).resolve().parent / "static"


def _state_or_default(cfg):
    if C.STATE_PATH.exists():
        with open(C.STATE_PATH, encoding="utf-8") as f:
            return json.load(f), True
    return default_state(cfg), False


def api_status():
    cfg = C.load()
    state, started = _state_or_default(cfg)
    bm = state["benchmark"]
    quotes = get_quotes(list(state["positions"]) + [bm]) if started else {}
    val = valuation(state, quotes)
    bm_price = quotes.get(bm, {}).get("price")
    bm_pnl = (bm_price / state["benchmark_start_price"] - 1) * 100 if bm_price and state.get("benchmark_start_price") else None
    configured = C.CONFIG_PATH.exists()
    nxt = next_slot(cfg) if cfg["enabled"] and configured else None
    pending_cash_change = started and abs(cfg["starting_cash"] - state["starting_cash"]) > 1e-9
    return {
        "version": __version__,
        "now_et": now_et().isoformat(),
        "started": started,
        "account": {
            "starting_cash": state["starting_cash"], "cash": val["cash"], "positions_value": val["positions_value"],
            "total": val["total"], "pnl": val["total"] - state["starting_cash"],
            "pnl_pct": (val["total"] / state["starting_cash"] - 1) * 100,
            "benchmark": bm, "benchmark_pnl_pct": bm_pnl, "created_at": state["created_at"],
            "run_count": state["run_count"],
        },
        "positions": val["rows"],
        "strategy_notes": state["strategy_notes"] if started else "",
        "watchlist": state.get("watchlist", []),
        "running": is_running(),
        "last_result": STATUS.get("last_result"),
        "next_run_et": nxt.isoformat() if nxt else None,
        "pending_cash_change": pending_cash_change,
        "cli_found": bool(cli_available()),
        "configured": configured,
    }


def api_runs(limit=500):
    con = db_connect()
    runs = [dict(r) for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
    trades = {}
    for t in con.execute("SELECT * FROM trades ORDER BY id"):
        trades.setdefault(t["run_id"], []).append(dict(t))
    for r in runs:
        r["trades"] = trades.get(r["id"], [])
    return {"runs": runs}


def api_trades():
    con = db_connect()
    rows = [dict(t) for t in con.execute("SELECT * FROM trades ORDER BY id DESC")]
    filled = [t for t in rows if t["status"] == "filled"]
    sells = [t for t in filled if t["side"] == "sell" and t["realized_pnl"] is not None]
    wins = [t for t in sells if t["realized_pnl"] > 0]
    return {"trades": rows, "stats": {
        "filled": len(filled), "rejected": len(rows) - len(filled), "closed": len(sells), "wins": len(wins),
        "realized": sum(t["realized_pnl"] for t in sells)}}


def api_equity():
    con = db_connect()
    rows = con.execute("SELECT id, ts, total_value, pnl_pct, benchmark_pnl_pct FROM runs WHERE status='ok' ORDER BY id").fetchall()
    pts = [dict(r) for r in rows]
    peak, mdd = None, 0.0
    for p in pts:
        peak = p["total_value"] if peak is None else max(peak, p["total_value"])
        mdd = min(mdd, p["total_value"] / peak - 1)
    usage = con.execute("SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
                        "COALESCE(SUM(web_searches),0) s, COUNT(*) n FROM runs WHERE status='ok'").fetchone()
    errors = con.execute("SELECT COUNT(*) FROM runs WHERE status='error'").fetchone()[0]
    return {"points": pts, "max_drawdown_pct": mdd * 100,
            "usage": {"input_tokens": usage["i"], "output_tokens": usage["o"], "web_searches": usage["s"],
                      "runs": usage["n"], "errors": errors}}


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeUSStockSimulator"

    def log_message(self, fmt, *args):   # keep the console quiet
        pass

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # DNS-rebinding guard: only accept localhost or the configured host
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        allowed = {"127.0.0.1", "localhost", "::1", str(self.server.cfg_host).lower()}
        return host in allowed

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("请求太大")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    # ---------- GET ----------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        u = urlparse(self.path)
        p = u.path
        try:
            if p == "/api/status":
                return self._send(200, api_status())
            if p == "/api/config":
                return self._send(200, {"config": C.public(C.load()), "styles": catalog(), "defaults": C.DEFAULTS})
            if p == "/api/runs":
                q = parse_qs(u.query)
                return self._send(200, api_runs(int(q.get("limit", ["500"])[0])))
            if p == "/api/trades":
                return self._send(200, api_trades())
            if p == "/api/equity":
                return self._send(200, api_equity())
            if p == "/api/journal":
                txt = C.JOURNAL_PATH.read_text(encoding="utf-8") if C.JOURNAL_PATH.exists() else ""
                return self._send(200, txt.encode("utf-8"), "text/markdown; charset=utf-8")
            if p.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            # static files
            rel = "index.html" if p in ("/", "") else p.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype)
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # ---------- POST ----------
    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        # JSON only: a cross-site page sending application/json triggers a CORS preflight that we never
        # answer, so the browser blocks it.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(415, {"error": "需要 application/json"})
        try:
            body = self._body()
            p = urlparse(self.path).path
            if p == "/api/config":
                cur = C.load()
                new = {**cur, **{k: v for k, v in body.items() if k in C.DEFAULTS}}
                if not body.get("api_key"):          # empty = keep the existing key
                    new["api_key"] = cur.get("api_key", "")
                if body.get("clear_api_key"):
                    new["api_key"] = ""
                try:
                    saved = C.save(new)
                except C.ConfigError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"ok": True, "config": C.public(saved)})
            if p == "/api/message":
                cur = C.load()
                cur["message_to_claude"] = str(body.get("message", ""))
                C.save(cur)
                return self._send(200, {"ok": True})
            if p == "/api/run-now":
                if is_running():
                    return self._send(409, {"error": "已经在运行中"})
                force = bool(body.get("force", True))
                threading.Thread(target=run_once, kwargs={"force": force, "trigger": "web"}, daemon=True).start()
                return self._send(202, {"ok": True})
            if p == "/api/reset":
                if body.get("confirm") != "RESET":
                    return self._send(400, {"error": "需要确认"})
                dest = reset_account()
                return self._send(200, {"ok": True, "backup": str(dest) if dest else None})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def serve(with_scheduler=True, host=None, port=None):
    cfg = C.load()
    host = host or cfg["web_host"]
    port = port or cfg["web_port"]
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.cfg_host = host
    if with_scheduler:
        threading.Thread(target=scheduler_loop, daemon=True).start()
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    log(f"网页面板：http://{shown}:{port}" + ("（调度已开启）" if with_scheduler else "（只开网页，不自动运行）"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        log("注意：网页监听在非本机地址，局域网里的人也能打开并修改设置。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("已停止。")
