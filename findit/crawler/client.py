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

# Subset of RISK_CONTROL_TEXT_MARKERS that are *interactive verification*
# (slider / puzzle / image code) rather than pure frequency limiting.
# Tracked separately for OBSERVATION (observe_risk_markers) so we can
# tell, from the logs, whether a captcha that may not actually block
# browsing is what tripped a sweep abort. detect_rate_limit still treats
# every RISK_CONTROL_TEXT_MARKERS entry the same — this list changes
# nothing about control flow.
CAPTCHA_TEXT_MARKERS = [
    "滑动验证", "拼图验证", "请输入验证码", "需要验证", "完成验证",
]

# Text fingerprints of the per-note "app-scan gate" modal — XHS's
# "当前笔记暂时无法浏览 / 请打开 App 扫码" popup. Deliberately kept
# OUT of RISK_CONTROL_TEXT_MARKERS: this is a per-POST gate, not
# account-level 风控. Folding it into the 风控 markers would abort the
# whole sweep on a single gated note. dismiss_modal() closes it and
# the sweep soft-skips just that post (see runner's modal_blocked
# stat). Markers are chosen to NOT overlap 风控's "扫码验证 / 扫码
# 验证身份" — those stay 风控.
_APP_SCAN_MODAL_MARKERS = [
    "暂时无法浏览", "笔记暂时无法",
    "打开App", "打开 App", "打开小红书", "小红书App",
]


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
        d = random.uniform(self._delay_min, self._delay_max)
        logger.info("⏱ request delay %.1fs", d)
        await asyncio.sleep(d)

    async def _navigate(self, url: str, *, settle_sec: float = 4.0,
                        wait_selector: str | None = None) -> None:
        """Navigate the single shared page and let it settle.

        With wait_selector, wait for that element to appear (capped at
        8s) instead of blind-sleeping the full settle_sec — the faster,
        smarter path. Falls through on timeout so a gated/404 page
        doesn't hang. A short jittered micro-pause follows either way so
        we never act the instant the DOM node appears.
        """
        assert self._page is not None
        async with self._lock:
            await self._page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.playwright_page_timeout,
            )
            if wait_selector:
                try:
                    await self._page.wait_for_selector(
                        wait_selector, timeout=8000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.6, 1.4))
            else:
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
        """Jittered human-paced pause before a profile scrape. The range
        (config crawl_profile_delay_*) is a tunable starting point — the
        jitter matters more than the absolute length. See memory
        feedback_xhs_rate_limit."""
        d = random.uniform(
            settings.crawl_profile_delay_min,
            settings.crawl_profile_delay_max,
        )
        logger.info("⏱ profile delay %.1fs", d)
        await asyncio.sleep(d)

    async def detect_rate_limit(self) -> tuple[bool, str]:
        """Inspect the current page for risk-control markers.

        Returns (is_rate_limited, signal). Match URL markers only in
        the PATH segment — earlier versions matched on the whole URL
        and false-positived on innocuous query strings (e.g. XHS's 404
        redirect URL carries a literal `verifyMsg=` query param even
        though there's no risk-control happening — the post is just
        deleted).
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
            from urllib.parse import urlparse
            path = urlparse(self._page.url or "").path.lower()
        except Exception:
            path = ""
        for u_marker in RISK_CONTROL_URL_MARKERS:
            if u_marker in path:
                return True, f"url:{u_marker}"
        return False, ""

    async def observe_risk_markers(self, context: str) -> list[str]:
        """OBSERVATION ONLY — never affects control flow or aborts.

        Scan the current page for risk-control / verification markers
        and log whatever is found, tagging captcha-class markers
        separately. This makes a non-blocking image captcha visible in
        the logs even when it appears somewhere detect_rate_limit isn't
        on the abort path (e.g. the search page). detect_rate_limit's
        behaviour is deliberately left unchanged.
        """
        if self._page is None:
            return []
        try:
            body = await self._page.evaluate(
                "() => (document.body && document.body.innerText) "
                "         ? document.body.innerText.slice(0, 3000) : ''"
            )
        except Exception:
            return []
        if not isinstance(body, str):
            return []
        found = []
        for marker in RISK_CONTROL_TEXT_MARKERS:
            if marker in body:
                kind = "captcha" if marker in CAPTCHA_TEXT_MARKERS else "rate"
                found.append(f"{kind}:{marker}")
        if found:
            logger.warning(
                "👁 risk-marker observed [%s]: %s — OBSERVATION ONLY, "
                "sweep continues", context, ", ".join(found),
            )
        return found

    async def detect_post_unavailable(self) -> tuple[bool, str]:
        """Detect that the page-load landed on XHS's "post unavailable"
        screen (404 / 300031 / deleted / private). Distinct from 风控:
        the post is just gone, not the account in trouble.

        Returns (unavailable, reason). When True, callers should treat
        the candidate as a soft skip — don't sleep, don't count toward
        a 风控 abort.
        """
        if self._page is None:
            return False, ""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self._page.url or "")
            path = parsed.path.lower()
            qs = parse_qs(parsed.query)
        except Exception:
            return False, ""
        if path.startswith("/404"):
            err = (qs.get("error_code") or [""])[0]
            return True, f"404:{err}" if err else "404"
        # XHS sometimes embeds error_code in the source param too
        if "300031" in (qs.get("error_code") or [""])[0]:
            return True, "error_code:300031"
        try:
            title = (await self._page.title()) or ""
        except Exception:
            title = ""
        if "页面不见了" in title or "页面不存在" in title:
            return True, "title:page-missing"
        return False, ""

    async def get_user_profile(
        self, user_id: str, nickname: str | None = None
    ) -> dict[str, Any]:
        """Legacy "search-warmup + goto" path. Kept for callers (diag
        scripts) that don't have a parent post URL to click from.

        Production batch step3 uses get_user_profile_via_click instead
        — see that docstring for the more organic path.

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
                await self._navigate(
                    search_url, settle_sec=random.uniform(2.0, 4.0)
                )
            await self._navigate(f"{WWW_HOST}/user/profile/{user_id}", settle_sec=4.0)
        except Exception:
            logger.exception("profile navigate failed for %s", user_id)
            return {"id": user_id}

        hit, reason = await self.detect_rate_limit()
        if hit:
            logger.warning("⚠️  风控 detected on %s (%s) — backing off",
                           user_id, reason)
            return {"id": user_id, "rate_limited": True,
                    "rate_limit_reason": reason}

        return await self._scrape_profile_fields_from_page(user_id)

    async def get_user_profile_via_click(
        self,
        user_id: str,
        source_note_id: str,
        comment_id: str | None = None,
        search_query: str | None = None,
    ) -> dict[str, Any]:
        """Scrape /user/profile/<user_id> via DOM clicks.

        Two paths, automatically chosen based on what's available:

          B2 (preferred, when search_query is given):
              goto /search_result?keyword=<search_query>
              → DOM click the target post card
              → DOM click the user's avatar/name (post header or comment)
              → land on /user/profile/<user_id>

          B1 (fallback, when search_query is None or target not in
          search results):
              goto /explore/<source_note_id>
              → DOM click the user's avatar/name
              → land on /user/profile/<user_id>

        Why B2: each request's Referer matches what a human-organic
        path produces (search → post → profile). The XHS risk-control
        signal we observed in earlier smoke runs (`为保护账号安全`
        banner) is path-sensitive, not pure rate. B2 trades one extra
        request per profile for a more natural request shape.

        Why B1 fallback: search query is sometimes too generic
        ("深圳找对象") and the target post isn't on page 1 of results.
        Don't waste time scrolling — fall back to a direct explore
        navigation and still get the explore-page Referer on the
        profile click.

        Returns:
          - {"id": ..., other profile fields}  on success
          - {"id": ..., "rate_limited": True, "rate_limit_reason": ...}
          - {"id": ..., "navigation_failed": True}  if click chain
            couldn't complete (target not visible anywhere, etc.) — caller
            should NOT count this toward the 风控 abort counter
        """
        await self._profile_sleep()
        assert self._page is not None

        landed_on_explore = False

        # --- B2: search → click post --------------------------------
        if search_query:
            from urllib.parse import quote
            search_url = (
                f"{WWW_HOST}/search_result?keyword={quote(search_query)}"
                "&source=web_search_result_notes"
            )
            try:
                await self._navigate(
                    search_url, settle_sec=random.uniform(3.0, 5.0)
                )
            except Exception:
                logger.exception("B2 search nav failed for query=%r", search_query)
            else:
                hit, reason = await self.detect_rate_limit()
                if hit:
                    logger.warning(
                        "⚠️  风控 on search page (%s) — abort early", reason,
                    )
                    return {"id": user_id, "rate_limited": True,
                            "rate_limit_reason": reason}

                post_sel = f'a[href*="/explore/{source_note_id}"]'
                count = await self._page.locator(post_sel).count()
                if count > 0:
                    try:
                        async with self._lock:
                            await self._page.locator(post_sel).first.click()
                            try:
                                await self._page.wait_for_url(
                                    f"**/explore/{source_note_id}**",
                                    timeout=10000,
                                )
                                await asyncio.sleep(random.uniform(3.0, 5.0))
                                landed_on_explore = True
                            except Exception:
                                logger.warning(
                                    "B2: post click didn't navigate for %s",
                                    source_note_id,
                                )
                    except Exception:
                        logger.exception("B2: post click failed")
                else:
                    logger.info(
                        "B2: post %s not in search results for query=%r — fallback to B1",
                        source_note_id, search_query,
                    )

        # --- B1 fallback: direct goto explore -----------------------
        if not landed_on_explore:
            source_url = f"{WWW_HOST}/explore/{source_note_id}"
            try:
                await self._navigate(source_url, settle_sec=4.0)
            except Exception:
                logger.exception("explore nav failed for %s", source_note_id)
                return {"id": user_id, "navigation_failed": True}
            # Old posts get deleted / hidden; XHS redirects to /404 with
            # an error code. Treat as soft-skip (post unavailable), not
            # 风控 — don't burn cooldown on a candidate we can't reach.
            gone, why = await self.detect_post_unavailable()
            if gone:
                logger.info(
                    "explore/%s unavailable (%s) — soft skip",
                    source_note_id, why,
                )
                return {"id": user_id, "navigation_failed": True}
            hit, reason = await self.detect_rate_limit()
            if hit:
                logger.warning(
                    "⚠️  风控 on explore page %s (%s)", source_note_id, reason,
                )
                return {"id": user_id, "rate_limited": True,
                        "rate_limit_reason": reason}

        # --- Click the user's link from the explore page ------------
        # When comment_id is set, scroll the comment list into view so
        # the right comment-item is rendered before we try to find it.
        if comment_id:
            try:
                await self._page.evaluate(
                    "() => { const el = document.querySelector('.comments-el')"
                    " || document.querySelector('.comment-container');"
                    " if (el) el.scrollIntoView(); }"
                )
                await asyncio.sleep(1.5)
            except Exception:
                pass

        selectors = []
        if comment_id:
            selectors.append(
                f'#comment-{comment_id} a[href*="/user/profile/{user_id}"]'
            )
        selectors.append(f'a[href*="/user/profile/{user_id}"]')

        target_locator = None
        for sel in selectors:
            if await self._page.locator(sel).count() > 0:
                target_locator = self._page.locator(sel).first
                break

        # Lazy-load: scroll within the page a few times to surface more
        # comments if the target's link isn't visible yet.
        if target_locator is None:
            for _ in range(3):
                try:
                    await self._page.evaluate("() => window.scrollBy(0, 800)")
                    await asyncio.sleep(1.5)
                except Exception:
                    break
                for sel in selectors:
                    if await self._page.locator(sel).count() > 0:
                        target_locator = self._page.locator(sel).first
                        break
                if target_locator is not None:
                    break

        if target_locator is None:
            logger.warning(
                "click: link to %s not found on explore/%s",
                user_id, source_note_id,
            )
            return {"id": user_id, "navigation_failed": True}

        try:
            async with self._lock:
                await target_locator.click()
                try:
                    await self._page.wait_for_url(
                        f"**/user/profile/{user_id}**",
                        timeout=10000,
                    )
                except Exception:
                    logger.warning(
                        "click: profile nav didn't complete for %s", user_id,
                    )
                    return {"id": user_id, "navigation_failed": True}
                await asyncio.sleep(4.0)
        except Exception:
            logger.exception("click: profile click failed for %s", user_id)
            return {"id": user_id, "navigation_failed": True}

        hit, reason = await self.detect_rate_limit()
        if hit:
            logger.warning("⚠️  风控 on profile %s (%s)", user_id, reason)
            return {"id": user_id, "rate_limited": True,
                    "rate_limit_reason": reason}

        return await self._scrape_profile_fields_from_page(user_id)

    async def _scrape_profile_fields_from_page(
        self, user_id: str,
    ) -> dict[str, Any]:
        """JS-evaluate the currently-loaded profile page and parse fields.

        Assumes page is already on /user/profile/<id> and not 风控'd —
        callers do navigation + rate-limit checks. Returns the same
        shape as get_user_profile.
        """
        assert self._page is not None
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
            logger.exception("profile field scrape failed for %s", user_id)
            return {"id": user_id}

        if not isinstance(data, dict):
            return {"id": user_id}

        # Parse the .user-info aggregate text. Observed format:
        #   "<nick or empty>26岁广东深圳5关注69粉丝226获赞与收藏关注"
        agg = data.get("aggregate_text") or ""
        age_match = re.search(r"(\d{1,2})岁", agg)
        age_tag = age_match.group(1) + "岁" if age_match else ""

        def _count(pattern: str) -> int:
            m = re.search(pattern, agg)
            if not m:
                return 0
            raw = m.group(1)
            if "万" in raw:
                try:
                    return int(float(raw.replace("万", "")) * 10000)
                except Exception:
                    return 0
            try:
                return int(raw)
            except Exception:
                return 0

        following = _count(r"([\d.]+万?)关注")
        followers = _count(r"([\d.]+万?)粉丝")
        likes_collected = _count(r"([\d.]+万?)获赞")

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
            "notes_summary": notes_summary,
            "followers": followers,
            "following": following,
            "likes_collected": likes_collected,
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

    # ── Single-session sweep helpers ────────────────────────────────────
    # Primitives for the click-everywhere sweep flow (sweep_keyword in
    # runner.py). The sweep stays in one logical browsing session so
    # xsec_tokens remain valid throughout — no DB persistence needed
    # for the auth chain, the live SPA carries it.

    # Passive stacking-layer overlays — NOT modals, just the post-detail
    # container's transparent mask that happens to sit above the link we
    # want to click. We don't dismiss these (they're benign, and removing
    # them could disturb XHS's UI state); _neutralize_overlays sets
    # pointer-events:none so a real click() passes through to the link
    # underneath. The layer stays visually present. v9-verified.
    #
    # Real modals (the "当前笔记暂时无法浏览 / 请打开 App 扫码"
    # access-prompt popup) are a different animal — see
    # _REAL_MODAL_SELECTORS / dismiss_modal below. Two jobs, two
    # mechanisms: stacking layer → pass through; real modal → close.
    _CLICK_BLOCKERS = (
        ".note-detail-mask",        # post-detail modal overlay
        ".note-detail-page-mask",
    )

    # Real XHS modals. Left open, these keep the SPA's UI state machine
    # stuck and silently swallow the next card/link click — so
    # dismiss_modal() truly closes them (ESC, then the 「关闭」 button)
    # instead of just neutralizing pointer events. List is deliberately
    # broad: an extra ESC keypress when no modal is open is harmless.
    _REAL_MODAL_SELECTORS = (
        ".access-modal",            # access-prompt modal
        ".access-modal-content",    # its inner content wrapper
        ".reds-modal-open",         # XHS UI-lib open-modal state class
        ".reds-modal",              # XHS UI-lib modal base
        ".reds-mask",               # XHS UI-lib modal backdrop
    )

    async def _neutralize_overlays(self) -> dict:
        """Set pointer-events:none on known XHS overlay containers so a
        real Playwright click() can land on the link underneath.
        Returns diagnostic info: {selector: {count, text_snippet}} for
        any overlay actually found.
        """
        assert self._page is not None
        sel_list = ",".join(f"'{s}'" for s in self._CLICK_BLOCKERS)
        try:
            return await self._page.evaluate(f"""
                () => {{
                    const sels = [{sel_list}];
                    const found = {{}};
                    sels.forEach(s => {{
                        const els = document.querySelectorAll(s);
                        if (els.length === 0) return;
                        const first = els[0];
                        found[s] = {{
                            count: els.length,
                            text: (first.innerText || '').trim().slice(0, 200),
                        }};
                        els.forEach(el => {{
                            el.style.pointerEvents = 'none';
                        }});
                    }});
                    return found;
                }}
            """)
        except Exception:
            return {}

    async def _detect_modal(self) -> dict:
        """Return {present, selector, text, kind} for any *visibly open*
        XHS access/reds modal. A state class on <body> (.reds-modal-open)
        also counts as present.

        `kind` classifies what was found:
          "app_scan" — the per-note "当前笔记暂时无法浏览 / 请打开 App
                       扫码" gate. A per-POST condition, NOT account-
                       level 风控 — callers soft-skip the post, they
                       must not abort the sweep.
          "other"    — some other modal we still want closed.
          ""         — nothing present.
        """
        assert self._page is not None
        sel_list = ",".join(f"'{s}'" for s in self._REAL_MODAL_SELECTORS)
        try:
            info = await self._page.evaluate(f"""
                () => {{
                    const sels = [{sel_list}];
                    for (const s of sels) {{
                        for (const el of document.querySelectorAll(s)) {{
                            if (el === document.body) {{
                                return {{present: true, selector: s, text: ''}};
                            }}
                            const r = el.getBoundingClientRect();
                            const cs = getComputedStyle(el);
                            if (r.width > 0 && r.height > 0 &&
                                cs.display !== 'none' &&
                                cs.visibility !== 'hidden') {{
                                return {{
                                    present: true, selector: s,
                                    text: (el.innerText || '').trim().slice(0, 120),
                                }};
                            }}
                        }}
                    }}
                    return {{present: false, selector: '', text: ''}};
                }}
            """)
        except Exception:
            return {"present": False, "selector": "", "text": "", "kind": ""}
        if not isinstance(info, dict) or not info.get("present"):
            return {"present": False, "selector": "", "text": "", "kind": ""}
        text = info.get("text") or ""
        selector = info.get("selector") or ""
        is_app_scan = (
            any(m in text for m in _APP_SCAN_MODAL_MARKERS)
            # an .access-modal / .reds-modal-open with no readable text
            # is almost always the app-scan gate (see _REAL_MODAL_SELECTORS)
            or (not text and selector in (
                ".access-modal", ".access-modal-content", ".reds-modal-open",
            ))
        )
        info["kind"] = "app_scan" if is_app_scan else "other"
        return info

    async def dismiss_modal(self) -> str | None:
        """Truly close XHS's access-prompt modal so the SPA's UI state
        machine returns to a clean state.

        XHS pops a "当前笔记暂时无法浏览 / 请打开 App 扫码" modal
        (.access-modal, or a .reds-modal* wrapper). Left open it keeps
        the SPA stuck — the next card/link click is silently swallowed.
        We close it for real, in order of preference:

          1. press ESC — lets XHS run its own close handler
          2. click the modal's close-icon / 「关闭」 button

        This is the deliberate counterpart to _neutralize_overlays:
        that one only makes the passive .note-detail-mask stacking layer
        click-through; this one dismisses a real modal. Two jobs.

        Returns the kind of modal that was found (a close was always
        attempted):
          None        — no modal present
          "app_scan"  — the per-note "暂时无法浏览 / 扫码" gate. A
                        per-POST soft-skip condition; callers must NOT
                        treat it as account-level 风控 / abort the sweep.
          "other"     — some other access/reds modal.
        """
        assert self._page is not None
        info = await self._detect_modal()
        if not info.get("present"):
            return None
        kind = info.get("kind") or "other"

        logger.info(
            "dismiss_modal: %s modal detected (%s) %r — closing",
            kind, info.get("selector"), (info.get("text") or "")[:80],
        )

        # 1) ESC first — cleanest, triggers XHS's own close handler.
        try:
            await self._page.keyboard.press("Escape")
            await asyncio.sleep(0.6)
        except Exception:
            pass
        if not (await self._detect_modal()).get("present"):
            logger.info("dismiss_modal: closed via ESC")
            return kind

        # 2) ESC didn't take — click the modal's close control. XHS
        #    renders it a few different ways across modal variants; try
        #    each and stop as soon as the modal is gone.
        close_selectors = [
            ".access-modal .close",
            ".access-modal .close-btn",
            ".access-modal .icon-close",
            ".access-modal-content .close",
            ".reds-modal .close",
            ".reds-modal .reds-modal-close",
            ".reds-modal .reds-icon-close",
            "[class*='access-modal'] [class*='close']",
            "[class*='reds-modal'] [class*='close']",
        ]
        for csel in close_selectors:
            try:
                loc = self._page.locator(csel).first
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=3000)
                await asyncio.sleep(0.6)
            except Exception:
                continue
            if not (await self._detect_modal()).get("present"):
                logger.info("dismiss_modal: closed via %s", csel)
                return kind

        # 3) Last resort — a control literally labelled 关闭.
        try:
            btn = self._page.get_by_text("关闭", exact=True).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                await asyncio.sleep(0.6)
        except Exception:
            pass

        if (await self._detect_modal()).get("present"):
            logger.warning(
                "dismiss_modal: %s modal still present after ESC + close "
                "attempts", kind,
            )
        else:
            logger.info("dismiss_modal: closed via 关闭 button")
        return kind

    async def click_post_card(self, note_id: str) -> bool:
        """Click a search-result card whose hidden routerLink points to
        `/explore/<note_id>`. Assumes the search-result page is currently
        loaded.

        DOM shape:
          <section.note-item>
            <a class="cover" href="/search_result/<id>?xsec_token=...">…</a>
            <div class="footer">
              <a class="title">…</a>
              <a href="/explore/<id>"></a>   ← hidden routerLink (not clickable)
            </div>
          </section>

        Visible clickables are `a.cover` and `a.title`; both trigger
        the SPA route to `/explore/<id>`. The `a[href*="/explore/..."]`
        anchor is hidden — clicking it directly times out on
        "element is not visible".
        """
        assert self._page is not None
        # Before anything: clear an access-modal left over from the
        # previous card. A stuck modal makes the SPA swallow card clicks.
        await self.dismiss_modal()

        card_sel = (
            f'section.note-item:has(a[href*="/explore/{note_id}"])'
        )
        ok = False
        try:
            card = self._page.locator(card_sel).first
            if await card.count() == 0:
                return False
            cover = card.locator("a.cover").first
            title = card.locator("a.title").first
            # Prefer cover; fall back to title if cover isn't there.
            target = cover if await cover.count() > 0 else title
            if await target.count() == 0:
                return False
            async with self._lock:
                # Bring it into view, then click. Don't pass force=True —
                # we need the real click handler to fire so XHS's SPA
                # routes us to /explore/<id> with xsec_token in the URL.
                # Strip target="_blank" first: XHS renders cover/title
                # anchors with target="_blank" so a plain click opens a
                # NEW tab and our wait_for_url on the original page
                # times out. Forcing same-tab navigation keeps the
                # session linear.
                await target.scroll_into_view_if_needed(timeout=5000)
                try:
                    await target.evaluate("el => el.removeAttribute('target')")
                except Exception:
                    pass
                overlays = await self._neutralize_overlays()
                if overlays:
                    logger.info("overlays neutralized before post click: %s", overlays)
                await target.click(timeout=10000)
                try:
                    await self._page.wait_for_url(
                        f"**/explore/{note_id}**", timeout=10000,
                    )
                    await asyncio.sleep(random.uniform(3.0, 5.0))
                    ok = True
                except Exception:
                    ok = False
        except Exception:
            logger.exception("click_post_card failed for %s", note_id)
            ok = False

        if not ok:
            # A click that didn't route usually means a fresh
            # access-modal popped up and ate it — close it now so it
            # doesn't carry over and break the next card too.
            await self.dismiss_modal()
        return ok

    async def open_note(self, note_id: str, xsec_token: str = "") -> bool:
        """Navigate (goto) to a note's /explore page WITH its xsec_token
        so the note body + comment list actually render.

        Used to re-enter a note between commenter scrapes: go_back from a
        profile lands on a bare /explore/<id> URL (XHS drops the token
        from the address bar), and that bare page renders no note overlay
        and no comments. Re-navigating with the token we still hold from
        the search card restores everything. The URL shape matches a
        click from search results (xsec_source=pc_search).
        """
        assert self._page is not None
        url = f"{WWW_HOST}/explore/{note_id}"
        if xsec_token:
            url += f"?xsec_token={xsec_token}&xsec_source=pc_search"
        try:
            # Smart-wait on the note shell instead of a blind 3-5s
            # settle — .note-scroller/.note-container appear within
            # ~1-2s on a loaded note; the comment scrape and the
            # commenter-link search downstream do their own waiting for
            # the comment list.
            await self._navigate(
                url, wait_selector=".note-scroller, .note-container",
            )
        except Exception:
            logger.exception("open_note failed for %s", note_id)
            return False
        return f"/explore/{note_id}" in (self._page.url or "")

    async def scrape_note_body(self) -> dict[str, str]:
        """Scrape the note's own title + body text from the current
        /explore page — the post author's full write-up, which for a
        dating post carries more than any single comment (height /
        education / requirements live in the body). Distinct from
        scrape_comments_on_current_page, which reads the comment list.
        """
        assert self._page is not None
        try:
            data = await self._page.evaluate(r"""
                () => {
                    const txt = el => el ? (el.innerText || '').trim() : '';
                    // #detail-title / #detail-desc are unique ids on the
                    // XHS note page — unambiguous when present.
                    let title = txt(document.querySelector('#detail-title'));
                    let body = txt(document.querySelector('#detail-desc'));
                    if (!body) {
                        // fallback: scope to the note container so we
                        // don't pick up comment text by mistake.
                        const root = document.querySelector('.note-container')
                                  || document.querySelector('.note-content');
                        if (root) {
                            body = txt(root.querySelector('.desc'))
                                || txt(root.querySelector('.note-text'));
                            if (!title) {
                                title = txt(root.querySelector('.title'));
                            }
                        }
                    }
                    return {title: title, body: body};
                }
            """)
        except Exception:
            logger.exception("scrape_note_body failed")
            return {"title": "", "body": ""}
        if not isinstance(data, dict):
            return {"title": "", "body": ""}
        title = data.get("title") or ""
        body = data.get("body") or ""
        logger.info("note body scraped: title=%dch body=%dch | %r",
                    len(title), len(body), (body or title)[:50])
        return {"title": title, "body": body}

    async def scrape_comments_on_current_page(self) -> list[dict[str, Any]]:
        """Scrape comments from the currently-loaded /explore/<id> page
        (no navigation). Mirrors get_note_comments' DOM scrape.
        """
        assert self._page is not None
        try:
            await self._page.evaluate(
                "() => { const el = document.querySelector('.comments-el')"
                " || document.querySelector('.comment-container');"
                " if (el) el.scrollIntoView(); }"
            )
            await asyncio.sleep(2)
        except Exception:
            pass
        try:
            comments = await self._page.evaluate(_SCRAPE_COMMENTS_JS)
        except Exception:
            logger.exception("scrape_comments_on_current_page failed")
            return []
        if not isinstance(comments, list):
            return []
        out = []
        for c in comments:
            try:
                like = int(re.sub(r"\D", "",
                                  c.get("like_count_text", "0") or "0") or 0)
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
        return out

    async def _diag_click_blocked(self, user_id: str) -> dict:
        """Diagnostic for a profile-link click that timed out on pointer-
        event interception. Dumps the target link's ancestor chain (with
        each ancestor's computed pointer-events / z-index), the element
        actually topmost at the click point, and that element's chain —
        enough to tell whether our own overlay handling killed the link
        or a sibling layer is genuinely on top."""
        assert self._page is not None
        try:
            return await self._page.evaluate(r"""
                (uid) => {
                    const a = document.querySelector(
                        'a[href*="/user/profile/' + uid + '"]'
                    );
                    if (!a) return {found: false};
                    const r = a.getBoundingClientRect();
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    const chainOf = (el, n) => {
                        const out = [];
                        for (let i = 0; i < n && el; i++) {
                            const cs = getComputedStyle(el);
                            const cls = String(el.className || '').trim()
                                .replace(/\s+/g, '.').slice(0, 50);
                            out.push(el.tagName + (cls ? '.' + cls : '') +
                                ' [pe=' + cs.pointerEvents +
                                ' z=' + cs.zIndex + ']');
                            el = el.parentElement;
                        }
                        return out;
                    };
                    const top = document.elementFromPoint(cx, cy);
                    return {
                        found: true,
                        url: location.href,
                        target_rect: {
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width), h: Math.round(r.height),
                        },
                        target_chain: chainOf(a, 12),
                        topmost_at_point: top ? chainOf(top, 8) : null,
                        target_contains_topmost: top ? a.contains(top) : false,
                    };
                }
            """, user_id)
        except Exception:
            return {"found": False, "err": "eval failed"}

    async def click_user_link_to_profile(self, user_id: str) -> dict[str, Any]:
        """Click any visible `a[href*="/user/profile/<uid>"]` on the
        current page (post header or comment item). Wait for navigation
        to /user/profile/<uid>, then scrape profile fields. Caller is
        responsible for go_back afterwards.

        Returns same shape as get_user_profile_via_click:
          - success: {"id": uid, ...full profile fields}
          - {"id": uid, "rate_limited": True, "rate_limit_reason": ...}
          - {"id": uid, "navigation_failed": True}
        """
        assert self._page is not None
        await self._profile_sleep()
        # Clear any leftover access-modal on the explore page before we
        # hunt for the user link — same rationale as click_post_card.
        await self.dismiss_modal()
        # `:visible` filters out XHS's hidden routerLink anchors (Vue
        # generates `<a href="/user/profile/..."></a>` placeholders that
        # are display:none — clicking them times out).
        sel = f'a[href*="/user/profile/{user_id}"]:visible'
        try:
            if await self._page.locator(sel).count() == 0:
                # The link isn't rendered. Common right after we've gone
                # back to the explore page from a previous profile — XHS
                # resets the note's comment list. Re-surface it: pull
                # .comments-el into view and scroll the note's OWN
                # scroller (.note-scroller), since a plain window.scrollBy
                # doesn't move the note overlay's internal scroll area.
                for _ in range(6):
                    try:
                        await self._page.evaluate("""
                            () => {
                                const cs = document.querySelector('.comments-el')
                                        || document.querySelector('.comment-container');
                                if (cs) cs.scrollIntoView({block: 'center'});
                                const sc = document.querySelector('.note-scroller')
                                        || document.querySelector('.comments-container');
                                if (sc) sc.scrollBy(0, 700);
                                else window.scrollBy(0, 700);
                            }
                        """)
                        await asyncio.sleep(1.2)
                    except Exception:
                        break
                    if await self._page.locator(sel).count() > 0:
                        break
                if await self._page.locator(sel).count() == 0:
                    try:
                        diag = await self._page.evaluate(r"""
                            () => ({
                                url: location.href,
                                comments_el: !!document.querySelector('.comments-el'),
                                comment_items:
                                    document.querySelectorAll('.comment-item').length,
                                profile_links:
                                    document.querySelectorAll(
                                        'a[href*="/user/profile/"]').length,
                                note_mask:
                                    !!document.querySelector('.note-detail-mask'),
                            })
                        """)
                    except Exception:
                        diag = "<eval err>"
                    logger.info(
                        "click_user: %s not visible — diag=%s", user_id, diag,
                    )
                    return {"id": user_id, "navigation_failed": True}
            async with self._lock:
                target = self._page.locator(sel).first
                await target.scroll_into_view_if_needed(timeout=5000)
                # Do NOT _neutralize_overlays() here. On an explore page
                # the user link lives INSIDE .note-detail-mask — the note
                # overlay, z-index 20, already the topmost layer. Setting
                # that overlay to pointer-events:none makes the value
                # INHERIT down to the link itself, so the click falls
                # through the whole note to the search page underneath
                # and times out. Diagnosed 2026-05-17 via
                # _diag_click_blocked: every ancestor of a commenter link
                # showed pe=none right after _neutralize_overlays ran.
                # The overlay is on top; the link inside it is clickable
                # as-is. (click_post_card still neutralizes — there the
                # mask is a stale leftover sitting over the search card.)
                # Capture what we're about to click for diagnostics.
                # ALSO strip target="_blank" — XHS renders author links
                # with target="_blank" so a normal click opens the
                # profile in a NEW tab. Our wait_for_url watches the
                # current page so we'd think nav failed. Forcing the
                # link to navigate in-tab keeps a single-page session.
                try:
                    clicked_html = await target.evaluate("""
                        el => {
                            const html = el.outerHTML.slice(0, 300);
                            el.removeAttribute('target');
                            return html;
                        }
                    """)
                except Exception:
                    clicked_html = "<eval err>"
                try:
                    await target.click(timeout=10000)
                except Exception:
                    diag = await self._diag_click_blocked(user_id)
                    logger.warning(
                        "click_user: click on %s timed out — pointer-event "
                        "interception. clicked_html=%r ; diag=%s",
                        user_id, clicked_html, diag,
                    )
                    return {"id": user_id, "navigation_failed": True}
                try:
                    await self._page.wait_for_url(
                        f"**/user/profile/{user_id}**", timeout=10000,
                    )
                except Exception:
                    # Click "succeeded" (no Playwright error) but no
                    # navigation. Either the element wasn't actually the
                    # user link, or XHS's handler intercepted/cancelled.
                    # Dump everything we know to diagnose.
                    try:
                        all_links = await self._page.evaluate("""
                            (uid) => {
                                const out = [];
                                document.querySelectorAll(
                                    'a[href*="/user/profile/"]'
                                ).forEach(a => {
                                    const r = a.getBoundingClientRect();
                                    out.push({
                                        href: a.getAttribute('href'),
                                        text: (a.textContent || '').trim().slice(0, 40),
                                        cls: a.className || '',
                                        w: r.width, h: r.height,
                                        match_target: a.getAttribute('href') &&
                                                      a.getAttribute('href').includes(uid),
                                    });
                                });
                                return out;
                            }
                        """, user_id)
                    except Exception:
                        all_links = []
                    logger.warning(
                        "click_user: clicked %s but URL didn't change "
                        "(current=%s). clicked_html=%r ; all_profile_links=%s",
                        user_id, self._page.url, clicked_html, all_links,
                    )
                    return {"id": user_id, "navigation_failed": True}
                # Smart-wait for the profile to render instead of a
                # flat 4s settle — the fields appear within ~1-2s.
                try:
                    await self._page.wait_for_selector(
                        ".user-name, .user-nickname, .user-info",
                        timeout=8000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.8, 1.6))
        except Exception:
            logger.exception("click_user_link_to_profile failed for %s", user_id)
            return {"id": user_id, "navigation_failed": True}

        gone, why = await self.detect_post_unavailable()
        if gone:
            return {"id": user_id, "navigation_failed": True}
        hit, reason = await self.detect_rate_limit()
        if hit:
            return {"id": user_id, "rate_limited": True,
                    "rate_limit_reason": reason}
        return await self._scrape_profile_fields_from_page(user_id)

    async def go_back(self, expect_url_part: str | None = None,
                      settle_sec: float = 2.0) -> bool:
        """Browser back. Returns True if URL contains expect_url_part
        (or no expectation given). Used to retreat from profile→explore
        and explore→search in the sweep flow.
        """
        assert self._page is not None
        try:
            async with self._lock:
                await self._page.go_back()
                await asyncio.sleep(settle_sec)
        except Exception:
            logger.exception("go_back failed")
            return False
        if expect_url_part:
            return expect_url_part in (self._page.url or "")
        return True

    # ── End sweep helpers ───────────────────────────────────────────────

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
