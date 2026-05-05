"""AI-powered tag extraction from post content using Claude API.

Extracts structured tags from Xiaohongshu posts/comments:
- Personal info: gender, age, height, education, location, occupation
- Requirements: preferred age range, height, education, location
- Dating intent seriousness score
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import anthropic

from findit.config import settings

logger = logging.getLogger(__name__)

_TAG_EXTRACTION_SYSTEM_PROMPT = """你是一个专业的交友信息提取专家。你的任务是从小红书的帖子或评论内容中，准确提取用户的个人信息和交友要求。

请严格按照JSON格式输出，不要输出任何其他内容。如果某项信息无法从文本中推断，请使用 null 值。"""

_TAG_EXTRACTION_USER_PROMPT = """## 待分析的内容

**类型：** {content_type}
**内容：**
{text}

**用户基本信息：**
- 昵称：{nickname}
- IP属地：{location}

## 请输出以下JSON格式

{{
    "personal_info": {{
        "gender": <"男" / "女" / null>,
        "age": <整数年龄或null>,
        "height": <整数身高(cm)或null>,
        "education": <"高中" / "大专" / "本科" / "硕士" / "博士" / null>,
        "location": <"城市"或null>,
        "occupation": <"职业"或null>,
        "income": <自由文本如"月入2w" / "30万年薪" / "中产家庭"或null>,
        "family": <自由文本如"独生女" / "深圳本地人" / "父母都是医生"或null>
    }},
    "requirements": {{
        "preferred_gender": <"男" / "女" / "不限" / null>,
        "age_range": {{
            "min": <最小年龄或null>,
            "max": <最大年龄或null>
        }},
        "min_height": <最小身高(cm)或null>,
        "education": <"本科以上" / "大专以上" / "不限" / null>,
        "location": <期望地区或null>,
        "min_income": <自由文本如"月入1w以上" / "稳定收入"或null>
    }},
    "dating_intent": {{
        "seriousness_score": <0-100整数, 100代表非常认真找对象，0代表只是随便聊聊>,
        "intent_type": <"认真征婚" / "真诚交友" / "随缘交友" / "聊聊天" / null>,
        "urgency": <"着急" / "正常" / "不急" / null>
    }},
    "extracted_tags": [
        "<提取的关键标签1>",
        "<提取的关键标签2>"
    ],
    "confidence_score": <0-100整数, 表示提取结果的可信度>
}}"""


@dataclass
class PersonalInfo:
    gender: str | None
    age: int | None
    height: int | None
    education: str | None
    location: str | None
    occupation: str | None
    income: str | None = None
    family: str | None = None


@dataclass
class Requirements:
    preferred_gender: str | None
    age_range: dict[str, int | None] | None
    min_height: int | None
    education: str | None
    location: str | None
    min_income: str | None = None


@dataclass
class DatingIntent:
    seriousness_score: int
    intent_type: str | None
    urgency: str | None


@dataclass
class TagExtractionResult:
    personal_info: PersonalInfo
    requirements: Requirements
    dating_intent: DatingIntent
    extracted_tags: list[str]
    confidence_score: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            "personal_info": {
                "gender": self.personal_info.gender,
                "age": self.personal_info.age,
                "height": self.personal_info.height,
                "education": self.personal_info.education,
                "location": self.personal_info.location,
                "occupation": self.personal_info.occupation,
                "income": self.personal_info.income,
                "family": self.personal_info.family,
            },
            "requirements": {
                "preferred_gender": self.requirements.preferred_gender,
                "age_range": self.requirements.age_range,
                "min_height": self.requirements.min_height,
                "education": self.requirements.education,
                "location": self.requirements.location,
                "min_income": self.requirements.min_income,
            },
            "dating_intent": {
                "seriousness_score": self.dating_intent.seriousness_score,
                "intent_type": self.dating_intent.intent_type,
                "urgency": self.dating_intent.urgency,
            },
            "extracted_tags": self.extracted_tags,
            "confidence_score": self.confidence_score,
        }


class AITagExtractor:
    """Uses Claude API to extract structured tags from post content."""

    def __init__(self):
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def extract_from_post(
        self,
        post: dict[str, Any],
        author: dict[str, Any] | None = None,
    ) -> TagExtractionResult | None:
        """Extract tags from a post or comment.

        Args:
            post: Post dict with at least 'content' and 'source_type' fields.
            author: Optional author dict for additional context.

        Returns TagExtractionResult or None if extraction fails.
        """
        content = post.get("content", "")
        if not content or len(content.strip()) < 5:
            logger.warning("Post content too short for tag extraction")
            return None

        # Determine content type
        content_type = "评论" if post.get("source_type") == "comment" else "帖子"

        # Get author info for context
        nickname = author.get("nickname", "未知") if author else "未知"
        location = author.get("ip_location", "") if author else ""

        prompt = _TAG_EXTRACTION_USER_PROMPT.format(
            content_type=content_type,
            text=content[:2000],  # Limit content length
            nickname=nickname,
            location=location,
        )

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                system=_TAG_EXTRACTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.content[0].text.strip()

            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            data = json.loads(text)
            return self._parse_result(data)

        except json.JSONDecodeError as e:
            logger.warning("Failed to parse AI tag extraction response as JSON: %s", e)
            logger.debug("Response text: %s", text)
            return None
        except Exception as e:
            logger.exception("AI tag extraction API call failed: %s", e)
            return None

    def extract_batch(
        self,
        posts: list[dict[str, Any]],
        authors: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, TagExtractionResult | None]:
        """Extract tags from multiple posts.

        Args:
            posts: List of post dicts.
            authors: Optional dict mapping author_id -> author dict.

            Returns dict mapping post_id -> TagExtractionResult or None.
        """
        results = {}
        for post in posts:
            post_id = post.get("id")
            author = authors.get(post.get("author_id")) if authors else None
            results[post_id] = self.extract_from_post(post, author)
        return results

    def _parse_result(self, data: dict[str, Any]) -> TagExtractionResult:
        """Parse JSON response into TagExtractionResult."""
        personal_info_data = data.get("personal_info", {})
        personal_info = PersonalInfo(
            gender=personal_info_data.get("gender"),
            age=personal_info_data.get("age"),
            height=personal_info_data.get("height"),
            education=personal_info_data.get("education"),
            location=personal_info_data.get("location"),
            occupation=personal_info_data.get("occupation"),
            income=personal_info_data.get("income"),
            family=personal_info_data.get("family"),
        )

        requirements_data = data.get("requirements", {})
        requirements = Requirements(
            preferred_gender=requirements_data.get("preferred_gender"),
            age_range=requirements_data.get("age_range"),
            min_height=requirements_data.get("min_height"),
            education=requirements_data.get("education"),
            location=requirements_data.get("location"),
            min_income=requirements_data.get("min_income"),
        )

        dating_intent_data = data.get("dating_intent", {})
        dating_intent = DatingIntent(
            seriousness_score=dating_intent_data.get("seriousness_score", 50),
            intent_type=dating_intent_data.get("intent_type"),
            urgency=dating_intent_data.get("urgency"),
        )

        return TagExtractionResult(
            personal_info=personal_info,
            requirements=requirements,
            dating_intent=dating_intent,
            extracted_tags=data.get("extracted_tags", []),
            confidence_score=data.get("confidence_score", 50),
        )


def main():
    """CLI for testing tag extraction."""
    import sys
    from findit.db import Database

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not settings.anthropic_api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env file")
        sys.exit(1)

    db = Database(settings.db_path)
    extractor = AITagExtractor()

    # Get unprocessed posts
    posts = db.get_unprocessed_posts(limit=10)

    if not posts:
        print("No unprocessed posts found in database")
        return

    print(f"Found {len(posts)} posts to process")

    for i, post in enumerate(posts, 1):
        print(f"\n[{i}/{len(posts)}] Processing post: {post['id']}")

        author = db.get_author(post["author_id"])
        result = extractor.extract_from_post(post, author)

        if result:
            print(f"✓ Extraction successful (confidence: {result.confidence_score}%)")
            print(f"  Personal: {result.personal_info.gender}, {result.personal_info.age}岁, {result.personal_info.location}")
            print(f"  Intent: {result.dating_intent.seriousness_score}/100 serious ({result.dating_intent.intent_type})")
            print(f"  Tags: {', '.join(result.extracted_tags[:3])}")

            # Save result to database (you'd need to add a column for this)
            # db.update_post_tags(post['id'], result.to_dict())
        else:
            print(f"✗ Extraction failed")


if __name__ == "__main__":
    main()