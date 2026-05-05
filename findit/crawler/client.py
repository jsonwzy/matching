"""Xiaohongshu client driven by a persistent logged-in browser, using
UI-navigation + DOM scraping (NOT page.evaluate(fetch))."""

# Why DOM-scraping instead of fetch:
#   Even from inside the logged-in page, calling fetch('/api/.../search/notes')
#   triggers risk-control (`code:300011 当前账号存在异常`). Yet the very same
#   account loads results fine when the user navigates to /search_result?...
#   manually. We mimic that human path: page.goto → wait → read .note-item
#   cards from the DOM. Comments are read the same way after navigating to
#   /explore/<id>.
#
# Why a persistent context with one page:
#   The user QR-scans into the browser once; cookies, localStorage, and
#   browser fingerprint live in a persistent user_data_dir. From XHS's
#   risk-control view, every request looks like "human opened a tab,
#   browsed a search page, clicked into a note" — because that's literally
#   what's happening.

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

from findit.config import settings

logger = logging.getLogger(__name__)

WWW_HOST = "https://www.xiaohongshu.com"
EDITH_HOST = "https://edith.xiaohongshu.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SEARCH_KEYWORDS = [
    "找对象", "找男友", "找搭子", "蹲boyfriend", "脱单",
    "相亲", "CPDD", "征男友", "找另一半", "单身交友",
]

COMMENT_DATING_KEYWORDS = [
    "蹲一个", "蹲男友", "蹲对象", "蹲男朋友", "蹲女友", "蹲女朋友",
    "找对象", "找男友", "找男朋友", "找女友", "找女朋友", "找另一半",
    "求脱单", "求认识", "求交友",
    "单身", "脱单", "交友", "同城",
    "坐标", "互相了解",
    "私聊", "dd", "cpdd", "CPDD",
    "身高1", "本科", "硕士", "研究生",
]


# JS to scrape rendered search-result cards.
_SCRAPE_SEARCH_JS = r"""
() => {
    const out = [];
    document.querySelectorAll('section.note-item').forEach(card => {
        const cover = card.querySelector('a.cover');
        const hidden = card.querySelector('a[href^="/explore/"]');
        let id = '', xsec = '';
        if (hidden) {
            const m = hidden.getAttribute('href').match(/\/explore\/([0-9a-f]+)/);
            if (m) id = m[1];
        }
        if (!id && cover) {
            const m = cover.getAttribute('href').match(/\/(?:search_result|explore)\/([0-9a-f]+)/);
            if (m) id = m[1];
        }
        if (cover) {
            const m = cover.getAttribute('href').match(/xsec_token=([^&]+)/);
            if (m) xsec = decodeURIComponent(m[1]);
        }
        // Card structure on the search-result page:
        //   <section.note-item>
        //     ... cover + img ...
        //     <div class="footer">
        //       <a class="title"><span>...title...</span></a>
        //       <div class="author-wrapper">
        //         <a class="author" href="/user/profile/<id>?...">
        //            <img.../><span class="name">作者名</span>
        //         </a>
        //         <span class="like-wrapper"><span class="count">42</span></span>
        //       </div>
        //     </div>
        //   </section>
        const titleEl = card.querySelector('a.title') || card.querySelector('.title');
        const authorAnchor = card.querySelector('.author-wrapper a.author')
                          || card.querySelector('a[href*="/user/profile/"]');
        const nameEl = card.querySelector('.author-wrapper .name')
                    || (authorAnchor && authorAnchor.querySelector('.name'))
                    || card.querySelector('.name');
        const likeEl = card.querySelector('.like-wrapper .count')
                    || card.querySelector('.count');
        const img = card.querySelector('img');

        let user_id = '';
        if (authorAnchor) {
            user_id = authorAnchor.getAttribute('data-user-id') || '';
            if (!user_id) {
                const m = (authorAnchor.getAttribute('href') || '')
                    .match(/\/user\/profile\/([0-9a-f]+)/);
                if (m) user_id = m[1];
            }
        }
        out.push({
            id,
            xsec_token: xsec,
            title: titleEl ? titleEl.textContent.trim() : '',
            author_user_id: user_id,
            author_nickname: nameEl ? nameEl.textContent.trim() : '',
            likes_text: likeEl ? likeEl.textContent.trim() : '0',
            cover_url: img ? (img.getAttribute('src') || '') : '',
        });
    });
    return out;
}
"""

# JS to scrape comments on a /explore/<id> page after it's loaded.
_SCRAPE_COMMENTS_JS = r"""
() => {
    const out = [];
    document.querySelectorAll('.comment-item').forEach(item => {
        const id = (item.id || '').replace(/^comment-/, '');
        const authorA = item.querySelector('.author a.name')
                     || item.querySelector('.author-wrapper a.name')
                     || item.querySelector('a.name');
        const contentEl = item.querySelector('.content .note-text')
                       || item.querySelector('.note-text')
                       || item.querySelector('.content');
        const ipEl = item.querySelector('.location')
                  || item.querySelector('.info .location');
        const likeEl = item.querySelector('.like .count')
                    || item.querySelector('.interaction .count');
        const tagEl = item.querySelector('.tag');

        let user_id = '';
        if (authorA) {
            user_id = authorA.getAttribute('data-user-id') || '';
            if (!user_id) {
                const m = (authorA.getAttribute('href') || '')
                    .match(/\/user\/profile\/([0-9a-f]+)/);
                if (m) user_id = m[1];
            }
        }
        const avatar = item.querySelector('.avatar img');
        out.push({
            comment_id: id,
            user_id,
            nickname: authorA ? authorA.textContent.trim() : '',
            avatar: avatar ? (avatar.getAttribute('src') || '') : '',
            content: contentEl ? contentEl.textContent.trim() : '',
            ip_location: ipEl ? ipEl.textContent.trim() : '',
            like_count_text: likeEl ? likeEl.textContent.trim() : '0',
            is_author: !!(tagEl && tagEl.textContent.trim() === '作者'),
        });
    });
    return out;
}
"""


# Markers XHS shows when its risk-control trips. We watch for these in the
# page body and the URL after every profile navigation. The list is
# deliberately broad — if XHS adds a new wording the worst case is one
# extra request before backoff, but we'd rather over-detect than under.
RISK_CONTROL_TEXT_MARKERS = [
    # 频次类
    "请求太频繁", "请求过于频繁", "访问过于频繁", "操作太频繁",
    "操作过于频繁", "请稍后再试", "稍后再试",
    # 异常账号类
    "账号存在异常", "存在异常行为", "异常请求", "当前账号存在异常",
    # 验证码 / 滑块 / 拼图
    "需要验证", "完成验证", "滑动验证", "拼图验证", "请输入验证码",
    # APP 扫码二次校验（XHS 常用）—
    # "为保护账号安全，请使用已登录该账号的「小红书APP」扫码验证身份"
    "为保护账号安全", "扫码验证身份", "扫码验证",
]
RISK_CONTROL_URL_MARKERS = ["captcha", "verify", "block", "punish"]


_LOGIN_PROBE_JS = r"""
() => {
    // any visible login modal/qr → NOT logged in
    const loginSelectors = [
        '.login-container', '.login-mask', '.qrcode-img',
        '.login-pannel', '.login-panel',
        'div[class*="login"][class*="modal"]', 'div[class*="qrcode"]',
    ];
    for (const sel of loginSelectors) {
        const el = document.querySelector(sel);
        if (el && el.offsetParent !== null) return {logged_in: false, why: 'login_modal'};
    }
    // any visible "登录" button → NOT logged in
    const allText = document.querySelectorAll('button, a, span, div');
    for (const el of allText) {
        if (!el.offsetParent) continue;
        const t = (el.textContent || '').trim();
        if (t === '登录' || t === 'Login' || t === 'Sign in' || t === '登录注册') {
            // but make sure it's a small button, not a paragraph with the word
            if (t.length <= 6) return {logged_in: false, why: 'login_button'};
        }
    }
    // require at least one logged-in marker
    const okSelectors = [
        '.side-bar-component .user .name',
        '.user-info .name',
        'div[class*="user"] img[class*="avatar"]',
        '.reds-avatar img',
    ];
    const found = okSelectors.find(sel => document.querySelector(sel));
    if (found) return {logged_in: true, why: 'marker:' + found};
    return {logged_in: false, why: 'no_marker'};
}
"""


class XHSClient:
    """Browser-driven XHS client.

    Owns a persistent headed Chromium session. The first time it's used,
    the user must QR-scan to log in. After that, the profile dir holds the
    cookies and localStorage so subsequent runs skip the login step.

    Public methods preserve the previous XHSClient interface so the
    runner / services don't need changes.
    """

    WEB_URL = WWW_HOST

    def __init__(self, cookie: str | None = None):
        # cookie param kept for backwards compatibility but unused —
        # cookies live in the persistent profile dir.
        del cookie
        self._delay_min = settings.crawl_request_delay_min
        self._delay_max = settings.crawl_request_delay_max
        self._max_retries = settings.crawl_max_retries
        self._retry_base = settings.crawl_retry_base_delay

        self._playwright = None
        self._context = None
        self._page: Page | None = None
        self._started = False
        self._lock = asyncio.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def setup(self) -> None:
        if self._started:
            return
        profile = Path(settings.browser_profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        logger.info("Launching persistent Chromium (profile=%s)", profile)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=settings.browser_headless,
            user_agent=USER_AGENT,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
        )
        # reuse first page or open new one
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        await self._page.goto(WWW_HOST, wait_until="domcontentloaded",
                              timeout=settings.playwright_page_timeout)
        await self._page.wait_for_function(
            "() => typeof window._webmsxyw === 'function'",
            timeout=settings.playwright_sign_timeout,
        )

        if not await self._is_logged_in():
            await self._wait_for_login()

        self._started = True
        logger.info("XHSClient ready (logged in, page on %s)", self._page.url)

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._page = None
        self._started = False

    async def _is_logged_in(self) -> bool:
        """Return True only when DOM signal AND a real web_session cookie exist."""
        if self._page is None or self._context is None:
            return False
        try:
            res = await self._page.evaluate(_LOGIN_PROBE_JS)
        except Exception:
            return False
        if not isinstance(res, dict) or not res.get("logged_in"):
            return False
        # also require a non-trivial web_session cookie
        try:
            cookies = await self._context.cookies()
        except Exception:
            return False
        for c in cookies:
            if c.get("name") == "web_session" and len(c.get("value", "")) >= 20:
                return True
        return False

    async def _wait_for_login(self, timeout_sec: int = 300) -> None:
        assert self._page is not None
        print("\n" + "=" * 60)
        print("👉 请在打开的 Chrome 窗口里用 XHS APP 扫码登录")
        print("   登录后脚本会自动检测并继续，无需手动关窗口。")
        print("=" * 60 + "\n")
        try:
            await self._page.evaluate("""
                const b = document.createElement('div');
                b.id = '__findit_banner';
                b.textContent = '🔄 等你扫码登录…完成后脚本自动继续';
                b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#e60023;color:#fff;font:bold 16px/36px sans-serif;text-align:center;padding:6px;box-shadow:0 2px 6px rgba(0,0,0,0.3)';
                document.body.appendChild(b);
            """)
        except Exception:
            pass

        start = time.time()
        stable = 0
        while time.time() - start < timeout_sec:
            if await self._is_logged_in():
                stable += 1
                if stable >= 4:
                    print("✅ 登录确认")
                    try:
                        await self._page.evaluate(
                            "const b = document.getElementById('__findit_banner'); if (b) b.remove();"
                        )
                    except Exception:
                        pass
                    return
            else:
                stable = 0
            await asyncio.sleep(1)
        raise RuntimeError(f"Login timeout after {timeout_sec}s")

    # ── Throttle / retry ────────────────────────────────────────────────

    async def _sleep(self) -> None:
        await asyncio.sleep(random.uniform(self._delay_min, self._delay_max))

    async def _navigate(self, url: str, *, settle_sec: float = 4.0) -> None:
        """Navigate the single shared page and let it settle."""
        assert self._page is not None
        async with self._lock:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.playwright_page_timeout,
            )
            await asyncio.sleep(settle_sec)

    # ── Public API (matches old XHSClient) ──────────────────────────────

    # cache xsec_token by note_id from the most recent search; needed when
    # navigating to /explore/<id> for comments
    _NOTE_XSEC: dict[str, str] = {}

    async def search_notes(
        self,
        keyword: str,
        sort: str = "time_descending",
        page: int = 1,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        del sort, page_size  # not parameters on UI search URL
        await self._sleep()
        from urllib.parse import quote
        url = (
            f"{WWW_HOST}/search_result?keyword={quote(keyword)}"
            f"&source=web_search_result_notes&page={page}"
        )
        logger.info("search '%s' (page %d) → %s", keyword, page, url)
        try:
            await self._navigate(url, settle_sec=5.0)
            cards = await self._page.evaluate(_SCRAPE_SEARCH_JS)
        except Exception:
            logger.exception("search_notes failed for '%s'", keyword)
            return []
        if not isinstance(cards, list):
            logger.warning("search returned non-list: %r", cards)
            return []

        results = []
        for c in cards:
            if not c.get("id"):
                continue
            self._NOTE_XSEC[c["id"]] = c.get("xsec_token", "")
            results.append({
                "id": c["id"],
                "title": c.get("title", ""),
                "desc": "",
                "content": c.get("title", ""),
                "user_id": c.get("author_user_id", ""),
                "user_nickname": c.get("author_nickname", ""),
                "user_avatar": c.get("cover_url", ""),
                "likes": c.get("likes_text", "0"),
                "image_list": [c.get("cover_url", "")] if c.get("cover_url") else [],
                "time": None,
                "ip_location": "",
                "xsec_token": c.get("xsec_token", ""),
            })
        logger.info("search '%s' page %d → %d cards (DOM)", keyword, page, len(results))
        return results

    async def get_note_comments(
        self, note_id: str, cursor: str = "", page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], str]:
        del cursor, page_size  # DOM scrape gets the first page already rendered
        await self._sleep()
        xsec = self._NOTE_XSEC.get(note_id, "")
        url = f"{WWW_HOST}/explore/{note_id}"
        if xsec:
            url += f"?xsec_token={xsec}&xsec_source=pc_search"
        try:
            await self._navigate(url, settle_sec=4.0)
            # try to scroll the comment area into view to ensure render
            try:
                await self._page.evaluate(
                    "() => { const el = document.querySelector('.comments-el')"
                    " || document.querySelector('.comment-container');"
                    " if (el) el.scrollIntoView(); }"
                )
                await asyncio.sleep(2)
            except Exception:
                pass
            comments = await self._page.evaluate(_SCRAPE_COMMENTS_JS)
        except Exception:
            logger.exception("get_note_comments failed for %s", note_id)
            return [], ""
        if not isinstance(comments, list):
            return [], ""

        out = []
        for c in comments:
            try:
                like = int(re.sub(r"\D", "", c.get("like_count_text", "0") or "0") or 0)
            except Exception:
                like = 0
            out.append({
                "comment_id": c.get("comment_id", ""),
                "content": c.get("content", ""),
                "user_id": c.get("user_id", ""),
                "nickname": c.get("nickname", ""),
                "avatar": c.get("avatar", ""),
                "ip_location": c.get("ip_location", ""),
                "like_count": like,
                "create_time": None,
                "is_author": c.get("is_author", False),
            })
        logger.info("note %s comments → %d (DOM)", note_id, len(out))
        # DOM only shows page 1; no cursor available
        return out, ""

    def filter_dating_comments(self, comments: list[dict]) -> list[dict]:
        out = []
        for c in comments:
            content_lower = c.get("content", "").lower()
            if any(kw in content_lower for kw in COMMENT_DATING_KEYWORDS):
                out.append(c)
        return out

    async def _profile_sleep(self) -> None:
        """Profile pages need a much longer, human-paced delay (see
        feedback_xhs_rate_limit memory)."""
        await asyncio.sleep(random.uniform(
            settings.crawl_profile_delay_min,
            settings.crawl_profile_delay_max,
        ))

    async def detect_rate_limit(self) -> tuple[bool, str]:
        """Inspect the current page for risk-control markers.

        Returns (is_rate_limited, signal). `signal` is a short reason
        suitable for logging. Cheap to call after every navigation.
        """
        if self._page is None:
            return False, ""
        try:
            body = await self._page.evaluate(
                "() => (document.body && document.body.innerText) "
                "         ? document.body.innerText.slice(0, 3000) : ''"
            )
        except Exception:
            return False, ""
        if isinstance(body, str):
            for marker in RISK_CONTROL_TEXT_MARKERS:
                if marker in body:
                    return True, f"text:{marker}"
        try:
            url = (self._page.url or "").lower()
        except Exception:
            url = ""
        for u_marker in RISK_CONTROL_URL_MARKERS:
            if u_marker in url:
                return True, f"url:{u_marker}"
        return False, ""

    async def get_user_profile(
        self, user_id: str, nickname: str | None = None
    ) -> dict[str, Any]:
        """Scrape /user/profile/<user_id>. Returns basics + a digest of
        the user's own posted notes (id, title, likes) — same page, no
        extra navigation.

        Anti-detection: when `nickname` is given, we first navigate to a
        search-results page for that nickname and dwell briefly. The
        subsequent profile-page request then carries a Referer of the
        search page, which is what an organic "user discovered via search
        → click" flow looks like. Hitting profile URLs directly with no
        upstream Referer is the pattern XHS rate-limits hardest. Falling
        back to a direct goto if no nickname is given.

        DOM contract (verified on a real profile page 2026-05-05):
          .user-name      → nickname
          .user-desc      → bio
          .user-IP        → "IP属地：广东"  (province granularity)
          .user-info      → aggregate text incl. age + city when present
                            e.g. "26岁广东深圳5关注69粉丝226获赞与收藏"
          section.note-item / .note-item (note grid)
              → each card: a[href*="/explore/<id>"], .title, .count
        """
        await self._profile_sleep()
        try:
            if nickname:
                from urllib.parse import quote
                search_url = (
                    f"{WWW_HOST}/search_result?keyword={quote(nickname)}"
                    "&source=web_search_result_notes"
                )
                # Warm-up: same-tab navigation establishes the search
                # page as the Referer for the next request.
                await self._navigate(
                    search_url, settle_sec=random.uniform(2.0, 4.0)
                )
            await self._navigate(f"{WWW_HOST}/user/profile/{user_id}", settle_sec=4.0)
        except Exception:
            logger.exception("profile navigate failed for %s", user_id)
            return {"id": user_id}

        # Realtime risk-control check. We do this BEFORE reading any
        # profile fields — when 风控 fires, the page either redirects to
        # a verify URL or replaces the body with a "请求太频繁" notice.
        hit, reason = await self.detect_rate_limit()
        if hit:
            logger.warning("⚠️  风控 detected on %s (%s) — backing off",
                           user_id, reason)
            return {"id": user_id, "rate_limited": True,
                    "rate_limit_reason": reason}

        try:
            data = await self._page.evaluate(r"""
                () => {
                    const text = sel => {
                        const e = document.querySelector(sel);
                        return e ? (e.textContent || '').trim() : '';
                    };
                    const nick = text('.user-name') || text('.user-nickname') || text('.nickname');
                    const desc = text('.user-desc') || text('.user-bio');
                    const ip_raw = text('.user-IP')
                                || text('[class*="user-IP"]')
                                || text('[class*="user-ip"]');
                    const aggregate = text('.user-info') || text('.basic-info') || '';
                    const avatar = document.querySelector(
                        '.user-avatar img, .avatar img, img[class*="avatar"]'
                    );
                    const ip = ip_raw.replace(/^IP属地[：:]\s*/, '').trim();

                    // Note grid on this same page. We deliberately drop
                    // cover image URL — the product surfaces a profile/
                    // post link, users click through for visuals.
                    const notes = [];
                    const cards = document.querySelectorAll(
                        'section.note-item, .note-item'
                    );
                    cards.forEach(card => {
                        const a = card.querySelector('a[href*="/explore/"]')
                               || card.querySelector('a');
                        if (!a) return;
                        const m = (a.getAttribute('href') || '')
                                  .match(/\/explore\/([0-9a-f]+)/);
                        if (!m) return;
                        const note_id = m[1];
                        const tEl = card.querySelector('a.title') ||
                                    card.querySelector('.title');
                        const cEl = card.querySelector('.like-wrapper .count') ||
                                    card.querySelector('.count');
                        notes.push({
                            note_id,
                            title: tEl ? (tEl.textContent || '').trim() : '',
                            like_count_text: cEl ? (cEl.textContent || '').trim() : '0',
                        });
                    });

                    return {
                        nickname: nick,
                        bio: desc,
                        ip_location: ip,
                        aggregate_text: aggregate,
                        avatar_url: avatar ? (avatar.getAttribute('src') || '') : '',
                        notes,
                    };
                }
            """)
        except Exception:
            logger.exception("get_user_profile failed for %s", user_id)
            return {"id": user_id}

        if not isinstance(data, dict):
            return {"id": user_id}

        agg = data.get("aggregate_text") or ""
        age_match = re.search(r"(\d{1,2})岁", agg)
        age_tag = age_match.group(1) + "岁" if age_match else ""

        notes_summary = []
        for n in (data.get("notes") or []):
            try:
                like = int(re.sub(r"\D", "",
                                  n.get("like_count_text") or "0") or 0)
            except Exception:
                like = 0
            notes_summary.append({
                "note_id": n.get("note_id", ""),
                "title": n.get("title", ""),
                "like_count": like,
            })

        return {
            "id": user_id,
            "nickname": data.get("nickname", ""),
            "avatar_url": data.get("avatar_url", ""),
            "ip_location": data.get("ip_location", ""),
            "bio": data.get("bio", ""),
            "age_tag": age_tag,
            "aggregate_text": agg,
            "notes_summary": notes_summary,
            "followers": 0,
            "following": 0,
            "likes_collected": 0,
        }

    async def get_user_notes(
        self, user_id: str, cursor: str = "", page_size: int = 30,
    ) -> tuple[list[dict[str, Any]], str]:
        del cursor, page_size
        await self._sleep()
        try:
            await self._navigate(f"{WWW_HOST}/user/profile/{user_id}", settle_sec=4.0)
            notes = await self._page.evaluate(r"""
                () => {
                    const out = [];
                    document.querySelectorAll('section.note-item, .note-item').forEach(card => {
                        const a = card.querySelector('a[href*="/explore/"]')
                              || card.querySelector('a[href*="/profile/"]')
                              || card.querySelector('a');
                        let id = '';
                        if (a) {
                            const m = (a.getAttribute('href') || '').match(/\/explore\/([0-9a-f]+)/);
                            if (m) id = m[1];
                        }
                        if (!id) return;
                        const title = card.querySelector('.title, a.title');
                        const likes = card.querySelector('.count, .like-wrapper .count');
                        const img = card.querySelector('img');
                        out.push({
                            note_id: id,
                            title: title ? title.textContent.trim() : '',
                            cover: img ? (img.getAttribute('src') || '') : '',
                            likes: likes ? likes.textContent.trim() : '0',
                            type: card.querySelector('video') ? 'video' : 'normal',
                        });
                    });
                    return out;
                }
            """)
        except Exception:
            logger.exception("get_user_notes failed for %s", user_id)
            return [], ""
        return notes if isinstance(notes, list) else [], ""

    def get_note_url(self, note_id: str) -> str:
        return f"{WWW_HOST}/explore/{note_id}"

    def get_user_url(self, user_id: str) -> str:
        return f"{WWW_HOST}/user/profile/{user_id}"


# Re-exported for any code importing it
__all__ = ["XHSClient", "SEARCH_KEYWORDS", "COMMENT_DATING_KEYWORDS", "USER_AGENT"]


# Helper (used by tests, kept for compat with old client)
def _cookie_str_to_dict(cookie_str: str) -> dict[str, str]:
    result = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            result[name.strip()] = value.strip()
    return result
