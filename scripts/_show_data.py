"""Throwaway: show the full per-author data picture for eyeballing —
who they are + the source post/comment text we found them in + every
note title on their homepage (notes_summary)."""

import json
import sqlite3

conn = sqlite3.connect("data/findit.db")
conn.row_factory = sqlite3.Row

authors = conn.execute("""
    SELECT id, nickname, ip_location, followers, age_tag, bio, notes_summary
    FROM authors
    WHERE profile_crawled_at IS NOT NULL
      AND notes_summary IS NOT NULL AND notes_summary != '[]'
    ORDER BY profile_crawled_at DESC
    LIMIT 15
""").fetchall()

for i, a in enumerate(authors, 1):
    rows = conn.execute(
        "SELECT source_type, content FROM posts WHERE author_id=? "
        "ORDER BY crawled_at LIMIT 4",
        (a["id"],),
    ).fetchall()
    print(f"\n[{i}] {a['nickname']}  |  IP {a['ip_location'] or '—'}  |  "
          f"粉丝 {a['followers']}  |  {a['age_tag'] or '年龄—'}")
    if (a["bio"] or "").strip():
        print(f"    bio: {a['bio'][:55]}")
    for r in rows:
        tag = "帖子" if r["source_type"] == "post" else "评论"
        print(f"    来源[{tag}]: {(r['content'] or '(空)')[:75]}")
    if not rows:
        print("    来源: (DB 里没有该作者的帖子/评论)")
    try:
        notes = json.loads(a["notes_summary"])
    except Exception:
        notes = []
    titles = [(n.get("title") or "").strip() or "(无标题)" for n in notes]
    print(f"    主页笔记 {len(titles)} 条:")
    for t in titles[:15]:
        print(f"      · {t[:40]}")
    if len(titles) > 15:
        print(f"      … 还有 {len(titles) - 15} 条")

conn.close()
