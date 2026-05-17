"""Single-session sweep smoke: keyword → posts → authors + commenters,
all in one live browser session.

Quick validation of the click-everywhere flow. Defaults to 1 keyword ×
5 posts so the test cycle is short. Stats show what survived: how many
posts seen, post-authors scraped, commenters scraped, posts unavailable,
posts app-scan-gated.

Overrides:
  SMOKE_SWEEP_KEYWORD=<keyword>   default: 深圳找对象
  SMOKE_SWEEP_PAGES=<int>         default: 1
  SMOKE_SWEEP_MAX_POSTS=<int>     default: 5  (0 = uncapped)
  SMOKE_SWEEP_NO_COMMENTERS=1     set to skip commenter clicks (faster)
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
log = logging.getLogger("smoke_sweep")


async def main():
    keyword = os.environ.get("SMOKE_SWEEP_KEYWORD", "深圳找对象")
    pages = int(os.environ.get("SMOKE_SWEEP_PAGES", "1"))
    include_commenters = not bool(os.environ.get("SMOKE_SWEEP_NO_COMMENTERS"))
    # Default test scope: 5 posts (+ comments + profiles). Set to 0 to
    # uncap. Small runs keep the test loop fast — see memory
    # feedback_xhs_rate_limit (iterate freely; the account won't ban).
    max_posts_raw = int(os.environ.get("SMOKE_SWEEP_MAX_POSTS", "5"))
    max_posts = max_posts_raw if max_posts_raw > 0 else None

    log.info(
        "smoke_sweep: keyword=%r pages=%d include_commenters=%s max_posts=%s",
        keyword, pages, include_commenters, max_posts,
    )

    runner = CrawlRunner()
    await runner.client.setup()
    try:
        stats = await runner.sweep_keyword(
            keyword=keyword,
            pages=pages,
            include_commenters=include_commenters,
            max_posts=max_posts,
        )
    finally:
        await runner.client.close()

    log.info("──────────  SWEEP RESULTS  ──────────")
    log.info("posts seen:       %d", stats["posts_seen"])
    log.info("post authors:     %d  (clicked + scraped)", stats["post_authors"])
    log.info("commenters:       %d  (clicked + scraped)", stats["commenters"])
    log.info("posts unavailable:%d  (404)", stats["unavailable"])
    log.info("modal-blocked:    %d  (app-scan gate, soft skip)",
             stats["modal_blocked"])
    log.info("rate-limited:     %s", stats["rate_limited"])


if __name__ == "__main__":
    asyncio.run(main())
