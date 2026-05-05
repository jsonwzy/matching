"""Tests for rule-based filtering — only Rule A (红娘) and Rule B (代发).

See `docs/DATA_SPEC.md` §3 for the rule definitions.
"""

from findit.ai.filter_rules import SharedFilter, UserFilter, RuleFilter


class TestSharedFilterRuleA:
    """Rule A: '红娘' anywhere in nickname / bio / post.content / notes_summary."""

    def setup_method(self):
        self.f = SharedFilter()

    def test_filters_红娘_in_nickname(self):
        author = {"nickname": "深圳红娘小王", "bio": ""}
        keep, reason = self.f.evaluate(author)
        assert keep is False
        assert reason == "matchmaker_keyword"

    def test_filters_红娘_in_bio(self):
        author = {"nickname": "小王", "bio": "我是红娘 帮你找对象"}
        keep, reason = self.f.evaluate(author)
        assert keep is False
        assert reason == "matchmaker_keyword"

    def test_filters_红娘_in_post_content(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "城市红娘平台 加微信"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_keyword"

    def test_filters_红娘_in_notes_summary_title(self):
        author = {
            "nickname": "小花",
            "bio": "",
            "notes_summary": [{"title": "本红娘成功案例", "content": ""}],
        }
        keep, reason = self.f.evaluate(author)
        assert keep is False
        assert reason == "matchmaker_keyword"

    def test_keeps_no_红娘_anywhere(self):
        author = {
            "nickname": "小花",
            "bio": "爱旅行爱美食",
            "notes_summary": [{"title": "周末去哪玩"}],
        }
        posts = [{"content": "今天天气真好,去吃了火锅"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is True
        assert reason is None

    def test_no_longer_filters_old_keywords(self):
        """Old keywords (婚介/牵线/加微/进群) are no longer in scope per spec."""
        for word in ["婚介服务", "牵线搭桥", "加微信咨询", "进群了解"]:
            author = {"nickname": "小花", "bio": word}
            keep, _ = self.f.evaluate(author)
            assert keep is True, f"{word!r} should pass — only 红娘 triggers Rule A"


class TestSharedFilterRuleB:
    """Rule B: 代发-style proxy-post phrases in post.content."""

    def setup_method(self):
        self.f = SharedFilter()

    def test_filters_代发_keyword(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "代发,本人28岁深圳找对象"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_proxy_post"

    def test_filters_代闺蜜发_via_regex(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "代闺蜜发的征婚帖,28岁医生"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_proxy_post"

    def test_filters_帮表姐发(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "帮表姐发,98年女生"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_proxy_post"

    def test_keeps_本人同意_alone(self):
        """'已获本人同意' 单独不算 — 防止法律披露语境误伤。"""
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "其本人同意公开,作为法院证据"}]
        keep, _ = self.f.evaluate(author, posts=posts)
        assert keep is True

    def test_filters_本人同意_with_explicit_proxy(self):
        """'已获本人同意' + 显式代发 → 仍然命中(通过显式词触发)。"""
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "代闺蜜发,已获本人同意,28岁医生"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_proxy_post"

    def test_filters_本人不在小红书(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "本人不在小红书,有意者私信"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_proxy_post"

    def test_filters_非本人(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "非本人,代朋友找对象"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is False
        assert reason == "matchmaker_proxy_post"

    def test_keeps_normal_self_post(self):
        author = {"nickname": "小花", "bio": ""}
        posts = [{"content": "我28岁深圳工程师 想找个聊得来的"}]
        keep, reason = self.f.evaluate(author, posts=posts)
        assert keep is True
        assert reason is None


class TestSharedFilterMinQuality:
    """§2 minimum-data bar — only enforced when final=True (post-Step 3)."""

    def setup_method(self):
        self.f = SharedFilter()

    def test_skipped_when_not_final(self):
        """Pre-Step 3: missing ip_location should NOT filter."""
        author = {"nickname": "小花", "bio": "", "ip_location": ""}
        keep, _ = self.f.evaluate(author, posts=[], final=False)
        assert keep is True

    def test_fails_without_ip_location_when_final(self):
        author = {"nickname": "小花", "bio": "爱生活", "ip_location": ""}
        posts = [{"content": "今天去爬山真开心"}]
        keep, reason = self.f.evaluate(author, posts=posts, final=True)
        assert keep is False
        assert reason == "low_quality_content"

    def test_fails_with_no_content_when_final(self):
        author = {"nickname": "小花", "bio": "", "ip_location": "深圳"}
        posts = [{"content": "ddd"}]  # too short
        keep, reason = self.f.evaluate(author, posts=posts, final=True)
        assert keep is False
        assert reason == "low_quality_content"

    def test_passes_with_ip_location_and_bio(self):
        author = {"nickname": "小花", "bio": "爱生活", "ip_location": "深圳"}
        keep, _ = self.f.evaluate(author, posts=[], final=True)
        assert keep is True

    def test_passes_with_ip_location_and_long_post(self):
        author = {"nickname": "小花", "bio": "", "ip_location": "深圳"}
        posts = [{"content": "我是98年女生,在深圳工作,想找个稳定的对象"}]
        keep, _ = self.f.evaluate(author, posts=posts, final=True)
        assert keep is True


class TestUserFilter:
    """Per-user location filter — content mention OR province match."""

    def test_filters_wrong_province(self):
        # IP shows a different province AND no content mentions Shenzhen
        f = UserFilter(user_city="深圳", allow_remote=False)
        author = {"ip_location": "北京", "bio": "爱生活"}
        keep, reason = f.evaluate(author)
        assert keep is False
        assert "location" in reason

    def test_allows_when_ip_is_full_city(self):
        f = UserFilter(user_city="深圳")
        author = {"ip_location": "广东深圳"}
        keep, _ = f.evaluate(author)
        assert keep is True

    def test_allows_when_ip_is_province_only(self):
        # XHS shows province only — '广东' is the closest grain we get for SZ
        f = UserFilter(user_city="深圳")
        author = {"ip_location": "广东"}
        keep, _ = f.evaluate(author)
        assert keep is True

    def test_allows_when_content_mentions_city(self):
        # Even if profile IP is missing or wrong, an explicit mention of
        # the target city in the user's content is sufficient.
        f = UserFilter(user_city="深圳")
        author = {"ip_location": "上海", "bio": "在深圳福田工作"}
        keep, _ = f.evaluate(author)
        assert keep is True

    def test_allows_when_content_mentions_district(self):
        f = UserFilter(user_city="深圳")
        author = {"ip_location": "北京"}
        posts = [{"content": "01宝安西乡蹲一个女孩子"}]
        keep, _ = f.evaluate(author, posts=posts)
        assert keep is True

    def test_allows_remote_when_configured(self):
        f = UserFilter(user_city="深圳", allow_remote=True)
        author = {"ip_location": "北京"}
        keep, _ = f.evaluate(author)
        assert keep is True

    def test_allows_unknown_location(self):
        # No IP yet (Step 3 hasn't run); don't reject — wait for profile.
        f = UserFilter(user_city="深圳")
        author = {"ip_location": ""}
        keep, _ = f.evaluate(author)
        assert keep is True

    def test_municipality_target(self):
        # 直辖市: city == province in the map
        f = UserFilter(user_city="上海")
        keep1, _ = f.evaluate({"ip_location": "上海"})
        keep2, _ = f.evaluate({"ip_location": "广东"})
        assert keep1 is True
        assert keep2 is False


class TestRuleFilterCompat:
    """Legacy wrapper: SharedFilter + UserFilter."""

    def test_filters_by_matchmaker_keyword(self):
        f = RuleFilter(user_city="深圳")
        author = {"nickname": "红娘小王", "bio": "", "ip_location": "深圳"}
        keep, _ = f.evaluate(author)
        assert keep is False

    def test_filters_by_location(self):
        f = RuleFilter(user_city="深圳", allow_remote=False)
        # IP=北京 (different province) AND no city mention in content
        author = {"nickname": "小花", "bio": "爱生活", "ip_location": "北京"}
        keep, reason = f.evaluate(author)
        assert keep is False
        assert "location" in reason
