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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

# ⚠️ 必须写在 sys.path.insert **之后**，否则从别的 cwd 跑会 ModuleNotFoundError
#    （实测踩过，见 collectors/zsxq_incr.py 同处注释）。
from aris.net_route import RouteConfigError, overseas_urlopen  # noqa: E402
from aris.daily_policy import matches, publication_day
from aris.article_audit import date_rejection, record, substack_details
from aris.paths import RAW, SOURCE_STATE as STATE  # noqa: E402
from aris import semi_media as sm  # noqa: E402
from aris.substack_api import ArchiveError, archive as fetch_archive  # noqa: E402

SITE = "semianalysis"
BASE = RAW / "web" / SITE
STATE_FILE = STATE / "semianalysis.json"
LOCK = STATE / "semianalysis.lock"

SUB_HOST = "https://newsletter.semianalysis.com"
WP_HOST = "https://semianalysis.com"

RETRY_BASE = 1.5          # 退避基数（秒）；429 会用它做指数退避

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 🔴 WP 正文里穿插的订阅 CTA，抽正文时必须剥掉。
# 最后一条是**硬付费墙**标记 —— 它出现即说明后文被截断。
WP_HARD_PAYWALL = "Subscribe for full access to this article"
WP_CTA_SNIPPETS = (
    "Subscribe to get notified of all SemiAnalysis articles",
    "Log in or Subscribe",
    WP_HARD_PAYWALL,
    "With a SemiAnalysis subscription you’ll get access to newsletter articles "
    "and article discussions.",
    "Model access not included – please reach out to sales@semianalysis.com "
    "for our institutional offerings.",
    "By subscribing, you agree to the Privacy Policy and Terms and Conditions",
)

# Substack 免费篇末尾的固定推广句，不算正文
SUB_CTA_SNIPPETS = (
    "SemiAnalysis is a reader-supported publication. To receive new posts and "
    "support my work, consider becoming a free or paid subscriber.",
)


def log(message):
    import logging
    logger = logging.getLogger('corpus.collector')
    logger.log(logging.ERROR if '✗' in message or '🔴' in message else logging.INFO, message)


# ---------------------------------------------------------------- state
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:          # 状态文件坏了不能让采集停摆
            log(f"⚠️ state 解析失败（当作空）：{e}")
    return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)            # 原子替换，避免半截文件


def single_instance():
    """非阻塞 flock。某轮卡住时下一轮不能叠上来。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("上一轮仍在运行，本轮跳过")
        return None
    return fh


# ---------------------------------------------------------------- HTTP
class FetchError(RuntimeError):
    pass


def fetch(url: str, *, timeout: float = 60, retries: int = 5,
          want_headers: bool = False):
    """GET → bytes（或 (bytes, headers)）。境外站，走 overseas_urlopen。

    ## 重试策略：按「这个错误会自己好吗」分类，不是按 4xx/5xx 分

    🔴 **429 必须重试**，且必须尊重 `Retry-After`。
    2026-08-24 实测踩到：WP 回灌 260 篇跑到第 252 篇时连吃 8 个 429，
    而我原来写的是「4xx 一律不重试」⇒ 8 篇直接判失败。
    这是把「限流」和「资源不存在」混为一类的后果 ——
    404/403 重试一万次也还是 404（确定性失败），
    而 429 的字面意思就是「你太快了，等一下再来」（暂时性失败）。
    ⭕ 判据是**「重试有没有可能成功」**，不是状态码的首位数字。

    ⚠️ `Retry-After` 可能是秒数也可能是 HTTP 日期；这里只认秒数，
    解析不出就退回指数退避（不猜、不硬编码一个大数字）。
    """
    last = None
    for i in range(retries):
        req = Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/json, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with overseas_urlopen(req, timeout=timeout) as r:
                body = r.read()
                return (body, dict(r.headers)) if want_headers else body
        except HTTPError as e:
            last = e
            if e.code == 429:
                wait = RETRY_BASE * (2 ** i)
                ra = (e.headers or {}).get("Retry-After")
                if ra:
                    try:
                        wait = max(wait, min(float(str(ra).strip()), 120.0))
                    except ValueError:
                        pass            # 日期格式 → 用退避值，不猜
                if i < retries - 1:
                    log(f"    · 429 限流，等 {wait:.0f}s 重试")
                    time.sleep(wait)
                    continue
            elif 400 <= e.code < 500:
                raise FetchError(f"{url} → HTTP {e.code}（确定性失败，不重试）") from e
        except RouteConfigError:
            raise
        except (URLError, OSError, TimeoutError) as e:
            last = e
        if i < retries - 1:
            time.sleep(RETRY_BASE * (2 ** i))
    raise FetchError(f"{url} → {last}")


def fetch_json(url: str, **kw):
    raw = fetch(url, **kw)
    if isinstance(raw, tuple):
        raw, hdrs = raw
        return json.loads(raw.decode("utf-8", "replace")), hdrs
    return json.loads(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------- 正文抽取
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def html_to_text(h: str | None) -> str:
    """HTML → 纯文本。图片替换成**带序号**的锚点，保留「这里有第几张图」的信息。

    🔴 2026-08-24 从无序号 `[图]` 改成 `[图1]`/`[图2]`。
    原因：无序号的占位符**无法与图文件对应** —— 正安 08-24 问「图都是[图]，
    是采不下来还是前端问题」，根因就是当时只存了「这里有图」而没存图。
    序号是把文字与图重新缝起来的唯一锂 —— 它必须与 `semi_media.parse_imgs()`
    的输出下标逐一对应，所以两边**共用同一个正则** `sm.IMG_TAG_RE`，
    且均跳过无 src 的 `<img>`（实测 7,221 个标签里 0 个无 src，但不能指望它永远为 0）。

    ⚠️ 锚点会进入全文检索的正文，也会被 `count_words()` 计入 ——
    这与旧的 `[图]` 同性质（都是 1 个词），所以 coverage 口径不变、
    `FULL_COVERAGE=0.95` 不用跟着改。
    """
    if not h:
        return ""
    import html as _html
    t = _SCRIPT.sub(" ", h)

    # 图片/图表：留带序号的锚点，否则读者不知道原文这里有一张关键图
    counter = {"n": 0}

    def _anchor(m: re.Match) -> str:
        if not sm._SRC_RE.search(m.group(0)):
            return " "          # 无 src 不占序号（与 parse_imgs 一致）
        counter["n"] += 1
        return " " + sm.ANCHOR_FMT.format(n=counter["n"]) + " "

    t = sm.IMG_TAG_RE.sub(_anchor, t)
    t = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote)>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip()


def strip_cta(text: str, snippets) -> str:
    for s in snippets:
        text = text.replace(s, "")
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


# ---------------------------------------------------------------- Substack
def sub_archive(limit: int = 50, max_pages: int = 500, *, max_posts: int | None = None) -> list[dict]:
    """完整 Substack 清单，兼容旧 offset 与新 nextCursor 协议。"""
    try:
        out = fetch_archive(fetch_json, SUB_HOST, limit=limit,
                            max_pages=max_pages, max_posts=max_posts, log=log)
    except ArchiveError as e:
        raise FetchError(f"SemiAnalysis Substack: {e}") from e
    if not out:
        raise FetchError("SemiAnalysis Substack archive 返回空清单（API 变更或 WAF）")
    return out


def sub_post(slug: str) -> dict:
    return fetch_json(f"{SUB_HOST}/api/v1/posts/{slug}")


# ---------------------------------------------------------------- WordPress
def wp_index(per_page: int = 100, max_pages: int = 20, *, max_posts: int | None = None) -> list[dict]:
    """全量清单（只要轻字段）。用 X-WP-Total 校验没漏页。"""
    out: dict[int, dict] = {}
    total = None
    if max_posts is not None:
        per_page = min(per_page, max_posts)
    for pg in range(1, max_pages + 1):
        url = (f"{WP_HOST}/wp-json/wp/v2/posts?per_page={per_page}&page={pg}"
               "&orderby=date&order=desc&_fields=id,date,date_gmt,modified_gmt,"
               "slug,link,title,excerpt")
        try:
            d, hdrs = fetch_json(url, want_headers=True)
        except FetchError as e:
            # 翻过尾页 WP 回 400 rest_post_invalid_page_number —— 正常终止
            if "HTTP 400" in str(e):
                break
            raise
        if total is None:
            total = int(hdrs.get("X-WP-Total") or hdrs.get("x-wp-total") or 0)
        if not d:
            break
        for p in d:
            out[p["id"]] = p
        if max_posts is not None and len(out) >= max_posts:
            break
        time.sleep(0.3)
    if not out:
        raise FetchError("SemiAnalysis WordPress 清单为空（API 变更或 WAF）")
    expected = min(total, max_posts) if total and max_posts is not None else total
    if expected and len(out) != expected:
        # 清单不完整不能推进水位线，按整轮失败处理。
        raise FetchError(f"WP 清单条数不符：拿到 {len(out)}，X-WP-Total={total}")
    return sorted(out.values(), key=lambda p: p.get("date") or "")


def wp_post(pid: int) -> dict:
    return fetch_json(f"{WP_HOST}/wp-json/wp/v2/posts/{pid}"
                      "?_fields=id,date,date_gmt,modified_gmt,slug,link,title,"
                      "content,excerpt,author,categories,tags")


# ---------------------------------------------------------------- 落盘
def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_index(path: Path, rows: list[dict]) -> None:
    """清单按 jsonl 覆盖写 —— 天然幂等，不用自己做去重。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def safe_slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "-", s or "")[:120]
    return s or "unnamed"


# ---------------------------------------------------------------- 采集主体
def collect_substack(*, full: bool, redo: bool, limit_posts: int | None,
                     st: dict) -> dict:
    idx = sub_archive(max_posts=limit_posts)
    write_index(BASE / "substack" / "index.jsonl", idx)
    log(f"  Substack 清单 {len(idx)} 篇")

    seen_hwm = st.get("substack", {}).get("last_post_date") or ""
    todo = []
    for p in idx:
        if not matches(p.get("post_date")):
            date_rejection(*substack_details(p, SUB_HOST), p.get("post_date"))
            continue
        f = BASE / "substack" / "posts" / f"{safe_slug(p['slug'])}.json"
        if redo or not f.exists():
            todo.append(p)
        elif not full and (p.get("post_date") or "") > seen_hwm:
            todo.append(p)
    log(f"  Substack 待抓 {len(todo)} 篇（已有 "
        f"{len(list((BASE / 'substack' / 'posts').glob('*.json')))} 篇）")

    ok = 0
    fail = sum(publication_day(p.get('post_date')) is None for p in idx) if os.getenv('CORPUS_TARGET_DATE') else 0
    if fail:log(f"    ✗ {fail} 篇无法确定发布日期，未采集")
    got: list = []
    for i, p in enumerate(todo, 1):
        slug = p["slug"]
        title, url = substack_details(p, SUB_HOST)
        try:
            d = sub_post(slug)
        except (FetchError, OSError, ValueError) as e:
            record("failed", title, url, p.get("post_date"), str(e))
            log(f"    ✗ {slug}: {e}")
            fail += 1
            continue
        # 清单里的 audience 是权威值（单篇 API 也有，取单篇优先）
        if publication_day(d.get('post_date')) is None:d['post_date']=p.get('post_date')
        if not matches(d.get('post_date')):
            record("failed", title, url, d.get("post_date"), "正文与清单发布日期不一致")
            fail += 1
            log(f"    ✗ 正文与清单发布日期不一致，未保存: {slug}")
            continue
        d.setdefault("audience", p.get("audience"))
        d["_aris"] = {"fetched_at": datetime.now().astimezone().isoformat(),
                      "archive_audience": p.get("audience")}
        try:
            write_json(BASE / "substack" / "posts" / f"{safe_slug(slug)}.json", d)
        except OSError as e:
            record("failed", title, url, d.get("post_date"), str(e))
            raise
        record("fetched", d.get("title") or title, d.get("canonical_url") or url, d.get("post_date"))
        ok += 1
        got.append(slug)
        if i % 20 == 0:
            log(f"    …{i}/{len(todo)}")
        time.sleep(0.4)
    hwm = max((p.get("post_date") or "") for p in idx) if idx else seen_hwm
    return {"n_index": len(idx), "n_fetched": ok, "n_failed": fail,
            "last_post_date": hwm, "site": "substack", "got": got}


def collect_wp(*, full: bool, redo: bool, limit_posts: int | None,
               st: dict) -> dict:
    idx = wp_index(max_posts=limit_posts)
    write_index(BASE / "wp" / "index.jsonl", idx)
    log(f"  WordPress 清单 {len(idx)} 篇")

    seen_hwm = st.get("wp", {}).get("last_post_date") or ""
    todo = []
    fail = 0
    for p in idx:
        if not matches(p.get("date_gmt") or p.get("date")):
            fail += date_rejection(p.get("slug"), p.get("link"), p.get("date_gmt") or p.get("date"))
            continue
        f = BASE / "wp" / "posts" / f"{p['id']}.json"
        if redo or not f.exists():
            todo.append(p)
        elif not full and (p.get("date") or "") > seen_hwm:
            todo.append(p)
    log(f"  WordPress 待抓 {len(todo)} 篇（已有 "
        f"{len(list((BASE / 'wp' / 'posts').glob('*.json')))} 篇）")

    ok = 0
    got: list = []
    for i, p in enumerate(todo, 1):
        try:
            d = wp_post(p["id"])
        except (FetchError, OSError, ValueError) as e:
            record("failed", p.get("slug"), p.get("link"), p.get("date_gmt") or p.get("date"), str(e))
            log(f"    ✗ wp/{p['id']}: {e}")
            fail += 1
            continue
        publication_date = d.get("date_gmt") or d.get("date") or p.get("date_gmt") or p.get("date")
        title = (d.get("title") or {}).get("rendered") or p.get("slug")
        url = d.get("link") or p.get("link")
        if not matches(publication_date):
            record("failed", title, url, publication_date, "正文与清单发布日期不一致")
            fail += 1
            continue
        if publication_date and not (d.get("date_gmt") or d.get("date")):
            d["date_gmt"] = publication_date
        d["_aris"] = {"fetched_at": datetime.now().astimezone().isoformat()}
        try:
            write_json(BASE / "wp" / "posts" / f"{p['id']}.json", d)
        except OSError as e:
            record("failed", title, url, publication_date, str(e))
            raise
        record("fetched", title, url, publication_date)
        ok += 1
        got.append(p["id"])
        if i % 20 == 0:
            log(f"    …{i}/{len(todo)}")
        time.sleep(0.4)
    hwm = max((p.get("date") or "") for p in idx) if idx else seen_hwm
    return {"n_index": len(idx), "n_fetched": ok, "n_failed": fail,
            "last_post_date": hwm, "site": "wp", "got": got}


# ---------------------------------------------------------------- 正文抽取落盘
# 🔴 为什么正文要抽成独立 .txt 而不是让 adapter 现算：
# ① kb 的约定是长正文走 `body_path`（相对 data/raw），PDF 已经这样做了；
#    本站单篇正文最大 104,390 字符，而库里现有 body 最大才 18,249 —— 属于「长正文」。
# ② adapter 必须是**纯读**层。让它现场解析 HTML，等于把「解析规则变了」
#    这件事藏进索引步骤，下次改抽取逻辑时没人知道要重跑索引。
# ③ 抽取结果带 coverage 等度量，本身就是要入库的事实，得有落盘副本可复核。
# 这与 zsxq 的 `extracted.jsonl` 是同一套模式，不另创花样。

TEXT_DIR = "text"
FULL_COVERAGE = 0.95      # 覆盖率 ≥ 此值视为全文（见下方实测分布）


def _wp_is_preview(content_html: str) -> bool:
    return WP_HARD_PAYWALL in (content_html or "")


def count_words(text: str) -> int:
    """正文词数。用于 coverage（本地词数 ÷ Substack 自报 wordcount）。

    🔴 **必须先剔掉图片锚点** —— 锚点是我们插的标记，不是原文的词。

    2026-08-24 实测踩到：把 `[图]` 改成带序号的 `[图1]` 后，正则
    `[A-Za-z0-9’'-]+` 会把那个数字当成一个词（旧的 `[图]` 计 0 词）
    ⇒ 图多的篇子 words_local 虚高 ⇒ coverage 虚高 ⇒ **跨过 0.95 阈值**。
    具体现场：`inference-race-to-the-bottom-make` 从 0.9487 漂到 0.9504，
    两站共 2 篇从「付费预览」错标成「完整全文」。

    ⛔ 方向错得很危险：它把残篇说成全文 ⇒ 半年后有人拿预览当全文下结论。
    这正是本文件顶部那条「必须把残缺标进库」的反面案例。
    ⭕ 通则：**改正文格式时，先问「有哪些度量是拿正文长度算的」。**
    度量与文本格式隐式耦合，改一头必须同步另一头，否则阈值静默漂移。
    """
    t = sm.ANCHOR_RE.sub(" ", text or "")
    return len(re.findall(r"[A-Za-z0-9\u2019'\-]+", t))


def title_key(t: str | None) -> str:
    """标题指纹，用来把两站的同一篇文章对上。

    只保留字母数字并截前 45 字符：两站标题偶有破折号/花引号/HTML 实体差异
    （`&#8217;` vs `’`），直接比字符串对不上。
    """
    import html as _html
    t = _html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    return re.sub(r"[^a-z0-9]", "", t.lower())[:45]


def _read_jsonl(p: Path):
    if not p.exists():
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def extract_texts() -> dict:
    """把已抓到的原始 JSON 抽成纯文本 + 度量索引。可单独重跑（--extract-text）。

    ## 🔴 `is_preview` 不能只看 `audience`

    实测 191 篇（2026-08-24）：

    | audience | n | 覆盖率中位 | ≥95%（实为全文） |
    |---|---|---|---|
    | everyone | 91 | 99.9% | 90/91 |
    | only_paid | 100 | 75.1% | **9/100** |

    `only_paid` 里有 9 篇覆盖率 ≥95% —— 光看 audience 会把它们错标成残篇；
    反过来付费篇覆盖率低至 **5.8%**（`nvidias-infiniband-problem-qmx-ai`）。
    ⇒ 判据必须是**实测覆盖率**（本地词数 ÷ Substack 自报 `wordcount`），
    audience 只作为旁证记录。

    ⚠️ 覆盖率可能略 >100%（实测最高 156%）：`wordcount` 是 Substack 自己的统计口径，
    与我们的分词不同，且图片占位符 `[图]` 会被计入。所以阈值取 0.95 而非 1.0，
    且**不允许**用覆盖率反推「缺了多少字」这种精确结论。
    """
    out_dir = BASE / TEXT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    # 标题指纹 → Substack 的实测判定，供 WP 侧借用（见下方 WP 循环里的说明）
    sub_verdict: dict[str, dict] = {}
    # 图片台账（下载器维护）。🔴 读一次、全程复用：
    # 每篇重读会把 581 次 I/O 变成无用开销，且一轮中途台账变了反而会造出不一致。
    ledger = sm.load_ledger()
    if not ledger:
        log("  ⚠️ 图片台账为空 —— 正文锚点会全部标为未获取（本轮尚无成功上传的配图）")

    for f in sorted((BASE / "substack" / "posts").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        text = strip_cta(html_to_text(d.get("body_html")), SUB_CTA_SNIPPETS)
        images = sm.resolve_images(sm.parse_imgs(d.get("body_html")), ledger)
        wc = d.get("wordcount") or 0
        lw = count_words(text)
        cov = (lw / wc) if wc else None
        # 🔴 判定顺序有讲究，别改成「先看 coverage」：
        # `audience='everyone'` 表示这篇**根本不存在付费墙** —— 这是结构事实，
        # 优先于任何度量。2026-08-24 实测踩到：`a-century-of-moores-law` 是免费篇，
        # coverage 只有 0.935（低于阈值）会被误标成残篇，
        # 但它的 WP 孪生本地词数**完全相同**（2431 == 2431）⇒ 正文其实是完整的，
        # 差异全来自 Substack 自报 `wordcount`(2600) 与我们分词口径不同。
        # ⭕ 通则：**结构事实（有没有墙）压过噪声度量（覆盖率）**。
        # 反过来 only_paid 才需要实测，因为「有墙」不等于「这次真截了」——
        # 实测 29/129 篇 only_paid 覆盖率 ≥95%（付费但全放），光看 audience 会错标。
        if (d.get("audience") or "") == "everyone":
            is_prev = False
            basis = "audience_free"
        elif cov is None:
            is_prev = (d.get("audience") == "only_paid")
            basis = "audience"
        else:
            is_prev = cov < FULL_COVERAGE
            basis = "coverage"
        name = f"substack-{safe_slug(d.get('slug') or f.stem)}.txt"
        (out_dir / name).write_text(text, encoding="utf-8")
        rows.append({
            "site": "substack", "post_id": d.get("id"), "slug": d.get("slug"),
            "title": d.get("title"), "subtitle": d.get("subtitle") or None,
            "post_date": d.get("post_date"), "updated_at": d.get("updated_at"),
            "url": d.get("canonical_url"),
            "authors": [((b.get("name") or "").strip())
                        for b in (d.get("publishedBylines") or [])
                        if (b.get("name") or "").strip()],
            "audience": d.get("audience"),
            "wordcount_upstream": wc or None, "words_local": lw,
            "coverage": round(cov, 4) if cov is not None else None,
            "is_preview": is_prev, "preview_basis": basis,
            "chars": len(text), "text_path": f"web/{SITE}/{TEXT_DIR}/{name}",
            "images": images,
            "n_images": len(images),
            "n_images_stored": sum(1 for x in images if x.get("file_key")),
            "reactions": d.get("reaction_count"),
            "comments": d.get("comment_count"),
            "raw_ref": f"web/{SITE}/substack/posts/{f.name}",
        })
        if basis == "coverage" or basis == "audience_free":
            sub_verdict[title_key(d.get("title"))] = {
                "is_preview": is_prev, "coverage": cov,
                "words": lw, "slug": d.get("slug")}

    for f in sorted((BASE / "wp" / "posts").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        html_body = (d.get("content") or {}).get("rendered") or ""
        text = strip_cta(html_to_text(html_body), WP_CTA_SNIPPETS)
        images = sm.resolve_images(sm.parse_imgs(html_body), ledger)
        wp_title = html_to_text((d.get("title") or {}).get("rendered"))
        lw_wp = count_words(text)
        # 🔴 WP 没有 wordcount 字段，只能看硬付费墙标记 —— 但它**不可靠**。
        # 2026-08-24 拿 164 组两站重叠文做混淆矩阵（以 Substack 实测覆盖率为参照）：
        #   TP=66  TN=88  **FP=8**  FN=2
        # ⇒ 8 篇正文其实完整的 WP 文被标成预览。原因：那句 CTA 是模板固定插入的，
        #   文章完整时照样出现 ⇒ 它证明「页面上有订阅入口」，不证明「正文被截了」。
        # ⭕ 修法：255 篇两站重叠，Substack 侧有 wordcount 可实测 ⇒ **借孪生的结论**。
        #   两站正文实测只差 1~3%（同一份内容），借用是安全的；
        #   借不到（WP 独有的 4 篇）才退回标记法，并把依据显式记进 preview_basis。
        twin = sub_verdict.get(title_key(wp_title))
        marker = _wp_is_preview(html_body)
        if twin is not None and twin["words"]:
            # 长度相差过大说明不是同一篇（标题撞车），不敢借
            ratio = lw_wp / twin["words"]
            if 0.85 <= ratio <= 1.18:
                is_prev = twin["is_preview"]
                wp_basis = "substack_twin"
            else:
                is_prev, wp_basis = marker, "wp_paywall_marker"
        else:
            is_prev, wp_basis = marker, "wp_paywall_marker"
        name = f"wp-{d.get('id')}.txt"
        (out_dir / name).write_text(text, encoding="utf-8")
        rows.append({
            "site": "wp", "post_id": d.get("id"), "slug": d.get("slug"),
            "title": wp_title,
            "subtitle": None,
            "post_date": d.get("date_gmt") or d.get("date"), "updated_at": d.get("modified_gmt"),
            "url": d.get("link"), "authors": [],
            "audience": "only_paid" if marker else "everyone",
            "wordcount_upstream": None, "words_local": lw_wp,
            "coverage": None,
            "is_preview": is_prev, "preview_basis": wp_basis,
            "wp_paywall_marker": marker,
            "chars": len(text), "text_path": f"web/{SITE}/{TEXT_DIR}/{name}",
            "images": images,
            "n_images": len(images),
            "n_images_stored": sum(1 for x in images if x.get("file_key")),
            "reactions": None, "comments": None,
            "raw_ref": f"web/{SITE}/wp/posts/{f.name}",
        })

    rows.sort(key=lambda r: (r.get("post_date") or "", str(r.get("post_id"))))
    write_index(BASE / TEXT_DIR / "index.jsonl", rows)
    n_prev = sum(1 for r in rows if r["is_preview"])
    n_img = sum(r["n_images"] for r in rows)
    n_loc = sum(r["n_images_stored"] for r in rows)
    log(f"  抽正文 {len(rows)} 篇（完整 {len(rows) - n_prev} / 预览 {n_prev}）"
        f" · 共 {sum(r['chars'] for r in rows):,} 字符")
    # 🔴 图片到位率必须每轮嗊出来：它是「正文里的 [图N] 能不能真渲染成图」的单一指标。
    #    静默的 0% 会让人以为「本来就没图」（正安 08-24 遇到的就是这个误读）。
    if n_img:
        log(f"  图片锚点 {n_img:,} 处，已存对象存储 {n_loc:,} "
            f"({n_loc / n_img * 100:.1f}%)")
    else:
        log("  图片锚点 0 处")
    return {"n": len(rows), "n_preview": n_prev,
            "n_images": n_img, "n_images_stored": n_loc}












# ---------------------------------------------------------------- CLI
def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from run import main as pipeline_main
    return pipeline_main(['collect', '--source', 'semianalysis', *sys.argv[1:]])

if __name__ == '__main__':
    raise SystemExit(main())
