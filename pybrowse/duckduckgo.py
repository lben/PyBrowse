"""Readable rendering of DuckDuckGo's JavaScript-free results page (html.duckduckgo.com).

Google only serves results to browsers that run JavaScript and pass its bot checks,
so PyBrowse searches with DuckDuckGo's HTML endpoint, which is made for such clients.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from .document import collapse
from .render import Renderer

HOSTS = {"html.duckduckgo.com"}


def prepare(soup: BeautifulSoup) -> None:
    for selector in ("#state_hidden", ".result--ad", ".feedback-btn", "img"):
        for tag in soup.select(selector):
            tag.decompose()


def _result(tag: Tag, r: Renderer) -> bool:
    classes = tag.get("class") or []
    if tag.name != "div" or "result" not in classes:
        return False
    title = tag.find("a", class_="result__a")
    if title is None:
        return False
    shown_url = tag.find(class_="result__url")
    snippet = tag.find(class_="result__snippet")
    r.blank()
    r.add_link(title.get("href"), collapse(title.get_text()).strip())
    r.flush()
    with r.indented(3):
        if shown_url:
            r.text(shown_url.get_text())
            r.flush()
        if snippet:
            r.text(snippet.get_text())
            r.flush()
    return True


HOOKS = (_result,)
