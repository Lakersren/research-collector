"""Parse article links from Anthropic's News and Engineering channels."""
from collectors.web_entries import parse_html_list

_NEWS_CHANNEL_LINKS = (
    "/news/announcements",
    "/news/features",
    "/news/partnerships",
    "/news/policy",
    "/news/product",
    "/news/safeguards",
    "/news/security",
)


def parse_news(html: str, url: str) -> list[dict]:
    return parse_html_list(html, url, exclude_paths=_NEWS_CHANNEL_LINKS)


def parse_engineering(html: str, url: str) -> list[dict]:
    # Engineering lists its articles under /research/ on the site.
    return parse_html_list(html, url, path_prefix="research")
