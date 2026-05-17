"""Rule-based filters for author profiles.

Implements the rules in `docs/DATA_SPEC.md`:

- SharedFilter: user-independent matchmaker checks
    - Rule A: "红娘" appears in nickname / bio / post.content / notes_summary
    - Rule B: post.content matches a 代发-style proxy-post phrase
  Optionally enforces §2 minimum-data bar (only after Step 3 / when `final=True`).
- UserFilter: per-user location filtering for the matching service.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# ── City / province / district maps ────────────────────────────────────
# XHS shows IP only at province level (or municipality for 直辖市). The user
# filter uses two signals together:
#   1. Content (bio + post text + notes) explicitly mentioning the target
#      city or one of its districts → the user is in/around that city,
#      regardless of where they were when they commented.
#   2. Profile-page ip_location matching the city's province → the only IP
#      granularity XHS gives us; coarser than ideal but sufficient.

CITY_PROVINCE_MAP = {
    # 广东
    "深圳": "广东", "广州": "广东", "东莞": "广东", "佛山": "广东", "珠海": "广东",
    # 直辖市 (province == city)
    "北京": "北京", "上海": "上海", "天津": "天津", "重庆": "重庆",
    # 长三角
    "杭州": "浙江", "宁波": "浙江",
    "南京": "江苏", "苏州": "江苏",
    # 其它
    "成都": "四川", "武汉": "湖北", "西安": "陕西",
}

CITY_DISTRICTS = {
    "深圳": ["福田", "罗湖", "南山", "宝安", "龙岗", "龙华",
             "坪山", "盐田", "光明", "大鹏"],
    "上海": ["浦东", "黄浦", "徐汇", "长宁", "静安", "普陀",
             "杨浦", "虹口", "闵行", "宝山", "嘉定", "松江",
             "金山", "青浦", "奉贤", "崇明"],
    "北京": ["东城", "西城", "朝阳", "海淀", "丰台", "石景山",
             "通州", "昌平", "大兴", "顺义", "房山"],
    "广州": ["天河", "越秀", "海珠", "白云", "黄埔", "番禺",
             "荔湾", "南沙", "增城", "从化"],
    "杭州": ["西湖", "上城", "下城", "拱墅", "滨江", "余杭",
             "萧山", "临平", "钱塘", "富阳"],
}

# Flat set of every city + district name. §2 uses it to decide an author
# is "locatable" even when XHS didn't expose an IP.
_LOCATION_TOKENS = set(CITY_PROVINCE_MAP) | {
    d for ds in CITY_DISTRICTS.values() for d in ds
}

# ── Rule A: matchmaker keyword ──────────────────────────────────────────

MATCHMAKER_KEYWORD = "红娘"

# "红娘" inside one of these is a real person *disclaiming* being a
# matchmaker ("这是本人发的，不是相亲红娘"), not a matchmaker. Masked out
# before the Rule A check so it doesn't false-positive on genuine daters.
MATCHMAKER_KEYWORD_NEGATIONS = [
    "不是红娘", "不是相亲红娘", "不是婚介红娘", "不是中介红娘",
    "我不是红娘", "非红娘", "非中介红娘", "拒绝红娘", "不收红娘",
    "谢绝红娘", "红娘勿扰", "红娘勿加", "红娘退散", "红娘绕道",
    "红娘别来", "不要红娘", "无关红娘",
]

# "中介" can't be a bare keyword — genuine daters write "中介勿扰 /
# 拒绝中介" constantly, so a bare match would false-positive massively.
# It only counts as a matchmaker signal inside an explicit agency
# phrase, i.e. where the author is identifying AS an agency.
MATCHMAKER_AGENCY_PHRASES = [
    "婚恋中介", "相亲中介", "情感中介", "婚介中介", "中介红娘",
    "中介服务", "专业中介", "中介公司", "中介机构", "中介团队",
    "我是中介", "本中介", "做中介",
]

# ── Rule B: proxy-post (代发) signals ────────────────────────────────────

PROXY_POST_KEYWORDS = [
    # 显式代发
    "代发", "代友发", "帮朋友发", "帮闺蜜发", "代闺蜜发",
    "替朋友发", "朋友委托", "本人委托",
    # 撇清"非本人"
    "非本人", "不是本人", "不是我本人",
    # 本人不在
    "本人不在小红书", "本人没小红书", "本人不刷小红书",
    "本人不玩小红书", "本人很少上小红书",
]
# 注：早期版本曾把"已获本人同意/经本人同意/本人同意"也当代发关键词，
# 实际数据中误伤法律披露语境（如"其本人同意公开"用于诉讼证据）。
# 真代发帖几乎都会同时出现"代发/代闺蜜发/帮朋友发"等显式信号，删除单独
# "本人同意"判定后召回率影响极小，且消除假阳性。

PROXY_POST_PATTERNS = [
    re.compile(r"代[一-龥]{1,3}发"),   # 代闺蜜发 / 代表妹发
    re.compile(r"帮[一-龥]{1,3}发"),   # 帮朋友发 / 帮表姐发
    re.compile(r"替[一-龥]{1,3}发"),   # 替哥哥发
    # 帮/替/代 + a relative → posting on someone else's behalf
    re.compile(
        r"[帮替代]\s*(我\s*)?(弟弟|哥哥|姐姐|妹妹|表弟|表哥|表姐"
        r"|表妹|儿子|女儿|侄子|侄女|外甥|闺蜜|同事)"
    ),
]


def _parse_notes(author: dict) -> list[dict]:
    """Extract notes_summary as a list, handling JSON string or list."""
    notes = author.get("notes_summary")
    if isinstance(notes, str):
        try:
            notes = json.loads(notes)
        except (json.JSONDecodeError, TypeError):
            return []
    return notes if isinstance(notes, list) else []


def _is_matchmaker_text(text: str) -> bool:
    """True if `text` identifies the author AS a matchmaker.

    - "红娘": counts on its own, but disclaimer phrases ("不是红娘",
      "红娘勿扰") are masked out first so genuine daters aren't flagged.
    - "中介": NOT a bare keyword — daters write "中介勿扰 / 拒绝中介"
      constantly. Only counts inside an explicit agency phrase
      (婚恋中介 / 中介服务 / 我是中介 / ...).
    """
    if MATCHMAKER_KEYWORD in text:
        cleaned = text
        for neg in MATCHMAKER_KEYWORD_NEGATIONS:
            cleaned = cleaned.replace(neg, "")
        if MATCHMAKER_KEYWORD in cleaned:
            return True
    if any(phrase in text for phrase in MATCHMAKER_AGENCY_PHRASES):
        return True
    return False


def _mentions_a_city(author: dict, posts: list[dict]) -> bool:
    """True if the author's text (nickname / bio / posts / notes) names
    any known city or district — used by §2 as a fallback when XHS
    didn't expose an IP."""
    parts = [author.get("nickname") or "", author.get("bio") or ""]
    parts.extend((p.get("content") or "") for p in posts)
    for note in _parse_notes(author):
        parts.append(note.get("title") or "")
        parts.append(note.get("content") or "")
    blob = " ".join(parts)
    return any(tok in blob for tok in _LOCATION_TOKENS)


# ── Shared Filter (user-independent) ───────────────────────────────────


class SharedFilter:
    """User-independent matchmaker filter (§3 of DATA_SPEC.md).

    Two rules, both meaning permanent exclusion:
      - matchmaker_keyword:    "红娘" anywhere
      - matchmaker_proxy_post: 代发 / 已获本人同意 / 本人不在小红书 / etc.
    """

    def evaluate(
        self,
        author: dict,
        posts: list[dict] | None = None,
        final: bool = False,
    ) -> tuple[bool, str | None]:
        """Evaluate an author profile.

        Args:
            author: row from `authors` (dict).
            posts: posts/comments by this author (rows from `posts`).
            final: True after Step 3 (profile crawled). Enables the §2
                minimum-data bar — without it, authors lacking
                ip_location/content stay un-filtered awaiting Step 3.

        Returns (should_keep, filter_reason).
        """
        posts = posts or []

        keep, reason = self._check_matchmaker_keyword(author, posts)
        if not keep:
            return False, reason

        keep, reason = self._check_proxy_post(posts)
        if not keep:
            return False, reason

        if final:
            keep, reason = self._check_min_quality(author, posts)
            if not keep:
                return False, reason

        return True, None

    # — Rule A —
    def _check_matchmaker_keyword(
        self, author: dict, posts: list[dict]
    ) -> tuple[bool, str | None]:
        haystacks = [
            author.get("nickname") or "",
            author.get("bio") or "",
        ]
        haystacks.extend((p.get("content") or "") for p in posts)
        for note in _parse_notes(author):
            haystacks.append(note.get("title") or "")
            haystacks.append(note.get("content") or "")

        for text in haystacks:
            if _is_matchmaker_text(text):
                return False, "matchmaker_keyword"
        return True, None

    # — Rule B —
    def _check_proxy_post(self, posts: list[dict]) -> tuple[bool, str | None]:
        for p in posts:
            content = p.get("content") or ""
            if not content:
                continue
            if any(kw in content for kw in PROXY_POST_KEYWORDS):
                return False, "matchmaker_proxy_post"
            if any(pat.search(content) for pat in PROXY_POST_PATTERNS):
                return False, "matchmaker_proxy_post"
        return True, None

    # — §2 minimum-data bar (final=True only) —
    def _check_min_quality(
        self, author: dict, posts: list[dict]
    ) -> tuple[bool, str | None]:
        # §2 location bar (relaxed 2026-05-17): an author is "locatable"
        # if XHS showed an IP OR their text names a city/district — same
        # two-signal logic UserFilter uses for matching. XHS often hides
        # IP, and requiring it alone discarded ~half of crawled profiles.
        ip = (author.get("ip_location") or "").strip()
        if not ip and not _mentions_a_city(author, posts):
            return False, "low_quality_content"
        bio = (author.get("bio") or "").strip()
        substantial_post = any(
            len((p.get("content") or "").strip()) >= 12 for p in posts
        )
        if not bio and not substantial_post:
            return False, "low_quality_content"
        return True, None


# ── User Filter (per-user) ─────────────────────────────────────────────


class UserFilter:
    """Per-user filters applied when generating matches.

    Currently only geographic location.

    Location check is two-signal: content mention (city/district name) OR
    profile-IP province match. See CITY_PROVINCE_MAP / CITY_DISTRICTS.
    """

    def __init__(
        self,
        user_city: str = "",
        allow_remote: bool = False,
        age_min: int | None = None,
        age_max: int | None = None,
    ):
        self.user_city = user_city
        self.allow_remote = allow_remote
        self.age_min = age_min
        self.age_max = age_max

    def evaluate(
        self,
        author: dict,
        posts: list[dict] | None = None,
    ) -> tuple[bool, str | None]:
        return self._check_location(author, posts or [])

    def _check_location(
        self, author: dict, posts: list[dict]
    ) -> tuple[bool, str | None]:
        if self.allow_remote or not self.user_city:
            return True, None

        # Signal 1: content mentions the city or a known district.
        # If a user explicitly says "在深圳" / "深圳福田" / "宝安西乡", they're
        # in/around that city — no need to interrogate IP further.
        haystack_parts = [
            author.get("bio") or "",
            author.get("nickname") or "",
        ]
        haystack_parts.extend((p.get("content") or "") for p in posts)
        for note in _parse_notes(author):
            haystack_parts.append(note.get("title") or "")
            haystack_parts.append(note.get("content") or "")
        haystack = " ".join(haystack_parts)
        tokens = [self.user_city] + CITY_DISTRICTS.get(self.user_city, [])
        if any(tok and tok in haystack for tok in tokens):
            return True, None

        # Signal 2: profile-page IP at province (or municipality) granularity.
        location = author.get("ip_location") or ""
        if not location:
            # No profile yet (Step 3 hasn't run). Don't reject; let later
            # passes decide once the profile is filled in.
            return True, None
        province = CITY_PROVINCE_MAP.get(self.user_city, self.user_city)
        if self.user_city in location or province in location:
            return True, None

        return False, f"location_mismatch:{location}"


# ── Backward compatibility alias ────────────────────────────────────────


class RuleFilter:
    """Legacy wrapper combining SharedFilter + UserFilter."""

    def __init__(self, user_city: str = "深圳", allow_remote: bool = False):
        self.shared = SharedFilter()
        self.user = UserFilter(user_city=user_city, allow_remote=allow_remote)

    def evaluate(self, author: dict) -> tuple[bool, str | None]:
        keep, reason = self.shared.evaluate(author)
        if not keep:
            return False, reason
        return self.user.evaluate(author)
