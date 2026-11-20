"""Substack 公开 archive 的新旧分页协议兼容。

2026-11-13 实测新接口：无 offset 的 GET 返回 {posts, nextCursor}；
下一页只传 sort=new&cursor=...，每页 12 条。旧接口返回数组，offset
按实际条数递增。两种协议均按 id 去重；重复页/游标不前进立即失败或停止，
绝不把分页卡死或空清单当作“没有新文章”。
"""
from __future__ import annotations

import time
import os
from datetime import date
from aris.daily_policy import publication_day
from urllib.parse import urlencode


class ArchiveError(RuntimeError):
    pass


def archive(fetch_json, host: str, *, limit: int = 50, max_pages: int = 500,
            delay: float = 0.3, log=print) -> list[dict]:
    if not 1 <= max_pages:
        raise ValueError("max_pages 必须 >= 1")
    out: dict[int, dict] = {}
    offset = 0
    cursor: str | None = None
    mode: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        if mode == "cursor":
            if not cursor:
                break
            query = urlencode({"sort": "new", "cursor": cursor})
        elif mode == "offset":
            query = urlencode({"sort": "new", "offset": offset, "limit": limit})
        else:
            # 第一页不传 offset/limit：新版会对这两个旧参数返回 400；
            # 旧版缺省从 0 开始，后续才按实际条数追加 offset。
            query = urlencode({"sort": "new"})
        url = f"{host.rstrip('/')}/api/v1/archive?{query}"
        page = fetch_json(url)
        if isinstance(page, dict):
            if mode == "offset":
                raise ArchiveError("Substack archive 在翻页中途切换分页协议")
            mode = "cursor"
            posts = page.get("posts")
            if not isinstance(posts, list):
                raise ArchiveError("Substack archive 对象缺少数组字段 posts")
            next_cursor = page.get("nextCursor")
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise ArchiveError("Substack nextCursor 不是字符串或 null")
        elif isinstance(page, list):
            if mode == "cursor":
                raise ArchiveError("Substack archive 在翻页中途退回数组协议")
            mode = "offset"
            posts = page
            next_cursor = None
        else:
            raise ArchiveError(f"Substack archive 未知响应类型：{type(page).__name__}")
        if not posts:
            break
        before = len(out)
        for post in posts:
            if not isinstance(post, dict) or not post.get("id"):
                raise ArchiveError("Substack archive 条目缺少数值/非空 id")
            out[post["id"]] = post
        if len(out) == before:
            if mode == "cursor":
                raise ArchiveError("Substack 游标页全部重复，拒绝写入不完整清单")
            log(f"    · 本页 {len(posts)} 条全部已见（旧接口重复返回），停止翻页")
            break
        target=os.getenv('CORPUS_TARGET_DATE')
        days=[publication_day(p.get('post_date')) for p in posts]
        if target and all(days) and max(days)<date.fromisoformat(target):
            break  # sort=new: the following pages are outside yesterday's window.
        if mode == "cursor":
            if next_cursor:
                if next_cursor == cursor or next_cursor in seen_cursors:
                    raise ArchiveError("Substack nextCursor 未推进，拒绝静默截断清单")
                seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            offset += len(posts)
        if delay:
            time.sleep(delay)
    else:
        # 只有空页或 nextCursor=null 才代表完整到达末尾。
        raise ArchiveError(f"Substack 翻页超过 max_pages={max_pages}，清单可能不完整")
    return sorted(out.values(), key=lambda p: p.get("post_date") or "")
