"""Tests for CrawlRunner Step 3 wiring (profile crawl + mark_profile_crawled)."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from findit.crawler.runner import CrawlRunner
from findit.db import Database


def _make_db() -> Database:
    return Database(Path(tempfile.mkdtemp()) / "test.db")


def _make_runner(db, client):
    """Build a CrawlRunner with a mock client (no real XHSClient needed)."""
    runner = CrawlRunner.__new__(CrawlRunner)
    runner.db = db
    runner.client = client
    return runner


class TestStep3ScrapeProfiles:
    """Step 3 should only touch pending_profile authors and stamp the timestamp."""

    def test_picks_pending_profile_and_marks_crawled(self):
        db = _make_db()
        # Three authors, varying states
        db.upsert_author({"id": "p1", "nickname": "old1"})  # pending_profile
        db.upsert_author({"id": "p2", "nickname": "old2"})
        db.update_author_scores("p2", crawl_state="filtered_out", is_filtered_out=True)
        db.upsert_author({"id": "p3", "nickname": "old3"})  # already crawled
        db.mark_profile_crawled("p3")

        client = MagicMock()
        client.get_user_profile = AsyncMock(side_effect=lambda uid, nickname=None: {
            "id": uid,
            "nickname": f"new_{uid}",
            "bio": "爱生活",
            "ip_location": "深圳",
        })
        client.get_user_notes = AsyncMock(return_value=([{"title": "日常"}], ""))

        runner = _make_runner(db, client)
        scraped = asyncio.run(runner.step3_scrape_profiles(limit=10))

        # Only p1 is pending_profile + uncrawled; p2 is filtered, p3 already done.
        assert scraped == 1
        assert client.get_user_profile.await_count == 1
        # Runner now passes nickname so the client can do a search-warmup
        # before navigating to the profile (anti-detection).
        client.get_user_profile.assert_awaited_with("p1", nickname="old1")

        a1 = db.get_author("p1")
        assert a1["profile_crawled_at"] is not None
        assert a1["nickname"] == "new_p1"  # upsert_author wrote new data
        assert a1["bio"] == "爱生活"
        assert a1["ip_location"] == "深圳"

        # p3 stays untouched (already crawled, skipped by query)
        a3 = db.get_author("p3")
        assert a3["profile_crawled_at"] is not None  # was already set

    def test_skips_when_profile_returns_no_nickname(self):
        """If client fails to scrape (empty nickname), don't stamp the author."""
        db = _make_db()
        db.upsert_author({"id": "p1", "nickname": "stub"})

        client = MagicMock()
        client.get_user_profile = AsyncMock(return_value={"id": "p1"})  # no nickname
        client.get_user_notes = AsyncMock(return_value=([], ""))

        runner = _make_runner(db, client)
        scraped = asyncio.run(runner.step3_scrape_profiles(limit=10))

        assert scraped == 0
        a1 = db.get_author("p1")
        assert a1["profile_crawled_at"] is None  # never stamped
        assert a1["crawl_state"] == "pending_profile"
