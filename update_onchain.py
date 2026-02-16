#!/usr/bin/env python3
"""
定时采集 NUPL/MVRV 数据（需要 Playwright）
运行后将数据写入 data/onchain.json 并推送到 GitHub
"""
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

def fetch_onchain():
    from playwright.sync_api import sync_playwright

    JS_EXTRACT = '''() => {
        const plot = document.querySelector(".js-plotly-plot");
        if (!plot || !plot.data) return null;
        return plot.data.map(t => ({ name: t.name, lastY: t.y ? t.y[t.y.length-1] : null }));
    }'''

    result = {"nupl": None, "mvrv": None, "mvrv_zscore": None,
              "market_cap": None, "realized_cap": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # NUPL
        try:
            page.goto("https://www.lookintobitcoin.com/charts/relative-unrealized-profit--loss/",
                       wait_until="networkidle", timeout=30000)
            page.wait_for_selector(".js-plotly-plot", timeout=10000)
            traces = page.evaluate(JS_EXTRACT)
            if traces:
                for t in traces:
                    if t.get("name") and "NUPL" in t["name"].upper():
                        result["nupl"] = round(float(t["lastY"]), 4) if t["lastY"] else None
                        break
        except Exception as e:
            print(f"NUPL error: {e}")

        # MVRV
        try:
            page.goto("https://www.lookintobitcoin.com/charts/mvrv-zscore/",
                       wait_until="networkidle", timeout=30000)
            page.wait_for_selector(".js-plotly-plot", timeout=10000)
            traces = page.evaluate(JS_EXTRACT)
            if traces:
                for t in traces:
                    name = t.get("name", "")
                    val = t.get("lastY")
                    if val is not None:
                        if name == "Z-Score":
                            result["mvrv_zscore"] = round(float(val), 4)
                        elif name == "MVRV":
                            result["mvrv"] = round(float(val), 4)
                        elif name == "Market Cap":
                            result["market_cap"] = float(val)
                        elif name == "Realized Cap":
                            result["realized_cap"] = float(val)
        except Exception as e:
            print(f"MVRV error: {e}")

        browser.close()

    return result

if __name__ == "__main__":
    print("📡 采集链上数据...")
    data = fetch_onchain()
    data["updated_at"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")
    print(f"   NUPL: {data['nupl']}, MVRV Z-Score: {data['mvrv_zscore']}")

    out = Path(__file__).parent / "data" / "onchain.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"✅ 已保存到 {out}")

    # 推送到 GitHub
    repo_dir = Path(__file__).parent
    subprocess.run(["git", "add", "data/onchain.json"], cwd=repo_dir)
    subprocess.run(["git", "commit", "-m", f"data: 更新链上数据 {data['updated_at']}"],
                   cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "push"], cwd=repo_dir, capture_output=True)
    print("✅ 已推送到 GitHub")
