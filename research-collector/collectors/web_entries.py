"""Date and HTML link parsing shared by web collectors."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import re
from urllib.parse import urljoin, urlsplit

HTML_TAG = re.compile(r"<[^>]+>")


def plain_html(h: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(HTML_TAG.sub(" ", h or ""))).strip()


def parse_date(s: str | None) -> tuple[str, int]:
    """Return a UTC ISO 8601 timestamp and epoch from an RSS or ISO date."""
    txt = (s or "").strip()
    if not txt:
        return "", 0
    d = None
    try:
        d = parsedate_to_datetime(txt)
    except Exception:
        try:
            d = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except Exception:
            return "", 0
    if d is None:
        return "", 0
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), int(d.timestamp())


def parse_html_list(txt: str, base: str, *, path_prefix=None, exclude_paths=()) -> list[dict]:
    """Read same-site article links under the configured path."""
    seen = {}
    host = urlsplit(base).netloc
    base_parts = [p for p in urlsplit(base).path.split("/") if p]
    prefix = path_prefix or (base_parts[0] if base_parts else "news")
    excluded = {path.rstrip("/") for path in exclude_paths}
    for m in re.finditer(r'<a\b[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         txt, re.S | re.I):
        href, inner = m.group(1), m.group(2)
        u = urljoin(base, href.strip())
        sp = urlsplit(u)
        if sp.netloc != host or sp.path.rstrip("/") in excluded:
            continue
        parts = [p for p in sp.path.split("/") if p]
        if len(parts) != 2 or parts[0] != prefix:
            continue
        title = plain_html(inner)
        if not title or len(title) < 4:
            continue
        if u not in seen or len(title) > len(seen[u]["title"]):
            seen[u] = {"title": title, "link": u, "date": "", "epoch": 0,
                       "author": None, "body_html": None, "guid": u}
    return list(seen.values())
