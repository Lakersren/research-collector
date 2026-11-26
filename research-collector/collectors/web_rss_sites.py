#!/usr/bin/env python3
"""Article collection and text extraction. Public entrypoint: python run.py collect."""
from __future__ import annotations

import os
import argparse
import fcntl
import gzip
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
from zlib import error as ZlibError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "lib"), str(ROOT)]

# ⚠️ 必须在 sys.path.insert 之后（从别的 cwd 跑会 ModuleNotFoundError，实测踩过）
from aris.net_route import PROXY_URL, RouteConfigError, overseas_opener, overseas_urlopen  # noqa: E402
from aris.daily_policy import candidate, matches, publication_day
from aris.article_audit import date_rejection, page_publication_date, record
from aris.paths import CONFIG, SOURCE_STATE as STATE  # noqa: E402
from aris import web_media as wm  # noqa: E402
from collectors.sites import anthropic, kimi, stratechery
from collectors.web_entries import HTML_TAG as _TAG, parse_date, parse_html_list, plain_html

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
RETRY_BASE = 1.5

_HTML_LIST_PARSERS = {
    "web:anthropic": anthropic.parse_news,
    "web:anthropic-engineering": anthropic.parse_engineering,
    "web:kimi": kimi.parse_blog,
}

# 配置表允许的字段（未知字段一律报错，见 load_cfg）
_KNOWN_RSS_KEYS = {
    "key", "name", "feed", "fallback_feed", "full_in_feed", "kind", "note",
    "list_path_prefix", "list_exclude_paths", "trailing_slash", "browser_headers", "ua",
}

# 🔴 `browser_headers: true` 的由来（2026-08-25 二分定位）
# ------------------------------------------------------------------
# OpenAI 文章页对普通 `UA + Accept:*/*` 回 **403**。逐项加头实测：
#   UA（采集器）+ Accept:*/*                  → 403
#   UA（探针）  + Accept:*/*                  → 403   ⇐ 与 UA 无关！
#   UA + Accept:text/html                    → 403
#   UA + Accept-Encoding:identity            → 403
#   **UA + Sec-Fetch-Dest/Mode/Site          → 200** ← 就是这三个
# ⇒ 它校的是「这看着像不像浏览器导航请求」，不是封 UA。
# ⚠️ 我一开始想改全局 UA —— 那会白改，而且动了其他 12 个本来正常的站。
# ⭕ 只给需要的站开关：改动面最小，且配置里写明为什么开。
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def log(message):
    import logging
    logger = logging.getLogger('corpus.collector')
    logger.log(logging.ERROR if '✗' in message or '🔴' in message else logging.INFO, message)


def load_cfg() -> list[dict]:
    import yaml
    d = yaml.safe_load((CONFIG / "web_sources.yaml").read_text(encoding="utf-8")) or {}
    rows = d.get("rss") or []
    for r in rows:
        for req in ("key", "name", "feed"):
            if not r.get(req):
                raise ValueError(f"配置缺字段 {req}: {r}")
        # 🔴 未知键必须报错，不能默默吃掉（2026-08-25 新增）
        #    拼错 `full_in_feed` → `full_in_feeds` 的后果是：本该零额外请求的站
        #    变成逐篇抓页面，正文可能变短而全程**零报错**。
        #    同族于 TOOLS.md 08-24 那条：`panel()` 不认 `note:`，
        #    两处调用方以为写上了、页面上根本没有。
        unknown = set(r) - _KNOWN_RSS_KEYS
        if unknown:
            raise ValueError(
                f"配置 {r.get('key')} 有未知字段 {sorted(unknown)}；"
                f"可用：{sorted(_KNOWN_RSS_KEYS)}")
    keys = [r["key"] for r in rows]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"配置里 key 重复：{dup}")
    return rows


def pick_sites(only: list[str] | None) -> list[dict]:
    rows = load_cfg()
    if not only:
        return rows
    want = {o if o.startswith("web:") else f"web:{o}" for o in only}
    out = [r for r in rows if r["key"] in want]
    missing = want - {r["key"] for r in out}
    if missing:
        raise SystemExit(f"配置里没有这些站：{sorted(missing)}\n"
                         f"可选：{sorted(r['key'] for r in load_cfg())}")
    return out


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
    """抓取失败。`permanent=True` 表示重试多少次都不会变好。

    🔴 为何要区分（2026-08-25）：水位线只在整轮无失败时推进（这个设计本身对），
    但它的隐含前提是「下轮能拉到」。**永久死链不满足这个前提** ⇒
    水位线永久冻结。现场：OpenAI feed 里的 `inworld-ai-DO-NOT-PUBLISH`
    （slug 自己写着是误发的草稿）回 404，导致连续 5 轮 `failed:1`、
    `last_item_date` 死在 08-20。
    """

    def __init__(self, msg: str, *, permanent: bool = False,
                 code: int | None = None):
        super().__init__(msg)
        self.permanent = permanent
        self.code = code


# 永久死链的 HTTP 码：404 Not Found / 410 Gone。
# ⚠️ **别把 403 算进来** —— 同日实测 OpenAI 的 403 真因是「尾斜杠/请求头不对」，
#    补上就 200。把可修的当成永久死链 = 把本能救回的文章永久埋掉，
#    而且埋得毫无声息（墩碑写完就不再重试了）。
_PERMANENT_HTTP = {404, 410}


def fetch(url: str, *, timeout: float = 60, retries: int = 4,
          browser_headers: bool = False, ua: str | None = None) -> bytes:
    last = None
    last_code = None
    for i in range(retries):
        hd = {"User-Agent": ua or UA, "Accept": "*/*"}
        if browser_headers:
            hd.update(_BROWSER_HEADERS)
        req = Request(url, headers=hd)
        try:
            with overseas_urlopen(req, timeout=timeout) as r:
                body = r.read()
                encoding = r.headers.get("Content-Encoding", "").strip().lower()
                # Some RSS responses contain gzip bytes without a Content-Encoding header.
                if encoding == "gzip" or body.startswith(b"\x1f\x8b"):
                    try:
                        return gzip.decompress(body)
                    except (OSError, EOFError, ZlibError) as exc:
                        raise FetchError(f"gzip 解压失败 {url}: {exc}") from exc
                return body
        except HTTPError as e:
            last = f"HTTP {e.code}"
            last_code = e.code
            if e.code == 429:
                ra = (e.headers or {}).get("Retry-After")
                wait = float(ra) if (ra or "").isdigit() else RETRY_BASE * (2 ** i)
                log(f"    · 429 限流，等 {wait:.1f}s")
                time.sleep(min(wait, 60))
                continue
            if 400 <= e.code < 500:
                final_url = e.geturl()
                address = f"{url} → {final_url}" if final_url and final_url != url else url
                raise FetchError(f"{last} {address}",
                                 permanent=e.code in _PERMANENT_HTTP,
                                 code=e.code) from e
        except RouteConfigError:
            # 路由未配置/互斥配置不是网络抖动，不做 4 次无意义重试。
            raise
        except (URLError, TimeoutError, OSError) as e:
            last = f"{type(e).__name__}: {e}"
        if i < retries - 1:
            time.sleep(RETRY_BASE * (2 ** i))
    raise FetchError(f"{last} {url}", code=last_code)


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", "replace")


def fetch_list(cfg: dict) -> str:
    options = {
        "browser_headers": bool(cfg.get("browser_headers")),
        "ua": cfg.get("ua"),
    }
    fallback = cfg.get("fallback_feed")
    if not fallback:
        return fetch_text(cfg["feed"], **options)

    # Leave time for the alternate feed when the primary endpoint stalls.
    try:
        return fetch_text(cfg["feed"], timeout=15, retries=2, **options)
    except FetchError as exc:
        if exc.code is not None and not 500 <= exc.code <= 599:
            raise
        log(f"  {cfg['name']}: 主订阅源请求失败（{exc}），尝试备用订阅源")
        try:
            return fetch_text(fallback, timeout=15, retries=2, **options)
        except FetchError as fallback_error:
            raise FetchError(
                f"主订阅源失败（{exc}），备用订阅源失败（{fallback_error}）",
                code=fallback_error.code,
            ) from fallback_error


# ---------------------------------------------------------------- feed 解析
def _inner(seg: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", seg, re.S | re.I)
    if not m:
        return None
    v = m.group(1).strip()
    c = re.match(r"^<!\[CDATA\[(.*)\]\]>$", v, re.S)
    return c.group(1) if c else v


def _unescape_if_escaped_html(v: str | None) -> str | None:
    """feed 里的正文可能是**转义存放**的 HTML，需要解一层。

    🔴 2026-08-25 实测：blogspot（Damodaran）的 Atom `<content type="html">`
    正文里有 **640 个 `&lt;`、零个 `<`** —— 整篇文章的标签全是转义的。
    不解转义的后果是下游 `html_body_to_text_with_anchors()` 一个标签也匹不到：
    图全丢、段落全平，而且 `_TAG.sub()` 之后才 unescape ⇒ **最终 text 为空**，
    全程零报错、日志还写着「正文 3 / 空 0」。

    ⭕ 判据不能是「含 `&lt;`」—— 正常 HTML 正文里也可能引用代码而带少量 `&lt;`。
    用**比例**：真标签数为 0 而转义标签数 > 0 才解，且只解一层。
    这样既修了 blogspot，又不会把正常站的代码示例误解成标签（那会把
    用户写的 `&lt;script&gt;` 变成真的 script 标签再被剔掉，丢内容）。
    """
    if not v:
        return v
    real = len(re.findall(r"<[a-zA-Z/!]", v))
    esc = v.count("&lt;")
    if real == 0 and esc > 0:
        import html as _html
        return _html.unescape(v)
    return v


def parse_feed(txt: str) -> list[dict]:
    """RSS 2.0 / Atom → [{title, link, date, author, body_html, guid}]。

    只认这两种。**不引入 feedparser** —— 标准库正则够用，且这里的
    失败模式（拿不到条目）会立刻被条数断言抓到，不是静默的。
    """
    items = re.findall(r"<item\b.*?</item>", txt, re.S | re.I)
    kind = "rss"
    if not items:
        items = re.findall(r"<entry\b.*?</entry>", txt, re.S | re.I)
        kind = "atom"
    out = []
    for seg in items:
        title = plain_html(_inner(seg, "title"))
        if kind == "rss":
            link = (_inner(seg, "link") or "").strip()
            date = _inner(seg, "pubDate") or _inner(seg, "dc:date")
        else:
            m = re.search(r'<link[^>]+href=["\']([^"\']+)["\']', seg)
            link = m.group(1).strip() if m else ""
            date = _inner(seg, "published")
            if not os.getenv("CORPUS_TARGET_DATE"):
                date = date or _inner(seg, "updated")
        # 全文字段：content:encoded（RSS 惯例）> content（Atom）> description
        # 🔴 过 `_unescape_if_escaped_html`：blogspot 的正文是转义存放的（实测）
        body = _unescape_if_escaped_html(
            _inner(seg, "content:encoded") or _inner(seg, "content")
            or _inner(seg, "description"))
        author = (plain_html(_inner(seg, "dc:creator"))
                  or plain_html(_inner(seg, "author")) or None)
        guid = (_inner(seg, "guid") or _inner(seg, "id") or link or title)
        iso, epoch = parse_date(date)
        if not link and not title:
            continue
        out.append({"title": title, "link": link, "date": iso,
                    "epoch": epoch, "author": author,
                    "body_html": body, "guid": (guid or "").strip()})
    return out


def item_id(guid: str) -> str:
    """条目稳定 id：guid 的 sha1 前 16 位。

    🔴 用 guid 而不是 URL：部分站的 guid 是 `?p=12345` 形式，URL 会随
    slug 改写而变，guid 不变 ⇒ 改标题不会造出重复条目。
    """
    return hashlib.sha1((guid or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 正文提取
def extract_article(html: str, url: str) -> dict:
    """trafilatura 提取正文（markdown，含图）+ 元数据。

    🔴 用 `output_format="markdown", include_images=True`：
    图以 `![alt](url)` 出现，我们随后把它换成 `[图N]` 锚点 ——
    与 Substack 侧同一套锚点机制，前端渲染代码可直接复用。
    纯文本模式会把图**静默丢掉**，那样正文里连"这儿本来有张图"都看不出来。
    """
    import trafilatura
    md = trafilatura.extract(html, url=url, favor_recall=True,
                             include_comments=False, include_tables=True,
                             include_images=True, output_format="markdown")
    meta = trafilatura.extract_metadata(html, default_url=url)
    # 🔴 在这里（数据入口）把图 src 规范成可抓的绝对地址，一次搞定。
    #    Anthropic 走 Next.js 图片优化器（`/_next/image?url=<编码地址>`）——
    #    相对路径 + 代理包装，不处理就是 12 个锚点全部永久“未获取”，
    #    而下载器只会看到“引用图 0 张”，**一句警告都不会给**（静默失败）。
    #    在边界规范化一次，下游三个消费者（锚点/需求清单/台账键）全都干净。
    md = wm.absolutize_md_images(md or "", url)
    return {
        "md": md or "",
        "meta_title": (meta.title if meta else None),
        "meta_author": (meta.author if meta else None),
        "meta_date": (page_publication_date(html) if os.getenv("CORPUS_TARGET_DATE")
                      else (meta.date if meta else None)),
        "meta_sitename": (meta.sitename if meta else None),
    }


_MD_LINK_ONLY = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def md_to_text_with_anchors(md: str) -> tuple[str, list[dict]]:
    """markdown → (纯文本带 `[图N]` 锚点, 图列表)。

    🔴 序号与返回列表的下标必须一一对应 —— 同 Substack 侧那条铁律。
    此处用 `wm.MD_IMG_RE`（**唯一定义源**），与下载器 `wanted()` 同一个正则。

    ⚠️ 锚点数**所有**图（含 `data:` 内联图这种不可抓的），
    而下载清单只要可抓的 ⇒ 不可抓的那几张在前端显示「未获取」。
    这是故意的：“这儿本来有张图”对读者有意义，静默丢掉比标出更差。
    """
    imgs: list[dict] = []

    def _rep(m):
        imgs.append({"src": m.group(2), "width": None, "height": None,
                     "alt": (m.group(1) or None)})
        return " " + wm.ANCHOR_FMT.format(n=len(imgs)) + " "

    t = wm.MD_IMG_RE.sub(_rep, md or "")
    # markdown 其余标记：链接保留文字、去掉标题井号与强调符
    t = _MD_LINK_ONLY.sub(r"\1", t)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_`]{1,3}", "", t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip(), imgs


def html_body_to_text_with_anchors(html: str) -> tuple[str, list[dict]]:
    """feed 自带的 HTML 全文 → (文本带锚点, 图列表)。

    复用 `web_media.parse_imgs` 保证图列表与锚点序号同源 ——
    先按文档顺序取图，再按同一个正则替换成锚点。
    """
    imgs = wm.parse_imgs(html)
    n = [0]

    def _img(m):
        tag = m.group(0)
        if not re.search(r"""\bsrc\s*=\s*(?:"[^"]*"|'[^']*')""", tag, re.I):
            return " "
        n[0] += 1
        return " " + wm.ANCHOR_FMT.format(n=n[0]) + " "

    t = re.sub(r"<(script|style)\b.*?</\1>", " ", html or "", flags=re.S | re.I)
    t = re.sub(r"<img\b[^>]*>", _img, t, flags=re.I)
    t = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote|figure|figcaption|section|table)\s*>",
               "\n", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    import html as _html
    t = _html.unescape(t)
    # 🔴 unescape 可能**新鲜出土一批标签** ⇒ 必须再剔一轮（2026-08-25 实测）
    #    blogspot 的 Atom 正文是双层转义的：`&amp;lt;p&amp;gt;` → 上游解一层得
    #    `&lt;p&gt;` → 这里 unescape 又得 `<p>`。而 `_TAG.sub()` 已经跑过了，
    #    于是 **裸 HTML 直接当正文入库** —— 42 KB 里满是 `<p style=...>`，
    #    而日志写着「正文 3 / 空 0」、chars 看着也很健康 ⇒ 完全静默。
    #    ⭕ 顺序不能反（先 unescape 再 sub 会把代码示例里的 `&lt;div&gt;` 剔成空白），
    #    所以是「剔 → 解 → 再剔」，且第二轮只在真的冲出标签时才动。
    if re.search(r"<[a-zA-Z/][^>]*>", t):
        t = re.sub(r"<img\b[^>]*>", _img, t, flags=re.I)
        t = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote|figure|figcaption|section|table)\s*>",
                   "\n", t, flags=re.I)
        t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
        t = _TAG.sub(" ", t)
        t = _html.unescape(t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n\s*\n+", "\n\n", t)
    return t.strip(), imgs


# 脏正文判据的两个正则 + 阈值（唯一定义源，is_dirty_text 与测试共用）
_ANY_TAG_RE = re.compile(r"<[a-zA-Z/][a-zA-Z0-9]*(?:\s[^>]*)?>")
# 只有**结构标签**算「真 HTML 遗留」的证据。
# 🔴 名单里故意不包含 `<tt>` / `<redacted>` / `<think>` 这类 ——
#    它们在正文里出现是合法的（代码排版 / 脱敏占位符）。
_STRUCT_TAG_RE = re.compile(
    r"</?(?:p|div|span|br|h[1-6]|li|ul|ol|table|tr|td|th"
    r"|figure|section|article|blockquote|body|html|head)\b[^>]*>", re.I)
# 标记字符占比阈值（实测标定：真脏 23.6% vs 干净 ≤ 7.1%）
_DIRTY_MARKUP_FRAC = 0.15


def is_dirty_text(text: str) -> bool:
    """正文是否「残留 HTML」—— 判据是**标记字符占比**，不是标签个数/密度。

    🔴 为何必须是独立函数而不是写在 collect 里的一行（2026-08-25）
    ------------------------------------------------------------------
    它最初是内联在 `collect_text()` 里的，回归测试只能「复刻一份相同逻辑」来验。
    注入验证当场拆穿：把生产判据改成 `n_tags >= 1`（会把正常站全判脏），
    测试**仍然全绿** —— 因为它测的是自己那份副本。
    ⇒ 判据必须只有**一份实现**，两边共用。
    （同族于 08-20 SCRIPT_ORDER：看着解耦、语义上依赖照旧。）

    🔴 为何从「标签密度」改成「标记字符占比」（2026-08-25 修正）
    ------------------------------------------------------------------
    旧判据（≥ 5 个标签 且 每千字符 ≥ 1 个）制造了 **3/3 全是假阳性**：
      · `openai/2163b413` —— 邮件里 11 个 `<redacted>` 脱敏占位符（根本不是 HTML）
      · `qwen/4873a6a8`  —— 正文里 5 对 `<tt>` 代码排版
      · `qwen/b9416ccc`  —— 讲「QwenVL HTML 文档解析格式」，合法展示 HTML 示例
    后果不是「多一盏黄灯」，而是**三篇合法文章被隔离出语料库**（不得入库当正文）。

    ⭕ 拿真脏样本做了量化标定（重现 damodaran 双层转义漏剔的那 33 KB）：

      | 样本 | chars | 标签数 | 结构标签 | 种类 | **markup%** |
      |---|---|---|---|---|---|
      | 【真脏】双层转义漏剔 | 33,139 | 258 | 178 | 11 | **23.6%** |
      | 【干净】qwen/b9416ccc | 11,154 | 40 | 34 | 6 | 7.1% |
      | 【干净】qwen/4873a6a8 | 7,310 | 10 | 0 | 0 | 0.6% |
      | 【干净】openai/2163b413 | 6,322 | 11 | 0 | 0 | 1.7% |

    只有 markup%（标记字符占全文比例）有**量级差**：23.6% vs ≤ 7.1%。
    它比「标签个数」稳：个数会随文章变长而涨，占比不会。
    阈值取 **15%**（落在 7.1 与 23.6 之间，两边各留 2 倍左右余量），
    并要求 ≥ 5 个结构标签（`<p>`/`<div>` 这类）—— `<redacted>`/`<tt>`
    不属结构标签，于是两个假阳性在第一道门就被挡住。

    ⚠️ 不要一刀切「含尖括号就脏」：技术文章正文里 `a < b` / `<think>` /
       HTML 代码示例都是正常的，那样又是一盏每天必亮的灯，
       而人会学会忽略它。
    """
    if not text:
        return False
    tags = _ANY_TAG_RE.findall(text)
    if not tags:
        return False
    # 第一道门：必须有足够多的**结构标签**。
    # `<redacted>` / `<tt>` / `<think>` 不在名单里 ⇒ 假阳性当场挡下。
    if len(_STRUCT_TAG_RE.findall(text)) < 5:
        return False
    # 第二道门：标记字符占比（实测真脏 23.6% vs 干净 ≤ 7.1%）
    return sum(len(x) for x in tags) / len(text) >= _DIRTY_MARKUP_FRAC


def is_dead_burst(n_dead: int, n_todo: int) -> bool:
    """本轮的成批 404/410 到底是「一堆死链」还是「站点改版」？

    🔴 墩碑机制本质上是「许可静默失败」，而那正是最危险的一类错误。
    若站方改版、换了 URL 规则，征状同样是成批 404 ⇒ 若无条件写墩碑，
    一轮之内整站文章就被永久埋掉，而日志只有几行 ✗、巡检全绿。

    ⭕ 判据：死链 **≥ 3 条** 且 **占本轮待抓量 > 50%** ⇒ 当作站点改版。
    两个门必须**同时**成立：
      · 只看比例 ⇒ `1/1`（OpenAI 那条草稿）会被误判成改版
      · 只看数量 ⇒ `3/100` 会被误判成改版（数量阈值是弱守护，
        它不会随站的体量伸缩）

    🔴 必须是**独立函数**而不是写在 collect_site 里的一行：
    否则回归测试只能「复刻一份相同逻辑」来验 ⇒ 生产判据改坏了测试照样全绿。
    （同 `is_dirty_text()` 那条：判据只能有一份实现，两边共用。）
    """
    return n_dead >= 3 and n_todo > 0 and n_dead > n_todo * 0.5


def normalize_article_url(url: str, cfg: dict) -> str:
    """文章页 URL 规范化。

    🔴 `trailing_slash: true` 的由来（2026-08-25 实测）
    ------------------------------------------------------------------
    OpenAI 的 RSS 里 link 写的是 `openai.com/index/<slug>`（无尾斜杠），
    直接抓回 **403 Forbidden**；补上 `/` 后 200、正文 10,153 字符。

    ⚠️ 我一开始把这个 403 当成了 Cloudflare 级封锁，差点把 OpenAI 删掉。
    真因是重定向：带完整浏览器头请求时它回 **308 Permanent Redirect**，
    而只带 UA 时直接回 403 —— **同一个原因两种表现**。
    ⇒ 通则：4xx 不要直接归因为「被封」，先把 URL 形式试到位。
    """
    if not url:
        return url
    if cfg.get("trailing_slash") and "?" not in url and "#" not in url:
        if not url.endswith("/"):
            return url + "/"
    return url


# ---------------------------------------------------------------- 采集一个站
def collect_site(cfg: dict, *, redo: bool, limit_items: int | None,
                 redo_dead: bool = False) -> dict:
    key, name, feed_url = cfg["key"], cfg["name"], cfg["feed"]
    full_in_feed = bool(cfg.get("full_in_feed"))
    is_html_list = (cfg.get("kind") == "html_list")
    store = wm.SiteStore(key)
    site = store.site

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
        entry_kind = "HTML 列表页" if is_html_list else "订阅源"
        try:
            if key == "web:stratechery" and not PROXY_URL:
                overseas_opener()  # Require an explicitly configured direct route.
                log(f"  {name}: 使用无头 Firefox 下载订阅文件")
                txt = stratechery.download_feed(feed_url)
            else:
                txt = fetch_list(cfg)
        except FetchError as exc:
            raise FetchError(f"{entry_kind}请求失败：{exc}", code=exc.code) from exc
        except (OSError, RuntimeError) as exc:
            raise FetchError(f"无头 Firefox 下载失败：{exc}") from exc
        parser = _HTML_LIST_PARSERS.get(key)
        if is_html_list and parser:
            items = parser(txt, feed_url)
        elif is_html_list:
            items = parse_html_list(txt, feed_url, path_prefix=cfg.get("list_path_prefix"),
                                    exclude_paths=cfg.get("list_exclude_paths", ()))
        else:
            items = parse_feed(txt)
        if limit_items is not None:
            items = items[:limit_items]
        # 🔴 零条目当失败而不是「没新内容」：feed 改版/被 WAF 挡都长这样，
        #    静默当成 0 新增会让一个源无声无息地死掉几个月。
        if not items:
            raise FetchError(f"{name}: {entry_kind}解析到 0 条"
                             f" bytes={len(txt)}")
        for it in items:
            it["iid"] = item_id(it["guid"])
        wm.write_jsonl(store.base / "index.jsonl", items)
        log(f"  {name}: 清单 {len(items)} 条")

        todo = []
        dead = dict(st.get("dead") or {})
        n_dead_skip = 0
        for it in items:
            if not candidate(it.get("date")):
                date_rejection(it.get("title"), it.get("link"), it.get("date"))
                continue
            f = store.base / "items" / f"{it['iid']}.json"
            if f.exists() and not redo:
                continue
            # 🔴 墩碑：已判定永久死链的不再重试（`--redo-dead` 可强制重试）
            if it["iid"] in dead and not redo_dead:
                n_dead_skip += 1
                continue
            todo.append(it)
        have = len(list((store.base / "items").glob("*.json")))
        log(f"  {name}: 待抓 {len(todo)} 条（本地已有 {have}"
            + (f"，跳过死链 {n_dead_skip}" if n_dead_skip else "") + "）")

        ok = fail = 0
        new_dead: dict[str, dict] = {}
        got: list[str] = []
        for i, it in enumerate(todo, 1):
            rec = {
                "iid": it["iid"], "source_key": key,
                "title": it["title"], "url": it["link"],
                "date": it["date"], "guid": it["guid"],
                "author": it.get("author"),
                "date_basis": "page_meta",
                "_aris": {"fetched_at": datetime.now().astimezone().isoformat()},
            }
            if it["date"]:
                rec["date_basis"] = "site_index" if is_html_list else "feed_pubdate"
            try:
                if full_in_feed and it.get("body_html") and (
                    not os.getenv("CORPUS_TARGET_DATE") or publication_day(rec["date"]) is not None
                ):
                    rec["body_html"] = it["body_html"]
                    rec["text_basis"] = "feed_full"
                else:
                    if not it["link"]:
                        raise FetchError("条目无链接且 feed 未带全文")
                    art_url = normalize_article_url(it["link"], cfg)
                    page = fetch_text(art_url,
                                      browser_headers=bool(cfg.get("browser_headers")),
                                      ua=cfg.get("ua"))
                    ext = extract_article(page, art_url)
                    rec["md"] = ext["md"]
                    rec["text_basis"] = "trafilatura"
                    rec["page_meta"] = {
                        "title": ext["meta_title"], "author": ext["meta_author"],
                        "date": ext["meta_date"], "sitename": ext["meta_sitename"],
                    }
                    # 🔴 日期以 feed 为准；页面日期只记进来备查（实测 trafilatura
                    #    对 Google Research 抽错 10 天）。仅当 feed 无日期时才用它。
                    if publication_day(rec["date"]) is None and ext["meta_date"]:
                        d, ep = parse_date(str(ext["meta_date"]))
                        rec["date"] = d or rec["date"]
            except (FetchError, OSError, ValueError) as e:
                reason = f"文章页请求失败：{e}" if isinstance(e, FetchError) else str(e)
                record("failed", it.get("title"), it.get("link"), rec.get("date"), reason)
                log(f"    ✗ {it['title'][:50]}: {reason}")
                if getattr(e, "permanent", False):
                    if os.getenv("CORPUS_TARGET_DATE"):
                        fail += 1
                    # 永久死链：记墩碑，不计入 fail ⇒ 不冻结水位线
                    new_dead[it["iid"]] = {
                        "url": it["link"], "title": it["title"],
                        "code": getattr(e, "code", None),
                        "at": datetime.now().astimezone().isoformat(),
                    }
                else:
                    fail += 1
                continue
            if not matches(rec.get("date")):
                if date_rejection(rec.get("title"), rec.get("url"), rec.get("date")):
                    fail += 1
                    log(f"    ✗ 无法确定发布日期，未保存: {it['link']}")
                continue
            try:
                wm.write_json(store.base / "items" / f"{it['iid']}.json", rec)
            except OSError as e:
                record("failed", rec.get("title"), rec.get("url"), rec.get("date"), str(e))
                raise
            record("fetched", rec.get("title"), rec.get("url"), rec.get("date"))
            ok += 1
            # 重试成功就掴墩碑（站方可能把文章恢复了）——
            # 不清的后果是此后永远跳过它，而文件已存在，矛盾且难查。
            dead.pop(it["iid"], None)
            got.append(it["iid"])
            if i % 20 == 0:
                log(f"    …{i}/{len(todo)}")
            time.sleep(0.4 if not full_in_feed else 0.05)
    except (FetchError, RouteConfigError) as e:
        record_run(site, {"mode": "failed", "index": 0, "fetched": 0,
                          "failed": 1, "error": f"{type(e).__name__}: {e}"})
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        log(f"  ✗ {name}: {e}")
        return {"site": site, "key": key, "name": name, "n_index": 0,
                "n_fetched": 0, "n_failed": 1, "error": str(e), "got": []}
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except (ValueError, OSError):
            pass

    newest = max((it["date"] or "") for it in items) if items else ""

    # ════════════════════════════════════════════════
    # 🔴 墩碑的防滥用门：「整站改版」不得被当成「一堆死链」
    # ════════════════════════════════════════════════
    # 墩碑机制本质上是「许可静默失败」，而那正是最危险的一类错误。
    # 若站方改版、改了 URL 规则，征状同样是成批 404 ⇒ 若无条件写墩碑，
    # 一轮之内整站文章就被永久埋掉，而日志只有几行 ✗、巡检全绿。
    # ⭕ 判据：死链占本轮待抓量过半且 ≥ 3 条 ⇒ 这不是死链，是站点出事了。
    #    此时一律**按真失败计（冻结水位线）+ 不写墩碑**，让它响亮地坏。
    #    （数量阈值是弱守护，比例才能随站的体量伸缩；两者同时要求。）
    n_todo = len(todo)
    dead_burst = is_dead_burst(len(new_dead), n_todo)
    if dead_burst:
        log(f"  🔴 {name}: 本轮 {len(new_dead)}/{n_todo} 条回 404/410 —— "
            f"超过半数，当作**站点改版**而非死链：不写墩碑、按失败计、水位线不推进")
        if not os.getenv("CORPUS_TARGET_DATE"):
            fail += len(new_dead)
        new_dead = {}
    elif new_dead:
        # 墩碑必须喊出来一次（之后每轮只会显示「跳过死链 N」）。
        for iid, d in new_dead.items():
            log(f"  🪦 {name}: 记墩碑 HTTP {d['code']} 《{(d['title'] or '')[:40]}》"
                f" —— 不再重试，水位线不再因它冻结（--redo-dead 可强试）")
        dead.update(new_dead)
    if dead:
        st["dead"] = dead
    else:
        # 墩碑全部复活（或 dead_burst 判定后清空）⇒ 把键拿掉，
        # 否则 state 里留个 `"dead": {}` 会让人以为还有死链。
        st.pop("dead", None)

    now_iso = datetime.now().astimezone().isoformat()
    st["last_run_at"] = now_iso
    st["last_item_date"] = newest if fail == 0 else st.get("last_item_date")
    if fail == 0:
        st["last_ok_at"] = now_iso
        st.pop("last_error", None)
    else:
        st["last_error"] = f"{fail} 条抓取失败，水位线未推进"
    if fail:
        log(f"  ⚠️ {name} 有 {fail} 条失败，水位线不推进")
    st["n_index"] = len(items)
    hist = st.get("history", [])
    hist.append({"at": st["last_run_at"], "index": len(items),
                 "fetched": ok, "failed": fail, "dead": len(dead),
                 "secs": round(time.time() - t0, 1)})
    st["history"] = hist[-200:]
    save_state(site, st)
    wm.write_json(store.base / "meta.json", {
        "site": site, "key": key, "name": name, "channel": "web",
        "feed": feed_url, "full_in_feed": full_in_feed,
        "kind": cfg.get("kind") or "rss",
        "note": cfg.get("note"), "n_index": len(items),
        "updated_at": datetime.now().astimezone().isoformat(),
    })
    return {"site": site, "key": key, "name": name, "n_index": len(items),
            "n_fetched": ok, "n_failed": fail, "n_dead": len(dead),
            "n_dead_new": len(new_dead), "got": got,
            "secs": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 抽取
def extract_site(cfg: dict) -> dict:
    key, name = cfg["key"], cfg["name"]
    store = wm.SiteStore(key)
    out_dir = store.base / "text"
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger = store.load_ledger()

    rows = []
    for f in sorted((store.base / "items").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        if d.get("text_basis") == "feed_full":
            text, imgs = html_body_to_text_with_anchors(d.get("body_html") or "")
        else:
            text, imgs = md_to_text_with_anchors(d.get("md") or "")
        images = store.resolve_images(imgs, ledger)
        # 🔴 RSS 源没有付费墙概念（都是公开内容）⇒ 只有 ready / no_text 两态。
        #    别为了「跟 Substack 一致」硬造一个 preview —— 那是无中生有的状态。
        state = "ready" if text else "no_text"
        # 🔴 脏正文守卫（2026-08-25 新增）—— 「有字符」不等于「是正文」。
        #    Damodaran 那次：42 KB 全是 `<p style=...>` 裸标签，而 state=ready、
        #    chars 看着很健康、日志写「正文 3 / 空 0」⇒ **全链路零信号**。
        #    判据在 `is_dirty_text()`（唯一实现，回归测试直接调它）。
        if is_dirty_text(text):
            state = "dirty_html"
        name_txt = f"{d['iid']}.txt"
        (out_dir / name_txt).write_text(text, encoding="utf-8")
        pm = d.get("page_meta") or {}
        authors = [a for a in [d.get("author") or pm.get("author")] if a]
        rows.append({
            "source_key": key, "site": store.site,
            "item_id": d["iid"], "post_id": d["iid"],
            "slug": None,
            "title": d.get("title") or pm.get("title"),
            "subtitle": None,
            "post_date": d.get("date"),
            "updated_at": None,
            "url": d.get("url"),
            "post_type": "newsletter",
            "authors": authors,
            "audience": "everyone",
            "wordcount_upstream": None,
            "words_local": len(re.findall(r"[A-Za-z0-9\u2019'\-]+",
                                          wm.ANCHOR_RE.sub(" ", text))),
            "coverage": None,
            "is_preview": False,
            "content_state": state,
            "preview_basis": "public_rss",
            "date_basis": d.get("date_basis"),
            "text_basis": d.get("text_basis"),
            "chars": len(text),
            "text_path": store.text_rel(name_txt),
            "images": images, "n_images": len(images),
            "n_images_stored": sum(1 for x in images if x.get("file_key")),
            "reactions": None, "comments": None,
            "raw_ref": f"web/{store.site}/items/{f.name}",
        })
    rows.sort(key=lambda r: (r.get("post_date") or "", str(r.get("item_id"))))
    wm.write_jsonl(out_dir / "index.jsonl", rows)
    n_none = sum(1 for r in rows if r["content_state"] == "no_text")
    n_dirty = sum(1 for r in rows if r["content_state"] == "dirty_html")
    n_img = sum(r["n_images"] for r in rows)
    n_loc = sum(r["n_images_stored"] for r in rows)
    log(f"  {name}: 抽正文 {len(rows)} 条（正文 {len(rows) - n_none - n_dirty} / "
        f"空 {n_none} / 脏 {n_dirty}）· {sum(r['chars'] for r in rows):,} 字符")
    if n_dirty:
        # 🔴 脏正文必须喊出来：它不会让任何环节报错，不喊就永远没人知道。
        log(f"  🔴 {name}: {n_dirty} 条正文残留 HTML 标签（feed 转义层数不对？）"
            f" —— 已标 content_state=dirty_html，不得入库当正文")
    if n_img:
        log(f"  {name}: 图片锚点 {n_img:,} 处，已存对象存储 {n_loc:,} "
            f"({n_loc / n_img * 100:.1f}%)")
    return {"n": len(rows), "n_no_text": n_none,
            "n_images": n_img, "n_images_stored": n_loc}






# ---------------------------------------------------------------- CLI
def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from run import main as pipeline_main
    from aris.kb_adapters import sources
    defaults = [] if '--source' in sys.argv else [v for s in sources() if s['_seg'] == 'rss' for v in ('--source', s['key'])]
    return pipeline_main(['collect', *defaults, *sys.argv[1:]])

if __name__ == '__main__':
    raise SystemExit(main())
