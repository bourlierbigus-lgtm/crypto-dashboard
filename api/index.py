"""
Vercel Serverless 入口 — 加密货币仪表盘
Playwright 在 Vercel 上不可用，NUPL/MVRV 使用备选方案
"""

import json
import time
import re
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()
CST = timezone(timedelta(hours=8))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}

_cache = {"data": None, "ts": 0}
CACHE_TTL = 300  # 5分钟缓存


# ── 数据采集 (精简版，无 Playwright) ──────────────────────

def fetch_klines(symbol: str, days: int = 365) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    all_data = []
    end_time = int(time.time() * 1000)
    remaining = days
    while remaining > 0:
        limit = min(remaining, 1000)
        r = requests.get(url, params={"symbol": symbol, "interval": "1d",
                                       "endTime": end_time, "limit": limit},
                         headers=HEADERS, timeout=15)
        if r.status_code == 451:
            # Binance 屏蔽美国 IP，尝试备用域名
            r = requests.get(url.replace("api.binance.com", "data-api.binance.vision"),
                             params={"symbol": symbol, "interval": "1d",
                                     "endTime": end_time, "limit": limit},
                             headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        all_data = data + all_data
        end_time = data[0][0] - 1
        remaining -= len(data)
        if len(data) < limit:
            break
    df = pd.DataFrame(all_data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    for col in ["close", "open", "high", "low", "volume"]:
        df[col] = df[col].astype(float)
    return df


def calc_indicators(df):
    close = df["close"]
    current = close.iloc[-1]
    mas = {}
    for name, p in {"MA30": 30, "MA40": 40, "MA120": 120, "MA200": 200, "MA365": 365}.items():
        mas[name] = round(float(close.rolling(p).mean().iloc[-1]), 2) if len(close) >= p else None
    change_60d = round(float((current - close.iloc[-61]) / close.iloc[-61] * 100), 2) if len(close) > 60 else None
    return {"price": round(float(current), 2), "mas": mas, "change_60d": change_60d}


def calc_ahr999(df):
    try:
        close = df["close"]
        current = float(close.iloc[-1])
        cost = float(close.iloc[-200:].mean()) if len(close) >= 200 else float(close.mean())
        days = (datetime.now() - datetime(2009, 1, 3)).days
        exp_val = 10 ** (5.84 * np.log10(days) - 17.01)
        return round(float((current / cost) * (current / exp_val)), 4)
    except Exception:
        return None


def fetch_24h(symbol):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr",
                         params={"symbol": symbol}, headers=HEADERS, timeout=10)
        return round(float(r.json()["priceChangePercent"]), 2)
    except Exception:
        return None


def fetch_fng():
    try:
        r = requests.get("https://api.alternative.me/fng/", params={"limit": 1}, timeout=10)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:
        return {"value": 0, "label": "N/A"}


def fetch_funding():
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                         params={"symbol": "BTCUSDT"}, timeout=10)
        return round(float(r.json()["lastFundingRate"]) * 100, 4)
    except Exception:
        return None


def fetch_oi(price):
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                         params={"symbol": "BTCUSDT"}, timeout=10)
        oi = float(r.json()["openInterest"])
        return {"oi_btc": round(oi, 2), "oi_usd": round(oi * price, 2)}
    except Exception:
        return {"oi_btc": None, "oi_usd": None}


def fetch_etf():
    try:
        r = requests.get("https://farside.co.uk/bitcoin-etf-flow-all-data",
                         headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        table = soup.find_all("table")[1]
        rows = table.find_all("tr")
        header = [h.text.strip() for h in rows[0].find_all(["th", "td"])]
        ti = header.index("Total") if "Total" in header else -1

        def pv(t):
            t = t.replace(",", "").strip()
            if not t or t == "-": return 0.0
            if t.startswith("(") and t.endswith(")"): return -float(t[1:-1])
            return float(t)

        data_rows = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells: continue
            dt = cells[0].text.strip()
            if any(x in dt for x in ["Total", "Average", "Maximum", "Minimum"]): continue
            data_rows.append(cells)

        if not data_rows: return None
        latest = data_rows[-1]
        date_str = latest[0].text.strip()
        total = pv(latest[ti].text) if ti >= 0 and ti < len(latest) else 0
        r5 = sum(pv(r[ti].text) if ti < len(r) else 0 for r in data_rows[-5:])
        return {"date": date_str, "daily_flow_m": round(total, 1), "recent_5d_flow_m": round(r5, 1)}
    except Exception:
        return None


def collect():
    btc_df = fetch_klines("BTCUSDT", 365)
    btc = calc_indicators(btc_df)
    eth_df = fetch_klines("ETHUSDT", 365)
    eth = calc_indicators(eth_df)
    btc["change_24h"] = fetch_24h("BTCUSDT")
    eth["change_24h"] = fetch_24h("ETHUSDT")

    fng = fetch_fng()
    etf = fetch_etf()
    oi = fetch_oi(btc["price"])
    funding = fetch_funding()
    ahr999 = calc_ahr999(btc_df)

    onchain = {"ahr999": ahr999, "nupl": None, "mvrv": None, "mvrv_zscore": None,
               "market_cap": None}

    # 从 GitHub 获取 Playwright 采集的链上数据
    try:
        r = requests.get(
            "https://raw.githubusercontent.com/bourlierbigus-lgtm/crypto-dashboard/main/data/onchain.json",
            timeout=5)
        if r.ok:
            gh_data = r.json()
            onchain["nupl"] = gh_data.get("nupl")
            onchain["mvrv"] = gh_data.get("mvrv")
            onchain["mvrv_zscore"] = gh_data.get("mvrv_zscore")
            if gh_data.get("market_cap"):
                onchain["market_cap"] = gh_data["market_cap"]
            if gh_data.get("realized_cap"):
                onchain["realized_cap"] = gh_data["realized_cap"]
    except Exception:
        pass

    # fallback: blockchain.info market cap
    if not onchain.get("market_cap"):
        try:
            r = requests.get("https://api.blockchain.info/charts/market-cap",
                             params={"timespan": "1days", "format": "json"}, timeout=10)
            if r.ok:
                vals = r.json().get("values", [])
                if vals:
                    onchain["market_cap"] = vals[-1]["y"]
        except Exception:
            pass

    signals = []
    ma200 = btc["mas"].get("MA200")
    if ma200:
        signals.append({"icon": "check" if btc["price"] >= ma200 else "warn",
                         "text": f"BTC 价格{'高于' if btc['price'] >= ma200 else '低于'} MA200"})
    if ahr999 is not None:
        if ahr999 < 0.45:
            signals.append({"icon": "red", "text": f"AHR999 = {ahr999:.4f} < 0.45 (抄底区间)"})
        elif ahr999 < 1.2:
            signals.append({"icon": "yellow", "text": f"AHR999 = {ahr999:.4f} (定投区间)"})
        else:
            signals.append({"icon": "green", "text": f"AHR999 = {ahr999:.4f} > 1.2 (观望区间)"})
    v = fng["value"]
    if v <= 25: signals.append({"icon": "red", "text": f"市场极度恐慌 (FGI={v})"})
    elif v <= 45: signals.append({"icon": "yellow", "text": f"市场恐慌 (FGI={v})"})
    elif v <= 55: signals.append({"icon": "neutral", "text": f"市场中性 (FGI={v})"})
    elif v <= 75: signals.append({"icon": "green", "text": f"市场贪婪 (FGI={v})"})
    else: signals.append({"icon": "red", "text": f"市场极度贪婪 (FGI={v})"})
    if funding is not None and funding < -0.01:
        signals.append({"icon": "red", "text": f"资金费率为负 ({funding}%)，空头占优"})
    if etf and etf.get("recent_5d_flow_m") is not None:
        f5 = etf["recent_5d_flow_m"]
        if f5 < -500: signals.append({"icon": "red", "text": f"ETF 近5日大幅净流出 ({f5:+.1f}M)"})
        elif f5 > 500: signals.append({"icon": "green", "text": f"ETF 近5日大幅净流入 ({f5:+.1f}M)"})

    high_prob = bool(ahr999 is not None and ahr999 < 0.45 and ma200 and btc["price"] < ma200)

    return {
        "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M CST"),
        "btc": btc, "eth": eth, "fear_greed": fng, "etf": etf,
        "open_interest": oi, "funding_rate": funding, "onchain": onchain,
        "signals": signals, "high_probability_zone": high_prob,
    }


def sanitize(obj):
    if isinstance(obj, dict): return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list): return [sanitize(v) for v in obj]
    if isinstance(obj, (np.integer,)): return int(obj)
    if isinstance(obj, (np.floating,)): return float(obj)
    if isinstance(obj, (np.bool_,)): return bool(obj)
    return obj


# ── Routes ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent.parent / "templates" / "index.html"
    if not html_path.exists():
        html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(html_path.read_text("utf-8"))


@app.get("/api/report")
async def report():
    now = time.time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    try:
        data = sanitize(collect())
        _cache["data"] = data
        _cache["ts"] = now
        return data
    except Exception as e:
        return {"error": str(e), "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")}


@app.get("/api/refresh")
async def refresh():
    data = sanitize(collect())
    _cache["data"] = data
    _cache["ts"] = time.time()
    return data
