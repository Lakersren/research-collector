#!/usr/bin/env python3
"""Article collection and text extraction. Public entrypoint: python run.py collect."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lib"))

# ⚠️ 必须在 sys.path.insert 之后 —— 从别的 cwd 跑会 ModuleNotFoundError
# （TOOLS.md 08-24 那条 import 位置陷阱，实测踩过）
from aris.net_route import RouteConfigError, overseas_urlopen  # noqa: E402
from aris.daily_policy import candidate, matches, publication_day
from aris.article_audit import date_rejection, page_publication_date, record
from aris.paths import SOURCE_STATE as STATE  # noqa: E402
from aris import web_media as wm  # noqa: E402

SOURCE_KEY = "web:epoch_site"
NAME = "Epoch AI (官网)"
HOST = "https://epoch.ai"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
RETRY_BASE = 1.5

# 列表页。`/publications` 一个就含全站 265 条；其余三个是冗余入口 ——
# 🔴 故意保留冗余：万一官网将来拆开分区，这里不会一下子变空。
INDEX_PAGES = ["/publications", "/gradient-updates", "/data-insights", "/blog"]

# 只收这三类路径。🔴 `/data/` 与 `/models/` 故意排除：
# 那是结构化数据集 / 模型词条，性质不同，不该塞 `doc` 表。
ARTICLE_PREFIXES = ("/publications/", "/data-insights/", "/gradient-updates/")
SECTION_NAMES = tuple(p.strip("/") for p in ARTICLE_PREFIXES)

# sitemap 仅作交叉校验。数字是 2026-08-25 实测基线。
CROSSCHECK_SITEMAPS = {
    "publications": ("sitemap-publications-0.xml", 74),
    "data-insights": ("sitemap-data-insights-0.xml", 88),
    "pages": ("sitemap-pages-0.xml", 230),
}


def log(message):
    import logging
    logger = logging.getLogger('corpus.collector')
    logger.log(logging.ERROR if '✗' in message or '🔴' in message else logging.INFO, message)


# ---------------------------------------------------------------- 状态
def single_instance():
    lock = STATE / "web_epoch_site.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("  上一轮仍在运行，跳过")
        return None
    return fh


def state_path() -> Path:
    return STATE / "web_epoch_site.json"


def load_state() -> dict:
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as e:
            log(f"  ⚠️ state 解析失败（当作空）：{e}")
    return {}


def save_state(st: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(p)


# ---------------------------------------------------------------- 抓取
class FetchError(RuntimeError):
    pass


def fetch(url: str, *, timeout: float = 60, retries: int = 4) -> bytes:
    last = None
    for i in range(retries):
        req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with overseas_urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 429:
                ra = (e.headers or {}).get("Retry-After")
                wait = float(ra) if (ra or "").isdigit() else RETRY_BASE * (2 ** i)
                log(f"    · 429 限流，等 {wait:.1f}s")
                time.sleep(min(wait, 60))
                continue
            if 400 <= e.code < 500:
                raise FetchError(f"{last} {url}") from e
        except RouteConfigError:
            raise
        except (URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
        if i < retries - 1:
            time.sleep(RETRY_BASE * (2 ** i))
    raise FetchError(f"{last} {url}")


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", "replace")


# ---------------------------------------------------------------- 日期
def parse_iso_date(s: str | None) -> str:
    """'2025-01-24T00:00:00.000Z' / '2025-01-24' → UTC ISO。空则空串。"""
    txt = (s or "").strip()
    if not txt:
        return ""
    try:
        d = datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except Exception:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", txt)
        if not m:
            return ""
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def item_id(url: str) -> str:
    """条目稳定 id：URL 的 sha1 前 16 位。

    🔴 用 URL 而非标题：URL 是站点的 canonical 地址，标题会被改写。
    """
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 清单
def parse_locs(xml: str) -> list[str]:
    """sitemap → URL 列表。只认 `<loc>`，不引入 XML 解析器。"""
    return [m.group(1).strip() for m in re.finditer(r"<loc>(.*?)</loc>", xml, re.S)]


# 🔴 匹配 Astro 序列化数据（HTML 实体转义形态）。
#    title 里可能夹别的实体（&#39; 等），故用「非 &quot;] 结尾」的宽松匹配。
_ITEM_RE = re.compile(
    r'\{&quot;url&quot;:\[0,&quot;(?P<url>[^&]+)&quot;\],'
    r'&quot;title&quot;:\[0,&quot;(?P<title>(?:[^&]|&(?!quot;\]))*)&quot;\],'
    r'&quot;date&quot;:\[\d+,&quot;(?P<date>[^&]+)&quot;\]'
)
_AUTHOR_RE = re.compile(r'&quot;name&quot;:\[0,&quot;((?:[^&]|&(?!quot;\]))*)&quot;\]')


def parse_index_page(html_text: str) -> list[dict]:
    """列表页 HTML → [{url, title, date, authors, section}]。

    零条目由调用方判为失败（见 build_index）—— 改版/WAF 都长这样，
    静默当 0 会让这个源无声无息地死掉几个月。
    """
    out: list[dict] = []
    for m in _ITEM_RE.finditer(html_text):
        u = html_mod.unescape(m.group("url"))
        if not u.startswith(ARTICLE_PREFIXES):
            continue
        # 作者：取该条目后紧跟的 authors 块。
        # ⚠️ 必须在下一条 url 处截断，否则会把下一篇的作者串过来。
        tail = html_text[m.end():m.end() + 600]
        cut = tail.find("&quot;url&quot;")
        if cut > 0:
            tail = tail[:cut]
        out.append({
            "url": HOST + u if u.startswith("/") else u,
            "title": html_mod.unescape(m.group("title")),
            "date": parse_iso_date(m.group("date")),
            "authors": [html_mod.unescape(a) for a in _AUTHOR_RE.findall(tail)],
            "section": u.strip("/").split("/")[0],
        })
    return out


def build_index(sections: list[str] | None = None) -> list[dict]:
    """汇总多个列表页 → 按 url 去重的权威清单。"""
    by_url: dict[str, dict] = {}
    for path in INDEX_PAGES:
        try:
            items = parse_index_page(fetch_text(HOST + path))
        except FetchError as e:
            log(f"  ⚠️ 列表页 {path} 拓失败（继续其他页）：{e}")
            continue
        new = sum(1 for it in items if it["url"] not in by_url)
        log(f"  列表页 {path:20} {len(items):4} 条（新增 {new}）")
        for it in items:
            by_url.setdefault(it["url"], it)
    if not by_url:
        raise FetchError("所有列表页都解析到 0 条（改版？WAF？）")
    rows = list(by_url.values())
    if sections:
        rows = [r for r in rows if r["section"] in set(sections)]
    for it in rows:
        it["iid"] = item_id(it["url"])
    n_nodate = sum(1 for r in rows if not r["date"])
    if n_nodate:
        log(f"  ⚠️ {n_nodate} 条清单项无日期（结构化 JSON 缺 date 字段？）")
    return sorted(rows, key=lambda x: (x["date"] or "", x["url"]))


def crosscheck_sitemap(index_urls: set[str]) -> dict:
    """用 sitemap 反验清单完整性 —— **双向**比对。"""
    sm_urls: set[str] = set()
    for name, (fn, exp) in CROSSCHECK_SITEMAPS.items():
        try:
            locs = parse_locs(fetch_text(f"{HOST}/{fn}"))
        except FetchError as e:
            log(f"  ⚠️ sitemap {fn} 拓失败，跳过校验：{e}")
            continue
        if not locs:
            log(f"  🔴 sitemap {fn} 解析到 0 条（改版？）")
            continue
        if abs(len(locs) - exp) > max(5, exp * 0.2):
            log(f"  ⚠️ sitemap {name} {len(locs)} 条，与实测基线 {exp} 偏差大")
        sm_urls |= {u for u in locs if any(p in u for p in ARTICLE_PREFIXES)}
    only_sm = sm_urls - index_urls
    only_ix = index_urls - sm_urls
    if only_sm:
        log(f"  ⚠️ sitemap 有而清单无：{len(only_sm)} 篇（列表页漏了？）"
            f" 例：{sorted(only_sm)[:2]}")
    if only_ix:
        log(f"  ℹ️  清单有而 sitemap 无：{len(only_ix)} 篇"
            f" 例：{sorted(only_ix)[:2]}")
    return {"n_sitemap": len(sm_urls), "only_sitemap": sorted(only_sm),
            "only_index": sorted(only_ix)}


# ---------------------------------------------------------------- 正文
def extract_article(html: str, url: str) -> dict:
    """trafilatura 提取正文（markdown，含图）+ 元数据。

    元数据只作备查 —— 日期走清单，见模块头注释。
    """
    import trafilatura
    md = trafilatura.extract(html, url=url, favor_recall=True,
                             include_comments=False, include_tables=True,
                             include_images=True, output_format="markdown")
    meta = trafilatura.extract_metadata(html, default_url=url)
    md = wm.absolutize_md_images(md or "", url)
    return {
        "md": md or "",
        "meta_title": (meta.title if meta else None),
        "meta_author": (meta.author if meta else None),
        "meta_date": (page_publication_date(html) if os.getenv("CORPUS_TARGET_DATE")
                      else (meta.date if meta else None)),
        "meta_sitename": (meta.sitename if meta else None),
    }


_PUNCT = re.compile(r"[^a-z0-9]+")


def norm_title(t: str) -> str:
    return _PUNCT.sub("", (t or "").lower())


def days_apart(a: str, b: str) -> float | None:
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
    except Exception:
        return None
    return abs((da - db).total_seconds()) / 86400.0


# ---------------------------------------------------------------- 采集
def collect(*, sections: list[str] | None, redo: bool, limit: int | None,
            full: bool, skip_crosscheck: bool = False) -> dict:
    store = wm.SiteStore(SOURCE_KEY)
    st = load_state()
    seen: dict = st.setdefault("seen", {})

    index = build_index(sections)
    log(f"  权威清单 {len(index)} 条")
    xc = {}
    if not skip_crosscheck and not sections:
        xc = crosscheck_sitemap({it["url"] for it in index})
    wm.write_jsonl(store.base / "index.jsonl", index)

    todo = []
    for it in index:
        if not candidate(it.get("date")):
            date_rejection(it.get("title"), it["url"], it.get("date"))
            continue
        f = store.base / "items" / f"{it['iid']}.json"
        if f.exists() and not redo:
            continue
        if not os.getenv("CORPUS_TARGET_DATE") and not full and not redo and it["iid"] in seen:
            continue
        todo.append(it)
    if limit:
        todo = todo[:limit]
    have = len(list((store.base / "items").glob("*.json")))
    log(f"  待抓 {len(todo)} 条（本地已有 {have} / 清单 {len(index)}）")

    ok = fail = 0
    meta_gap = 0
    for i, it in enumerate(todo, 1):
        rec = {
            "iid": it["iid"], "source_key": SOURCE_KEY,
            "section": it["section"], "url": it["url"],
            "_aris": {"fetched_at": datetime.now().astimezone().isoformat()},
        }
        try:
            page = fetch_text(it["url"])
            ext = extract_article(page, it["url"])
        except (FetchError, OSError, ValueError) as e:
            record("failed", it.get("title"), it["url"], it.get("date"), str(e))
            log(f"    ✗ {it['url'][-52:]}: {e}")
            fail += 1
            continue
        # 在采集边界统一展开相对图 URL / Next.js 图片代理，确保锚点和下载
        # 清单使用同一组可抓取的绝对 URL。
        publication_date = it.get("date")
        if publication_day(publication_date) is None:
            publication_date = parse_iso_date(str(ext.get("meta_date") or "")) or publication_date
        if not matches(publication_date):
            if date_rejection(it.get("title"), it["url"], publication_date):
                fail += 1
                log(f"    ✗ 无法确定发布日期，未保存: {it['url']}")
            continue
        rec["md"] = wm.absolutize_md_images(ext["md"], it["url"])
        rec["text_basis"] = "trafilatura"
        # 🔴 标题/日期/作者一律来自清单，不用 trafilatura 的
        rec["title"] = it["title"] or ext["meta_title"] or ""
        rec["date"] = publication_date
        rec["date_basis"] = "site_index" if it.get("date") else "page_meta"
        rec["author"] = ", ".join(it["authors"]) if it["authors"] else None
        # 保留结构化作者列表（列表页已给逐人拆分，别丢掉）
        rec["authors"] = it["authors"]
        rec["page_meta"] = {
            "title": ext["meta_title"], "author": ext["meta_author"],
            "date": ext["meta_date"], "sitename": ext["meta_sitename"],
        }
        # 观测 trafilatura 与清单的日期差 —— 不用于取值，只用于知道它有多不靠谱
        md_ = parse_iso_date(str(ext["meta_date"] or ""))
        if md_ and it["date"]:
            gap = days_apart(md_, it["date"])
            if gap is not None and gap > 3:
                rec["meta"] = {"trafilatura_date_gap": {
                    "index_date": it["date"], "trafilatura_date": md_,
                    "days_apart": round(gap, 1)}}
                meta_gap += 1
        try:
            wm.write_json(store.base / "items" / f"{it['iid']}.json", rec)
        except OSError as e:
            record("failed", rec["title"], rec["url"], publication_date, str(e))
            raise
        record("fetched", rec["title"], rec["url"], publication_date)
        seen[it["iid"]] = it["date"] or ""
        ok += 1
        if i % 20 == 0:
            log(f"    …{i}/{len(todo)}")
        time.sleep(0.4)

    if meta_gap:
        # 这不是故障，是**对抽取器可信度的度量**。喊出来是为了下次别再想用它。
        log(f"  ℹ️  {meta_gap}/{ok} 篇 trafilatura 抽的日期与清单差 > 3 天"
            f" —— 正是不用它的理由，已记进 meta 备查")

    # 🔴 state 字段名必须遵守境外源的现有契约（2026-08-25 踩过）：
    #    `last_run_at` / `last_item_date` / `n_index`。
    #    我最初自创了 `last_run` / `last_ok` ⇒ `tools/web_source_stale.py`
    #    读不到，巡检报「从未跑过」—— 而采集其实一切正常。
    #    ⚠️ 当时差点去改巡检器适配我 —— 那是反了：
    #    **新模块适配旧契约，不是让监控去追每个新模块的新叫法。**
    now_iso = datetime.now().astimezone().isoformat()
    st["last_run_at"] = now_iso
    st["n_index"] = len(index)
    # 清单里最新一篇的发布日（巡检用它算「内容滞后」）
    dates = sorted(x["date"] for x in index if x.get("date"))
    if dates:
        st["last_item_date"] = dates[-1]
    st["n_seen"] = len(seen)
    if xc:
        st["crosscheck"] = {k: (v if not isinstance(v, list) else len(v))
                            for k, v in xc.items()}
    if fail == 0:
        st["last_ok_at"] = now_iso
    hist = st.get("history") or []
    hist.append({"at": now_iso, "fetched": ok, "failed": fail,
                 "index": len(index)})
    st["history"] = hist[-200:]
    save_state(st)
    return {"n_index": len(index), "n_fetched": ok, "n_failed": fail,
            "n_meta_gap": meta_gap}


# ---------------------------------------------------------------- 正文抽取
def build_text() -> dict:
    """items/*.json → text/*.txt + text/index.jsonl。纯本地，可重跑。"""
    store = wm.SiteStore(SOURCE_KEY)
    out_dir = store.base / "text"
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = store.load_ledger()

    # 🔴 复用 web_rss_sites 的锚点与脏正文判据 —— 不重写一份
    #    （08-20 SCRIPT_ORDER：为解耦造的层不能抄实现）
    from collectors.web_rss_sites import (md_to_text_with_anchors,
                                          is_dirty_text)

    rows = []
    for f in sorted((store.base / "items").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        text, imgs = md_to_text_with_anchors(d.get("md") or "")
        images = store.resolve_images(imgs, ledger)
        state = "ready" if text else "no_text"
        if is_dirty_text(text):
            state = "dirty_html"
        (out_dir / f"{d['iid']}.txt").write_text(text, encoding="utf-8")
        pm = d.get("page_meta") or {}
        rows.append({
            "source_key": SOURCE_KEY, "site": store.site,
            "item_id": d["iid"], "post_id": d["iid"],
            "slug": d["url"].rstrip("/").rsplit("/", 1)[-1],
            "title": d.get("title") or pm.get("title"),
            "subtitle": None,
            "post_date": d.get("date"),
            "updated_at": None,
            "url": d.get("url"),
            "post_type": d.get("section") or "publications",
            "authors": (d.get("authors")
                        or [a for a in [d.get("author")] if a]),
            "audience": "everyone",
            "wordcount_upstream": None,
            "words_local": len(re.findall(r"[A-Za-z0-9\u2019'\-]+",
                                          wm.ANCHOR_RE.sub(" ", text))),
            "coverage": None,
            "is_preview": False,
            "content_state": state,
            "chars": len(text),
            "text_path": f"web/{store.site}/text/{d['iid']}.txt",
            "n_images": len(imgs),
            "n_images_stored": sum(1 for im in images if im.get("file_key")),
            "images": images,
            "date_basis": d.get("date_basis") or "site_index",
            "reactions": None, "comments": None,
            "raw_ref": f"web/{store.site}/items/{f.name}",
        })
    rows.sort(key=lambda r: (r.get("post_date") or "", str(r.get("item_id"))))
    wm.write_jsonl(out_dir / "index.jsonl", rows)
    n_none = sum(1 for r in rows if r["content_state"] == "no_text")
    n_dirty = sum(1 for r in rows if r["content_state"] == "dirty_html")
    n_nodate = sum(1 for r in rows if not r.get("post_date"))
    log(f"  抽正文 {len(rows)} 条（正文 {len(rows) - n_none - n_dirty} / "
        f"空 {n_none} / 脏 {n_dirty}）· {sum(r['chars'] for r in rows):,} 字符")
    if n_dirty:
        log(f"  🔴 {n_dirty} 条正文残留 HTML 标签 —— 已标 dirty_html，不得入库当正文")
    if n_nodate:
        log(f"  ⚠️ {n_nodate} 条无日期")
    return {"n": len(rows), "n_no_text": n_none, "n_dirty": n_dirty,
            "chars": sum(r["chars"] for r in rows)}




def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from run import main as pipeline_main
    return pipeline_main(['collect', '--source', 'epoch_site', *sys.argv[1:]])

if __name__ == '__main__':
    raise SystemExit(main())
