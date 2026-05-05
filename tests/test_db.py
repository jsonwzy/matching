"""Tests for the database layer."""

import tempfile
from pathlib import Path

from findit.db import Database


def test_schema_creation():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        with db._conn() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {r["name"] for r in tables}
        assert "posts" in names
        assert "authors" in names
        assert "users" in names
        assert "matches" in names


def test_upsert_author_and_post():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        db.upsert_author({"id": "u1", "nickname": "Alice", "ip_location": "深圳"})
        author = db.get_author("u1")
        assert author is not None
        assert author["nickname"] == "Alice"

        db.upsert_post({
            "id": "p1",
            "author_id": "u1",
            "content": "找对象",
            "source_type": "post",
        })

        # Upsert again should update
        db.upsert_author({"id": "u1", "nickname": "Alice Updated"})
        author = db.get_author("u1")
        assert author["nickname"] == "Alice Updated"


def test_user_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        user = db.get_or_create_user("tg123")
        assert user["telegram_id"] == "tg123"
        assert user["setup_complete"] == 0

        db.update_user("tg123", age=28, city="深圳", hobbies=["跑步", "摄影"])
        user = db.get_user_by_telegram("tg123")
        assert user["age"] == 28
        assert user["city"] == "深圳"


def test_user_profile_roundtrip_with_income_family():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")
        db.upsert_author({"id": "u1", "nickname": "Alice"})

        ok = db.update_user_profile(
            "u1",
            {
                "gender": "女",
                "age": 26,
                "height": 162,
                "education": ["本科"],
                "locations": ["深圳"],
                "occupations": ["运营"],
                "income": "月入2w",
                "family": "深圳本地人",
                "interests": ["看电影", "爬山"],
                "requirements": {
                    "preferred_gender": "男",
                    "age_range": {"min": 26, "max": 32},
                    "min_height": 175,
                    "min_income": "稳定收入",
                },
            },
            confidence=0.8,
        )
        assert ok

        prof = db.get_user_profile("u1")
        assert prof["age"] == 26
        assert prof["income"] == "月入2w"
        assert prof["family"] == "深圳本地人"
        assert prof["requirements"]["min_income"] == "稳定收入"
        assert prof["interests"] == ["看电影", "爬山"]


def test_match_workflow():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "test.db")

        db.upsert_author({"id": "a1", "nickname": "Girl"})
        db.upsert_post({"id": "p1", "author_id": "a1", "content": "找男友", "source_type": "post"})
        user = db.get_or_create_user("tg456")

        match_id = db.create_match({
            "user_id": user["id"],
            "post_id": "p1",
            "author_id": "a1",
            "match_score": 85.0,
            "match_analysis": "条件匹配",
            "generated_opener": "你好",
        })
        assert match_id > 0

        unpushed = db.get_unpushed_matches(user["id"])
        assert len(unpushed) == 1

        db.mark_match_pushed(match_id)
        unpushed = db.get_unpushed_matches(user["id"])
        assert len(unpushed) == 0

        db.update_match_action(match_id, "liked")
        match = db.get_match_by_id(match_id)
        assert match["user_action"] == "liked"
