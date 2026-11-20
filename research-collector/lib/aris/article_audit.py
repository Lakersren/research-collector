"""Article decisions recorded by the collector for the operator's local report."""
from contextlib import contextmanager
from contextvars import ContextVar
import json

from aris.daily_policy import publication_day


_journal = ContextVar("article_audit_journal", default=None)


@contextmanager
def capture(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        token = _journal.set(stream)
        try:
            yield
        finally:
            _journal.reset(token)


def article_details(title, url, published_at):
    day = publication_day(published_at)
    return {
        "title": title or None,
        "url": url or None,
        "published_at": published_at or None,
        "publication_date_beijing": day.isoformat() if day else None,
    }


def record(status, title, url, published_at, reason=None):
    stream = _journal.get()
    if stream is not None:
        row = article_details(title, url, published_at)
        row.update(status=status, reason=reason)
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()


def date_rejection(title, url, published_at):
    if publication_day(published_at) is None:
        record("failed", title, url, published_at, "无法确定发布日期")
        return True
    record("skipped", title, url, published_at, "发布日期不在目标日期")
    return False


def substack_details(post, host):
    return post.get("title"), post.get("canonical_url") or f"{host.rstrip('/')}/p/{post['slug']}"


def page_publication_date(html):
    """Read explicit publication metadata and keep the publisher's timezone when present."""
    from lxml import etree

    page = etree.HTML(html)
    if page is None:
        return None
    for value in page.xpath(
        '//meta[@property="article:published_time" or @itemprop="datePublished"'
        ' or @name="datePublished" or @name="citation_publication_date"]/@content'
        ' | //*[@itemprop="datePublished"]/@datetime'
    ):
        if publication_day(value):
            return value

    for script in page.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            data = json.loads(script)
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph", [])
            candidates = [node, *graph] if isinstance(graph, list) else [node]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    value = candidate.get("datePublished")
                    if isinstance(value, str) and publication_day(value):
                        return value
    return None
