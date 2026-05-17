"""Offline data-cleaning pass — classify every author per DATA_SPEC §2/§3.

Reads each author + their posts/comments from the DB, runs SharedFilter
(Rule A 红娘 / Rule B 代发 / §2 入库门槛), and stamps `crawl_state`:

    pending_profile  — passed the early 中介 filter, profile not crawled yet
    kept             — profile crawled AND passes §2 入库门槛 + §3
    filtered_out     — hit a 中介 rule, OR profile crawled but fails §2

`final` (the §2 minimum-data bar) is enabled only for authors whose
profile has actually been crawled (`profile_crawled_at` set) — matching
DATA_SPEC §1/§2: the §2 bar is judged after Step 3.

Pure CPU over already-stored text — no crawling, no network. Idempotent:
re-running re-evaluates from current data and overwrites the verdict, so
it's safe to run after every crawl.

  python scripts/backfill_filter.py            # classify + write
  python scripts/backfill_filter.py --dry-run  # show the breakdown only
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter

from findit.ai.filter_rules import SharedFilter
from findit.db import Database

logger = logging.getLogger(__name__)


def classify_all(db: Database, dry_run: bool = False) -> Counter:
    """Re-classify every author from current DB data. Returns a Counter
    of the resulting verdicts."""
    flt = SharedFilter()
    stats: Counter = Counter()

    with db._conn() as c:
        authors = [
            dict(r) for r in c.execute("SELECT * FROM authors").fetchall()
        ]

    for a in authors:
        posts = db.get_author_posts(a["id"])
        crawled = bool(a.get("profile_crawled_at"))
        keep, reason = flt.evaluate(a, posts=posts, final=crawled)

        if not keep:
            state = "filtered_out"
        elif crawled:
            state, reason = "kept", None
        else:
            # passed the 中介 filter but still needs Step 3
            state, reason = "pending_profile", None

        if state == "filtered_out":
            stats[f"filtered_out · {reason}"] += 1
        else:
            stats[state] += 1

        if not dry_run:
            db.set_classification(a["id"], state, reason)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/findit.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the breakdown without writing.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = Database(args.db)

    logger.info("== Offline classification (DATA_SPEC §2/§3) ==%s",
                "  [DRY RUN — no writes]" if args.dry_run else "")
    stats = classify_all(db, dry_run=args.dry_run)

    total = sum(stats.values())
    logger.info("Classified %d authors:", total)
    for label, n in stats.most_common():
        logger.info("  %-34s %5d  (%d%%)", label, n,
                    (n * 100 // total) if total else 0)


if __name__ == "__main__":
    main()
