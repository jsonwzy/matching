"""Per-user matching service.

Draws from the shared data pool (populated by the crawler service)
to generate personalized matches for each registered user.

Pipeline per user:
  1. Get candidates from screened pool (passed shared filter)
  2. Apply per-user filters (city, age preferences)
  3. AI scoring + opener generation (if API key configured)
     OR simple match creation by recency (if no API key)
  4. Select daily matches
"""

from __future__ import annotations

import json
import logging

from findit.ai.filter_rules import UserFilter
from findit.config import settings
from findit.db import Database

logger = logging.getLogger(__name__)


class MatchingService:
    """Generates personalized matches for users from the shared pool."""

    def __init__(self, db: Database | None = None):
        self.db = db or Database(settings.db_path)
        self._api_enabled = bool(settings.anthropic_api_key)

        # Only import and initialize AI modules when API key is present
        if self._api_enabled:
            from findit.ai.opener import OpenerGenerator
            from findit.ai.scorer import AIScorer
            self.scorer = AIScorer()
            self.opener_gen = OpenerGenerator()
        else:
            self.scorer = None
            self.opener_gen = None
            logger.info("No ANTHROPIC_API_KEY configured — running without AI scoring/openers")

    def generate_matches(self, user: dict) -> list[dict]:
        """Full matching pipeline for a single user.

        Returns the daily match list ready for pushing.
        """
        prefs = user.get("preferences", "{}")
        if isinstance(prefs, str):
            try:
                prefs = json.loads(prefs)
            except (json.JSONDecodeError, TypeError):
                prefs = {}

        # Step 1: Get candidates from shared pool
        candidates = self.db.get_screened_candidates(
            city=user.get("city"),
            limit=200,
        )

        # Step 2: Per-user filtering
        user_filter = UserFilter(
            user_city=user.get("city", ""),
            allow_remote=prefs.get("allow_remote", False),
            age_min=prefs.get("age_min"),
            age_max=prefs.get("age_max"),
        )

        already_matched = self.db.get_already_matched_author_ids(user["id"])

        filtered_candidates = []
        for c in candidates:
            author_id = c.get("author_id") or c.get("id")
            if author_id in already_matched:
                continue
            author_dict = {
                "ip_location": c.get("ip_location"),
                "age_tag": c.get("age_tag"),
                "bio": c.get("bio"),
                "nickname": c.get("nickname"),
                "notes_summary": c.get("notes_summary"),
            }
            posts_for_check = [{"content": c.get("content") or ""}]
            keep, _ = user_filter.evaluate(author_dict, posts=posts_for_check)
            if keep:
                filtered_candidates.append(c)

        # Step 3: Create matches
        if self._api_enabled:
            self._create_matches_with_ai(filtered_candidates, user)
        else:
            self._create_matches_simple(filtered_candidates, user)

        # Step 4: Select daily matches
        return self._select_daily_matches(user["id"])

    def _create_matches_with_ai(self, candidates: list[dict], user: dict) -> int:
        """Score candidates with AI and generate openers."""
        scored = 0
        for post in candidates[:30]:
            author = self.db.get_author(post.get("author_id", ""))
            if not author:
                continue

            if self._has_existing_match(user["id"], post["id"]):
                continue

            result = self.scorer.score(post, author, user)
            if not result:
                continue

            if author.get("is_real_person") == 0.5:
                self.db.update_author_scores(
                    author["id"],
                    is_real_person=result.authenticity_score / 100.0,
                )

            scoring_dict = {
                "her_requirements": result.her_requirements,
                "common_topics": result.common_topics,
                "communication_style": result.communication_style,
                "opener_hooks": result.opener_hooks,
            }
            opener_1, opener_2, _ = self.opener_gen.generate(
                post, author, user, scoring_dict
            )

            self.db.create_match({
                "user_id": user["id"],
                "post_id": post["id"],
                "author_id": post.get("author_id", ""),
                "authenticity_score": result.authenticity_score,
                "seriousness_score": result.seriousness_score,
                "match_score": result.match_score,
                "match_analysis": result.match_analysis,
                "generated_opener": opener_1,
                "alt_opener": opener_2,
            })
            scored += 1

        logger.info("AI scored %d candidates for user %s", scored, user.get("telegram_id"))
        return scored

    def _create_matches_simple(self, candidates: list[dict], user: dict) -> int:
        """Create match records without AI scoring (no API key mode).

        Candidates are taken as-is from the keyword-filtered pool.
        No match_score or opener is generated.
        """
        created = 0
        for post in candidates[:settings.daily_match_count]:
            if self._has_existing_match(user["id"], post["id"]):
                continue

            self.db.create_match({
                "user_id": user["id"],
                "post_id": post["id"],
                "author_id": post.get("author_id", ""),
                "authenticity_score": None,
                "seriousness_score": None,
                "match_score": None,
                "match_analysis": None,
                "generated_opener": None,
                "alt_opener": None,
            })
            created += 1

        logger.info(
            "Created %d simple matches (no AI) for user %s",
            created,
            user.get("telegram_id"),
        )
        return created

    def _has_existing_match(self, user_id: int, post_id: str) -> bool:
        with self.db._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM matches WHERE user_id=? AND post_id=?",
                (user_id, post_id),
            ).fetchone()
            return row is not None

    def _select_daily_matches(
        self, user_id: int, count: int | None = None
    ) -> list[dict]:
        """Select daily matches.

        With AI: 3 high-score + 2 medium-score mix.
        Without AI: most recent candidates by crawl time.
        """
        total = count or settings.daily_match_count

        if self._api_enabled:
            return self._select_scored_matches(user_id, total)
        else:
            return self._select_unscored_matches(user_id, total)

    def _select_scored_matches(self, user_id: int, total: int) -> list[dict]:
        """Select matches using AI scores (3 high + 2 medium)."""
        high_count = settings.high_match_count
        medium_count = total - high_count

        with self.db._conn() as conn:
            high_rows = conn.execute(
                """SELECT m.*, p.content as post_content, p.post_url, p.image_urls,
                   a.nickname, a.ip_location, a.bio, a.age_tag, a.avatar_url
                   FROM matches m
                   JOIN posts p ON m.post_id = p.id
                   JOIN authors a ON m.author_id = a.id
                   WHERE m.user_id = ? AND m.pushed_at IS NULL
                   AND m.match_score > 70
                   ORDER BY m.match_score DESC, p.crawled_at DESC
                   LIMIT ?""",
                (user_id, high_count),
            ).fetchall()

            medium_rows = conn.execute(
                """SELECT m.*, p.content as post_content, p.post_url, p.image_urls,
                   a.nickname, a.ip_location, a.bio, a.age_tag, a.avatar_url
                   FROM matches m
                   JOIN posts p ON m.post_id = p.id
                   JOIN authors a ON m.author_id = a.id
                   WHERE m.user_id = ? AND m.pushed_at IS NULL
                   AND m.match_score BETWEEN 40 AND 70
                   ORDER BY m.match_score DESC, p.crawled_at DESC
                   LIMIT ?""",
                (user_id, medium_count),
            ).fetchall()

        matches = [dict(r) for r in high_rows] + [dict(r) for r in medium_rows]

        if len(matches) < total:
            matched_ids = {m["id"] for m in matches}
            with self.db._conn() as conn:
                fill_rows = conn.execute(
                    """SELECT m.*, p.content as post_content, p.post_url, p.image_urls,
                       a.nickname, a.ip_location, a.bio, a.age_tag, a.avatar_url
                       FROM matches m
                       JOIN posts p ON m.post_id = p.id
                       JOIN authors a ON m.author_id = a.id
                       WHERE m.user_id = ? AND m.pushed_at IS NULL
                       AND m.id NOT IN ({})
                       ORDER BY m.match_score DESC
                       LIMIT ?""".format(
                        ",".join(str(mid) for mid in matched_ids) or "0"
                    ),
                    (user_id, total - len(matches)),
                ).fetchall()
                matches.extend(dict(r) for r in fill_rows)

        return matches[:total]

    def _select_unscored_matches(self, user_id: int, total: int) -> list[dict]:
        """Select matches without AI scores — order by recency."""
        with self.db._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, p.content as post_content, p.post_url, p.image_urls,
                   a.nickname, a.ip_location, a.bio, a.age_tag, a.avatar_url
                   FROM matches m
                   JOIN posts p ON m.post_id = p.id
                   JOIN authors a ON m.author_id = a.id
                   WHERE m.user_id = ? AND m.pushed_at IS NULL
                   ORDER BY p.crawled_at DESC
                   LIMIT ?""",
                (user_id, total),
            ).fetchall()
        return [dict(r) for r in rows]

    def process_all_users(self) -> dict[str, int]:
        """Generate matches for all registered users."""
        with self.db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE setup_complete = 1"
            ).fetchall()

        results = {}
        for row in rows:
            user = dict(row)
            try:
                matches = self.generate_matches(user)
                results[user["telegram_id"]] = len(matches)
                logger.info(
                    "Generated %d matches for user %s",
                    len(matches),
                    user["telegram_id"],
                )
            except Exception:
                logger.exception(
                    "Failed to generate matches for user %s", user["telegram_id"]
                )
                results[user["telegram_id"]] = 0

        return results
