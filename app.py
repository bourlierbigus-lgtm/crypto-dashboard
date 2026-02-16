"""
加密货币 Web 仪表盘 — FastAPI 后端
复用 crypto_report.py 的数据采集逻辑
"""

import asyncio
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path

from crypto_report import (
    fetch_binance_klines, calc_indicators, calc_ahr999,
    fetch_fear_greed, fetch_binance_funding_rate,
    fetch_binance_open_interest, fetch_farside_etf,
    fetch_onchain_via_browser, HEADERS,
)

CST = timezone(timedelta(hours=8))
_cache: dict | None = None


def sanitize(obj):
    """递归将 numpy 类型转为 Python 原生类型，确保 JSON 可序列化"""
    import numpy as np
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def fetch_24h_change(symbol: str) -> float | None:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol}, headers=HEADERS, timeout=10,
        )
        r.raise_for_status()
        return round(float(r.json()["priceChangePercent"]), 2)
    except Exception:
        return None


def collect_all_data() -> dict:
    print("📡 采集数据中...")
    btc_df = fetch_binance_klines("BTCUSDT", 365)
    btc = calc_indicators(btc_df)
    eth_df = fetch_binance_klines("ETHUSDT", 365)
    eth = calc_indicators(eth_df)

    btc["change_24h"] = fetch_24h_change("BTCUSDT")
    eth["change_24h"] = fetch_24h_change("ETHUSDT")

    fng = fetch_fear_greed()
    etf = fetch_farside_etf()
    oi = fetch_binance_open_interest(btc["price"])
    funding = fetch_binance_funding_rate()
    ahr999 = calc_ahr999(btc_df)

    try:
        onchain = fetch_onchain_via_browser()
    except Exception:
        onchain = {"nupl": None, "mvrv": None, "mvrv_zscore": None,
                   "market_cap": None, "realized_cap": None}
    onchain["ahr999"] = ahr999

    signals = build_signals(btc, fng, funding, etf, ahr999)
    high_prob = (ahr999 is not None and ahr999 < 0.45
                 and btc["mas"].get("MA200") is not None
                 and btc["price"] < btc["mas"]["MA200"])

    now = datetime.now(CST)
    return sanitize({
        "updated_at": now.strftime("%Y-%m-%d %H:%M CST"),
        "btc": btc, "eth": eth,
        "fear_greed": fng, "etf": etf,
        "open_interest": oi, "funding_rate": funding,
        "onchain": onchain, "signals": signals,
        "high_probability_zone": high_prob,
    })


def build_signals(btc, fng, funding, etf, ahr999) -> list[dict]:
    signals = []
    price = btc["price"]
    ma200 = btc["mas"].get("MA200")

    if ma200:
        above = price >= ma200
        signals.append({
            "icon": "check" if above else "warn",
            "text": f"BTC 价格{'高于' if above else '低于'} MA200",
        })

    if ahr999 is not None:
        if ahr999 < 0.45:
            signals.append({"icon": "red", "text": f"AHR999 = {ahr999:.4f} < 0.45 (抄底区间)"})
        elif ahr999 < 1.2:
            signals.append({"icon": "yellow", "text": f"AHR999 = {ahr999:.4f} (定投区间)"})
        else:
            signals.append({"icon": "green", "text": f"AHR999 = {ahr999:.4f} > 1.2 (观望区间)"})

    v = fng["value"]
    if v <= 25:
        signals.append({"icon": "red", "text": f"市场极度恐慌 (FGI={v})"})
    elif v <= 45:
        signals.append({"icon": "yellow", "text": f"市场恐慌 (FGI={v})"})
    elif v <= 55:
        signals.append({"icon": "neutral", "text": f"市场中性 (FGI={v})"})
    elif v <= 75:
        signals.append({"icon": "green", "text": f"市场贪婪 (FGI={v})"})
    else:
        signals.append({"icon": "red", "text": f"市场极度贪婪 (FGI={v})"})

    if funding is not None:
        if funding < -0.01:
            signals.append({"icon": "red", "text": f"资金费率为负 ({funding}%)，空头占优"})
        elif funding > 0.05:
            signals.append({"icon": "yellow", "text": f"资金费率偏高 ({funding}%)，多头杠杆较重"})

    if etf and etf.get("recent_5d_flow_m") is not None:
        f5 = etf["recent_5d_flow_m"]
        if f5 < -500:
            signals.append({"icon": "red", "text": f"ETF 近5日大幅净流出 ({f5:+.1f}M)"})
        elif f5 > 500:
            signals.append({"icon": "green", "text": f"ETF 近5日大幅净流入 ({f5:+.1f}M)"})

    return signals


# ── FastAPI ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cache
    _cache = await asyncio.to_thread(collect_all_data)
    print("✅ 初始数据采集完成")
    yield

app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "templates" / "index.html").read_text("utf-8")
    return HTMLResponse(html)


@app.get("/api/report")
async def api_report():
    return _cache or {"error": "数据尚未就绪"}


@app.get("/api/refresh")
async def api_refresh():
    global _cache
    _cache = await asyncio.to_thread(collect_all_data)
    return _cache


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False)
