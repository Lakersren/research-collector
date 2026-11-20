"""Parse dated article cards from the Kimi Blog listing."""
import re
from urllib.parse import urljoin, urlsplit

from collectors.web_entries import parse_date


def parse_blog(txt: str, base: str) -> list[dict]:
    """Read the article URL, title and publication date from each Kimi card."""
    from lxml import html

    if not txt.strip():
        return []
    page = html.fromstring(txt)
    seen = {}
    for card in page.xpath('//main//*[contains(concat(" ", normalize-space(@class), " "), " menu-card ")]'):
        links = card.xpath('./a/@href')
        titles = card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " card-title ")]')
        dates = card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " card-date ")]')
        if not links or not titles:
            continue
        url = urljoin(base, links[0])
        address = urlsplit(url)
        if address.netloc != urlsplit(base).netloc or not re.fullmatch(r"/(?:en/)?blog/[^/]+/?", address.path):
            continue
        title = " ".join(titles[0].text_content().split())
        if not title:
            continue
        value = dates[0].text_content().strip() if dates else ""
        date, epoch = parse_date(value) if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else ("", 0)
        if url not in seen or (date and not seen[url]["date"]):
            seen[url] = {"title": title, "link": url, "date": date, "epoch": epoch,
                         "author": None, "body_html": None, "guid": url}
    return list(seen.values())
