#!/usr/bin/env python3
"""
加密货币每日决策日报生成器
数据源: Binance, Alternative.me, Farside Investors, blockchain.info
"""

import requests
import pandas as pd
import numpy as np
import json
import time
import re
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from pathlib import Path

# ─── 配置 ───────────────────────────────────────────────
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FUTURES = "https://fapi.binance.com"
ALTME_API = "https://api.alternative.me/fng/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}

CST = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════
# 第一步：数据采集
# ═══════════════════════════════════════════════════════════

def fetch_binance_klines(symbol: str, days: int = 365) -> pd.DataFrame:
    """从 Binance 获取日线数据"""
    url = f"{BINANCE_SPOT}/api/v3/klines"
    all_data = []
    end_time = int(time.time() * 1000)
    remaining = days

    while remaining > 0:
        limit = min(remaining, 1000)
        params = {"symbol": symbol, "interval": "1d", "endTime": end_time, "limit": limit}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
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
    df["date"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def calc_indicators(df: pd.DataFrame) -> dict:
    """计算 MA 和涨幅"""
    close = df["close"]
    current = close.iloc[-1]

    ma_periods = {"MA30": 30, "MA40": 40, "MA120": 120, "MA200": 200, "MA365": 365}
    mas = {}
    for name, period in ma_periods.items():
        mas[name] = round(close.rolling(period).mean().iloc[-1], 2) if len(close) >= period else None

    change_60d = None
    if len(close) > 60:
        change_60d = round((current - close.iloc[-61]) / close.iloc[-61] * 100, 2)

    return {"price": round(current, 2), "mas": mas, "change_60d": change_60d}


def calc_ahr999(btc_df: pd.DataFrame) -> float | None:
    """
    AHR999 = (当前价格 / 200日定投成本) × (当前价格 / 指数增长估值)
    指数增长估值 = 10^(5.84 × log10(币龄天数) - 17.01)
    """
    try:
        close = btc_df["close"]
        current = close.iloc[-1]
        cost_200d = close.iloc[-200:].mean() if len(close) >= 200 else close.mean()
        days = (datetime.now() - datetime(2009, 1, 3)).days
        exp_val = 10 ** (5.84 * np.log10(days) - 17.01)
        return round(float((current / cost_200d) * (current / exp_val)), 4)
    except Exception as e:
        print(f"  ⚠️ AHR999 计算失败: {e}")
        return None


def fetch_fear_greed() -> dict:
    """恐慌贪婪指数"""
    resp = requests.get(ALTME_API, params={"limit": 1}, timeout=10)
    resp.raise_for_status()
    d = resp.json()["data"][0]
    return {"value": int(d["value"]), "label": d["value_classification"]}


def fetch_binance_funding_rate() -> float | None:
    """Binance BTCUSDT 资金费率"""
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/premiumIndex",
                         params={"symbol": "BTCUSDT"}, timeout=10)
        r.raise_for_status()
        return round(float(r.json()["lastFundingRate"]) * 100, 4)
    except Exception as e:
        print(f"  ⚠️ Binance 资金费率获取失败: {e}")
        return None


def fetch_binance_open_interest(btc_price: float) -> dict:
    """Binance BTCUSDT 合约持仓"""
    try:
        r = requests.get(f"{BINANCE_FUTURES}/fapi/v1/openInterest",
                         params={"symbol": "BTCUSDT"}, timeout=10)
        r.raise_for_status()
        oi_btc = float(r.json()["openInterest"])
        return {"oi_btc": round(oi_btc, 2), "oi_usd": round(oi_btc * btc_price, 2)}
    except Exception as e:
        print(f"  ⚠️ Binance OI 获取失败: {e}")
        return {"oi_btc": None, "oi_usd": None}


def fetch_farside_etf() -> dict | None:
    """从 Farside Investors 获取 BTC ETF 净流入 (单位: 百万美元)"""
    try:
        r = requests.get("https://farside.co.uk/bitcoin-etf-flow-all-data",
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        tables = soup.find_all("table")
        if len(tables) < 2:
            return None

        table = tables[1]
        rows = table.find_all("tr")

        # 找表头中的 Total 列索引
        header_cells = rows[0].find_all(["th", "td"])
        cols = [h.text.strip() for h in header_cells]
        total_idx = cols.index("Total") if "Total" in cols else -1

        # 收集数据行 (排除汇总行)
        data_rows = []
        for row in rows[1:]:
            cells = row.find_all("td")
            if not cells:
                continue
            date_text = cells[0].text.strip()
            if any(x in date_text for x in ["Total", "Average", "Maximum", "Minimum"]):
                continue
            data_rows.append(cells)

        if not data_rows:
            return None

        latest = data_rows[-1]
        date_str = latest[0].text.strip()

        # 解析 Total 列
        def parse_val(text):
            text = text.replace(",", "").strip()
            if not text or text == "-":
                return 0.0
            if text.startswith("(") and text.endswith(")"):
                return -float(text[1:-1])
            return float(text)

        if total_idx >= 0 and total_idx < len(latest):
            total_flow = parse_val(latest[total_idx].text)
        else:
            # 手动求和
            total_flow = sum(parse_val(c.text) for c in latest[1:])

        # 最近5天总流入
        recent_5d = 0
        for row in data_rows[-5:]:
            if total_idx >= 0 and total_idx < len(row):
                recent_5d += parse_val(row[total_idx].text)

        return {
            "date": date_str,
            "daily_flow_m": round(total_flow, 1),  # 百万美元
            "recent_5d_flow_m": round(recent_5d, 1),
        }
    except Exception as e:
        print(f"  ⚠️ Farside ETF 获取失败: {e}")
        return None


def fetch_onchain_via_browser() -> dict:
    """
    通过 Playwright 浏览器从 LookIntoBitcoin 提取 NUPL, MVRV, Realized Cap
    页面使用 Plotly.js 渲染图表，数据存储在 DOM 元素的 .data 属性中
    """
    result = {"nupl": None, "mvrv": None, "mvrv_zscore": None,
              "market_cap": None, "realized_cap": None}

    JS_EXTRACT = '''() => {
        const plot = document.querySelector(".js-plotly-plot");
        if (!plot || !plot.data) return null;
        return plot.data.map(t => ({ name: t.name, lastY: t.y ? t.y[t.y.length-1] : null }));
    }'''

    charts = {
        "nupl": {
            "url": "https://www.lookintobitcoin.com/charts/relative-unrealized-profit--loss/",
            "extract": lambda traces: next(
                (t["lastY"] for t in traces
                 if t["name"] and "NUPL" in t["name"].upper()), None),
        },
        "mvrv": {
            "url": "https://www.lookintobitcoin.com/charts/mvrv-zscore/",
            "extract": lambda traces: {
                "zscore": next((t["lastY"] for t in traces if t["name"] == "Z-Score"), None),
                "mvrv": next((t["lastY"] for t in traces if t["name"] == "MVRV"), None),
                "market_cap": next((t["lastY"] for t in traces if t["name"] == "Market Cap"), None),
                "realized_cap": next((t["lastY"] for t in traces if t["name"] == "Realized Cap"), None),
            },
        },
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠️ Playwright 未安装，跳过链上指标")
        return result

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # NUPL
            try:
                page.goto(charts["nupl"]["url"], timeout=30000)
                page.wait_for_selector(".js-plotly-plot", timeout=15000)
                traces = page.evaluate(JS_EXTRACT)
                if traces:
                    val = charts["nupl"]["extract"](traces)
                    if val is not None:
                        result["nupl"] = round(float(val), 4)
            except Exception as e:
                print(f"  ⚠️ NUPL 提取失败: {e}")

            # MVRV
            try:
                page.goto(charts["mvrv"]["url"], timeout=30000)
                page.wait_for_selector(".js-plotly-plot", timeout=15000)
                traces = page.evaluate(JS_EXTRACT)
                if traces:
                    vals = charts["mvrv"]["extract"](traces)
                    if vals["zscore"] is not None:
                        result["mvrv_zscore"] = round(float(vals["zscore"]), 4)
                    if vals["mvrv"] is not None:
                        result["mvrv"] = round(float(vals["mvrv"]), 4)
                    if vals["market_cap"] is not None:
                        result["market_cap"] = vals["market_cap"]
                    if vals["realized_cap"] is not None:
                        result["realized_cap"] = vals["realized_cap"]
            except Exception as e:
                print(f"  ⚠️ MVRV 提取失败: {e}")

            browser.close()
    except Exception as e:
        print(f"  ⚠️ 浏览器启动失败: {e}")

    return result


# ═══════════════════════════════════════════════════════════
# 第二步：数据格式化
# ═══════════════════════════════════════════════════════════

def fmt_price(v):
    return f"${v:,.0f}" if v and v >= 10000 else f"${v:,.2f}" if v else "N/A"

def fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "N/A"

def fmt_flow(v):
    if v is None: return "N/A"
    return f"{v:+.1f}M" if abs(v) < 1000 else f"{v/1000:+.2f}B"

def fmt_oi(v):
    if v is None: return "N/A"
    return f"${v/1e9:.2f}B" if v >= 1e9 else f"${v/1e6:.0f}M"

def fmt_val(v, decimals=4):
    return f"{v:.{decimals}f}" if v is not None else "N/A"


def generate_report(btc, eth, fng, etf, oi, funding_rate, onchain) -> str:
    """生成 Markdown 日报"""
    now = datetime.now(CST)
    btc_price = btc["price"]
    btc_ma200 = btc["mas"].get("MA200")
    ahr999 = onchain.get("ahr999")

    lines = [
        f"# 📊 加密货币每日决策日报",
        f"**日期**: {now.strftime('%Y-%m-%d')}　**更新时间**: {now.strftime('%H:%M')} CST\n",
    ]

    # ── BTC ──
    lines.append("## BTC 行情概览\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 当前价格 | {fmt_price(btc_price)} |")
    for name in ["MA30", "MA40", "MA120", "MA200", "MA365"]:
        val = btc["mas"].get(name)
        diff = f" ({(btc_price - val) / val * 100:+.1f}%)" if val else ""
        lines.append(f"| {name} | {fmt_price(val)}{diff} |")
    lines.append(f"| 60日涨幅 | {fmt_pct(btc['change_60d'])} |")

    # ── ETH ──
    lines.append("\n## ETH 行情概览\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 当前价格 | {fmt_price(eth['price'])} |")
    for name in ["MA30", "MA40", "MA120", "MA200", "MA365"]:
        val = eth["mas"].get(name)
        diff = f" ({(eth['price'] - val) / val * 100:+.1f}%)" if val else ""
        lines.append(f"| {name} | {fmt_price(val)}{diff} |")
    lines.append(f"| 60日涨幅 | {fmt_pct(eth['change_60d'])} |")

    # ── 市场情绪与资金 ──
    lines.append("\n## 市场情绪与资金\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 恐慌贪婪指数 | {fng['value']} ({fng['label']}) |")

    if etf:
        lines.append(f"| BTC ETF 日净流入 | {fmt_flow(etf['daily_flow_m'])} ({etf['date']}) |")
        lines.append(f"| BTC ETF 近5日净流入 | {fmt_flow(etf['recent_5d_flow_m'])} |")
    else:
        lines.append("| BTC ETF 净流入 | N/A |")

    lines.append(f"| Binance BTC 合约持仓 | {fmt_oi(oi.get('oi_usd'))} ({fmt_val(oi.get('oi_btc'), 0)} BTC) |")

    fr_str = f"{funding_rate}%" if funding_rate is not None else "N/A"
    lines.append(f"| Binance 资金费率 | {fr_str} |")

    # ── 链上指标 ──
    lines.append("\n## 链上指标\n")
    lines.append("| 指标 | 数值 | 参考区间 |")
    lines.append("|------|------|----------|")
    lines.append(f"| AHR999 | {fmt_val(ahr999)} | <0.45 抄底, 0.45-1.2 定投, >1.2 观望 |")

    nupl = onchain.get("nupl")
    lines.append(f"| NUPL | {fmt_val(nupl)} | <0 投降, 0-0.25 希望, 0.25-0.5 乐观, >0.75 贪婪 |")

    mvrv = onchain.get("mvrv_zscore")
    lines.append(f"| MVRV Z-Score | {fmt_val(mvrv)} | <0 低估, 0-2 正常, >7 高估 |")

    mvrv_raw = onchain.get("mvrv")
    if mvrv_raw is not None:
        lines.append(f"| MVRV | {fmt_val(mvrv_raw)} |  |")

    mc = onchain.get("market_cap")
    if mc:
        lines.append(f"| BTC 总市值 | ${mc/1e12:.2f}T |  |")

    # ── 系统判断 ──
    lines.append("\n## 系统判断\n")
    signals = []

    if btc_ma200:
        signals.append("⚠️ BTC 价格低于 MA200" if btc_price < btc_ma200 else "✅ BTC 价格高于 MA200")

    if ahr999 is not None:
        if ahr999 < 0.45:
            signals.append(f"🔴 AHR999 = {ahr999:.4f} < 0.45 (抄底区间)")
        elif ahr999 < 1.2:
            signals.append(f"🟡 AHR999 = {ahr999:.4f} (定投区间)")
        else:
            signals.append(f"🟢 AHR999 = {ahr999:.4f} > 1.2 (观望区间)")

    if fng["value"] <= 25:
        signals.append(f"😱 市场极度恐慌 (FGI={fng['value']})")
    elif fng["value"] <= 45:
        signals.append(f"😟 市场恐慌 (FGI={fng['value']})")
    elif fng["value"] <= 55:
        signals.append(f"😐 市场中性 (FGI={fng['value']})")
    elif fng["value"] <= 75:
        signals.append(f"😊 市场贪婪 (FGI={fng['value']})")
    else:
        signals.append(f"🤑 市场极度贪婪 (FGI={fng['value']})")

    if funding_rate is not None:
        if funding_rate < -0.01:
            signals.append(f"📉 资金费率为负 ({funding_rate}%)，空头占优")
        elif funding_rate > 0.05:
            signals.append(f"📈 资金费率偏高 ({funding_rate}%)，多头杠杆较重")

    if etf and etf["recent_5d_flow_m"] is not None:
        if etf["recent_5d_flow_m"] < -500:
            signals.append(f"🔻 ETF 近5日大幅净流出 ({fmt_flow(etf['recent_5d_flow_m'])})")
        elif etf["recent_5d_flow_m"] > 500:
            signals.append(f"🔺 ETF 近5日大幅净流入 ({fmt_flow(etf['recent_5d_flow_m'])})")

    for s in signals:
        lines.append(f"- {s}")

    # 极高胜率区间
    if ahr999 is not None and btc_ma200 is not None:
        if ahr999 < 0.45 and btc_price < btc_ma200:
            lines.append("")
            lines.append("> 🚨 **系统进入极高胜率区间** — AHR999 < 0.45 且价格低于 MA200，"
                         "历史上此区间买入持有1年以上胜率极高。")

    lines.append(f"\n---\n*数据仅供参考，不构成投资建议。*")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════

def main():
    print("🚀 开始采集数据...\n")

    print("📈 获取 BTC 日线...")
    btc_df = fetch_binance_klines("BTCUSDT", 365)
    btc = calc_indicators(btc_df)
    print(f"   BTC: ${btc['price']:,.2f}")

    print("📈 获取 ETH 日线...")
    eth_df = fetch_binance_klines("ETHUSDT", 365)
    eth = calc_indicators(eth_df)
    print(f"   ETH: ${eth['price']:,.2f}")

    print("😱 获取恐慌贪婪指数...")
    fng = fetch_fear_greed()
    print(f"   FGI: {fng['value']} ({fng['label']})")

    print("📊 获取 BTC ETF 净流入 (Farside)...")
    etf = fetch_farside_etf()
    if etf:
        print(f"   ETF: {etf['date']} → {etf['daily_flow_m']:+.1f}M, 近5日: {etf['recent_5d_flow_m']:+.1f}M")
    else:
        print("   ETF: 获取失败")

    print("📊 获取 Binance 合约持仓...")
    oi = fetch_binance_open_interest(btc["price"])
    print(f"   OI: {oi}")

    print("📊 获取 Binance 资金费率...")
    funding = fetch_binance_funding_rate()
    print(f"   Funding: {funding}%")

    print("⛓️ 计算 AHR999...")
    ahr999 = calc_ahr999(btc_df)
    print(f"   AHR999: {ahr999}")

    print("⛓️ 获取 NUPL/MVRV (浏览器提取)...")
    onchain = fetch_onchain_via_browser()
    onchain["ahr999"] = ahr999
    print(f"   NUPL: {onchain.get('nupl')}, MVRV Z-Score: {onchain.get('mvrv_zscore')}")

    print("\n📝 生成日报...")
    report = generate_report(btc, eth, fng, etf, oi, funding, onchain)

    out_dir = Path(__file__).parent / "reports"
    out_dir.mkdir(exist_ok=True)
    filename = f"report_{datetime.now(CST).strftime('%Y%m%d_%H%M')}.md"
    out_path = out_dir / filename
    out_path.write_text(report, encoding="utf-8")

    print(f"\n✅ 日报已保存: {out_path}")
    print("\n" + "=" * 60)
    print(report)
    return report


if __name__ == "__main__":
    main()
