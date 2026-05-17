"""SQLite database layer with schema management."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'xiaohongshu',
    author_id TEXT NOT NULL,
    content TEXT,
    image_urls TEXT DEFAULT '[]',          -- JSON array
    likes INTEGER DEFAULT 0,
    comments_count INTEGER DEFAULT 0,
    created_at TEXT,
    crawled_at TEXT NOT NULL,
    post_url TEXT,
    source_type TEXT NOT NULL DEFAULT 'post',  -- 'post' or 'comment'
    parent_note_id TEXT,                       -- the /explore/<id> note this row belongs to
    FOREIGN KEY (author_id) REFERENCES authors(id)
);

CREATE TABLE IF NOT EXISTS authors (
    id TEXT PRIMARY KEY,
    nickname TEXT,
    avatar_url TEXT,
    ip_location TEXT,
    followers INTEGER DEFAULT 0,
    following INTEGER DEFAULT 0,
    likes_collected INTEGER DEFAULT 0,
    bio TEXT,
    age_tag TEXT,
    notes_summary TEXT DEFAULT '[]',       -- JSON array
    is_real_person REAL,                   -- 0.0 - 1.0 confidence
    is_filtered_out INTEGER DEFAULT 0,     -- 1 if ruled out by filters (legacy boolean)
    filter_reason TEXT,
    crawl_state TEXT DEFAULT 'pending_profile',  -- pending_profile | kept | filtered_out (DATA_SPEC.md §2)
    profile_crawled_at TEXT,                     -- Step 3 完成时间; NULL 表示主页未爬
    homepage_url TEXT,                           -- https://www.xiaohongshu.com/user/profile/<id>
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS user_profiles (
    author_id TEXT PRIMARY KEY,
    gender TEXT,
    age INTEGER,
    locations TEXT DEFAULT '[]',           -- JSON array
    occupations TEXT DEFAULT '[]',         -- JSON array
    height INTEGER,
    education TEXT DEFAULT '[]',           -- JSON array
    income TEXT,                           -- free text e.g. "月入2w" / "30万年薪"
    family TEXT,                           -- free text e.g. "独生女" / "深圳本地人"
    status TEXT,                           -- relationship status
    personality TEXT DEFAULT '[]',         -- JSON array
    interests TEXT DEFAULT '[]',           -- JSON array
    requirements TEXT DEFAULT '{}',        -- JSON object incl. min_income, age_range, ...
    confidence_score REAL DEFAULT 0.0,     -- 0.0 - 1.0
    profile_complete REAL DEFAULT 0.0,     -- 0.0 - 1.0
    data_source TEXT,                      -- 'crawler_inferred' | 'self_reported'
    source_post_id TEXT,
    raw_text_summary TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (author_id) REFERENCES authors(id)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    author_id TEXT PRIMARY KEY,
    age_range_min INTEGER,
    age_range_max INTEGER,
    min_height INTEGER,
    max_height INTEGER,
    preferred_locations TEXT DEFAULT '[]',
    preferred_occupations TEXT DEFAULT '[]',
    preferred_education TEXT DEFAULT '[]',
    gender_preference TEXT,
    location_preference TEXT,
    financial_preference TEXT,
    looks_preference TEXT,
    marriage_preference TEXT,
    updated_at TEXT,
    FOREIGN KEY (author_id) REFERENCES authors(id)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id TEXT UNIQUE NOT NULL,
    age INTEGER,
    height INTEGER,
    education TEXT,
    school TEXT,
    occupation TEXT,
    income_range TEXT,
    city TEXT,
    hobbies TEXT DEFAULT '[]',             -- JSON array
    highlights TEXT DEFAULT '[]',          -- JSON array
    preferences TEXT DEFAULT '{}',         -- JSON object
    setup_complete INTEGER DEFAULT 0,
    setup_step TEXT DEFAULT 'start',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    post_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    authenticity_score REAL,
    seriousness_score REAL,
    match_score REAL,
    match_analysis TEXT,
    generated_opener TEXT,
    alt_opener TEXT,
    user_action TEXT,                      -- 'liked' / 'passed' / 'sent'
    result TEXT,                           -- 'replied' / 'no_reply' / 'unknown'
    pushed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (post_id) REFERENCES posts(id),
    FOREIGN KEY (author_id) REFERENCES authors(id)
);

CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id);
CREATE INDEX IF NOT EXISTS idx_posts_crawled ON posts(crawled_at);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source_type);
CREATE INDEX IF NOT EXISTS idx_authors_location ON authors(ip_location);
CREATE INDEX IF NOT EXISTS idx_authors_filtered ON authors(is_filtered_out);
CREATE INDEX IF NOT EXISTS idx_matches_user ON matches(user_id);
CREATE INDEX IF NOT EXISTS idx_matches_pushed ON matches(pushed_at);
CREATE INDEX IF NOT EXISTS idx_matches_user_author ON matches(user_id, author_id);
"""


class Database:
    """Thin wrapper around SQLite with helper methods for each table."""

    def __init__(self, db_path: str | Path = "data/findit.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA_SQL)
            self._migrate_in_place(conn)

    def _migrate_in_place(self, conn) -> None:
        """Idempotent ALTER TABLE migrations for existing DBs.

        SQLite has no ADD COLUMN IF NOT EXISTS, so we check via PRAGMA.
        """
        existing = {
            r["name"] for r in conn.execute("PRAGMA table_info(authors)").fetchall()
        }
        if "crawl_state" not in existing:
            conn.execute(
                "ALTER TABLE authors ADD COLUMN crawl_state TEXT "
                "DEFAULT 'pending_profile'"
            )
        if "profile_crawled_at" not in existing:
            conn.execute(
                "ALTER TABLE authors ADD COLUMN profile_crawled_at TEXT"
            )

        existing_up = {
            r["name"] for r in conn.execute("PRAGMA table_info(user_profiles)").fetchall()
        }
        if existing_up and "income" not in existing_up:
            conn.execute("ALTER TABLE user_profiles ADD COLUMN income TEXT")
        if existing_up and "family" not in existing_up:
            conn.execute("ALTER TABLE user_profiles ADD COLUMN family TEXT")

        existing_p = {
            r["name"] for r in conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "xsec_token" not in existing_p:
            # Posts on /explore/<id> need ?xsec_token=...&xsec_source=pc_search
            # to load; without it XHS 404s. Token is short-lived but persisting
            # it lets a follow-up run re-enter a post within its TTL.
            conn.execute("ALTER TABLE posts ADD COLUMN xsec_token TEXT DEFAULT ''")

        if "parent_note_id" not in existing_p:
            # The /explore/<id> note a row belongs to. For a post row it
            # IS the note; for a comment row it's the note the comment
            # sits under — a clean JOIN key (previously only buried in
            # the post_url string).
            conn.execute("ALTER TABLE posts ADD COLUMN parent_note_id TEXT")
        # Backfill parent_note_id wherever it's missing. Idempotent —
        # WHERE parent_note_id IS NULL makes re-runs no-ops.
        conn.execute(
            "UPDATE posts SET parent_note_id = id "
            "WHERE source_type = 'post' AND parent_note_id IS NULL"
        )
        # comment post_url is .../explore/<note_id> optionally + #comment-<cid>
        # — extract the id whether or not the #comment fragment is there.
        conn.execute(
            "UPDATE posts SET parent_note_id = substr("
            "  post_url, instr(post_url, '/explore/') + 9, "
            "  CASE WHEN instr(post_url, '#') > 0 "
            "       THEN instr(post_url, '#') - instr(post_url, '/explore/') - 9 "
            "       ELSE length(post_url) END) "
            "WHERE source_type = 'comment' AND parent_note_id IS NULL "
            "  AND instr(post_url, '/explore/') > 0"
        )

        if "homepage_url" not in existing:
            conn.execute("ALTER TABLE authors ADD COLUMN homepage_url TEXT")
        # Backfill homepage_url wherever it's missing — purely derivable
        # from the author id (the XHS uid). Idempotent.
        conn.execute(
            "UPDATE authors SET homepage_url = "
            "  'https://www.xiaohongshu.com/user/profile/' || id "
            "WHERE homepage_url IS NULL OR homepage_url = ''"
        )

    # ── Posts ────────────────────────────────────────────────────────────

    def upsert_post(self, post: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO posts (id, platform, author_id, content, image_urls,
                   likes, comments_count, created_at, crawled_at, post_url,
                   source_type, xsec_token, parent_note_id)
                   VALUES (:id, :platform, :author_id, :content, :image_urls,
                   :likes, :comments_count, :created_at, :crawled_at, :post_url,
                   :source_type, :xsec_token, :parent_note_id)
                   ON CONFLICT(id) DO UPDATE SET
                   content=excluded.content, likes=excluded.likes,
                   comments_count=excluded.comments_count,
                   crawled_at=excluded.crawled_at,
                   parent_note_id=COALESCE(excluded.parent_note_id,
                                           posts.parent_note_id),
                   xsec_token=CASE WHEN excluded.xsec_token != '' THEN
                       excluded.xsec_token ELSE posts.xsec_token END""",
                {
                    "id": post["id"],
                    "platform": post.get("platform", "xiaohongshu"),
                    "author_id": post["author_id"],
                    "content": post.get("content", ""),
                    "image_urls": json.dumps(post.get("image_urls", [])),
                    "likes": post.get("likes", 0),
                    "comments_count": post.get("comments_count", 0),
                    "created_at": post.get("created_at"),
                    "crawled_at": post.get("crawled_at", datetime.now().isoformat()),
                    "post_url": post.get("post_url"),
                    "source_type": post.get("source_type", "post"),
                    "xsec_token": post.get("xsec_token", ""),
                    "parent_note_id": post.get("parent_note_id"),
                },
            )

    def get_unprocessed_posts(self, limit: int = 100) -> list[dict]:
        """Get posts whose authors haven't been AI-scored yet."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT p.* FROM posts p
                   JOIN authors a ON p.author_id = a.id
                   WHERE a.is_filtered_out = 0 AND a.is_real_person IS NULL
                   ORDER BY p.likes DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_posts_for_profile_extraction(self, limit: int = 100) -> list[dict]:
        """Get posts suitable for profile extraction (dating-related content)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT p.*, a.nickname
                   FROM posts p
                   JOIN authors a ON p.author_id = a.id
                   WHERE (p.content LIKE '%找对象%'
                      OR p.content LIKE '%相亲%'
                      OR p.content LIKE '%交友%'
                      OR p.content LIKE '%脱单%'
                      OR p.content LIKE '%单身%'
                      OR p.content LIKE '%谈恋爱%'
                      OR p.content LIKE '%女朋友%'
                      OR p.content LIKE '%男朋友%')
                   AND p.source_type = 'post'
                   ORDER BY p.likes DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ── User Profiles ───────────────────────────────────────────────────────

    def update_user_profile(self, author_id: str, profile: dict, confidence: float, source_post_id: str = None) -> bool:
        """Update or insert user profile from extracted data."""
        try:
            with self._conn() as conn:
                # Check if profile exists
                existing = conn.execute(
                    "SELECT confidence_score FROM user_profiles WHERE author_id = ?",
                    (author_id,)
                ).fetchone()

                now = datetime.now().isoformat()

                # Only update if new confidence is higher
                if existing and existing['confidence_score'] > confidence:
                    return False

                conn.execute(
                    """INSERT INTO user_profiles
                       (author_id, gender, age, locations, occupations, height, education,
                        income, family, status, personality, interests, requirements,
                        confidence_score, data_source, source_post_id, raw_text_summary,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(author_id) DO UPDATE SET
                       gender=excluded.gender,
                       age=excluded.age,
                       locations=excluded.locations,
                       occupations=excluded.occupations,
                       height=excluded.height,
                       education=excluded.education,
                       income=excluded.income,
                       family=excluded.family,
                       status=excluded.status,
                       personality=excluded.personality,
                       interests=excluded.interests,
                       requirements=excluded.requirements,
                       confidence_score=excluded.confidence_score,
                       updated_at=excluded.updated_at""",
                    (
                        author_id,
                        profile.get('gender'),
                        profile.get('age'),
                        json.dumps(profile.get('locations', []), ensure_ascii=False),
                        json.dumps(profile.get('occupations', []), ensure_ascii=False),
                        profile.get('height'),
                        json.dumps(profile.get('education', []), ensure_ascii=False),
                        profile.get('income'),
                        profile.get('family'),
                        profile.get('status'),
                        json.dumps(profile.get('personality', []), ensure_ascii=False),
                        json.dumps(profile.get('interests', []), ensure_ascii=False),
                        json.dumps(profile.get('requirements', {}), ensure_ascii=False),
                        confidence,
                        'crawler_inferred',
                        source_post_id,
                        profile.get('raw_text', '')[:200],
                        now,
                        now,
                    )
                )
                return True
        except Exception as e:
            print(f"Error updating profile: {e}")
            return False

    def get_user_profile(self, author_id: str) -> dict:
        """Get user profile by author ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE author_id = ?",
                (author_id,)
            ).fetchone()

            if not row:
                return {}

            profile = dict(row)
            # Parse JSON fields
            for field in ['locations', 'occupations', 'education', 'personality', 'interests', 'requirements']:
                if profile.get(field):
                    try:
                        profile[field] = json.loads(profile[field])
                    except:
                        profile[field] = []
            return profile

    def search_profiles(self, **filters) -> list[dict]:
        """Search user profiles by filters."""
        conditions = []
        params = []

        if 'gender' in filters:
            conditions.append("gender = ?")
            params.append(filters['gender'])

        if 'min_age' in filters:
            conditions.append("age >= ?")
            params.append(filters['min_age'])

        if 'max_age' in filters:
            conditions.append("age <= ?")
            params.append(filters['max_age'])

        if 'location' in filters:
            conditions.append("locations LIKE ?")
            params.append(f"%{filters['location']}%")

        if 'min_confidence' in filters:
            conditions.append("confidence_score >= ?")
            params.append(filters['min_confidence'])

        if 'status' in filters:
            conditions.append("status = ?")
            params.append(filters['status'])

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT up.*, a.nickname, a.avatar_url, a.ip_location
                   FROM user_profiles up
                   JOIN authors a ON up.author_id = a.id
                   WHERE {where_clause}
                   ORDER BY confidence_score DESC, age ASC
                   LIMIT 100""",
                params
            ).fetchall()

            results = []
            for row in rows:
                profile = dict(row)
                # Parse JSON fields
                for field in ['locations', 'occupations', 'education', 'personality', 'interests', 'requirements']:
                    if profile.get(field):
                        try:
                            profile[field] = json.loads(profile[field])
                        except:
                            profile[field] = []
                results.append(profile)

            return results

    def print_profile_statistics(self):
        """Print user profile statistics."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) as count FROM user_profiles").fetchone()['count']
            high_conf = conn.execute("SELECT COUNT(*) as count FROM user_profiles WHERE confidence_score >= 0.7").fetchone()['count']
            med_conf = conn.execute("SELECT COUNT(*) as count FROM user_profiles WHERE confidence_score >= 0.5 AND confidence_score < 0.7").fetchone()['count']

            # Gender distribution
            gender_dist = conn.execute("""
                SELECT
                    COALESCE(gender, 'unknown') as gender,
                    COUNT(*) as count
                FROM user_profiles
                GROUP BY gender
            """).fetchall()

            # Age distribution
            age_dist = conn.execute("""
                SELECT
                    CASE
                        WHEN age < 23 THEN '18-22'
                        WHEN age < 26 THEN '23-25'
                        WHEN age < 30 THEN '26-29'
                        WHEN age < 35 THEN '30-34'
                        WHEN age >= 35 THEN '35+'
                        ELSE 'unknown'
                    END as age_group,
                    COUNT(*) as count
                FROM user_profiles
                WHERE age IS NOT NULL
                GROUP BY age_group
            """).fetchall()

            print(f"  总用户画像: {total}")
            print(f"  ├─ 高可信度 (≥70%): {high_conf}")
            print(f"  ├─ 中等可信度 (50-70%): {med_conf}")
            print(f"  └─ 低可信度 (<50%): {total - high_conf - med_conf}")

            print(f"\n  性别分布:")
            for row in gender_dist:
                print(f"    {row['gender']}: {row['count']}")

            print(f"\n  年龄分布:")
            for row in age_dist:
                print(f"    {row['age_group']}: {row['count']}")

            return {
                'total': total,
                'high_confidence': high_conf,
                'medium_confidence': med_conf,
                'gender_distribution': {r['gender']: r['count'] for r in gender_dist},
                'age_distribution': {r['age_group']: r['count'] for r in age_dist}
            }

    def get_posts_without_tags(self, limit: int = 100, source_type: str | None = None) -> list[dict]:
        """Get posts that haven't had AI tag extraction yet."""
        with self._conn() as conn:
            query = """SELECT * FROM posts WHERE ai_tags IS NULL"""
            params = []
            if source_type:
                query += " AND source_type = ?"
                params.append(source_type)
            query += " ORDER BY crawled_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def update_post_tags(self, post_id: str, tags: dict) -> None:
        """Update AI tags for a post."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE posts SET ai_tags = ? WHERE id = ?""",
                (json.dumps(tags, ensure_ascii=False), post_id),
            )

    def get_scored_posts(self, city: str | None = None, limit: int = 200) -> list[dict]:
        """Get posts with AI scores, optionally filtered by city."""
        with self._conn() as conn:
            query = """SELECT p.*, a.nickname, a.ip_location, a.bio, a.age_tag,
                       a.is_real_person, a.notes_summary, a.followers, a.avatar_url
                       FROM posts p
                       JOIN authors a ON p.author_id = a.id
                       WHERE a.is_filtered_out = 0 AND a.is_real_person IS NOT NULL"""
            params: list[Any] = []
            if city:
                query += " AND a.ip_location LIKE ?"
                params.append(f"%{city}%")
            query += " ORDER BY p.crawled_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # ── Authors ─────────────────────────────────────────────────────────

    def upsert_author(self, author: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO authors (id, nickname, avatar_url, ip_location,
                   followers, following, likes_collected, bio, age_tag,
                   notes_summary, homepage_url, updated_at)
                   VALUES (:id, :nickname, :avatar_url, :ip_location,
                   :followers, :following, :likes_collected, :bio, :age_tag,
                   :notes_summary, :homepage_url, :updated_at)
                   ON CONFLICT(id) DO UPDATE SET
                   nickname=excluded.nickname, avatar_url=excluded.avatar_url,
                   ip_location=excluded.ip_location, followers=excluded.followers,
                   following=excluded.following, likes_collected=excluded.likes_collected,
                   bio=excluded.bio, age_tag=excluded.age_tag,
                   notes_summary=excluded.notes_summary,
                   homepage_url=excluded.homepage_url,
                   updated_at=excluded.updated_at""",
                {
                    "id": author["id"],
                    "nickname": author.get("nickname"),
                    "avatar_url": author.get("avatar_url"),
                    "ip_location": author.get("ip_location"),
                    "followers": author.get("followers", 0),
                    "following": author.get("following", 0),
                    "likes_collected": author.get("likes_collected", 0),
                    "bio": author.get("bio"),
                    "age_tag": author.get("age_tag"),
                    "notes_summary": json.dumps(author.get("notes_summary", []), ensure_ascii=False),
                    "homepage_url": (
                        "https://www.xiaohongshu.com/user/profile/"
                        + str(author["id"])
                    ),
                    "updated_at": datetime.now().isoformat(),
                },
            )

    def update_author_scores(
        self, author_id: str, is_real_person: float | None = None,
        is_filtered_out: bool = False, filter_reason: str | None = None,
        crawl_state: str | None = None,
    ) -> None:
        # Derive crawl_state from is_filtered_out if not explicitly passed,
        # so existing callers keep working without changes.
        if crawl_state is None:
            crawl_state = "filtered_out" if is_filtered_out else "kept"
        with self._conn() as conn:
            conn.execute(
                """UPDATE authors SET is_real_person=?, is_filtered_out=?,
                   filter_reason=?, crawl_state=?, updated_at=? WHERE id=?""",
                (is_real_person, int(is_filtered_out), filter_reason,
                 crawl_state, datetime.now().isoformat(), author_id),
            )

    def set_classification(self, author_id: str, crawl_state: str,
                           filter_reason: str | None = None) -> None:
        """Stamp an author's DATA_SPEC §2/§3 verdict — crawl_state +
        filter_reason — without touching AI scores (is_real_person).
        Keeps the legacy is_filtered_out boolean in sync. Used by the
        offline classification pass (scripts/backfill_filter.py).
        """
        with self._conn() as conn:
            conn.execute(
                """UPDATE authors SET crawl_state=?, filter_reason=?,
                   is_filtered_out=?, updated_at=? WHERE id=?""",
                (crawl_state, filter_reason,
                 1 if crawl_state == "filtered_out" else 0,
                 datetime.now().isoformat(), author_id),
            )

    def mark_profile_crawled(self, author_id: str) -> None:
        """Stamp Step 3 completion time. Call this after the profile crawler
        successfully fetches /user/profile/<author_id>."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE authors SET profile_crawled_at=?, updated_at=? WHERE id=?",
                (datetime.now().isoformat(), datetime.now().isoformat(), author_id),
            )

    def get_author(self, author_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
            return dict(row) if row else None

    def get_unscraped_author_ids(self, limit: int = 50) -> list[str]:
        """Get author IDs that don't have profile data yet."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT p.author_id FROM posts p
                   LEFT JOIN authors a ON p.author_id = a.id
                   WHERE a.id IS NULL LIMIT ?""",
                (limit,),
            ).fetchall()
            return [r["author_id"] for r in rows]

    def get_unfiltered_authors(self, limit: int = 100) -> list[dict]:
        """Get authors that haven't been through shared filtering yet.

        Used by the crawler service to run shared filters on new authors.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM authors
                   WHERE is_filtered_out = 0 AND is_real_person IS NULL
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_screened_candidates(
        self, city: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Get authors that passed shared filtering, optionally by city.

        Used by the matching service to get candidates for per-user scoring.
        Returns authors joined with their posts.

        SQL prefilter is intentionally loose — it matches on city OR province
        OR content mentioning the city. Final-grain filtering happens in
        UserFilter, which also covers districts. The SQL just ensures we
        don't drop rows that UserFilter would later accept.
        """
        from findit.ai.filter_rules import CITY_PROVINCE_MAP

        with self._conn() as conn:
            query = """SELECT p.*, a.nickname, a.ip_location, a.bio, a.age_tag,
                       a.is_real_person, a.notes_summary, a.followers, a.avatar_url
                       FROM posts p
                       JOIN authors a ON p.author_id = a.id
                       WHERE a.is_filtered_out = 0
                       AND a.is_real_person IS NOT NULL"""
            params: list[Any] = []
            if city:
                province = CITY_PROVINCE_MAP.get(city, city)
                query += (
                    " AND (a.ip_location LIKE ? OR a.ip_location LIKE ?"
                    "      OR a.ip_location IS NULL OR a.ip_location = ''"
                    "      OR p.content LIKE ? OR a.bio LIKE ?)"
                )
                params.extend([f"%{city}%", f"%{province}%", f"%{city}%", f"%{city}%"])
            query += " ORDER BY p.crawled_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_author_posts(self, author_id: str) -> list[dict]:
        """Get all posts by an author (for inactive check)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM posts WHERE author_id = ? ORDER BY created_at DESC",
                (author_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Users ───────────────────────────────────────────────────────────

    def get_or_create_user(self, telegram_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if row:
                return dict(row)
            conn.execute(
                "INSERT INTO users (telegram_id, created_at) VALUES (?, ?)",
                (telegram_id, datetime.now().isoformat()),
            )
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return dict(row)

    def update_user(self, telegram_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_fields = {"hobbies", "highlights", "preferences"}
        set_parts = []
        values: list[Any] = []
        for k, v in fields.items():
            set_parts.append(f"{k}=?")
            values.append(json.dumps(v, ensure_ascii=False) if k in json_fields else v)
        values.append(telegram_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE users SET {', '.join(set_parts)} WHERE telegram_id=?", values
            )

    def get_user_by_telegram(self, telegram_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None

    # ── Matches ─────────────────────────────────────────────────────────

    def create_match(self, match: dict[str, Any]) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO matches (user_id, post_id, author_id,
                   authenticity_score, seriousness_score, match_score,
                   match_analysis, generated_opener, alt_opener, created_at)
                   VALUES (:user_id, :post_id, :author_id,
                   :authenticity_score, :seriousness_score, :match_score,
                   :match_analysis, :generated_opener, :alt_opener, :created_at)""",
                {
                    "user_id": match["user_id"],
                    "post_id": match["post_id"],
                    "author_id": match["author_id"],
                    "authenticity_score": match.get("authenticity_score"),
                    "seriousness_score": match.get("seriousness_score"),
                    "match_score": match.get("match_score"),
                    "match_analysis": match.get("match_analysis"),
                    "generated_opener": match.get("generated_opener"),
                    "alt_opener": match.get("alt_opener"),
                    "created_at": datetime.now().isoformat(),
                },
            )
            return cur.lastrowid  # type: ignore[return-value]

    def get_unpushed_matches(self, user_id: int, limit: int = 5) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT m.*, p.content as post_content, p.post_url, p.image_urls,
                   a.nickname, a.ip_location, a.bio, a.age_tag, a.avatar_url
                   FROM matches m
                   JOIN posts p ON m.post_id = p.id
                   JOIN authors a ON m.author_id = a.id
                   WHERE m.user_id = ? AND m.pushed_at IS NULL
                   ORDER BY m.match_score DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_match_pushed(self, match_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE matches SET pushed_at=? WHERE id=?",
                (datetime.now().isoformat(), match_id),
            )

    def update_match_action(self, match_id: int, action: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE matches SET user_action=? WHERE id=?", (action, match_id)
            )

    def update_match_opener(self, match_id: int, opener: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE matches SET generated_opener=? WHERE id=?", (opener, match_id)
            )

    def get_already_matched_author_ids(self, user_id: int) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT author_id FROM matches WHERE user_id=?", (user_id,)
            ).fetchall()
            return {r["author_id"] for r in rows}

    def get_match_by_id(self, match_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT m.*, p.content as post_content, p.post_url,
                   a.nickname, a.ip_location, a.bio, a.age_tag
                   FROM matches m
                   JOIN posts p ON m.post_id = p.id
                   JOIN authors a ON m.author_id = a.id
                   WHERE m.id=?""",
                (match_id,),
            ).fetchone()
            return dict(row) if row else None
