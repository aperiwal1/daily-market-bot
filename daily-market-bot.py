"""
Daily Market Summary + S&P 500 Heatmap
---------------------------------------
1. Reads market data from Google Sheet (Score!A2:I2)
2. Screenshots S&P 500 heatmap from Finviz
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

FINVIZ_URL = "https://finviz.com/map.ashx"
REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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

    lines = [
        f"*Date: {d['date']} Market*",
        "───────────────────────────",
        f":us: S&P 500: {fmt_pct(d['sp500'])}",
        f":us: Nasdaq: {fmt_pct(d['nasdaq'])}",
        f":flag-ca: TSX Comp: {fmt_pct(d['tsx'])}",
        f":seven: Magnificent7: {fmt_pct(d['mags'])}",
        f":bitcoin: Bitcoin: {fmt_pct(d['btc'])}",
        f":ethereum: Ethereum: {fmt_pct(d['eth'])}",
        f":chart_with_upwards_trend: USD/CAD: {d['usdcad'] if d['usdcad'].startswith('
        f":gold_ingot: Gold: {fmt_pct(d['gold'])}",
    ]
    return "\n".join(lines)


# ── 2. Heatmap Screenshot ──────────────────────────────────────────────
async def goto_with_retries(page, url, attempts=3):
    for i in range(1, attempts + 1):
        try:
            print(f"[Heatmap] goto attempt {i}/{attempts}")
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_load_state("networkidle", timeout=60_000)
            return True
        except Exception as e:
            if i == attempts:
                print(f"[Heatmap][ERR] goto failed: {e}")
            await asyncio.sleep(2 + i)
    return False


async def capture_heatmap():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
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

        ok = await goto_with_retries(page, FINVIZ_URL, attempts=3)
        if not ok:
            await page.goto(FINVIZ_URL, wait_until="load", timeout=120_000)

        # Dismiss cookie banners (best effort)
        for sel in [
            'button:has-text("Accept")',
            'button:has-text("I Accept")',
            'button:has-text("Agree")',
            '[aria-label*="accept"]',
        ]:
            try:
                await page.locator(sel).first.click(timeout=1500)
                await asyncio.sleep(0.4)
                break
            except:
                pass

        # Screenshot map element, fallback to full page
        saved = False
        for sel in ["#map", 'div[id*="map"]', 'img[src*="map.ashx"]', "canvas"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.screenshot(path=HEATMAP_PATH)
                    saved = True
                    break
            except:
                pass
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
) else '
        f":gold_ingot: Gold: {fmt_pct(d['gold'])}",
    ]
    return "\n".join(lines)


# ── 2. Heatmap Screenshot ──────────────────────────────────────────────
async def goto_with_retries(page, url, attempts=3):
    for i in range(1, attempts + 1):
        try:
            print(f"[Heatmap] goto attempt {i}/{attempts}")
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_load_state("networkidle", timeout=60_000)
            return True
        except Exception as e:
            if i == attempts:
                print(f"[Heatmap][ERR] goto failed: {e}")
            await asyncio.sleep(2 + i)
    return False


async def capture_heatmap():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
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

        ok = await goto_with_retries(page, FINVIZ_URL, attempts=3)
        if not ok:
            await page.goto(FINVIZ_URL, wait_until="load", timeout=120_000)

        # Dismiss cookie banners (best effort)
        for sel in [
            'button:has-text("Accept")',
            'button:has-text("I Accept")',
            'button:has-text("Agree")',
            '[aria-label*="accept"]',
        ]:
            try:
                await page.locator(sel).first.click(timeout=1500)
                await asyncio.sleep(0.4)
                break
            except:
                pass

        # Screenshot map element, fallback to full page
        saved = False
        for sel in ["#map", 'div[id*="map"]', 'img[src*="map.ashx"]', "canvas"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.screenshot(path=HEATMAP_PATH)
                    saved = True
                    break
            except:
                pass
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
 + d['usdcad']}",
        f":gold_ingot: Gold: {fmt_pct(d['gold'])}",
    ]
    return "\n".join(lines)


# ── 2. Heatmap Screenshot ──────────────────────────────────────────────
async def goto_with_retries(page, url, attempts=3):
    for i in range(1, attempts + 1):
        try:
            print(f"[Heatmap] goto attempt {i}/{attempts}")
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            await page.wait_for_load_state("networkidle", timeout=60_000)
            return True
        except Exception as e:
            if i == attempts:
                print(f"[Heatmap][ERR] goto failed: {e}")
            await asyncio.sleep(2 + i)
    return False


async def capture_heatmap():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
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

        ok = await goto_with_retries(page, FINVIZ_URL, attempts=3)
        if not ok:
            await page.goto(FINVIZ_URL, wait_until="load", timeout=120_000)

        # Dismiss cookie banners (best effort)
        for sel in [
            'button:has-text("Accept")',
            'button:has-text("I Accept")',
            'button:has-text("Agree")',
            '[aria-label*="accept"]',
        ]:
            try:
                await page.locator(sel).first.click(timeout=1500)
                await asyncio.sleep(0.4)
                break
            except:
                pass

        # Screenshot map element, fallback to full page
        saved = False
        for sel in ["#map", 'div[id*="map"]', 'img[src*="map.ashx"]', "canvas"]:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.screenshot(path=HEATMAP_PATH)
                    saved = True
                    break
            except:
                pass
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
