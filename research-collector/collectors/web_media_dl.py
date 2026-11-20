"""Shared image discovery and bounded HTTP download; XSKY orchestration is image_ingest."""
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from aris.net_route import overseas_urlopen
from aris import web_media as wm
UA = 'Mozilla/5.0 (compatible; CorpusCollector/1.0)'
MAX_BYTES = 40 * 1024 * 1024


def wanted(store: wm.SiteStore) -> dict[str, dict]:
    """扫该站原始 JSON → {语义键: {cands, w, h, refs}}。

    `refs` = 被引用次数，用于排序（多篇引用的图先下），也让 `--limit` 抽样
    优先覆盖高价值图。

    ⚠️ 数据源是 `posts/*.json`（Substack）与 `items/*.json`（RSS）里的
    **原始内容**，不是 `text/index.jsonl`。后者存的是已解析结果，
    拿它当需求源会形成「图没下→锚点为空→需求清单为空」的死锁。

    🔴 **三种承载图的字段，缺一个就有一整类图永远下不到**：

    | 字段 | 来源 | 图的写法 |
    |---|---|---|
    | `body_html` | Substack API / feed 自带全文 | `<img src=…>` |
    | `md` | trafilatura（markdown 模式） | `![alt](url)` |

    2026-08-24 实测踩到：只读 `body_html` 时 DeepMind/Anthropic 报
    「引用图 0 张」，而抽取阶段明明数出 20 / 12 个锚点 ⇒ 图永久停在
    「未获取」，且**下载器一句警告都不会给**（它认为本来就没有图）。
    ⭕ 判据：**下载器的需求清单必须与抽取器的锚点来源一一对应**。
       抽取器读了哪些字段，这里就必须读同样的字段。
    """
    out: dict[str, dict] = {}

    def add(src: str, width=None, height=None):
        k = wm.canonical_key(src)
        r = out.setdefault(k, {"cands": [], "w": None, "h": None, "refs": 0})
        r["refs"] += 1
        for c in wm.fetch_candidates(src):
            if c not in r["cands"]:
                r["cands"].append(c)
        r["w"] = r["w"] or width
        r["h"] = r["h"] or height

    def feed_html(html: str | None):
        for im in wm.parse_imgs(html):
            add(im["src"], im.get("width"), im.get("height"))

    def feed_md(md: str | None):
        # 🔴 走 web_media.md_image_urls —— 与抽取器同一个正则、同一套过滤规则。
        #    自己在这里再写一份就是两份可漂移的真相（见该函数注释）。
        for src in wm.md_image_urls(md):
            add(src)

    for f in sorted((store.base / "posts").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        feed_html(d.get("body_html"))
    for f in sorted((store.base / "items").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
        feed_html(d.get("body_html"))
        feed_md(d.get("md"))
    return out


def _read_limited(r) -> bytes:
    buf = b""
    while len(buf) <= MAX_BYTES:
        chunk = r.read(65536)
        if not chunk:
            break
        buf += chunk
    if len(buf) > MAX_BYTES:
        raise ValueError(f"超过单图上限 {MAX_BYTES} 字节")
    return buf


def try_one(url: str, referer: str, timeout: float) -> tuple[bytes, str, str]:
    """返回 (body, content_type, via)；实际出口由统一网络配置决定。"""
    hdrs = {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8",
            "Referer": referer}
    try:
        with overseas_urlopen(Request(url, headers=hdrs), timeout=timeout) as r:
            return _read_limited(r), (r.headers.get("Content-Type") or ""), "configured"
    except HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}") from e
    except (URLError, TimeoutError, OSError, ValueError) as e:
        raise RuntimeError(f"{type(e).__name__}: {e}") from e
