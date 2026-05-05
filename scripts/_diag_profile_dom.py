"""One-shot profile-page DOM diagnostic.

Open a single user profile, screenshot it, and dump the HTML region
around where the IP / nickname / bio likely live. Distinguishes:
  - 风控 / 异常 page (banner/redirect text)
  - profile rendered but DOM selectors don't match
  - login lost
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from findit.config import settings
from findit.crawler.client import USER_AGENT, _LOGIN_PROBE_JS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("diag")

# Pick one of the smoke-run commenter user_ids (5dd0176f... navigated cleanly)
TARGET = sys.argv[1] if len(sys.argv) > 1 else "5dd0176f00000000010029f9"


async def main():
    profile = Path(settings.browser_profile_dir).resolve()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://www.xiaohongshu.com",
                            wait_until="domcontentloaded", timeout=30000)
            login = await page.evaluate(_LOGIN_PROBE_JS)
            log.info("login probe: %s", login)

            url = f"https://www.xiaohongshu.com/user/profile/{TARGET}"
            log.info("→ %s", url)
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)

            # capture the page
            shot = profile.parent / "diag_profile.png"
            await page.screenshot(path=str(shot), full_page=False)
            log.info("screenshot: %s", shot)

            # body text first 1500 chars (looking for 风控 / 异常 markers)
            body_text = await page.evaluate(
                "() => document.body.innerText.slice(0, 1500)"
            )
            log.info("body text (first 1500 chars):\n%s", body_text)

            # try several candidate selectors for IP/nickname/bio
            probes = await page.evaluate(r"""
                () => {
                    const sels = [
                        '.user-info', '.user-info-container',
                        '.user-info .name', '.user-name', '.user-nickname',
                        '.user-desc', '.user-bio', '.user-info .desc',
                        '.user-info .location', '.ip-info', '.user-IP',
                        '.user-IP-info', '.basic-info', '.basic-info .info',
                        '[class*="ip"]', '[class*="location"]',
                        '[class*="nickname"]', '[class*="user-name"]',
                    ];
                    const out = {};
                    sels.forEach(s => {
                        try {
                            const els = document.querySelectorAll(s);
                            if (els.length) {
                                out[s] = {
                                    count: els.length,
                                    sample: (els[0].textContent || '').trim().slice(0, 120),
                                };
                            }
                        } catch (e) {}
                    });
                    return out;
                }
            """)
            log.info("selector probe results:")
            for sel, info in (probes or {}).items():
                log.info("  %-40s n=%d  %s", sel, info["count"], info["sample"])

            # also try to find the IP-info text by class fragment search
            ip_class_grep = await page.evaluate(r"""
                () => {
                    const out = [];
                    document.querySelectorAll('*[class*="IP"], *[class*="ip"]').forEach(el => {
                        const c = el.className || '';
                        if (typeof c === 'string' && c.length < 80) {
                            out.push({cls: c, text: (el.textContent||'').trim().slice(0, 80)});
                        }
                    });
                    return out.slice(0, 20);
                }
            """)
            log.info("class-name 'ip/IP' grep:")
            for r in (ip_class_grep or [])[:20]:
                log.info("  %s :: %s", r["cls"], r["text"])
        finally:
            await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
