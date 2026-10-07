"""
Daily Market Summary + S&P 500 Heatmap
---------------------------------------
1. Reads market data from Google Sheet (Score!A2:I2)
2. Screenshots S&P 500 heatmap from TradingView (embedded stock-heatmap widget)
3. Outputs: heatmap image in ./site/ + market text to GITHUB_OUTPUT
"""

import asyncio, os, shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

# ── Config ──────────────────────────────────────────────────────────────
SHEET_ID = "1oukBzlyEkFRzTKgmO_6-Zrw6JcEr8k1ZP4Mp-QisDY4"
GID = "404426642"  # "Score" tab

OUTPUT_DIR = "site"
os.makedirs(OUTPUT_DIR, exist_ok=True)
DATE_NY = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
HEATMAP_PATH = os.path.join(OUTPUT_DIR, f"sp500_heatmap_{DATE_NY}.png")
HEATMAP_LATEST = os.path.join(OUTPUT_DIR, "sp500_heatmap_latest.png")

REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# TradingView heatmap widget size. Must be fixed pixels — "100%" collapses
# the treemap to ~150px tall.
HEATMAP_WIDTH = 1600
HEATMAP_HEIGHT = 1000

# Optional: set PLAYWRIGHT_CHANNEL=chrome to use the system Chrome instead of
# Playwright's bundled Chromium (e.g. local runs where Chromium can't be
# downloaded). Leave unset in CI.
BROWSER_CHANNEL = os.environ.get("PLAYWRIGHT_CHANNEL") or None

TV_WIDGET_HTML = f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>html,body{{margin:0;background:#131722;}}</style></head>
<body>
<div class="tradingview-widget-container" style="width:{HEATMAP_WIDTH}px;height:{HEATMAP_HEIGHT}px;">
  <div class="tradingview-widget-container__widget" style="width:100%;height:100%;"></div>
  <script type="text/javascript"
    src="https://s3.tradingview.com/external-embedding/embed-widget-stock-heatmap.js" async>
  {{
    "exchanges": [],
    "dataSource": "SPX500",
    "grouping": "sector",
    "blockSize": "market_cap_basic",
    "blockColor": "change",
    "locale": "en",
    "symbolUrl": "",
    "colorTheme": "dark",
    "hasTopBar": false,
    "isDataSetEnabled": false,
    "isZoomEnabled": false,
    "hasSymbolTooltip": false,
    "isMonoSize": false,
    "width": {HEATMAP_WIDTH},
    "height": {HEATMAP_HEIGHT}
  }}
  </script>
</div>
</body></html>
"""

DETAILS_URL = "https://docs.google.com/spreadsheets/d/14yA2ZECdrf4z5qfmFC7_ctOjBZSUZbsqVtQw2pdpN0Y/edit?usp=sharing"


# ── 1. Google Sheet Reader ──────────────────────────────────────────────
def read_google_sheet():
    """Read row 2 (A2:I2) from the Score tab via CSV export — no API key needed."""
    import csv, io, requests

    export_url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/export?format=csv&gid={GID}"
    )
    resp = requests.get(export_url, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)  # row 1 (headers)
    row = next(reader)     # row 2 (data)

    if len(row) < 9:
        raise ValueError(f"Expected 9 columns (A-I), got {len(row)}: {row}")

    return {
        "date":   row[0],   # A2
        "sp500":  row[1],   # B2
        "nasdaq": row[2],   # C2
        "tsx":    row[3],   # D2
        "mags":   row[4],   # E2
        "btc":    row[5],   # F2
        "eth":    row[6],   # G2
        "usdcad": row[7],   # H2
        "gold":   row[8],   # I2
    }


def format_slack_message(d):
    """Build Slack mrkdwn text matching the desired format."""

    def fmt_pct(val):
        v = str(val).strip()
        return v if "%" in v else v + "%"

    def fmt_usdcad(val):
        v = str(val).strip()
        return v if v.startswith("$") else "$" + v

    lines = [
        f"Date: {d['date']} Market",
        "--------------------------------------",
        f":us: S&P 500: {fmt_pct(d['sp500'])}",
        f":us: Nasdaq: {fmt_pct(d['nasdaq'])}",
        f":flag-ca: TSX Comp: {fmt_pct(d['tsx'])}",
        f":seven: Magnificent7: {fmt_pct(d['mags'])}",
        f":bitcoin: Bitcoin: {fmt_pct(d['btc'])}",
        f":ethereum: Ethereum: {fmt_pct(d['eth'])}",
        f":chart_with_upwards_trend: USD/CAD: {fmt_usdcad(d['usdcad'])}",
        f":gold-nugget: Gold: {fmt_pct(d['gold'])}",
        "",
        "Details:",
        DETAILS_URL,
        "",
        "_Percentage change for BTC and ETH is measured relative to the ETF benchmark_",
    ]
    return "\n".join(lines)


# ── 2. Heatmap Screenshot ──────────────────────────────────────────────
async def load_widget_with_retries(page, attempts=3):
    """Load the TradingView widget page and wait for its iframe to appear."""
    for i in range(1, attempts + 1):
        try:
            print(f"[Heatmap] widget load attempt {i}/{attempts}")
            await page.set_content(TV_WIDGET_HTML, wait_until="networkidle", timeout=90_000)
            await page.wait_for_selector(".tradingview-widget-container iframe", timeout=60_000)
            return True
        except Exception as e:
            if i == attempts:
                print(f"[Heatmap][ERR] widget load failed: {e}")
            await asyncio.sleep(2 + i)
    return False


async def capture_heatmap():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            channel=BROWSER_CHANNEL,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": HEATMAP_WIDTH, "height": HEATMAP_HEIGHT + 200},
            user_agent=REAL_UA,
            java_script_enabled=True,
            accept_downloads=False,
        )
        page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        ok = await load_widget_with_retries(page, attempts=3)
        if not ok:
            await browser.close()
            raise RuntimeError("TradingView heatmap widget failed to load")

        # Let the treemap finish rendering inside the iframe
        print("[Heatmap] Waiting for treemap to render...")
        await asyncio.sleep(10)

        # Screenshot the widget container, fallback to full page
        saved = False
        try:
            el = await page.query_selector(".tradingview-widget-container")
            if el:
                await el.screenshot(path=HEATMAP_PATH)
                saved = True
        except Exception as e:
            print(f"[Heatmap][WARN] element screenshot failed: {e}")
        if not saved:
            await page.screenshot(path=HEATMAP_PATH, full_page=True)

        # Keep a "latest" copy
        try:
            shutil.copyfile(HEATMAP_PATH, HEATMAP_LATEST)
        except Exception as e:
            print(f"[Heatmap][WARN] latest copy failed: {e}")

        print(f"[Heatmap][OK] Saved: {HEATMAP_PATH}")
        await browser.close()


# ── Main ────────────────────────────────────────────────────────────────
async def main():
    print("=" * 50)
    print(f"Daily Market Bot — {DATE_NY}")
    print("=" * 50)

    # 1. Read Google Sheet
    sheet_data = read_google_sheet()
    message_text = format_slack_message(sheet_data)
    print(f"\n[Sheet] Data:\n{message_text}\n")

    # 2. Capture heatmap
    await capture_heatmap()

    # 3. Write message text to GITHUB_OUTPUT for the workflow
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            # Use multiline output syntax
            f.write("market_text<<EOF\n")
            f.write(message_text + "\n")
            f.write("EOF\n")
        print("[Output] Written to GITHUB_OUTPUT")
    else:
        print("[Output] No GITHUB_OUTPUT (local run)")

    print("\n[DONE] ✓")


if __name__ == "__main__":
    asyncio.run(main())
