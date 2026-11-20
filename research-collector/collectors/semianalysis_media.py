"""Discover images referenced by SemiAnalysis articles. Upload handled by image_ingest."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from aris import semi_media as sm

def log(message):
    import logging
    logger = logging.getLogger('corpus.collector')
    logger.log(logging.ERROR if '✗' in message or '🔴' in message else logging.INFO, message)


def wanted() -> dict[str, dict]:
    """扫两站原始 JSON → {语义键: {"cands": [...], "w":, "h":, "refs": n}}。

    `refs` 是被引用次数，用来排序：被多篇引用的图先下（同一张图多处用，价值更高），
    也让 `--limit` 抽样时优先覆盖高价值图。
    """
    out: dict[str, dict] = {}

    def feed(html: str | None):
        for im in sm.parse_imgs(html):
            k = sm.canonical_key(im["src"])
            r = out.setdefault(k, {"cands": [], "w": None, "h": None, "refs": 0})
            r["refs"] += 1
            for c in sm.fetch_candidates(im["src"]):
                if c not in r["cands"]:
                    r["cands"].append(c)
            r["w"] = r["w"] or im.get("width")
            r["h"] = r["h"] or im.get("height")

    for f in sorted((sm.BASE / "substack" / "posts").glob("*.json")):
        try:
            feed(json.loads(f.read_text(encoding="utf-8")).get("body_html"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
    for f in sorted((sm.BASE / "wp" / "posts").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            feed((d.get("content") or {}).get("rendered"))
        except Exception as e:
            raise ValueError(f"原始文章 JSON 损坏: {f.name}") from e
    return out
