"""Step-3 smoke: scrape profile pages for the dating-intent commenters
saved by the recent step-2 run, then re-evaluate UserFilter against the
authoritative profile IP.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from findit.ai.filter_rules import UserFilter
from findit.config import settings
from findit.crawler.client import XHSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("smoke_step3")


def _comment_authors_since(db_path: str, since_iso: str) -> list[str]:
    # Fresh comment authors that haven't been profile-scraped yet.
    # We check both signals: profile_crawled_at (set by new smoke runs)
    # AND a non-empty ip_location (set by all prior successful scrapes
    # whether or not they touched profile_crawled_at).
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT DISTINCT a.id
               FROM posts p JOIN authors a ON a.id = p.author_id
               WHERE p.crawled_at >= ?
                 AND p.source_type = 'comment'
                 AND a.profile_crawled_at IS NULL
                 AND (a.ip_location IS NULL OR a.ip_location = '')""",
            (since_iso,),
        ).fetchall()
        return [r[0] for r in rows]


async def main():
    SINCE = "2026-05-05T18:30"
    db_path = str(settings.db_path)
    ids = _comment_authors_since(db_path, SINCE)
    log.info("step3 candidates: %d (no/partial profile IP)", len(ids))

    client = XHSClient()
    await client.setup()
    profiles: dict[str, dict] = {}
    consecutive_hits = 0
    consecutive_ok = 0
    try:
        for i, uid in enumerate(ids):
            log.info("(%d/%d) profile %s", i + 1, len(ids), uid)
            try:
                p = await client.get_user_profile(uid)
            except Exception:
                log.exception("profile failed: %s", uid)
                continue

            if p.get("rate_limited"):
                consecutive_hits += 1
                consecutive_ok = 0
                cool = 15 + (consecutive_hits - 1) * 5
                log.warning(
                    "⚠️  风控 #%d on %s (%s) — cooling down %ds",
                    consecutive_hits, uid,
                    p.get("rate_limit_reason", "?"), cool,
                )
                await asyncio.sleep(cool)
                if consecutive_hits >= 5:
                    log.error("aborting smoke after 5 consecutive 风控 hits")
                    break
                continue

            consecutive_ok += 1
            if consecutive_ok >= 3:
                consecutive_hits = 0
            profiles[uid] = p
            # Persist authoritative IP and mark profile_crawled_at so a
            # rerun skips this author.
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """UPDATE authors
                       SET ip_location = ?, bio = ?,
                           profile_crawled_at = datetime('now'),
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (p.get("ip_location") or "", p.get("bio") or "", uid),
                )
    finally:
        await client.close()

    log.info("──────────  STEP 3 RESULTS  ──────────")
    log.info("scraped %d profiles", len(profiles))
    ip_dist: dict[str, int] = {}
    for p in profiles.values():
        ip = p.get("ip_location") or "<none>"
        ip_dist[ip] = ip_dist.get(ip, 0) + 1
    for ip, n in sorted(ip_dist.items(), key=lambda x: -x[1]):
        log.info("  %4d  %s", n, ip)

    # Re-evaluate UserFilter with the new IPs
    log.info("\n──────────  RE-FILTER WITH PROFILE IP  ──────────")
    f = UserFilter(user_city="深圳", allow_remote=False)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT a.id, a.nickname, a.ip_location, a.bio, p.content
               FROM posts p JOIN authors a ON a.id = p.author_id
               WHERE p.crawled_at >= ? AND p.source_type = 'comment'""",
            (SINCE,),
        ).fetchall()

    kept = drop = 0
    for r in rows:
        author = {
            "nickname": r["nickname"], "bio": r["bio"],
            "ip_location": r["ip_location"],
        }
        posts = [{"content": r["content"] or ""}]
        keep, _reason = f.evaluate(author, posts=posts)
        if keep:
            kept += 1
        else:
            drop += 1
    log.info("kept: %d   drop: %d   (over %d comment authors)",
             kept, drop, len(rows))


if __name__ == "__main__":
    asyncio.run(main())
