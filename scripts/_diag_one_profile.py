"""Single-user profile fetch — verify the new notes_summary path."""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from findit.crawler.client import XHSClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("diag1")

# 爱吃白菜 — diag earlier showed this one renders fine.
TARGET = sys.argv[1] if len(sys.argv) > 1 else "5dd0176f00000000010029f9"
NICK = sys.argv[2] if len(sys.argv) > 2 else "爱吃白菜"


async def main():
    c = XHSClient()
    await c.setup()
    try:
        p = await c.get_user_profile(TARGET, nickname=NICK)
        log.info("nickname=%r", p.get("nickname"))
        log.info("ip_location=%r", p.get("ip_location"))
        log.info("age_tag=%r", p.get("age_tag"))
        log.info("bio=%r", (p.get("bio") or "")[:80])
        notes = p.get("notes_summary") or []
        log.info("notes (%d):", len(notes))
        for n in notes[:10]:
            log.info("  %d 赞  %s", n.get("like_count"),
                     (n.get("title") or "")[:60])
        log.info("full json: %s",
                 json.dumps(p, ensure_ascii=False, indent=2)[:1500])
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
