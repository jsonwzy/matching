"""Smoke run of the new browser-DOM crawler, scoped to Shenzhen.

5 city-prefixed keywords × 2 pages each through step1, then step2 on the
fresh posts. Prints DB-level stats so we can judge whether the new path
actually produces dating-intent commenters in 深圳.

Browser is headed by design — uses the persistent profile in
data/chromium_profile/ so no QR scan is needed if cookies are still fresh.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from findit.config import settings
from findit.crawler.runner import CrawlRunner
from findit.db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("smoke_sz")

KEYWORDS = [
    "深圳找对象",
    "深圳找男友",
    "深圳脱单",
    "深圳cpdd",
    "深圳交友",
]


def _db_stats(db: Database, since_iso: str) -> dict:
    with sqlite3.connect(str(db.path)) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        post_total = c.execute(
            "SELECT COUNT(*) FROM posts WHERE crawled_at >= ?", (since_iso,)
        ).fetchone()[0]
        post_only = c.execute(
            "SELECT COUNT(*) FROM posts WHERE crawled_at >= ? AND source_type='post'",
            (since_iso,),
        ).fetchone()[0]
        comment_only = c.execute(
            "SELECT COUNT(*) FROM posts WHERE crawled_at >= ? AND source_type='comment'",
            (since_iso,),
        ).fetchone()[0]
        # author IP location distribution for fresh authors
        ip_rows = c.execute(
            """SELECT a.ip_location, COUNT(*) AS n
               FROM authors a
               JOIN posts p ON p.author_id = a.id
               WHERE p.crawled_at >= ?
               GROUP BY a.ip_location
               ORDER BY n DESC
               LIMIT 15""",
            (since_iso,),
        ).fetchall()
        # sample of dating-comment content
        sample = c.execute(
            """SELECT p.content, a.nickname, a.ip_location
               FROM posts p JOIN authors a ON a.id = p.author_id
               WHERE p.crawled_at >= ? AND p.source_type='comment'
               LIMIT 8""",
            (since_iso,),
        ).fetchall()
    return {
        "posts_total": post_total,
        "posts_only": post_only,
        "comment_intent": comment_only,
        "ip_distribution": [(r["ip_location"] or "<none>", r["n"]) for r in ip_rows],
        "comment_samples": [
            (r["nickname"], r["ip_location"], (r["content"] or "")[:100])
            for r in sample
        ],
    }


async def main():
    runner = CrawlRunner()
    db = runner.db
    started = (datetime.now() - timedelta(seconds=2)).isoformat()
    log.info("smoke run start; keywords=%s", KEYWORDS)
    log.info("city setting (post-hoc filter): %s", settings.crawl_city)

    await runner.client.setup()
    try:
        try:
            n_posts = await runner.step1_search_posts(
                keywords=KEYWORDS, pages_per_keyword=2,
            )
            log.info("step1 reported %d posts saved", n_posts)
        except Exception:
            log.exception("step1 failed")

        try:
            n_comments = await runner.step2_scrape_comments(max_posts=30)
            log.info("step2 reported %d dating comments saved", n_comments)
        except Exception:
            log.exception("step2 failed")
    finally:
        await runner.client.close()

    stats = _db_stats(db, started)
    log.info("──────────────  SMOKE RESULTS  ──────────────")
    log.info("posts (search):       %d", stats["posts_only"])
    log.info("dating-intent commts: %d", stats["comment_intent"])
    log.info("total fresh rows:     %d", stats["posts_total"])
    log.info("IP distribution (top 15):")
    for loc, n in stats["ip_distribution"]:
        log.info("  %5d  %s", n, loc)
    log.info("comment samples:")
    for nick, ip, content in stats["comment_samples"]:
        log.info("  [%s | %s] %s", nick, ip, content)


if __name__ == "__main__":
    asyncio.run(main())
