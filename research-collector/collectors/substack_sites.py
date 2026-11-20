#!/usr/bin/env python3
"""Article collection and text extraction. Public entrypoint: python run.py collect."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

# ⚠️ 必须在 sys.path.insert **之后**（从别的 cwd 跑会 ModuleNotFoundError，实测踩过）
from aris.net_route import RouteConfigError, overseas_urlopen  # noqa: E402
from aris.daily_policy import matches, publication_day
from aris.article_audit import date_rejection, record, substack_details
from aris.paths import CONFIG, SOURCE_STATE as STATE  # noqa: E402
from aris import web_media as wm  # noqa: E402
from aris.substack_api import ArchiveError, archive as fetch_archive  # noqa: E402

CFG = CONFIG / "web_sources.yaml"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

RETRY_BASE = 1.5
FULL_COVERAGE = 0.95      # 与 semianalysis.py 同阈值（同一口径，别各自定）

# Substack 免费篇末尾的固定推广句族。🔴 站名会变，所以用模式而不是字面量。
CTA_RE = re.compile(
    r"[^.\n]*is a reader-supported publication\.\s*To receive new posts and "
    r"support my work, consider becoming a (?:free or paid )?subscriber\.",
    re.I)
CTA_EXTRA = (
    "Thanks for reading! Subscribe for free to receive new posts.",
    "By subscribing, you agree to the Privacy Policy and Terms and Conditions",
)


def log(message):
    import logging
    logger = logging.getLogger('corpus.collector')
    logger.log(logging.ERROR if '✗' in message or '🔴' in message else logging.INFO, message)


# ---------------------------------------------------------------- 配置
def load_cfg() -> list[dict]:
    """读配置表里的 substack 段。

    🔴 用 yaml.safe_load，不用 eval/full_load —— 配置文件是数据，不是代码。
    """
    import yaml
    d = yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    rows = d.get("substack") or []
    for r in rows:
        for req in ("key", "name", "host"):
            if not r.get(req):
                raise ValueError(f"配置缺字段 {req}: {r}")
    return rows


def pick_sites(only: list[str] | None) -> list[dict]:
    rows = load_cfg()
    if not only:
        return rows
    want = set()
    for o in only:
        want.add(o if o.startswith("web:") else f"web:{o}")
    out = [r for r in rows if r["key"] in want]
    missing = want - {r["key"] for r in out}
    if missing:
        # 🔴 抛错而不是静默跳过：`--only typo` 静默跑 0 个站会被误读成「没新内容」
        raise SystemExit(f"配置里没有这些站：{sorted(missing)}\n"
                         f"可选：{sorted(r['key'] for r in load_cfg())}")
    return out


# ---------------------------------------------------------------- 单实例
def single_instance(site: str):
    lock = STATE / f"web_{site}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(f"  {site}: 上一轮仍在运行，跳过")
        return None
    return fh


def state_path(site: str) -> Path:
    return STATE / f"web_{site}.json"


def load_state(site: str) -> dict:
    p = state_path(site)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as e:
            log(f"  ⚠️ {site} state 解析失败（当作空）：{e}")
    return {}


def save_state(site: str, st: dict) -> None:
    p = state_path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(p)


def record_run(site: str, entry: dict) -> None:
    """包括清单失败和锁冲突在内的运行事实，供连续失败巡检使用。"""
    st = load_state(site)
    at = datetime.now().astimezone().isoformat()
    st["last_run_at"] = at
    if entry.get("failed"):
        st["last_error"] = entry.get("error")
    else:
        st["last_ok_at"] = at
        st.pop("last_error", None)
    hist = st.get("history") or []
    hist.append({"at": at, **entry})
    st["history"] = hist[-200:]
    save_state(site, st)


# ---------------------------------------------------------------- 抓取
class FetchError(RuntimeError):
    pass


def fetch(url: str, *, timeout: float = 60, retries: int = 4) -> bytes:
    """带退避的 GET。429 用指数退避，5xx 重试，4xx 直接抛（重试无意义）。"""
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
            # 路由未配置/互斥配置不是网络抖动，不做 4 次无意义重试。
            raise
        except (URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
        if i < retries - 1:
            time.sleep(RETRY_BASE * (2 ** i))
    raise FetchError(f"{last} {url}")


def fetch_json(url: str, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


def archive(host: str, *, limit: int = 50, max_pages: int = 500,
            max_posts: int | None = None) -> list[dict]:
    """完整清单，兼容旧 offset 数组和新 {posts,nextCursor} 游标协议。

    旧协议的 offset 按实际返回条数累加；重复旧页可终止。新协议整页
    重复、游标不推进或达到页数上限都视为不完整，不能覆盖本地清单。
    """
    try:
        return fetch_archive(fetch_json, host, limit=limit,
                             max_pages=max_pages, max_posts=max_posts, log=log)
    except ArchiveError as e:
        raise FetchError(f"{host}: {e}") from e


def post(host: str, slug: str) -> dict:
    return fetch_json(f"{host}/api/v1/posts/{slug}")


# ---------------------------------------------------------------- 正文
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_BLOCK_END = re.compile(
    r"</(p|div|h[1-6]|li|tr|blockquote|figure|figcaption|section|table)\s*>",
    re.I)


def html_to_text(h: str | None) -> str:
    """HTML → 纯文本，`<img>` 逐个替换成 `[图N]`。

    🔴 序号是把文字与图缝起来的唯一键，必须与 `web_media.parse_imgs()` 的
    列表下标一一对应 —— 两边都按 `IMG_TAG_RE` 的**文档顺序**走，
    且都跳过无 src 的 img。改任一边必须同步另一边。
    """
    t = _SCRIPT.sub(" ", h or "")

    n = [0]

    def _img(m):
        tag = m.group(0)
        # 与 parse_imgs 同步：无 src 不占序号
        if not re.search(r"""\bsrc\s*=\s*(?:"[^"]*"|'[^']*')""", tag, re.I):
            return " "
        n[0] += 1
        return " " + wm.ANCHOR_FMT.format(n=n[0]) + " "

    t = re.sub(r"<img\b[^>]*>", _img, t, flags=re.I)
    t = _BLOCK_END.sub("\n", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    import html as _html
    t = _html.unescape(t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()


def strip_cta(text: str) -> str:
    t = CTA_RE.sub("", text or "")
    for s in CTA_EXTRA:
        t = t.replace(s, "")
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


def count_words(text: str) -> int:
    """正文词数。🔴 先剔掉 `[图N]` 锚点 —— 它是我们插的标记，不是原文的词。

    2026-08-24 在 SemiAnalysis 上实测过这个坑：不剔的话锚点里的数字被算成词
    ⇒ 图多的篇 coverage 虚高 ⇒ 跨过 0.95 阈值 ⇒ **残篇被标成全文**。
    方向错得危险（把不完整说成完整），必须守住。
    """
    t = wm.ANCHOR_RE.sub(" ", text or "")
    return len(re.findall(r"[A-Za-z0-9\u2019'\-]+", t))


# ---------------------------------------------------------------- 采集一个站
def collect_site(cfg: dict, *, full: bool, redo: bool,
                 limit_posts: int | None) -> dict:
    key, name, host = cfg["key"], cfg["name"], cfg["host"]
    store = wm.SiteStore(key)
    site = store.site
    since = str(cfg.get("since") or "")

    fh = single_instance(site)
    if fh is None:
        res = {"site": site, "key": key, "name": name, "skipped": "locked",
               "n_index": 0, "n_fetched": 0, "n_failed": 1, "got": []}
        record_run(site, {"mode": "skipped", "index": 0, "fetched": 0,
                          "failed": 1, "error": "上一轮仍在运行，锁冲突",
                          "secs": 0})
        return res
    t0 = time.time()
    st = load_state(site)
    try:
        idx = archive(host, max_posts=limit_posts)
        if not idx:
            raise FetchError(f"{name}: archive 返回空清单（站点/API 变更或 WAF）")
        wm.write_jsonl(store.base / "index.jsonl", idx)
        log(f"  {name}: 清单 {len(idx)} 篇")

        # ---------------------------------------------------------------- 待抓判定
        # 🔴 判据只有两条，故意做得极简：
        #   ① 本地没有这篇的原始 JSON（`--redo` 时无视已有）
        #   ② 且它在回灌范围内（`post_date >= since`；`--full` 无视 since）
        #
        # 为什么不需要「比水位线新」这个条件：**缺文件本身就是唯一判据**。
        # 增量新文必然缺文件，且 post_date 必然晚于 since（since 是过去的日期）
        # ⇒ 条件 ① + ② 天然覆盖增量，不用第三条规则。
        # ⭕ 收益：想往前扩历史，只改 `since` 重跑即可（或直接 `--full`），
        #    不必清 state；而水位线只用来做「本轮是否有新内容」的通知判断。
        # ⚠️ 反面教材是我第一版写的三条互相叠加的 if —— 它们的组合语义我自己都要
        #    推演半天，而实际效果与这两条完全相同。规则越少越守得住。
        hwm = st.get("last_post_date") or ""
        todo = []
        for p in idx:
            pd = p.get("post_date") or ""
            if not matches(pd):
                date_rejection(*substack_details(p, host), pd)
                continue
            f = store.base / "posts" / f"{wm.safe_slug(p['slug'])}.json"
            if f.exists() and not redo:
                continue
            if since and not os.getenv("CORPUS_TARGET_DATE") and not full and pd[:10] < since:
                continue
            todo.append(p)
        have = len(list((store.base / "posts").glob("*.json")))
        log(f"  {name}: 待抓 {len(todo)} 篇（本地已有 {have}）")

        ok = 0
        fail = sum(publication_day(p.get('post_date')) is None for p in idx) if os.getenv('CORPUS_TARGET_DATE') else 0
        if fail:log(f"    ✗ {fail} 篇无法确定发布日期，未采集")
        got: list[str] = []
        for i, p in enumerate(todo, 1):
            slug = p["slug"]
            title, url = substack_details(p, host)
            try:
                d = post(host, slug)
            except (FetchError, OSError, ValueError) as e:
                record("failed", title, url, p.get("post_date"), str(e))
                log(f"    ✗ {slug}: {e}")
                fail += 1
                continue
            if publication_day(d.get('post_date')) is None:d['post_date']=p.get('post_date')
            if not matches(d.get('post_date')):
                record("failed", title, url, d.get("post_date"), "正文与清单发布日期不一致")
                fail += 1
                log(f"    ✗ 正文与清单发布日期不一致，未保存: {slug}")
                continue
            d.setdefault("audience", p.get("audience"))
            d["_aris"] = {
                "fetched_at": datetime.now().astimezone().isoformat(),
                "archive_audience": p.get("audience"),
                "source_key": key,
            }
            try:
                wm.write_json(store.base / "posts" / f"{wm.safe_slug(slug)}.json", d)
            except OSError as e:
                record("failed", title, url, d.get("post_date"), str(e))
                raise
            record("fetched", d.get("title") or title, d.get("canonical_url") or url, d.get("post_date"))
            ok += 1
            got.append(slug)
            if i % 25 == 0:
                log(f"    …{i}/{len(todo)}")
            time.sleep(0.4)
    except Exception as e:
        # 清单/网络/路由失败留下失败历史，不能让进程跑过被误当采集成功。
        res = {"site": site, "key": key, "name": name, "n_index": 0,
               "n_fetched": 0, "n_failed": 1,
               "error": f"{type(e).__name__}: {e}", "got": []}
        record_run(site, {"mode": "full" if full else "incr", "index": 0,
                          "fetched": 0, "failed": 1, "error": res["error"]})
        raise
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    newest = max((p.get("post_date") or "") for p in idx) if idx else hwm
    res = {"site": site, "key": key, "name": name,
           "n_index": len(idx), "n_fetched": ok, "n_failed": fail,
           "last_post_date": newest, "got": got,
           "secs": round(time.time() - t0, 1)}

    now_iso = datetime.now().astimezone().isoformat()
    st["last_run_at"] = now_iso
    # 🔴 水位线只在整轮无失败时推进：宁可下轮重拉，不许留静默空洞。
    # 成功时间也必须取本轮同一个时间，不能读尚未更新的 last_run_at。
    if fail == 0:
        st["last_post_date"] = newest
        st["last_ok_at"] = now_iso
        st.pop("last_error", None)
    else:
        st["last_error"] = f"{fail} 篇抓取失败，水位线未推进"
        log(f"  ⚠️ {name} 有 {fail} 篇失败，水位线不推进")
    st["n_index"] = len(idx)
    hist = st.get("history", [])
    hist.append({"at": st["last_run_at"], "mode": "full" if full else "incr",
                 "index": len(idx), "fetched": ok, "failed": fail,
                 "secs": res["secs"]})
    st["history"] = hist[-200:]
    save_state(site, st)
    wm.write_json(store.base / "meta.json", {
        "site": site, "key": key, "name": name, "channel": "web",
        "host": host, "since": since or None,
        "author_hint": cfg.get("author_hint"),
        "note": cfg.get("note"),
        "n_index": len(idx),
        "updated_at": datetime.now().astimezone().isoformat(),
    })
    return res


# ---------------------------------------------------------------- 正文抽取
def extract_site(cfg: dict) -> dict:
    """把已抓的原始 JSON 抽成 text/*.txt + text/index.jsonl。不联网。"""
    key, name = cfg["key"], cfg["name"]
    store = wm.SiteStore(key)
    out_dir = store.base / "text"
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = store.load_ledger()

    rows = []
    for f in sorted((store.base / "posts").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        bh = d.get("body_html")
        text = strip_cta(html_to_text(bh))
        images = store.resolve_images(wm.parse_imgs(bh), ledger)
        wc = d.get("wordcount") or 0
        lw = count_words(text)
        cov = (lw / wc) if wc else None
        aud = d.get("audience") or ""

        # 🔴 三态判定，顺序不可改（每一步都有实测依据，见模块头）
        # ① 没有 body_html ⇒ no_text。这是**结构事实**：站方一个字都没给。
        #    Fabricated Knowledge 付费篇实测如此（wordcount 有值、body_html 为 None）。
        #    ⛔ 标成 preview 会让人以为「点进去能读到一部分」。
        if not bh or not text:
            state, is_prev, basis = "no_text", True, "no_body_html"
        # ② audience=everyone ⇒ 结构上无付费墙，压过噪声度量。
        #    （SemiAnalysis 实测：免费篇 coverage 0.935 也是完整的，
        #    差异全来自 Substack 自报 wordcount 与我们分词口径不同。）
        elif aud == "everyone":
            state, is_prev, basis = "ready", False, "audience_free"
        elif cov is None:
            is_prev = (aud == "only_paid")
            state = "preview" if is_prev else "ready"
            basis = "audience"
        else:
            is_prev = cov < FULL_COVERAGE
            state = "preview" if is_prev else "ready"
            basis = "coverage"

        name_txt = f"{wm.safe_slug(d.get('slug') or f.stem)}.txt"
        (out_dir / name_txt).write_text(text, encoding="utf-8")
        authors = [((b.get("name") or "").strip())
                   for b in (d.get("publishedBylines") or [])
                   if (b.get("name") or "").strip()]
        if not authors and cfg.get("author_hint"):
            authors = [cfg["author_hint"]]
        rows.append({
            "source_key": key, "site": store.site,
            "post_id": d.get("id"), "slug": d.get("slug"),
            "title": d.get("title"), "subtitle": d.get("subtitle") or None,
            "post_date": d.get("post_date"), "updated_at": d.get("updated_at"),
            "url": d.get("canonical_url"),
            "post_type": d.get("type"),
            "authors": authors,
            "audience": aud or None,
            "wordcount_upstream": wc or None, "words_local": lw,
            "coverage": round(cov, 4) if cov is not None else None,
            "is_preview": is_prev, "content_state": state,
            "preview_basis": basis,
            "chars": len(text),
            "text_path": store.text_rel(name_txt),
            "images": images, "n_images": len(images),
            "n_images_stored": sum(1 for x in images if x.get("file_key")),
            "reactions": d.get("reaction_count"),
            "comments": d.get("comment_count"),
            "raw_ref": f"web/{store.site}/posts/{f.name}",
        })

    rows.sort(key=lambda r: (r.get("post_date") or "", str(r.get("post_id"))))
    wm.write_jsonl(out_dir / "index.jsonl", rows)
    n_prev = sum(1 for r in rows if r["is_preview"])
    n_none = sum(1 for r in rows if r["content_state"] == "no_text")
    n_img = sum(r["n_images"] for r in rows)
    n_loc = sum(r["n_images_stored"] for r in rows)
    log(f"  {name}: 抽正文 {len(rows)} 篇（全文 {len(rows) - n_prev} / "
        f"预览 {n_prev - n_none} / 无正文 {n_none}）· "
        f"{sum(r['chars'] for r in rows):,} 字符")
    if n_img:
        log(f"  {name}: 图片锚点 {n_img:,} 处，已存对象存储 {n_loc:,} "
            f"({n_loc / n_img * 100:.1f}%)")
    return {"n": len(rows), "n_preview": n_prev - n_none, "n_no_text": n_none,
            "n_images": n_img, "n_images_stored": n_loc}


# ---------------------------------------------------------------- 图片




# ---------------------------------------------------------------- CLI
def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from run import main as pipeline_main
    from aris.kb_adapters import sources
    defaults = [] if '--source' in sys.argv else [v for s in sources() if s['_seg'] == 'substack' for v in ('--source', s['key'])]
    return pipeline_main(['collect', *defaults, *sys.argv[1:]])

if __name__ == '__main__':
    raise SystemExit(main())
