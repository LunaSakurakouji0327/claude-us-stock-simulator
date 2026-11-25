"""Configuration: read/write config.json with defaults and validation. Shared by the web UI and the CLI."""

import copy
import json
import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent          # where trader.py lives
# config.json and data/ live here; override with CLAUDE_TRADER_HOME to keep several accounts apart
ROOT = Path(os.environ.get("CLAUDE_TRADER_HOME") or CODE_DIR)
CONFIG_PATH = ROOT / "config.json"
DATA = ROOT / "data"
STATE_PATH = DATA / "state.json"
DB_PATH = DATA / "trader.db"
JOURNAL_PATH = DATA / "journal.md"
ERROR_LOG = DATA / "errors.log"
RUN_LOG = DATA / "run.log"
SCHED_PATH = DATA / "scheduler.json"
LOCK_PATH = DATA / ".run.lock"

DEFAULTS = {
    # ---- account ----
    "starting_cash": 10000.0,          # starting capital (USD); a change takes effect after an account reset
    "benchmark": "SPY",                # benchmark to compare against

    # ---- investment style ----
    "style": "long_term",              # aggressive / conservative / long_term / custom
    "custom_style_prompt": "",         # custom style text; with a preset style it is added as extra instructions
    "max_position_pct": None,          # max weight of one position (0-1); None = the style's default

    # ---- schedule ----
    "enabled": True,                   # run automatically during market hours
    "interval_hours": 2.0,             # hours between checks during market hours (0.5-6.5)
    "first_run_et": "09:45",           # first check of the day, US Eastern time

    # ---- how Claude is called ----
    "backend": "subscription",         # subscription = your Claude plan via Claude Code; api = pay-as-you-go API key
    "model": "opus",                   # opus / sonnet / haiku
    "api_key": "",                     # or set the ANTHROPIC_API_KEY environment variable (recommended)
    "effort": "high",                  # API mode only: low / medium / high / xhigh / max
    "web_search_max_uses": 5,          # max web searches per run
    "max_turns": 40,                   # max tool-use turns per run
    "timeout_sec": 1200,               # timeout per run (seconds)

    # ---- simulated fills ----
    "slippage": 0.0005,                # 0.05% slippage
    "commission": 0.0,                 # commission per trade (USD)
    "allow_fractional": True,          # allow fractional shares
    "min_trade_usd": 5.0,

    # ---- message to Claude (included in every run) ----
    "message_to_claude": "",

    # ---- web dashboard ----
    "web_host": "127.0.0.1",
    "web_port": 8765,
}

STYLE_KEYS = ("aggressive", "conservative", "long_term", "custom")
BACKENDS = ("subscription", "api")
MODELS = ("opus", "sonnet", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")

API_MODEL_IDS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}


class ConfigError(ValueError):
    pass


def _hhmm_ok(s):
    try:
        h, m = map(int, str(s).split(":"))
        return 9 <= h <= 15 and 0 <= m < 60 and (h, m) >= (9, 30)
    except Exception:
        return False


def validate(cfg):
    """Return a cleaned config, or raise ConfigError (its message is shown in the web UI)."""
    c = copy.deepcopy(DEFAULTS)
    for k in DEFAULTS:
        if k in cfg:
            c[k] = cfg[k]
    try:
        c["starting_cash"] = float(c["starting_cash"])
        c["interval_hours"] = float(c["interval_hours"])
        c["slippage"] = float(c["slippage"])
        c["commission"] = float(c["commission"])
        c["min_trade_usd"] = float(c["min_trade_usd"])
        c["web_search_max_uses"] = int(c["web_search_max_uses"])
        c["max_turns"] = int(c["max_turns"])
        c["timeout_sec"] = int(c["timeout_sec"])
        c["web_port"] = int(c["web_port"])
        if c["max_position_pct"] in ("", None):
            c["max_position_pct"] = None
        else:
            c["max_position_pct"] = float(c["max_position_pct"])
    except (TypeError, ValueError) as e:
        raise ConfigError(f"数字格式不对：{e}")
    for k in ("enabled", "allow_fractional"):
        c[k] = c[k] in (True, "true", "1", 1, "on")

    if not 100 <= c["starting_cash"] <= 100_000_000:
        raise ConfigError("本金需要在 $100 到 $100,000,000 之间")
    if not 0.5 <= c["interval_hours"] <= 6.5:
        raise ConfigError("检查间隔需要在 0.5 到 6.5 小时之间（美股一天只开盘 6.5 小时）")
    if not _hhmm_ok(c["first_run_et"]):
        raise ConfigError("第一次检查时间要在美东 09:30 到 15:59 之间，格式 HH:MM")
    if c["style"] not in STYLE_KEYS:
        raise ConfigError(f"风格必须是 {STYLE_KEYS} 之一")
    if c["style"] == "custom" and not str(c["custom_style_prompt"]).strip():
        raise ConfigError("选了自定义风格，请写一段风格描述")
    if c["backend"] not in BACKENDS:
        raise ConfigError(f"调用方式必须是 {BACKENDS} 之一")
    if c["model"] not in MODELS:
        raise ConfigError(f"模型必须是 {MODELS} 之一")
    if c["effort"] not in EFFORTS:
        raise ConfigError(f"effort 必须是 {EFFORTS} 之一")
    if c["max_position_pct"] is not None and not 0.05 <= c["max_position_pct"] <= 1.0:
        raise ConfigError("单只持仓上限需要在 5% 到 100% 之间")
    if not 0 <= c["slippage"] <= 0.05:
        raise ConfigError("滑点需要在 0 到 5% 之间")
    if not 0 <= c["web_search_max_uses"] <= 20:
        raise ConfigError("搜索次数需要在 0 到 20 之间")
    if not 5 <= c["max_turns"] <= 100:
        raise ConfigError("最大轮数需要在 5 到 100 之间")
    c["benchmark"] = str(c["benchmark"]).strip().upper() or "SPY"
    c["custom_style_prompt"] = str(c["custom_style_prompt"])[:3000]
    c["message_to_claude"] = str(c["message_to_claude"])[:2000]
    c["api_key"] = str(c["api_key"]).strip()
    return c


def load():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        try:
            return validate(raw)
        except ConfigError:
            # A badly hand-edited field must not stop the app: fall back to that field's default.
            merged = copy.deepcopy(DEFAULTS)
            for k, v in raw.items():
                if k in DEFAULTS:
                    try:
                        validate({**DEFAULTS, k: v})
                        merged[k] = v
                    except ConfigError:
                        pass
            return validate(merged)
    return copy.deepcopy(DEFAULTS)


def save(cfg):
    c = validate(cfg)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return c


def api_key(cfg):
    return cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""


def public(cfg):
    """Config for the web UI, with the API key masked."""
    c = dict(cfg)
    k = api_key(cfg)
    c["api_key"] = ""
    c["api_key_hint"] = (k[:7] + "…" + k[-4:]) if len(k) > 12 else ("已设置" if k else "")
    c["api_key_from_env"] = bool(os.environ.get("ANTHROPIC_API_KEY")) and not cfg.get("api_key")
    return c
