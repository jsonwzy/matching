"""Two-step crawl pipeline: search → comments.

Orchestrates the stable crawl cycle that doesn't require login:
  Step 1: Search keywords → collect posts + author stubs
  Step 2: Scrape comments on those posts → find dating-intent commenters

Profile scraping (step 3) is optional and disabled by default because
the user_posted and get_user_info APIs require login state, which is
fragile and causes account issues.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from findit.config import settings
from findit.crawler.client import SEARCH_KEYWORDS, XHSClient
from findit.db import Database

logger = logging.getLogger(__name__)


class CrawlRunner:
    """Runs the crawl pipeline."""

    def __init__(self, db: Database | None = None, client: XHSClient | None = None):
        self.db = db or Database(settings.db_path)
        self.client = client or XHSClient()

    async def step1_search_posts(
        self,
        keywords: list[str] | None = None,
        pages_per_keyword: int = 3,
    ) -> int:
        """Search for dating-related posts and save to DB.

        Continues to the next keyword on failure instead of aborting.
        """
        keywords = keywords or SEARCH_KEYWORDS
        saved = 0

        for kw in keywords:
            empty_pages = 0
            for page in range(1, pages_per_keyword + 1):
                results = await self.client.search_notes(kw, page=page)
                if not results:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                    continue

                for note in results:
                    author = self.db.get_author(note["user_id"])
                    if not author:
                        # ip_location is intentionally omitted: search cards
                        # don't carry it, and even when they do the value is
                        # not the user's home IP. Step 3 (profile crawl) is
                        # the source of truth.
                        self.db.upsert_author({
                            "id": note["user_id"],
                            "nickname": note.get("user_nickname"),
                            "avatar_url": note.get("user_avatar"),
                        })

                    self.db.upsert_post({
                        "id": note["id"],
                        "author_id": note["user_id"],
                        "content": note.get("content", ""),
                        "image_urls": note.get("image_list", []),
                        "likes": _parse_int(note.get("likes", "0")),
                        "comments_count": 0,
                        "created_at": note.get("time"),
                        "crawled_at": datetime.now().isoformat(),
                        "post_url": self.client.get_note_url(note["id"]),
                        "source_type": "post",
                    })
                    saved += 1

                logger.info("Keyword '%s' page %d: saved %d posts", kw, page, len(results))

        logger.info("Step 1 complete: saved %d posts total", saved)
        return saved

    async def step2_scrape_comments(self, max_posts: int = 50) -> int:
        """Scrape comments on recent posts and find dating-intent commenters."""
        # Always filter to source_type='post' — without this, prior step2 runs
        # save dating comments as posts (with synthesized id "comment_<id>"),
        # and the next step2 picks those up and tries to scrape them as notes,
        # wasting half the crawl budget on rows that always return 0 comments.
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM posts
                   WHERE source_type='post'
                   ORDER BY crawled_at DESC
                   LIMIT ?""",
                (max_posts,),
            ).fetchall()
            posts = [dict(r) for r in rows]

        found = 0
        for post in posts:
            comments, _ = await self.client.get_note_comments(post["id"])
            dating_comments = self.client.filter_dating_comments(comments)

            for c in dating_comments:
                user_id = c["user_id"]
                if not user_id:
                    continue

                author = self.db.get_author(user_id)
                if not author:
                    # ip_location from comment is the IP at comment-time,
                    # not the user's home IP. Step 3 (profile crawl) sets
                    # the authoritative ip_location.
                    self.db.upsert_author({
                        "id": user_id,
                        "nickname": c.get("nickname"),
                        "avatar_url": c.get("avatar"),
                    })

                comment_id = f"comment_{c['comment_id']}"
                self.db.upsert_post({
                    "id": comment_id,
                    "author_id": user_id,
                    "content": c.get("content", ""),
                    "image_urls": [],
                    "likes": c.get("like_count", 0),
                    "comments_count": 0,
                    "created_at": c.get("create_time"),
                    "crawled_at": datetime.now().isoformat(),
                    "post_url": self.client.get_note_url(post["id"]),
                    "source_type": "comment",
                })
                found += 1

        logger.info("Step 2 complete: found %d dating-intent comments", found)
        return found

    async def step3_scrape_profiles(self, limit: int = 50) -> int:
        """Scrape user profiles for authors still in pending_profile state.

        Per `docs/DATA_SPEC.md` §1.3: only authors that survived early
        filtering get a profile crawl. After a successful fetch we stamp
        `profile_crawled_at` so `crawler_service.run_shared_filter(final=True)`
        knows they're ready for final evaluation.

        Profile pages are rate-limited much more aggressively than search
        or comment pages. We pace via the client's `_profile_sleep` and
        take a longer pause every `crawl_profile_batch_size` profiles.
        """
        author_ids = self.db.get_unscraped_author_ids(limit=limit)
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT id FROM authors
                   WHERE crawl_state='pending_profile'
                     AND profile_crawled_at IS NULL
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            for r in rows:
                if r["id"] not in author_ids:
                    author_ids.append(r["id"])

        scraped = 0
        batch_size = settings.crawl_profile_batch_size
        batch_pause = settings.crawl_profile_batch_pause_sec

        # Linear back-off when 风控 fires:
        #   1st consecutive hit → 10s
        #   2nd                 → 15s
        #   3rd                 → 20s
        #   ...                 → 10 + 5*(n-1)
        # Only abort after 10 consecutive hits — at that point the account
        # is genuinely blocked and the runner should yield to a human.
        # Reset to 0 after 3 consecutive successful profiles.
        consecutive_hits = 0
        consecutive_ok = 0

        for i, uid in enumerate(author_ids[:limit]):
            if i > 0 and i % batch_size == 0:
                logger.info(
                    "step3 batch pause: scraped %d so far, sleeping %.0fs",
                    i, batch_pause,
                )
                await asyncio.sleep(batch_pause)

            profile = await self.client.get_user_profile(uid)

            if profile.get("rate_limited"):
                consecutive_hits += 1
                consecutive_ok = 0
                cool = 10 + (consecutive_hits - 1) * 5
                logger.warning(
                    "step3 风控 #%d on %s (%s) — cooling down %ds",
                    consecutive_hits, uid,
                    profile.get("rate_limit_reason", "?"), cool,
                )
                await asyncio.sleep(cool)
                if consecutive_hits >= 10:
                    logger.error("step3 aborting: 10 consecutive 风控 hits")
                    break
                continue

            if not profile.get("nickname"):
                # Empty profile but no 风控 banner — likely a deleted/
                # private user. Don't count as a 风控 hit.
                continue

            consecutive_ok += 1
            if consecutive_ok >= 3:
                consecutive_hits = 0  # cooled off, reset back-off

            self.db.upsert_author(profile)
            self.db.mark_profile_crawled(uid)
            scraped += 1

        logger.info("Step 3 complete: scraped %d profiles", scraped)
        return scraped

    async def run_full_pipeline(self, include_profiles: bool = False) -> dict[str, int]:
        """Run the crawl pipeline.

        By default only runs search + comments (stable, no login needed).
        Set include_profiles=True to also scrape user profiles (requires login).
        """
        logger.info("Starting crawl pipeline (profiles=%s)", include_profiles)
        await self.client.setup()
        result = {"posts": 0, "comments": 0, "profiles": 0}
        try:
            try:
                result["posts"] = await self.step1_search_posts()
            except Exception:
                logger.exception("Step 1 (search) failed, continuing to step 2")

            try:
                result["comments"] = await self.step2_scrape_comments()
            except Exception:
                logger.exception("Step 2 (comments) failed")

            if include_profiles:
                try:
                    result["profiles"] = await self.step3_scrape_profiles()
                except Exception:
                    logger.exception("Step 3 (profiles) failed")

            return result
        finally:
            await self.client.close()


def _parse_int(value: str | int) -> int:
    if isinstance(value, int):
        return value
    value = str(value).strip()
    if not value:
        return 0
    if value.endswith("万"):
        try:
            return int(float(value[:-1]) * 10000)
        except ValueError:
            return 0
    try:
        return int(value)
    except ValueError:
        return 0


def main():
    """CLI entry point for running the crawler."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    logger.info("Starting crawl pipeline for city: %s", settings.crawl_city)
    result = asyncio.run(CrawlRunner().run_full_pipeline())
    logger.info("Crawl complete: %s", result)


if __name__ == "__main__":
    main()
