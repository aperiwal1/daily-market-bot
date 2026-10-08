"""
Daily Market Summary + S&P 500 Heatmap
---------------------------------------
1. Reads market data from Google Sheet (Score!A2:I2)
2. Screenshots S&P 500 heatmap from TradingView (full heatmap page), after
   verifying the data it draws is live (not stale/end-of-day)
3. Outputs: heatmap image in ./site/ + market text to GITHUB_OUTPUT
"""

import asyncio, json, os, shutil
from datetime import datetime
from urllib.parse import quote
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

# Full TradingView heatmap page. (The embeddable widget is NOT used: it is
# served "end of day" data that is often a day or more stale.)
HEATMAP_THEME = "dark"   # "light" or "dark"
HEATMAP_CONFIG = {
    "dataSource": "SPX500",
    "blockColor": "change",
    "blockSize": "market_cap_basic",
    "grouping": "sector",
}
HEATMAP_URL = "https://www.tradingview.com/heatmap/stock/#" + quote(
    json.dumps(HEATMAP_CONFIG, separators=(",", ":"))
)
VIEWPORT = {"width": 1600, "height": 1200}
LEGEND_HEIGHT = 45  # color legend drawn just below the treemap canvas

# Accuracy checks — the heatmap is only posted if all pass
MIN_STOCKS = 490            # S&P 500 has ~503 constituents
MIN_LIVE_SHARE = 0.95       # share of stocks that must be live, not "endofday"
SPOT_CHECK_COUNT = 10       # largest stocks re-fetched independently...
SPOT_CHECK_MIN_MATCH = 8    # ...and at least this many must agree
SPOT_CHECK_TOLERANCE = 0.25 # percentage points
LOAD_ATTEMPTS = 3

# Optional: set PLAYWRIGHT_CHANNEL=chrome to use the system Chrome instead of
# Playwright's bundled Chromium (e.g. local runs where Chromium can't be
# downloaded). Leave unset in CI.
BROWSER_CHANNEL = os.environ.get("PLAYWRIGHT_CHANNEL") or None

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
# Bounding box of the treemap canvas, or null until it has rendered
CANVAS_JS = """() => {
    const r = [...document.querySelectorAll('.js-market-heatmap canvas')]
        .map(e => e.getBoundingClientRect())
        .find(r => r.width > 600 && r.height > 400);
    return r ? {x: r.x, y: r.y, width: r.width, height: r.height} : null;
}"""

# Independent fetch of the % change for a list of tickers (runs in the page)
SPOT_CHECK_JS = """async (tickers) => {
    const r = await fetch("https://scanner.tradingview.com/america/scan", {
        method: "POST",
        headers: {"Content-Type": "text/plain;charset=UTF-8"},
        body: JSON.stringify({symbols: {tickers: tickers}, columns: ["change"]}),
    });
    return await r.json();
}"""


async def wait_for_heatmap_data(scan, timeout=30):
    for _ in range(timeout * 2):
        if "rows" in scan:
            return True
        await asyncio.sleep(0.5)
    return False


def check_freshness(rows, cols):
    """Enough stocks, and nearly all live rather than stale end-of-day data."""
    if len(rows) < MIN_STOCKS:
        return False, f"only {len(rows)} stocks in heatmap data (need {MIN_STOCKS})"
    ui = cols.index("update_mode")
    live = sum(1 for r in rows if r["d"][ui] != "endofday")
    share = live / len(rows)
    msg = f"{len(rows)} stocks, {share:.0%} live"
    if share < MIN_LIVE_SHARE:
        return False, msg + f" (need {MIN_LIVE_SHARE:.0%}) — data is stale"
    return True, msg


async def spot_check(page, rows, cols):
    """Re-fetch the largest stocks independently and compare % change."""
    ci, mi, ni = cols.index("change"), cols.index("market_cap_basic"), cols.index("name")
    top = sorted(rows, key=lambda r: -(r["d"][mi] or 0))[:SPOT_CHECK_COUNT]
    fresh = await page.evaluate(SPOT_CHECK_JS, [r["s"] for r in top])
    fresh_change = {r["s"]: r["d"][0] for r in fresh.get("data", [])}

    matched, details = 0, []
    for r in top:
        shown, now = r["d"][ci], fresh_change.get(r["s"])
        ok = shown is not None and now is not None and abs(shown - now) <= SPOT_CHECK_TOLERANCE
        matched += ok
        details.append(f"{r['d'][ni]} {shown:+.2f}/{now:+.2f}" if shown is not None and now is not None
                       else f"{r['d'][ni]} n/a")
    msg = f"spot check {matched}/{len(top)} match (shown/fresh): " + ", ".join(details)
    return matched >= SPOT_CHECK_MIN_MATCH, msg


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
            viewport=VIEWPORT,
            user_agent=REAL_UA,
            java_script_enabled=True,
            accept_downloads=False,
        )
        await context.add_cookies([{
            "name": "theme", "value": HEATMAP_THEME,
            "domain": ".tradingview.com", "path": "/",
        }])
        page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
        """)

        # Capture the exact data the heatmap draws
        scan = {}

        async def on_response(resp):
            if ("scanner.tradingview.com" in resp.url and "heatmap" in resp.url
                    and resp.request.method == "POST"):
                try:
                    scan["cols"] = json.loads(resp.request.post_data)["columns"]
                    scan["rows"] = (await resp.json())["data"]
                except Exception as e:
                    print(f"[Heatmap][WARN] could not read heatmap data: {e}")

        page.on("response", on_response)

        clip = None
        for attempt in range(1, LOAD_ATTEMPTS + 1):
            scan.clear()
            print(f"[Heatmap] load attempt {attempt}/{LOAD_ATTEMPTS}")
            try:
                await page.goto(HEATMAP_URL, wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_function(CANVAS_JS, timeout=60_000)
                if not await wait_for_heatmap_data(scan):
                    raise RuntimeError("heatmap data never arrived")

                # Dismiss cookie / sign-in popups (best effort)
                for sel in [
                    'button:has-text("Accept all")',
                    'button:has-text("Accept")',
                    'button:has-text("Got it")',
                    '[aria-label="Close"]',
                ]:
                    try:
                        await page.locator(sel).first.click(timeout=1000)
                        await asyncio.sleep(0.4)
                    except:
                        pass

                # Let logos and labels finish drawing
                await asyncio.sleep(10)

                ok, msg = check_freshness(scan["rows"], scan["cols"])
                print(f"[Heatmap] freshness: {msg}")
                if ok:
                    ok, msg = await spot_check(page, scan["rows"], scan["cols"])
                    print(f"[Heatmap] {msg}")
                if ok:
                    canvas = await page.evaluate(CANVAS_JS)
                    clip = {
                        "x": 0,
                        "y": canvas["y"],
                        "width": VIEWPORT["width"],
                        "height": min(canvas["height"] + LEGEND_HEIGHT, VIEWPORT["height"] - canvas["y"]),
                    }
                    break
            except Exception as e:
                print(f"[Heatmap][WARN] attempt {attempt} failed: {e}")
            await asyncio.sleep(5)

        if clip is None:
            await browser.close()
            raise RuntimeError("Heatmap data failed accuracy checks — not posting")

        await page.screenshot(path=HEATMAP_PATH, clip=clip)

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
