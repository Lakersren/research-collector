#!/usr/bin/env python3
"""SemiAnalysis 图片：纯函数层（URL 规范化 / 候选 / 台账）。

## 为什么单独一个模块

`collectors/semianalysis.py` 的抽取阶段（`extract_texts`，**不联网**）和
`collectors/semianalysis_media.py` 的下载阶段（**联网**）必须用**同一套**
URL 规范化规则 —— 否则「下载时存成 A 键、抽取时按 B 键找」会静默配不上，
症状是正文里图全变「未获取」而盘上明明有文件。
⇒ 规则只能有一份，放在 lib 里被两边 import。

分层：本模块只做**纯计算 + 台账文件读写**，零网络。下载器负责网络与重试。

## 🔴 URL 三种形态（2026-08-24 全量实测 7,100 个图 URL）

| 形态 | 数量 | 直接抓 | 处置 |
|---|---|---|---|
| `substackcdn.com/image/fetch/$s_!xx!,w_1456,…/<urlencode 原址>` | 4,358 | ✅ 200 | 原样抓 |
| `i0.wp.com/semianalysis.com/wp-content/uploads/YYYY/MM/<name>?q` | 1,099 | ❌ **404** | 改写成原站 |
| 同上但 `<name>` 是 `https3A2F2F…`（**站方迁移损坏**） | 1,643 | ❌ 404 | 同上（文件真的以这怪名存在原站） |

### 三个坑，每个都实测过

1. **`i0.wp.com` 全线 404**（Jetpack Photon 代理）。正常名 8/8 失败、畸形名 8/8 失败；
   改写成 `https://semianalysis.com/<path>` 并**去掉 query** 后 8/8 + 8/8 成功。
   ⇒ 别看到 `?resize=1024` 就以为要保留参数，Photon 参数在原站是噪声。
2. **畸形名里内嵌的 s3 原址不可抓**：`https3A2F2F…` 还原出
   `substack-post-media.s3.amazonaws.com/...` 后实测 **403（7/10）** ——
   S3 桶不开放匿名读。⇒ 还原出的 URL **只当去重键，不当抓取地址**。
   （反过来「怪名文件在原站真实存在」倒是站方迁移时的意外之幸。）
3. **`substackcdn.com/image/fetch/` 不能自己拼**：路径里 `$s_!wijJ!` 是签名，
   拿原址自己拼 fetch URL 实测三种写法全 404 ⇒ 无法为 WP 的 s3 图借道 Substack CDN。
   只能靠「同一张图在 Substack 侧本来就有」这条（交集实测 1,023 张）。

## 🔴 去重键 ≠ 抓取地址

键取**语义上的那张图**（尽量还原到原始存储地址），抓取地址取**实测能通的**那个。
两者分开的收益：Substack 与 WP 交集 1,023 张只下一次；
且 WP 畸形图若在 Substack 侧已下过，直接命中，连试都不用试。

## 路由（独立部署版）

目标站能否直连由公司网络环境决定，不能沿用原服务器的测速结论。
本包由 `aris.net_route` 显式选择公司代理或经验证的直连出口；两者都未配置时，
真正的网络请求响亮失败。图片语义键与实际抓取地址仍然分离，失败写台账。
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import urllib.parse
from urllib.parse import quote
from pathlib import Path

from aris.paths import RAW

SITE = "semianalysis"
BASE = RAW / "web" / SITE
MEDIA_DIR = BASE / "media"
FILES_DIR = MEDIA_DIR / "files"
LEDGER = MEDIA_DIR / "index.jsonl"

# 正文里的图片锚点。🔴 带序号 —— 无序号的 `[图]` 无法把「第几张」对上文件。
ANCHOR_FMT = "[图{n}]"
ANCHOR_RE = re.compile(r"\[图(\d+)\]")

IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_SRC_RE = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_W_RE = re.compile(r"""\bwidth\s*=\s*(?:"(\d+)"|'(\d+)')""", re.I)
_H_RE = re.compile(r"""\bheight\s*=\s*(?:"(\d+)"|'(\d+)')""", re.I)

# substackcdn 的 fetch 包装：路径末段是 urlencode 过的原址
_CDN_INNER_RE = re.compile(r"/(https?%3A%2F%2F[^/]+)$", re.I)
# fetch 变换串里的宽度参数。🔴 实测签名 `$s_!xx!` 与宽度**无关**：
# 把 `w_56` 改成 `w_1456` 仍 200，且返回原始 1280×1280（56×56 的 6.6 KB → 50.8 KB）。
# ⇒ 必须规范化，否则「哪个宽度先被遇到」决定了永久留在盘上的是缩略图还是原图。
# 全量实测：4,354 个键用 w_1456，**3 个用 w_56**。只 3 张，但这是结构性缺陷 ——
# 站方任何时候改默认宽度都会放大它，而症状是「图能显示但糊到读不出数字」，
# 比缺图更难被发现。
_CDN_W_RE = re.compile(r"(?<=[,/])w_\d+")
CDN_TARGET_W = 1456      # 站方自己的最大档（实测 w_9999 也只回原图 1280，不会放大）
# Jetpack Photon 代理：i0/i1/i2.wp.com/<真实 host>/<path>?<photon 参数>
_PHOTON_RE = re.compile(r"^https?://i\d\.wp\.com/(.+)$", re.I)
# WP 迁移损坏：上传目录里的文件名本身是一整条被吃掉冒号斜杠的 URL
_MANGLED_RE = re.compile(r"/uploads/\d{4}/\d{2}/(https?3A2F2F.+)$", re.I)

# 允许的图片 MIME（含 avif —— 站方 2025 年起用它，实测 image/avif）
IMAGE_MIME_EXT = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
    "image/gif": "gif", "image/webp": "webp", "image/avif": "avif",
    "image/svg+xml": "svg", "image/bmp": "bmp", "image/tiff": "tiff",
}
# 魔术字节 → 扩展名。用于 Content-Type 缺失/撒谎时兜底（也用来拦「返回的是 HTML 错误页」）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"),
)


def _unesc(u: str) -> str:
    return _html.unescape((u or "").strip())


def parse_imgs(html: str | None) -> list[dict]:
    """HTML → 按**文档顺序**的图片列表 `[{src, width, height}]`。

    顺序即锚点序号的依据：`html_to_text()` 把 `<img>` 逐个替换成 `[图N]`，
    两边都按同一个正则的出现顺序走 ⇒ 序号与列表下标恒一致。
    """
    out = []
    for tag in IMG_TAG_RE.findall(html or ""):
        m = _SRC_RE.search(tag)
        src = _unesc((m.group(1) or m.group(2)) if m else "")
        if not src:
            continue                      # 无 src 的 img 不占锚点（下方 html_to_text 同步跳过）
        mw, mh = _W_RE.search(tag), _H_RE.search(tag)
        out.append({
            "src": src,
            "width": int(mw.group(1) or mw.group(2)) if mw else None,
            "height": int(mh.group(1) or mh.group(2)) if mh else None,
        })
    return out


def demangle(name_part: str) -> str | None:
    """`https3A2F2Fhost2Fpath` → `https://host/path`；不像就返回 None。"""
    s = name_part.replace("3A", ":").replace("2F", "/")
    return s if re.match(r"^https?://", s) else None


def canonical_key(src: str) -> str:
    """图 URL → **语义键**（同一张图跨站/跨篇必须得到同一个键）。

    只做「还原到更接近原始存储地址」的变换，不猜、不裁剪查询串以外的东西。
    """
    u = _unesc(src)
    u = u.split("#", 1)[0]

    # ① substackcdn fetch 包装 → 内层原址
    m = _CDN_INNER_RE.search(u)
    if m and "substackcdn.com/image/fetch" in u.lower():
        return urllib.parse.unquote(m.group(1))

    # ② Photon 代理 → 内层真实 host + path（去 photon 查询串）
    m = _PHOTON_RE.match(u)
    if m:
        inner = m.group(1).split("?", 1)[0]
        # ②b 迁移损坏名 → 还原成 s3 原址（**只作键**，抓取另有候选）
        mm = _MANGLED_RE.search("/" + inner)
        if mm:
            restored = demangle(mm.group(1))
            if restored:
                return restored
        return "https://" + inner

    # ③ 原站直链（含 .avif）/ 其他站：去掉查询串再当键
    return u.split("?", 1)[0]


def upscale_cdn(u: str) -> str:
    """substackcdn fetch URL 里的 `w_NNN` 统一抬到 `CDN_TARGET_W`。

    只动 substackcdn 的 fetch 路径 —— 别的站没这个语义，乱改会造出 404。

    🔴 「不会改到内层原址」靠的是 **`_CDN_W_RE` 要求 `w_` 前面是字面 `,` 或 `/`**，
    而内层原址是 percent-encoded（`%2F`）⇒ 结构上命中不了。
    全库实测：4,398 条 fetch URL，内层能被该正则命中的 **0 条**。

    下面的 partition 是额外一道保险（假如将来站方改成不编码内层 URL）。
    ⚠️ 它**在现有数据上不可差异化** —— 删了结果一模一样，
    所以回归测试里**没有也不应有**一条声称守它的断言（那会是永远绿的空壳）。
    真正被测试守住的是下面这个可验证的不变量：
    **升档前后 `canonical_key` 必须逐字相同（全库 4,398 条）。**
    """
    if "substackcdn.com/image/fetch" not in u.lower():
        return u
    head, sep, tail = u.partition("/https%3A%2F%2F")
    if not sep:
        return _CDN_W_RE.sub(f"w_{CDN_TARGET_W}", u)
    return _CDN_W_RE.sub(f"w_{CDN_TARGET_W}", head) + sep + tail


# Substack CDN 的 fetch 端点。**对 s3 源不校验签名** —— 2026-08-24 实测：
# 借用别张图的签名、完全不带签名、只留 `f_auto`，三种写法全 200。
# 用途：`bucketeer-*` 早期图床已改成私有桶（直连/代理都 403），
# 只有经 CDN 回源才拿得到。
CDN_FETCH_PREFIX = "https://substackcdn.com/image/fetch/"
CDN_XFORM = f"w_{CDN_TARGET_W},c_limit,f_auto,q_auto:good,fl_progressive:steep"

# 需要走 CDN 回源的私有 s3 源（直连必 403）
_PRIVATE_S3_RE = re.compile(
    r"^https?://(?:bucketeer-[0-9a-f-]+|substack-post-media)\.s3\.amazonaws\.com/",
    re.I)


def cdn_fetch_url(origin: str) -> str:
    """把一个原址包成 substackcdn fetch URL（不带签名）。"""
    return CDN_FETCH_PREFIX + CDN_XFORM + "/" + quote(origin, safe="")


def fetch_candidates(src: str) -> list[str]:
    """图 URL → **按实测成功率排序**的抓取候选。

    🔴 顺序有实测依据，别按「看着更原始」重排：
    `i0.wp.com` 全 404，改写原站 16/16 成功 ⇒ 原站在前、Photon 在后当兜底。
    畸形名**不还原**去抓 s3（403 7/10），s3 只作键。

    🔴 最后一道兜底：私有 s3 源走**无签名的 substackcdn fetch**。
    `bucketeer-*` 桶已统一 403（直连和代理都一样 ⇒ 不是路由问题），
    但 CDN 回源 200。2026-08-24 靠这条救回 6 张只在 WP 侧出现、
    因而从未走过 CDN 通道的图。
    """
    u = upscale_cdn(_unesc(src).split("#", 1)[0])
    out: list[str] = []

    m = _PHOTON_RE.match(u)
    if m:
        inner = m.group(1).split("?", 1)[0]
        out.append("https://" + inner)     # 原站/内层 host 直连（畸形怪名在原站真实存在）
        out.append(u)                      # Photon 原样兜底
    else:
        out.append(u)

    # 候选里出现私有 s3 直连址 ⇒ 补一条 CDN 回源址放到末尾。
    # 🔴 不能放前面：Substack 侧原文的 <img> 本来就是带签名的 CDN URL（直接可用），
    #    给它们重新包一层是多余动作。
    for c in list(out):
        if _PRIVATE_S3_RE.match(c):
            out.append(cdn_fetch_url(c))

    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def key_hash(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def file_rel(key: str, ext: str) -> str:
    """键 + 扩展名 → **相对 data/raw** 的落盘路径（仅用于读取旧缓存）。

    两级散列目录：单目录放 6,000+ 文件在 ext4 上没问题，但 `ls` 会卡人，
    且将来图片量只会涨。用前两位分 256 个子目录。
    """
    h = key_hash(key)
    return f"web/{SITE}/media/files/{h[:2]}/{h}.{ext}"


def sniff_ext(head: bytes, content_type: str | None) -> str | None:
    """魔术字节优先，Content-Type 兜底。都不认 → None（**当失败，不猜**）。

    🔴 先看字节再看头：CDN 会对错误页回 `Content-Type: image/*` 吗？
    实测没遇到，但反过来遇到过 —— 站方对 .avif 回 `image/avif`（表里有），
    而 webp/avif 无稳定魔术前缀 ⇒ 需要两条路互补。
    """
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return IMAGE_MIME_EXT.get(ct)


# ---------------------------------------------------------------- 台账
def load_ledger() -> dict[str, dict]:
    """`media/index.jsonl` → {key: row}。文件不存在 → 空（首轮）。"""
    out: dict[str, dict] = {}
    if not LEDGER.exists():
        return out
    with LEDGER.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError("图片台账 JSON 损坏") from e
            if r.get("key"):
                out[r["key"]] = r
    return out


def save_ledger(rows: dict[str, dict]) -> None:
    """覆盖写（原子替换）。台账是**全量投影**，天然幂等，不做追加去重。"""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for k in sorted(rows):
            f.write(json.dumps(rows[k], ensure_ascii=False) + "\n")
    tmp.replace(LEDGER)


def resolve_images(imgs: list[dict], ledger: dict[str, dict]) -> list[dict]:
    """图列表 + 台账 → 正文锚点用的紧凑清单。

    返回 `[{n, k, p, w, h}]`：`p` 为 None 表示**没拿到**（前端必须显示
    「未获取」而不是渲染破图 —— 假装有图比没图更坏）。
    键名故意短：这份要进 `doc.meta` JSON，581 篇 × 最多 30 张，省下的都是库体积。
    """
    out = []
    for i, im in enumerate(imgs, 1):
        k = canonical_key(im["src"])
        row = ledger.get(k) or {}
        ok = row.get("status") == "ok" and row.get("file")
        if ok:
            try:
                p = RAW / row["file"]
                ok = p.is_file() and p.stat().st_size > 0
            except OSError:
                ok = False
        out.append({
            "n": i, "k": k,
            "p": row.get("file") if ok else None,
            "file_key": row.get("file_key") if row.get("status") == "ok" else None,
            "w": im.get("width") or row.get("w"),
            "h": im.get("height") or row.get("h"),
        })
    return out
