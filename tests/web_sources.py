#!/usr/bin/env python3
"""境外 web 源（第二批）回归测试。

跑法：
    python3 tests/web_sources.py            # 离线网站解析回归
    python3 tests/web_sources.py --no-live  # 同上，兼容参数

## 🔴 这个文件存在的理由：本批次踩到的坑全是**静默**的

第二批调试中抓到两个真 bug，都不报错、退出码 0、日志看着正常：

| bug | 症状 | 谁会发现 |
|---|---|---|
| `wanted()` 只读 `body_html`，漏 `md` | DeepMind 20 个图锚点永远「未获取」 | 没人 —— 下载器报「引用图 0 张」，与「本来没图」不可区分 |
| Anthropic 的 `/_next/image?url=…` 未解包 | 12 个锚点全部下不来 | 同上 |

⇒ **凡是「缺了也不报错」的链路，必须有断言守着。**
每条断言都做过注入验证（改坏 → 变红），不是永远绿的空壳。

## 🔴 断言必须自己持有真值，不许 import 被测常量

2026-08-18 `composite_ic` 事故的教训：33 项测试全绿却漏掉序号错位，
因为守的都是「自洽性」（两边一起错照样过）。所以本文件里
`EXPECT_SITES`、`ANCHOR_FMT_LITERAL` 等真值是**手写字面量**，
🔴 **不要改成从 config/web_media 里 import** —— 那样就退化成自洽性测试了。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / 'research-collector'
sys.path[:0] = [str(REPO/'research-data-service'),str(REPO/'research-data-service/lib'),str(REPO/'shared/lib'),str(REPO/'contracts')]
os.environ.setdefault('ARIS_ROOT', str(ROOT))
os.environ.setdefault("ARIS_ROOT", str(ROOT))
os.environ.setdefault("ARIS_OVERSEAS_DIRECT", "1")
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT))

from aris import web_media as wm  # noqa: E402

FAILED: list[str] = []
PASSED = [0]


def ok(cond: bool, msg: str) -> bool:
    if cond:
        PASSED[0] += 1
    else:
        FAILED.append(msg)
        print(f"❌ {msg}")
    return bool(cond)


def section(t: str) -> None:
    print(f"\n=== {t} ===")


# ================================================================ 真值
# 🔴 手写字面量，不从配置 import（见模块头）
# ⚠️ 用 set 比较：站点在 yaml 里的**顺序没有语义**（采集是逐站独立的），
#    断言顺序会在「加一个站」这种正常操作上假红。
#    但**成员**必须断言 —— 少一个站就是少一个源，且静默。
EXPECT_SITES = {
    "substack": {"web:interconnects", "web:thechipletter", "web:importai",
                 "web:thezvi", "web:fabricatedknowledge", "web:aisupremacy",
                 "web:epochai"},
    "rss": {"web:deepmind", "web:googleresearch", "web:nvidia_news",
            "web:nvidia_dev", "web:anthropic", "web:stratechery",
            "web:semiengineering", "web:anthropic-research",
            "web:anthropic-engineering", "web:meta-engineering", "web:meta-about",
            "web:openai", "web:qwen", "web:deepseek", "web:minimax", "web:kimi",
            "web:msresearch", "web:amd_ir", "web:servethehome", "web:damodaran"},
    "standalone": {"web:epoch_site"},
}
ANCHOR_FMT_LITERAL = "[图{n}]"
# feed 自带全文的站（实测：content:encoded 就是全文）
EXPECT_FULL_IN_FEED = {"web:stratechery", "web:semiengineering", "web:msresearch", "web:damodaran"}


# ================================================================ 1 配置
def t_config():
    section("1 配置表")
    import yaml
    p = ROOT / "config" / "web_sources.yaml"
    ok(p.is_file(), "config/web_sources.yaml 不存在")
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    for seg, keys in EXPECT_SITES.items():
        got = {r["key"] for r in (d.get(seg) or [])}
        ok(got == keys, f"{seg} 段站点清单变了：\n  缺 {keys - got}\n  多 {got - keys}")

    # 每站必备字段
    for r in (d.get("substack") or []):
        for f in ("key", "name", "host", "since"):
            ok(bool(r.get(f)), f"substack {r.get('key')} 缺字段 {f}")
        ok(str(r.get("host", "")).startswith("https://"),
           f"{r.get('key')} host 必须是 https 绝对地址：{r.get('host')}")
    for r in (d.get("rss") or []):
        for f in ("key", "name", "feed"):
            ok(bool(r.get(f)), f"rss {r.get('key')} 缺字段 {f}")

    got_full = {r["key"] for r in (d.get("rss") or []) if r.get("full_in_feed")}
    ok(got_full == EXPECT_FULL_IN_FEED,
       f"full_in_feed 集合变了：期望 {EXPECT_FULL_IN_FEED}，实际 {got_full}")

    # key 全局唯一（两段合起来）
    allk = [r["key"] for seg in ("substack", "rss", "standalone") for r in (d.get(seg) or [])]
    ok(len(allk) == len(set(allk)), f"key 有重复：{allk}")
    # 🔴 不能与已有源撞车 —— 撞了会让两个源写进同一个目录
    ok("web:semianalysis" not in allk,
       "web:semianalysis 不能出现在 web_sources.yaml（它有独立采集器与目录）")


# ================================================================ 2 规则同源
def t_shared_rules():
    section("2 规则共用（不是各写一份）")
    from aris import semi_media as sm

    # 🔴 这几条是「web_media 只是站点作用域层」的可执行证明。
    #    2026-08-20 SCRIPT_ORDER 事故：为解耦而新建的层抄了原值 ⇒ 结构解耦、语义照旧。
    #    判据不是「看起来在复用」，而是**同一个函数对象**。
    for n in ("canonical_key", "fetch_candidates", "parse_imgs", "sniff_ext",
              "upscale_cdn", "key_hash"):
        ok(getattr(wm, n) is getattr(sm, n),
           f"web_media.{n} 不是 semi_media.{n} 同一对象（被重新定义了？）")
    ok(wm.ANCHOR_RE is sm.ANCHOR_RE, "ANCHOR_RE 被重新定义")
    ok(wm.ANCHOR_FMT is sm.ANCHOR_FMT, "ANCHOR_FMT 被重新定义")
    ok(wm.ANCHOR_FMT == ANCHOR_FMT_LITERAL,
       f"锚点格式变了：{wm.ANCHOR_FMT!r} != {ANCHOR_FMT_LITERAL!r}")

    # 🔴 扰动测试：改 semi_media 的规则参数，web_media 行为必须跟着变。
    #    这一条才真正排除「抄了一份同样的值」——「同一对象」断言可以靠
    #    `wm.canonical_key = sm.canonical_key` 一行糊过去，但行为联动不行。
    old = sm.CDN_TARGET_W
    probe = "https://substackcdn.com/image/fetch/w_600,c_limit/https%3A%2F%2Fx.com%2Fa.png"
    try:
        sm.CDN_TARGET_W = 1456
        a = wm.fetch_candidates(probe)
        sm.CDN_TARGET_W = 999
        b = wm.fetch_candidates(probe)
        ok(a != b,
           "改 semi_media.CDN_TARGET_W 后 web_media.fetch_candidates 行为没变"
           " ⇒ 规则被复制而非共用")
        ok(any("999" in c for c in b),
           f"新的 CDN_TARGET_W 未体现在候选里：{b}")
    finally:
        sm.CDN_TARGET_W = old


# ================================================================ 3 目录名
def t_site_dirname():
    section("3 站点目录名")
    ok(wm.site_dirname("web:interconnects") == "interconnects",
       "site_dirname 去前缀失败")
    ok(wm.site_dirname("interconnects") == "interconnects",
       "site_dirname 无前缀时应原样")
    # 🔴 非法名必须**抛错**，不许清洗后放过 —— 清洗会把 `a/b` 与 `a_b` 合成同一目录，
    #    两个源静默共用一个台账，症状是「图莫名其妙互相覆盖」。
    for bad in ("web:../etc", "web:a/b", "web:", "web:.", "web:a b",
                "web:A-B!", "web:名字"):
        try:
            wm.site_dirname(bad)
            ok(False, f"site_dirname({bad!r}) 应该抛错但没有")
        except (ValueError, KeyError):
            PASSED[0] += 1


# ================================================================ 4 锚点对齐
def t_anchors():
    section("4 图片锚点与图列表对齐")
    import importlib.util as iu

    def load(name, path):
        spec = iu.spec_from_file_location(name, path)
        m = iu.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    ss = load("ss_t", ROOT / "collectors" / "substack_sites.py")
    rs = load("rs_t", ROOT / "collectors" / "web_rss_sites.py")

    # --- HTML 路径（Substack）
    h = ('<p>one</p><img src="a.png"><p>two</p>'
         '<img alt="no src here"><img src="b.png" width="30">')
    txt = ss.html_to_text(h)
    n_anchor = len(wm.ANCHOR_RE.findall(txt))
    n_img = len(wm.parse_imgs(h))
    ok(n_anchor == n_img == 2,
       f"HTML 路径锚点/图数不一致：锚点 {n_anchor}、图 {n_img}（都应为 2）")
    ok("[图1]" in txt and "[图2]" in txt, f"锚点编号不连续：{txt!r}")
    # 🔴 无 src 的 <img> 两边都必须跳过 —— 只有一边跳会让后续全部错位一格，
    #    表现为「图文对不上」而不报错。
    ok("[图3]" not in txt, f"无 src 的 img 不该占序号：{txt!r}")

    # --- markdown 路径（RSS/trafilatura）
    md = ("intro ![one](https://x.com/1.png) mid "
          "![](data:image/png;base64,AA) tail ![three](https://x.com/3.png)")
    t2, imgs2 = rs.md_to_text_with_anchors(md)
    ok(len(imgs2) == 3, f"md 锚点应数全部 3 张图（含不可抓的），实际 {len(imgs2)}")
    ok(len(wm.ANCHOR_RE.findall(t2)) == 3, f"md 文本锚点数不对：{t2!r}")
    # 可抓清单只要 http(s) 那两张 —— 与锚点数**故意不同**
    ok(len(wm.md_image_urls(md)) == 2,
       f"md_image_urls 应只给可抓的 2 张：{wm.md_image_urls(md)}")
    ok("![" not in t2 and "](" not in t2, f"markdown 图标记未清干净：{t2!r}")

    # --- feed 全文 HTML 路径
    t3, imgs3 = rs.html_body_to_text_with_anchors(h)
    ok(len(imgs3) == len(wm.ANCHOR_RE.findall(t3)) == 2,
       f"feed 全文路径锚点/图不一致：{len(imgs3)} vs {len(wm.ANCHOR_RE.findall(t3))}")

    # --- count_words 必须剔掉锚点
    ok(ss.count_words("alpha [图1] beta [图22] gamma") == 3,
       "count_words 没剔掉 [图N] 锚点 ⇒ 图多的篇 coverage 虚高 ⇒ 残篇被判全文")


# ================================================================ 5 代理解包
def t_proxy_unwrap():
    section("5 图片代理解包 / 相对路径绝对化")
    base = "https://www.anthropic.com/news/some-post"
    # 🔴 真实事故形态：Next.js 优化器 + 相对路径。不解包 ⇒ 整站图静默全丢。
    nxt = ("/_next/image?url=https%3A%2F%2Fwww-cdn.anthropic.com%2Fimages"
           "%2F4zrzovbb%2Fwebsite%2Fabc.png&w=1920&q=75")
    got = wm.absolutize_md_images(f"![x]({nxt})", base)
    ok(got == "![x](https://www-cdn.anthropic.com/images/4zrzovbb/website/abc.png)",
       f"Next.js 图片解包失败：{got}")

    cases = [
        ("https://cdn.example.com/a.png", "https://cdn.example.com/a.png",
         "普通绝对地址不该被改"),
        ("/static/b.png", "https://www.anthropic.com/static/b.png",
         "相对路径未绝对化"),
        ("data:image/png;base64,AAAA", "data:image/png;base64,AAAA",
         "data: 内联图不该被改"),
    ]
    for src, want, why in cases:
        got = wm.absolutize_md_images(f"![]({src})", base)
        ok(got == f"![]({want})", f"{why}：{got}")

    # 🔴 解包后必须成为「可抓」的 —— 这才是这条链路的目的。
    #    只断言字符串对了不够：md_image_urls 的过滤规则也可能把它挡掉。
    ok(len(wm.md_image_urls(wm.absolutize_md_images(f"![]({nxt})", base))) == 1,
       "Next.js 图解包后仍未进入可抓清单")
    # 未解包时确实抓不到（证明这个 bug 真的会漏图，不是我多虑）
    ok(len(wm.md_image_urls(f"![]({nxt})")) == 0,
       "未解包的 /_next/image 竟被当成可抓 ⇒ 本条测试失去意义")


# ================================================================ 6 下载需求同源
def t_wanted_sources():
    section("6 下载需求清单覆盖所有承载图的字段")
    import importlib.util as iu
    spec = iu.spec_from_file_location("dl_t", ROOT / "collectors" / "web_media_dl.py")
    dl = iu.module_from_spec(spec)
    spec.loader.exec_module(dl)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        import aris.paths as P
        old_raw = P.RAW
        try:
            P.RAW = Path(td)
            dl.RAW = Path(td)
            store = wm.SiteStore("web:probe", base=Path(td) / "web" / "probe")
            (store.base / "posts").mkdir(parents=True, exist_ok=True)
            (store.base / "items").mkdir(parents=True, exist_ok=True)
            # Substack 形态：body_html
            (store.base / "posts" / "p1.json").write_text(json.dumps(
                {"body_html": '<p>x</p><img src="https://x.com/h1.png">'}))
            # RSS + feed 全文形态：body_html
            (store.base / "items" / "i1.json").write_text(json.dumps(
                {"body_html": '<img src="https://x.com/h2.png">'}))
            # RSS + trafilatura 形态：md  ← 这一条就是当初漏掉的
            (store.base / "items" / "i2.json").write_text(json.dumps(
                {"md": "text ![a](https://x.com/m1.png) more "
                       "![b](https://x.com/m2.png)"}))
            need = dl.wanted(store)
            keys = {wm.canonical_key(u) for u in (
                "https://x.com/h1.png", "https://x.com/h2.png",
                "https://x.com/m1.png", "https://x.com/m2.png")}
            ok(set(need) == keys,
               f"wanted() 漏字段。期望 4 个来源（posts.body_html / "
               f"items.body_html / items.md×2），实际 {len(need)}：\n"
               f"  缺 {keys - set(need)}\n  多 {set(need) - keys}")
        finally:
            P.RAW = old_raw


# ================================================================ 7 三态
def t_three_states():
    section("7 content_state 三态（preview ≠ no_text）")
    import importlib.util as iu
    spec = iu.spec_from_file_location("ss_t2", ROOT / "collectors" / "substack_sites.py")
    ss = iu.module_from_spec(spec)
    spec.loader.exec_module(ss)

    # 🔴 判定表：每行一个实测形态。别改成「从代码推」——那就成自洽性测试了。
    #    形态来自 2026-08-24 实测（Fabricated Knowledge / AI Supremacy / Import AI）。
    def judge(body_html, audience, wordcount):
        text = ss.strip_cta(ss.html_to_text(body_html))
        lw = ss.count_words(text)
        cov = (lw / wordcount) if wordcount else None
        if not body_html or not text:
            return "no_text"
        if audience == "everyone":
            return "ready"
        if cov is None:
            return "preview" if audience == "only_paid" else "ready"
        return "preview" if cov < 0.95 else "ready"

    long_txt = "<p>" + " ".join(["word"] * 1000) + "</p>"
    short_txt = "<p>" + " ".join(["word"] * 50) + "</p>"
    cases = [
        (None, "only_paid", 799, "no_text",
         "body_html 缺失（Fabricated Knowledge 付费篇）必须是 no_text，不是 preview"),
        ("", "only_paid", 500, "no_text", "空 body_html 必须是 no_text"),
        (short_txt, "only_paid", 1137, "preview",
         "付费墙截断（AI Supremacy）应为 preview"),
        (long_txt, "everyone", 1000, "ready", "免费全文应为 ready"),
        (short_txt, "everyone", 5000, "ready",
         "audience=everyone 应压过低 coverage（Substack wordcount 口径不同）"),
        (long_txt, "only_paid", 1000, "ready",
         "付费但覆盖率够（漏配 paywall）应为 ready"),
    ]
    for bh, aud, wc, want, why in cases:
        got = judge(bh, aud, wc)
        ok(got == want, f"{why}（得到 {got}）")

    # CTA 剔除：不能把正文一起吃掉
    body = ("Real content here. Interconnects is a reader-supported publication. "
            "To receive new posts and support my work, consider becoming a "
            "free or paid subscriber. More real content.")
    s = ss.strip_cta(body)
    ok("Real content here." in s and "More real content." in s,
       f"strip_cta 吃掉了正文：{s!r}")
    ok("reader-supported publication" not in s, f"strip_cta 未剔除推广句：{s!r}")


# ================================================================ 8 日期口径
def t_dates():
    section("8 日期口径")
    import importlib.util as iu
    spec = iu.spec_from_file_location("rs_t2", ROOT / "collectors" / "web_rss_sites.py")
    rs = iu.module_from_spec(spec)
    spec.loader.exec_module(rs)

    # RFC-822（RSS）与 ISO（Atom）都要能解，且统一成 UTC 的 Z 形式 ——
    # 🔴 与 Substack 的 post_date 同格式，下游 ts_from_utc_iso 才能一视同仁。
    iso1, ep1 = rs.parse_date("Thu, 21 Aug 2026 09:30:00 +0000")
    ok(iso1 == "2026-08-21T09:30:00Z", f"RFC-822 解析：{iso1}")
    iso2, ep2 = rs.parse_date("2026-08-21T09:30:00Z")
    ok(iso2 == "2026-08-21T09:30:00Z", f"ISO 解析：{iso2}")
    ok(ep1 == ep2 > 0, f"两种格式 epoch 应相同：{ep1} vs {ep2}")
    # 带偏移的必须换算到 UTC，不是截断时区
    iso3, _ = rs.parse_date("Thu, 21 Aug 2026 17:30:00 +0800")
    ok(iso3 == "2026-08-21T09:30:00Z", f"+0800 未换算到 UTC：{iso3}")
    ok(rs.parse_date("")[0] == "" and rs.parse_date("garbage")[0] == "",
       "空/垃圾日期应返回空串而不是抛错")

    # feed 解析
    rss = """<rss><channel>
      <item><title>A &amp; B</title><link>https://x.com/a</link>
        <pubDate>Thu, 21 Aug 2026 09:30:00 +0000</pubDate>
        <dc:creator>Jane</dc:creator>
        <content:encoded><![CDATA[<p>full body</p>]]></content:encoded>
        <guid>https://x.com/?p=7</guid></item>
    </channel></rss>"""
    items = rs.parse_feed(rss)
    ok(len(items) == 1, f"RSS 应解析出 1 条，实际 {len(items)}")
    it = items[0]
    ok(it["title"] == "A & B", f"标题实体未解码：{it['title']!r}")
    ok(it["author"] == "Jane", f"作者：{it['author']!r}")
    ok("full body" in (it["body_html"] or ""), "content:encoded 未取到")
    ok(it["guid"] == "https://x.com/?p=7", f"guid：{it['guid']!r}")

    atom = """<feed><entry><title>T</title>
      <link href="https://y.com/b"/><published>2026-08-20T01:02:03Z</published>
      <content>body here</content><id>tag:y,2026:1</id></entry></feed>"""
    a = rs.parse_feed(atom)
    ok(len(a) == 1 and a[0]["link"] == "https://y.com/b",
       f"Atom 解析失败：{a}")

    # 🔴 guid 变了才算新条目；标题/URL 改写不该造出重复
    ok(rs.item_id("https://x.com/?p=7") == rs.item_id("https://x.com/?p=7"),
       "item_id 不稳定")
    ok(rs.item_id("a") != rs.item_id("b"), "item_id 撞车")


# ================================================================ 9 adapter




# ================================================================ 10 出网


# ================================================================ 11 cron 契约


# ================================================================ 12 落盘布局
def t_layout():
    section("12 落盘布局与幂等")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "web" / "probe"
        store = wm.SiteStore("web:probe", base=base)
        k = "https://cdn.x/a.png"
        rel = store.file_rel(k, "png")
        ok(rel.startswith("web/probe/media/files/"), f"图片相对路径：{rel}")
        h = wm.key_hash(k)
        ok(f"/{h[:2]}/{h}.png" in rel, f"路径未按 hash 分桶：{rel}")
        # 🔴 同一 key 必须映射到同一路径（幂等靠这个，reconcile 也靠这个反推）
        ok(store.file_rel(k, "png") == rel, "file_rel 不稳定")
        ok(store.file_rel(k, "jpg") != rel, "不同扩展名应是不同路径")

        # resolve_images：台账缺失时 p=None（前端显示「未获取」），不是抛错
        imgs = [{"src": k, "width": 100, "height": 50, "alt": "x"}]
        got = store.resolve_images(imgs, {})
        ok(len(got) == 1 and got[0]["p"] is None,
           f"台账缺失时应给 p=None：{got}")
        led = {wm.canonical_key(k): {"status": "ok", "file": rel}}
        # 台账不是事实来源：必须有真实非空文件才给出图片路径。
        old_raw = wm.RAW
        wm.RAW = Path(td) / "raw"
        physical = wm.RAW / rel
        physical.parent.mkdir(parents=True, exist_ok=True)
        physical.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        try:
            got2 = store.resolve_images(imgs, led)
            ok(got2[0]["p"] == rel, f"台账与物理文件同时存在时应给出路径：{got2}")
            physical.unlink()
            got3 = store.resolve_images(imgs, led)
            ok(got3[0]["p"] is None, f"台账 ok 但文件缺失时必须回落 p=None：{got3}")
        finally:
            wm.RAW = old_raw
        # 🔴 序号必须与输入顺序一致（图文缝合的唯一键）
        two = store.resolve_images(
            [{"src": "https://cdn.x/1.png"}, {"src": "https://cdn.x/2.png"}], {})
        ok([x["n"] for x in two] == [1, 2], f"图序号不对：{two}")


# ================================================================ 13 联网


# ================================================================ 14 图片补欠


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true")
    a = ap.parse_args()

    t_config()
    t_shared_rules()
    t_site_dirname()
    t_anchors()
    t_proxy_unwrap()
    t_wanted_sources()
    t_three_states()
    t_dates()
    t_layout()

    print(f"\n{'=' * 56}")
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败 / {PASSED[0]} 项通过")
        for m in FAILED:
            print(f"  · {m}")
        return 1
    print(f"✅ 全部通过（{PASSED[0]} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
