#!/usr/bin/env python3
"""web_media —— 多站点图片存储的**站点作用域**层。

## 🔴 这个模块存在的唯一理由：把「站点」参数化，**不是**重写规则

URL 规范化 / 候选顺序 / 魔术字节嗅探这些规则在 `semi_media.py` 里，
2026-08-24 用 7,100 个真实 URL 实测出来的。本模块**直接 import 它们**，
一个字都不重新定义。

⚠️ 这条是 2026-08-20 `factor_ic.SCRIPT_ORDER` 事故的直接教训：
「为摆脱对 X 的依赖而新建的常量层，绝不能抄 X 的值填充」——
反过来同样成立：**为参数化站点而新建的层，绝不能抄 X 的规则实现**。
抄了就会出现「semi_media 修了 bug、web_media 还是旧规则」，
而症状是「A 站图正常、B 站图全是未获取」，极难定位。

⭕ 验收判据：改 `semi_media.CDN_TARGET_W`，本模块的行为必须跟着变
（`tests/web_sources.py` 有一条断言守这个）。

## 与 semi_media 的分工

| | semi_media | web_media（本模块） |
|---|---|---|
| URL → 语义键 / 抓取候选 / 嗅探扩展名 | **定义** | import |
| 台账路径、落盘路径 | 写死 `web/semianalysis/…` | **按站点参数化** |
| 适用范围 | 仅 SemiAnalysis（历史遗留） | 第二批全部境外源 |

🔴 **没有把 semianalysis 迁过来**：它的台账 6,077 行、正文索引已落库、
回归测试 107 项全绑在 `sm.BASE` 上。迁移收益是「代码更整齐」，
风险是动到一条每小时在跑且已验收的链路 —— 不值。
新源用新层，旧源不动，规则共用一份 ⇒ 三者可以同时成立。

## 台账按站点分开，不做跨站去重

六个 Substack 站的图各自独立（实测无交集：图 URL 里含站点专属的 s3 前缀）。
跨站共享一个台账只会让「重跑某站」变成「必须锁住所有站」。
（SemiAnalysis 内部两站共享台账是因为它们**同一份内容的两个镜像**，
交集实测 1,023 张 —— 那是真需求，这里不是。）
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from aris.paths import RAW
# 🔴 只 import，不重新定义。见模块头注释。
from aris.semi_media import (  # noqa: F401  (转出给采集器用，故意 re-export)
    ANCHOR_FMT,
    ANCHOR_RE,
    IMAGE_MIME_EXT,
    canonical_key,
    fetch_candidates,
    key_hash,
    parse_imgs,
    sniff_ext,
    upscale_cdn,
)

# markdown 图片模式。🔴 **定义在这里**，`collectors/web_rss_sites.py` 与
# `collectors/web_media_dl.py` 都从本模块 import。
# 一边把 `![alt](url)` 换成 `[图N]` 锚点，另一边把同一批 url 下下来 ——
# 两边各写一份正则就会在漂移时产生「锚点数与台账数对不上」，
# 症状是部分图永久“未获取”而且**不报错**。
# （同 2026-08-20 SCRIPT_ORDER 那条：让错误写法无法表达，比注释警告可靠。）
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def md_image_urls(md: str | None) -> list[str]:
    """markdown → 按文档顺序的**可抓取**图 URL。

    🔴 过滤规则也必须只有一份：只收 http(s)，丢掉 `data:` 内联图与相对路径。
    ⚠️ 但锚点编号用的是 **全部** `![]()` 出现位置（含被过滤掉的），
    因为“这儿本来有张图”这个事实对读者有意义 ——
    所以 `md_to_text_with_anchors` 不能拿本函数去算序号，
    它自己用 `MD_IMG_RE.sub` 逐个替换。两者分工明确：
      - 锚点序号：数**所有**图（含不可抓的）
      - 下载清单：只要**可抓的**
    不可抓的图在台账里永远无行 ⇒ `resolve_images` 给 `p=None`
    ⇒ 前端显示「未获取」。这是正确的：它确实未获取。

    ⚠️ 前提：md 已经过 `absolutize_md_images()` 处理（采集时做）。
    未处理的 md 里相对路径会在这里被丢掉 —— 这也是正确的（无法抓），
    但那意味着采集层漏了一步。
    """
    out = []
    for m in MD_IMG_RE.finditer(md or ""):
        src = (m.group(2) or "").strip()
        if src.lower().startswith(("http://", "https://")):
            out.append(src)
    return out


# Next.js 图片优化器：`/_next/image?url=<urlencode 真实地址>&w=1920&q=75`
# 🔴 必须拆到内层真实地址，理由与 Photon 代理那条一样：
#    ① 包装层带 `w=`/`q=` 参数 ⇒ 同一张图会因宽度不同而算成两个键
#    ② 相对路径本身不可抓，而内层已是绝对地址（www-cdn.anthropic.com/…）
_NEXT_IMG_RE = re.compile(r"^(?:https?://[^/]+)?/_next/image\?(.*)$", re.I)


def unwrap_proxy_src(src: str) -> str:
    """拆掉图片代理包装，还原成真实地址。不像包装就原样返回。

    目前只处理 Next.js。🔴 **不把 Photon/substackcdn 的拆包搬到这里** ——
    那两条在 `semi_media.canonical_key` 里，它们的内层地址**不一定可抓**
    （实测 s3 403）⇒ 那里的定位是“只作去重键”；
    而 Next.js 的内层地址既是键也是可抓地址，层次不同。
    混在一起会把两种语义摊平，下一个人必然改错。
    """
    m = _NEXT_IMG_RE.match((src or "").strip())
    if not m:
        return src
    qs = urllib.parse.parse_qs(m.group(1))
    inner = (qs.get("url") or [""])[0]
    if inner.lower().startswith(("http://", "https://")):
        return inner
    return src


def absolutize_md_images(md: str | None, base_url: str) -> str:
    """把 markdown 里的图 src 统一成**可抓的绝对地址**。采集时调一次。

    🔴 为什么在入口做而不是让下游各自处理：
    下游有三个消费者（抽取器算锚点、下载器算需求、resolve_images 算键），
    而只有采集时才知道 `base_url`。让下游处理 ⇒ 得把 base_url 一路传下去，
    而且三处各写一份就是三份可漂移的真相。
    ⭕ **在边界上规范化一次，库里存的就永远是干净值。**

    2026-08-24 实测踩到：Anthropic 的 12 个图锚点全部无法下载，
    因为 src 是 `/_next/image?url=…`（相对路径 + 代理包装）。
    而下载器**一句警告也不会给** —— 它只看到“引用图 0 张”，
    与“这篇本来没图”不可区分。静默失败，所以必须有回归测试守。
    """
    if not md:
        return md or ""

    def _rep(m):
        alt, src = m.group(1), (m.group(2) or "").strip()
        if not src.lower().startswith(("data:", "#")):
            src = unwrap_proxy_src(urllib.parse.urljoin(base_url, src))
            # 内层可能自己又是一层代理（实测未遇，但代价只是一次函数调用）
            src = unwrap_proxy_src(src)
        return f"![{alt}]({src})"

    return MD_IMG_RE.sub(_rep, md)


# 站点 key（`web:interconnects`）→ 目录名（`interconnects`）。
# 🔴 目录名不允许出现 `:` `/` `..` —— 它会拼进文件路径。
_SAFE_SITE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def site_dirname(source_key: str) -> str:
    """`web:interconnects` → `interconnects`。非法名直接抛，不做静默清洗。

    🔴 不清洗而是抛错：静默清洗会让 `web:a/b` 和 `web:a-b` 撞到同一个目录，
    两个站的原始数据互相覆盖，且没有任何报错。配置写错就该当场炸。
    """
    name = source_key.split(":", 1)[-1].strip().lower()
    if not _SAFE_SITE.match(name):
        raise ValueError(f"非法站点名 {source_key!r} → {name!r}："
                         "只允许小写字母/数字/下划线/短横，且首位为字母数字")
    return name


class SiteStore:
    """一个站点的落盘布局 + 台账读写。纯本地，零网络。

        data/raw/web/<site>/
            meta.json                  站点信息 + 水位线
            posts/<slug>.json          单篇原始 JSON（Substack）
            items/<hash>.json          单篇原始记录（RSS：feed 项 + 页面正文）
            index.jsonl                清单（覆盖写，天然幂等）
            text/index.jsonl           抽取后的正文索引 + 度量
            text/<name>.txt            正文
            media/index.jsonl          图片台账
            media/files/xx/<sha1>.ext  图片
    """

    def __init__(self, source_key: str, base: Path | None = None):
        """`base` 只为测试注入临时目录用。

        🔴 生产路径**永远**是 `RAW/web/<site>`：站点名经 `site_dirname` 校验，
        所以外部无法把数据写到别处。给 `base` 留口是为了让回归测试能在
        `tempfile` 里造夹具 —— 否则测试要么污染真实 `data/raw`，
        要么得 monkeypatch 模块级常量（那会漏到其他测试里，更脏）。
        """
        self.source_key = source_key
        self.site = site_dirname(source_key)
        self.base = base if base is not None else (RAW / "web" / self.site)
        self.ledger_path = self.base / "media" / "index.jsonl"

    # ---------------------------------------------------------------- 路径
    def file_rel(self, key: str, ext: str) -> str:
        """图片键 + 扩展名 → **相对 data/raw** 的路径（仅用于读取旧缓存）。

        两级散列目录（前两位分 256 桶）：与 semi_media 同一套布局，
        但站点段不同 ⇒ 各站文件互不干扰。
        """
        h = key_hash(key)
        return f"web/{self.site}/media/files/{h[:2]}/{h}.{ext}"

    def text_rel(self, name: str) -> str:
        return f"web/{self.site}/text/{name}"

    # ---------------------------------------------------------------- 台账
    def load_ledger(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not self.ledger_path.exists():
            return out
        with self.ledger_path.open(encoding="utf-8") as f:
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

    def save_ledger(self, rows: dict[str, dict]) -> None:
        """覆盖写 + 原子替换。台账是**全量投影**，不做追加去重。"""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for k in sorted(rows):
                f.write(json.dumps(rows[k], ensure_ascii=False) + "\n")
        tmp.replace(self.ledger_path)

    def ledger_stale_vs(self, text_index: Path) -> bool:
        """台账是否比正文索引新（→ 需重抽正文把新下的图接上）。

        与 `semianalysis.manifest_stale()` 同一语义。比 mtime 不比内容：
        误判代价只是多跑一次抽取（秒级），而算 hash 是每轮白花的 CPU。
        台账不存在 → False（从未下过图时重抽只会得到全部 p=None，无意义）。
        """
        if not self.ledger_path.exists() or not text_index.exists():
            return False
        try:
            return self.ledger_path.stat().st_mtime > text_index.stat().st_mtime
        except OSError:
            return False

    # ---------------------------------------------------------------- 锚点
    def resolve_images(self, imgs: list[dict],
                       ledger: dict[str, dict]) -> list[dict]:
        """图列表 + 台账 → 正文锚点清单 `[{n,k,p,w,h}]`。

        `p is None` 表示**没拿到** —— 前端必须显示「未获取」而不是渲染破图。
        （与 `semi_media.resolve_images` 同结构，前端那套渲染代码可直接复用；
        这里独立实现只因为它不依赖任何站点状态，且键名必须逐字一致。）
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


def image_needs_download(image: dict, ledger: dict[str, dict]) -> bool:
    """未尝试，或台账 ok 但物理文件缺失/为空。failed 需显式 --retry-failed。"""
    key = image.get("k")
    if not key:
        return False
    row = ledger.get(key)
    if not row:
        return True
    if row.get("status") != "ok":
        return False
    try:
        path = RAW / (row.get("file") or "")
        return not path.is_file() or path.stat().st_size == 0
    except OSError:
        return True


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def read_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def safe_slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "-", s or "")[:120]
    return s or "unnamed"
