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
import random
import time
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

                    # image_urls intentionally empty: the product surfaces
                    # post_url and lets users click through to XHS for
                    # visuals — we don't mirror cover/photos.
                    self.db.upsert_post({
                        "id": note["id"],
                        "author_id": note["user_id"],
                        "content": note.get("content", ""),
                        "image_urls": [],
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
                # Deep-link the comment URL: parent-note URL + the
                # comment's DOM anchor. Browsers open the note page
                # and scroll directly to that comment, so users
                # following the link arrive at the right context to
                # message the author.
                parent_note_url = self.client.get_note_url(post["id"])
                comment_url = f"{parent_note_url}#comment-{c['comment_id']}"
                self.db.upsert_post({
                    "id": comment_id,
                    "author_id": user_id,
                    "content": c.get("content", ""),
                    "image_urls": [],
                    "likes": c.get("like_count", 0),
                    "comments_count": 0,
                    "created_at": c.get("create_time"),
                    "crawled_at": datetime.now().isoformat(),
                    "post_url": comment_url,
                    "source_type": "comment",
                })
                found += 1

        logger.info("Step 2 complete: found %d dating-intent comments", found)
        return found

    def _build_step3_candidates(self, limit: int) -> list[dict]:
        """For each pending author pick the best source post to enter
        from. Returns up to `limit` candidates, each carrying:
            user_id, nickname, source_note_id, comment_id, search_query
        Ordering: prefer the author's own post over a comment they made
        (their own post page renders the author at the top, richer
        on-page context). Then prefer recent crawled_at.

        search_query is a random pick from
        settings.crawl_dating_search_keywords — same generic terms a
        real Shenzhen user would type ("深圳找对象", "深圳脱单", ...),
        not the specific post title. Real users browse a generic feed,
        click whatever catches their eye, then check the author. If the
        target post isn't on page 1 of the search results, the client
        falls back to direct goto-explore (B1).
        """

        import re
        candidates: list[dict] = []
        seen: set[str] = set()
        keywords = settings.crawl_dating_search_keywords or ()

        with self.db._conn() as conn:
            rows = conn.execute("""
                SELECT
                    a.id AS user_id,
                    a.nickname,
                    p.id AS post_pk,
                    p.source_type,
                    p.post_url,
                    p.content
                FROM authors a
                JOIN posts p ON p.author_id = a.id
                WHERE a.profile_crawled_at IS NULL
                  AND a.is_filtered_out = 0
                ORDER BY a.id,
                    CASE p.source_type WHEN 'post' THEN 0 ELSE 1 END,
                    p.crawled_at DESC
            """).fetchall()

        for r in rows:
            uid = r["user_id"]
            if uid in seen:
                continue
            seen.add(uid)

            if r["source_type"] == "post":
                source_note_id = r["post_pk"]
                comment_id = None
            else:  # 'comment'
                m = re.search(r"/explore/([0-9a-f]+)", r["post_url"] or "")
                if not m:
                    continue
                source_note_id = m.group(1)
                comment_id = (r["post_pk"] or "").removeprefix("comment_")

            search_query = random.choice(keywords) if keywords else None

            candidates.append({
                "user_id": uid,
                "nickname": r["nickname"],
                "source_note_id": source_note_id,
                "comment_id": comment_id,
                "search_query": search_query,
            })
            if len(candidates) >= limit:
                break

        return candidates

    async def step3_scrape_profiles(self, limit: int = 50) -> int:
        """Scrape user profiles for authors still pending step3, using
        the comment-page click path (see XHSClient.get_user_profile_via_click).

        Per `docs/DATA_SPEC.md` §1.3: after a successful fetch we stamp
        `profile_crawled_at` so the shared filter knows the author is
        ready for final evaluation.

        Profile pages are rate-limited much more aggressively than
        search or comment pages. We pace via the client's
        `_profile_sleep` and take a longer pause every
        `crawl_profile_batch_size` profiles.
        """
        candidates = self._build_step3_candidates(limit)
        logger.info("step3: %d candidates queued", len(candidates))


        scraped = 0
        batch_size = settings.crawl_profile_batch_size

        # Linear back-off when 风控 fires:
        #   1st consecutive hit → 25s
        #   2nd                 → 30s
        #   3rd                 → 35s
        #   ...                 → 25 + 5*(n-1)
        # Abort after 5 consecutive hits. Reset back-off after 3
        # successful profiles. navigation_failed is a soft skip and
        # does NOT count toward the abort counter.
        consecutive_hits = 0
        consecutive_ok = 0

        for i, c in enumerate(candidates):
            if i > 0 and i % batch_size == 0:
                batch_pause = random.uniform(
                    settings.crawl_profile_batch_pause_min,
                    settings.crawl_profile_batch_pause_max,
                )
                logger.info(
                    "step3 batch pause: scraped %d so far, sleeping %.0fs",
                    i, batch_pause,
                )
                await asyncio.sleep(batch_pause)

            profile = await self.client.get_user_profile_via_click(
                user_id=c["user_id"],
                source_note_id=c["source_note_id"],
                comment_id=c.get("comment_id"),
                search_query=c.get("search_query"),
            )

            if profile.get("rate_limited"):
                consecutive_hits += 1
                consecutive_ok = 0
                cool = 25 + (consecutive_hits - 1) * 5
                logger.warning(
                    "step3 风控 #%d on %s (%s) — cooling down %ds",
                    consecutive_hits, c["user_id"],
                    profile.get("rate_limit_reason", "?"), cool,
                )
                await asyncio.sleep(cool)
                if consecutive_hits >= 5:
                    logger.error("step3 aborting: 5 consecutive 风控 hits")
                    break
                continue

            if profile.get("navigation_failed"):
                # Click chain couldn't complete (target not visible).
                # Soft skip — don't mark profile_crawled_at, don't
                # bump 风控 counter. Author stays in pending queue.
                logger.info("step3 nav failed for %s — soft skip", c["user_id"])
                continue

            if not profile.get("nickname"):
                # Empty profile but no 风控 banner — likely deleted /
                # private user. Don't count as a 风控 hit.
                continue

            consecutive_ok += 1
            if consecutive_ok >= 3:
                consecutive_hits = 0  # cooled off

            self.db.upsert_author(profile)
            self.db.mark_profile_crawled(c["user_id"])
            scraped += 1

        logger.info("Step 3 complete: scraped %d profiles", scraped)
        return scraped

    def step4_extract_user_profiles(self, limit: int = 50) -> int:
        """Run AI tag extraction over authors that have step3 data but
        haven't been parsed yet.

        Doesn't touch XHS — only Claude API. Pulls each author + all
        their content (their own posts + comments they wrote) and writes
        the parsed result to user_profiles.

        Returns count of authors processed (skips silently if the
        Anthropic API key isn't configured).
        """
        if not settings.anthropic_api_key:
            logger.info("step4 skipped: ANTHROPIC_API_KEY not set")
            return 0

        # Local import — keeps the crawler runnable without anthropic
        # installed when only step1–3 are needed.
        from findit.ai.tag_extractor import AITagExtractor

        extractor = AITagExtractor()
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT a.id, a.nickname, a.bio, a.ip_location, a.age_tag
                   FROM authors a
                   LEFT JOIN user_profiles up ON up.author_id = a.id
                   WHERE a.profile_crawled_at IS NOT NULL
                     AND up.author_id IS NULL
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            authors = [dict(r) for r in rows]

        processed = 0
        for author in authors:
            posts = self.db.get_author_posts(author["id"])
            if not posts:
                continue
            # Concatenate all content the author authored (their own
            # post titles + their dating-intent comments) into one
            # synthetic post for extraction. This gives the LLM the
            # widest context per author with one API call.
            combined = "\n---\n".join(
                (p.get("content") or "").strip() for p in posts
                if (p.get("content") or "").strip()
            )
            if not combined:
                continue
            synthetic = {
                "id": f"synthetic_{author['id']}",
                "content": combined,
                "source_type": "post",
            }
            result = extractor.extract_from_post(synthetic, author)
            if result is None:
                continue
            data = result.to_dict()
            # Flatten the nested personal_info dict to top-level keys
            # that update_user_profile expects.
            flat = {
                **data["personal_info"],
                "locations": [data["personal_info"]["location"]] if data["personal_info"].get("location") else [],
                "occupations": [data["personal_info"]["occupation"]] if data["personal_info"].get("occupation") else [],
                "education": [data["personal_info"]["education"]] if data["personal_info"].get("education") else [],
                "interests": data.get("extracted_tags", []),
                "requirements": data["requirements"],
                "raw_text": combined,
            }
            self.db.update_user_profile(
                author["id"], flat,
                confidence=data.get("confidence_score", 50) / 100.0,
            )
            processed += 1

        logger.info("Step 4 complete: parsed %d user profiles", processed)
        return processed

    async def sweep_keyword(
        self, keyword: str, pages: int = 1,
        include_commenters: bool = True,
        max_posts: int | None = None,
    ) -> dict[str, int]:
        """Single-session sweep: keyword search → click each post →
        click each author/commenter, all within one live SPA session.

        Replaces step1+step2+step3 for fresh data. xsec_tokens stay
        valid throughout because we never break the click chain. Posts
        get persisted with their xsec_token in case a follow-up run
        wants to re-enter (within token TTL).

        Aborts the whole sweep on the first 风控 hit — the entire
        session's auth is now suspect. The caller should re-run later.

        Args:
          keyword: e.g. "深圳找对象"
          pages: how many pages of search results to walk (1-3 typical)
          include_commenters: also click into dating-intent commenters
            on each post. Set False for a fast post-author-only smoke.
          max_posts: cap the total post cards processed across all pages.
            None = no cap. Used to keep test runs small (e.g. 5).

        Returns:
          {"posts_seen": N, "post_authors": N, "commenters": N,
           "unavailable": N, "modal_blocked": N, "rate_limited": bool}

        modal_blocked counts posts gated by XHS's per-note app-scan
        modal ("当前笔记暂时无法浏览 / 请打开 App 扫码") — a per-POST
        soft skip, kept separate from `unavailable` (real 404s) and
        never escalated to a 风控 abort.
        """
        stats = {
            "posts_seen": 0, "post_authors": 0, "commenters": 0,
            "unavailable": 0, "modal_blocked": 0, "rate_limited": False,
        }

        # Don't re-scrape authors we already have profiles for.
        with self.db._conn() as conn:
            already = {
                r["id"] for r in conn.execute(
                    "SELECT id FROM authors WHERE profile_crawled_at IS NOT NULL"
                ).fetchall()
            }

        async def _try_scrape_user(uid: str, where: str) -> str:
            """click → scrape → go_back. Returns one of:
              'ok', 'rate_limited', 'failed', 'skipped'.
            """
            if not uid:
                return "skipped"
            if uid in already:
                logger.info("  ↩ %s already scraped — skipping [%s]", uid, where)
                return "skipped"
            logger.info("  → click into %s [%s]", uid, where)
            profile = await self.client.click_user_link_to_profile(uid)
            if profile.get("rate_limited"):
                logger.warning("  ✗ %s 风控 [%s]: %s", uid, where,
                              profile.get("rate_limit_reason"))
                return "rate_limited"
            if profile.get("navigation_failed"):
                logger.info("  ✗ %s nav failed [%s]", uid, where)
                if "/user/profile/" in (self.client._page.url or ""):
                    await self.client.go_back(expect_url_part="/explore/")
                return "failed"
            if not profile.get("nickname"):
                logger.info("  ✗ %s empty profile [%s]", uid, where)
                await self.client.go_back(expect_url_part="/explore/")
                return "failed"
            self.db.upsert_author(profile)
            self.db.mark_profile_crawled(uid)
            already.add(uid)
            logger.info("  ✓ scraped %s (%s) [%s]",
                       uid, profile.get("nickname"), where)
            await self.client.go_back(expect_url_part="/explore/")
            return "ok"

        processed = 0
        for page in range(1, pages + 1):
            if max_posts is not None and processed >= max_posts:
                break
            cards = await self.client.search_notes(keyword, page=page)
            # OBSERVATION ONLY — log if a verification/captcha marker is
            # showing on the search page (the spot detect_rate_limit's
            # abort path doesn't cover). Does not abort or change flow.
            await self.client.observe_risk_markers(f"search:{keyword}")
            if not cards:
                logger.info("sweep: no results for '%s' page %d", keyword, page)
                continue
            if max_posts is not None:
                cards = cards[: max_posts - processed]
            logger.info("sweep: '%s' page %d → %d post cards",
                       keyword, page, len(cards))

            for idx, card in enumerate(cards, start=1):
                processed += 1
                note_id = card["id"]
                xsec = card.get("xsec_token", "")
                post_author_id = card.get("user_id") or ""
                logger.info(
                    "▶ [%d/%d] post=%s author=%s", idx, len(cards),
                    note_id, post_author_id or "<none>",
                )

                # Open the note by direct navigation with its xsec_token
                # (captured from the search scrape), NOT by clicking the
                # search card. A goto doesn't depend on the search page
                # still being mounted and leaves browser history clean —
                # the commenter loop re-opens the note repeatedly, and a
                # go_back chain can't survive that. Same URL shape as a
                # search-result click (xsec_source=pc_search).
                opened = await self.client.open_note(note_id, xsec)
                if not opened:
                    logger.info("  ✗ couldn't open post %s — skipping", note_id)
                    continue

                # On /explore/<note_id> now. Check for trouble.
                gone, why = await self.client.detect_post_unavailable()
                if gone:
                    stats["unavailable"] += 1
                    logger.info("  ✗ post %s unavailable (%s)", note_id, why)
                    continue
                hit, reason = await self.client.detect_rate_limit()
                if hit:
                    logger.error(
                        "sweep aborting: 风控 on explore %s (%s)",
                        note_id, reason,
                    )
                    stats["rate_limited"] = True
                    return stats

                stats["posts_seen"] += 1
                post_started = time.monotonic()

                # Scrape the note's own title + body. For a dating post
                # the body ("相亲帖正文") carries more than any comment —
                # height / education / requirements live there. Falls
                # back to the search-card title if the body didn't render.
                note = await self.client.scrape_note_body()
                note_text = "\n".join(
                    t for t in (note.get("title"), note.get("body")) if t
                ) or card.get("content", "")

                # Upsert author FIRST — posts.author_id has a FK
                # constraint to authors.id, so the author row must exist
                # before we insert the post.
                if post_author_id:
                    self.db.upsert_author({
                        "id": post_author_id,
                        "nickname": card.get("user_nickname"),
                        "avatar_url": card.get("user_avatar"),
                    })
                self.db.upsert_post({
                    "id": note_id,
                    "author_id": post_author_id,
                    "content": note_text,
                    "image_urls": [],
                    "likes": _parse_int(card.get("likes", "0")),
                    "comments_count": 0,
                    "created_at": None,
                    "crawled_at": datetime.now().isoformat(),
                    "post_url": self.client.get_note_url(note_id),
                    "source_type": "post",
                    "xsec_token": xsec,
                    "parent_note_id": note_id,
                })

                # Scrape comments NOW — before the post-author detour.
                # XHS's app-scan modal ("当前笔记暂时无法浏览") surfaces
                # ~15-20s after the note loads; the author scrape would
                # burn that whole window. Grab + persist the dating-intent
                # comments here so the data survives even if the note then
                # gets gated. The commenter PROFILE scrape happens later,
                # only on a non-gated note.
                dating: list[dict] = []
                if include_commenters:
                    comments = await self.client.scrape_comments_on_current_page()
                    dating = self.client.filter_dating_comments(comments)
                    logger.info("  post %s: %d comments, %d dating-intent",
                               note_id, len(comments), len(dating))
                    for c in dating:
                        cuid = c.get("user_id") or ""
                        if not cuid:
                            continue
                        # Upsert author stub first (FK constraint)
                        self.db.upsert_author({
                            "id": cuid,
                            "nickname": c.get("nickname"),
                            "avatar_url": c.get("avatar"),
                        })
                        self.db.upsert_post({
                            "id": f"comment_{c['comment_id']}",
                            "author_id": cuid,
                            "content": c.get("content", ""),
                            "image_urls": [],
                            "likes": c.get("like_count", 0),
                            "comments_count": 0,
                            "created_at": None,
                            "crawled_at": datetime.now().isoformat(),
                            "post_url": (
                                f"{self.client.get_note_url(note_id)}"
                                f"#comment-{c['comment_id']}"
                            ),
                            "source_type": "comment",
                            "parent_note_id": note_id,
                        })

                # Click into post author. An author's /user/profile page
                # stays reachable even when the note itself gets gated, so
                # this is safe to run after the comment scrape.
                result = await _try_scrape_user(post_author_id, "post author")
                if result == "rate_limited":
                    stats["rate_limited"] = True
                    return stats
                if result == "ok":
                    stats["post_authors"] += 1

                # By now the app-scan modal has had time to surface if
                # this note is gated — close it and count it. It's a
                # per-POST block, NOT account 风控; detect_rate_limit ran
                # above already so a real 风控 banner still aborts first.
                note_gated = await self.client.dismiss_modal() == "app_scan"
                if note_gated:
                    stats["modal_blocked"] += 1
                    logger.info("  ⊘ post %s app-scan gated", note_id)

                # Full commenter profile scrape — only on a non-gated
                # note. (The comments themselves were already scraped +
                # persisted above, gated or not.)
                if include_commenters and not note_gated:
                    # Drop commenters already in the DB BEFORE the loop —
                    # each iteration re-opens the note (a full page nav),
                    # so there's no point paying that just to discover
                    # we'd skip. (Cuts the bulk of the wasted time.)
                    fresh = [
                        c for c in dating
                        if (c.get("user_id") or "")
                        and c["user_id"] not in already
                    ]
                    n_skip = len(dating) - len(fresh)
                    if n_skip:
                        logger.info(
                            "  ↩ %d commenter(s) already scraped — skipping",
                            n_skip,
                        )
                    for c in fresh:
                        cuid = c["user_id"]
                        # Re-open the note fresh before each commenter.
                        # go_back from the previous profile lands on a
                        # bare /explore URL (XHS drops the xsec_token)
                        # that renders no comment list — re-navigating
                        # with the token restores it so the link is found.
                        if not await self.client.open_note(note_id, xsec):
                            logger.info(
                                "  ✗ couldn't reopen note %s for commenter %s",
                                note_id, cuid,
                            )
                            continue
                        result = await _try_scrape_user(cuid, "commenter")
                        if result == "rate_limited":
                            stats["rate_limited"] = True
                            return stats
                        if result == "ok":
                            stats["commenters"] += 1

                logger.info("  ⏱ post %s done in %.0fs",
                           note_id, time.monotonic() - post_started)
                # Next post is reached by its own open_note() — no
                # go_back chain to unwind. Just a short human-paced gap.
                await asyncio.sleep(random.uniform(2.0, 4.0))

        return stats

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
