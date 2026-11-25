"""Investment style presets: guidance for Claude plus a per-position cap that the code enforces."""

STYLES = {
    "aggressive": {
        "label": "激进",
        "summary": "集中持有少数高成长、强势股，追求超额收益，能承受大回撤。",
        "max_position_pct": 0.40,
        "prompt": """Investment style: AGGRESSIVE GROWTH.
- Goal: maximize return and beat the benchmark by a wide margin; accept large drawdowns (30%+).
- Concentrate in your 3-6 highest-conviction ideas: growth stocks, momentum leaders, catalysts (earnings, product cycles).
- Sector ETFs and leveraged-free thematic ETFs are fine. Keep little idle cash when you have conviction.
- Cut losers that break your thesis quickly; let winners run. Act on news when the edge is clear.""",
    },
    "conservative": {
        "label": "保守",
        "summary": "保本优先、分散持仓、留现金缓冲，回撤小于大盘，收益可以略低于大盘。",
        "max_position_pct": 0.15,
        "prompt": """Investment style: CONSERVATIVE / CAPITAL PRESERVATION.
- Goal: protect capital first; aim for a smaller drawdown than the benchmark, even at the cost of some upside.
- Prefer broad and defensive ETFs (total market, dividend, low-volatility, short/intermediate Treasury ETFs)
  and large, profitable, low-volatility companies. Avoid speculative or highly volatile names.
- Keep a meaningful cash/Treasury buffer (typically 20-40%) and diversify across at least 5 holdings.
- Use predefined exit levels; reduce risk when volatility (VIX) spikes. Trade rarely.""",
    },
    "long_term": {
        "label": "长期稳健",
        "summary": "以宽基指数为核心、少量个股为卫星，低换手、分批建仓，长期跑赢或贴近大盘。",
        "max_position_pct": 0.60,
        "prompt": """Investment style: LONG-TERM, STEADY (core-satellite).
- Goal: the best long-term risk-adjusted return; think in years, not days.
- Core (60-90%): broad index ETFs. Satellite (0-40%): a few high-quality companies you would hold for years.
- Build positions gradually (dollar-cost averaging, buying weakness); rebalance occasionally.
- Very low turnover: most runs should end with no trades. Ignore intraday noise; only act on
  real changes to valuation, fundamentals or your plan.""",
    },
    "custom": {
        "label": "自定义",
        "summary": "完全按你写的风格描述来。",
        "max_position_pct": 1.0,
        "prompt": "",
    },
}


def style_prompt(cfg):
    s = STYLES[cfg["style"]]
    extra = (cfg.get("custom_style_prompt") or "").strip()
    if cfg["style"] == "custom":
        return "Investment style (written by the account owner, follow it):\n" + extra
    if extra:
        return s["prompt"] + "\n\nAdditional instructions from the account owner:\n" + extra
    return s["prompt"]


def position_cap(cfg):
    cap = cfg.get("max_position_pct")
    return float(cap) if cap is not None else STYLES[cfg["style"]]["max_position_pct"]


def catalog():
    return {k: {"label": v["label"], "summary": v["summary"], "max_position_pct": v["max_position_pct"]}
            for k, v in STYLES.items()}
