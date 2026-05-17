"""Multi-keyword sweep smoke: rotates through city × dating-pattern
keywords, capped at a total post count. Exercises fresh-target
throughput toward the 100+ profile goal — varied cities keep dedup low,
so most posts yield new profiles instead of re-hitting scraped ones.

Each keyword is a separate sweep_keyword() call (fresh xsec_tokens);
cross-keyword dedup works because profile_crawled_at is persisted to the
DB and re-read at the start of every call.

Overrides:
  SWEEP_CITIES=<csv>       default: 深圳,广州,北京,上海
  SWEEP_TOTAL=<int>        default: 50   total posts across all keywords
  SWEEP_PER_KEYWORD=<int>  default: 5    cap per keyword (<= 20)
  SWEEP_NO_COMMENTERS=1    set to skip commenter clicks (faster)
"""

from __future__ import annotations

import asyncio
import logging
import os

from findit.crawler.runner import CrawlRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("smoke_multi")

# Dating-intent search patterns appended to each city.
PATTERNS = ["找对象", "找男友", "找女友", "脱单", "交友"]


async def main():
    cities = [
        c.strip() for c in
        os.environ.get("SWEEP_CITIES", "深圳,广州,北京,上海").split(",")
        if c.strip()
    ]
    total_target = int(os.environ.get("SWEEP_TOTAL", "50"))
    per_keyword = int(os.environ.get("SWEEP_PER_KEYWORD", "5"))
    include_commenters = not bool(os.environ.get("SWEEP_NO_COMMENTERS"))
    # pattern-outer / city-inner ordering → the first keywords span all
    # cities ("深圳找对象, 广州找对象, 北京找对象, 上海找对象, 深圳找男友,
    # ..."), so even a short run gets city variety instead of front-
    # loading one city.
    keywords = [city + p for p in PATTERNS for city in cities]

    log.info(
        "smoke_multi: cities=%s total=%d per_keyword=%d keywords=%d "
        "include_commenters=%s",
        cities, total_target, per_keyword, len(keywords), include_commenters,
    )

    runner = CrawlRunner()
    await runner.client.setup()
    agg = {
        "posts_seen": 0, "post_authors": 0, "commenters": 0,
        "unavailable": 0, "modal_blocked": 0, "rate_limited": False,
    }
    per_kw_log: list[tuple[str, dict]] = []
    try:
        for kw in keywords:
            done = (agg["posts_seen"] + agg["unavailable"]
                    + agg["modal_blocked"])
            if done >= total_target:
                break
            cap = min(per_keyword, total_target - done)
            log.info("════ keyword %r (cap %d) ════", kw, cap)
            stats = await runner.sweep_keyword(
                keyword=kw, pages=1,
                include_commenters=include_commenters,
                max_posts=cap,
            )
            per_kw_log.append((kw, stats))
            for k in ("posts_seen", "post_authors", "commenters",
                      "unavailable", "modal_blocked"):
                agg[k] += stats.get(k, 0)
            if stats.get("rate_limited"):
                agg["rate_limited"] = True
                log.warning("风控 hit on keyword %r — stopping rotation", kw)
                break
    finally:
        await runner.client.close()

    log.info("════════════  MULTI-SWEEP TOTALS  ════════════")
    for kw, s in per_kw_log:
        log.info(
            "  %-14s posts=%d authors=%d commenters=%d "
            "unavail=%d modal=%d",
            kw, s["posts_seen"], s["post_authors"], s["commenters"],
            s["unavailable"], s["modal_blocked"],
        )
    log.info("  ─────────────────────────────────────────")
    log.info("  posts seen:       %d", agg["posts_seen"])
    log.info("  post authors:     %d", agg["post_authors"])
    log.info("  commenters:       %d", agg["commenters"])
    log.info("  posts unavailable:%d", agg["unavailable"])
    log.info("  modal-blocked:    %d", agg["modal_blocked"])
    log.info("  rate-limited:     %s", agg["rate_limited"])


if __name__ == "__main__":
    asyncio.run(main())
