"""Built-in about: pages (home, help, settings, bookmarks, history, tabs)."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .config import SETTINGS
from .document import Document
from .render import render_html

if TYPE_CHECKING:
    from .browser import Browser, Tab

HELP = """\
Reading
  Up/Down, j/k          scroll one line
  PgDn/Space, PgUp/b    scroll one screen
  Home/g, End/G         top / bottom of the page
  /                     find text on the page; n / N next / previous match

Links and forms (every link and form field has a number like [12])
  12 Enter              follow link 12, or edit / toggle / press form field 12
  12 t                  open link 12 in a new tab
  12 e                  open link 12 in your system browser
  12 i                  show where link 12 goes

Going places
  o                     open an address, r/subreddit, or search the web
  O                     edit the current address
  s                     search the web
  Left/h/Backspace      back
  Right/l               forward
  r                     reload        Esc  stop loading
  e                     open the current page in your system browser

Tabs
  t                     new tab (asks for an address; Enter alone opens the home page)
  ] or Tab              next tab
  [ or Shift+Tab        previous tab
  x                     close tab
  T                     list all tabs

Pages
  a                     bookmark this page
  B                     bookmarks
  H                     history
  c                     settings
  ?                     this help
  q                     quit (Ctrl+C also quits)
"""

QUICK_LINKS = [
    ("Reddit front page", "https://www.reddit.com/"),
    ("r/popular", "https://www.reddit.com/r/popular/"),
    ("r/all", "https://www.reddit.com/r/all/"),
]


def _page(url: str, title: str, body: str) -> Document:
    html = f"<html><head><title>{escape(title)}</title></head><body><h1>{escape(title)}</h1>{body}</body></html>"
    return render_html(html, url, allow_about=True)


def handle(browser: "Browser", tab: "Tab", url: str, method: str, data) -> tuple[Document, bool] | None:
    """Return (document, replace current entry), or None when the URL was only an action."""
    parts = urlsplit(url)
    name = parts.path.lower().strip("/")
    query = parse_qs(parts.query)

    if name == "search":
        browser.open(query.get("q", [""])[0], tab)
        return None
    if name == "tabs" and "switch" in query:
        try:
            if not browser.switch_to(int(query["switch"][0])):
                browser.message = "That tab is closed."
        except ValueError:
            pass
        return None
    if name == "bookmarks" and "remove" in query:
        try:
            browser.remove_bookmark(int(query["remove"][0]))
        except ValueError:
            pass
        return bookmarks_page(browser), True
    if name == "config":
        notice = ""
        if "reset" in query:
            errors = browser.config.reset()
            notice = "; ".join(errors) or "Settings reset to defaults."
        elif method == "POST" and data:
            changes = {key: value for key, value in data if key in SETTINGS}
            errors = browser.config.update(changes)
            notice = "Not saved: " + "; ".join(errors) if errors else "Settings saved."
        browser.message = notice
        return config_page(browser, notice), bool(notice)
    if name == "home":
        return home_page(browser), False
    if name == "help":
        return _page("about:help", "PyBrowse help", f"<pre>{escape(HELP)}</pre>"), False
    if name == "bookmarks":
        return bookmarks_page(browser), False
    if name == "history":
        return history_page(browser), False
    if name == "tabs":
        return tabs_page(browser), False
    if name == "blank":
        return Document(url="about:blank", title="Blank"), False
    browser.message = f"Unknown page {url}"
    return home_page(browser), False


def home_page(browser: "Browser") -> Document:
    links = "".join(f'<li><a href="{escape(u)}">{escape(t)}</a></li>' for t, u in QUICK_LINKS)
    marks = "".join(f'<li><a href="{escape(b["url"])}">{escape(b["title"])}</a></li>'
                    for b in browser.bookmarks[:10])
    body = (
        '<form action="about:search" method="get"><p>Search the web: '
        '<input type="text" name="q" placeholder="type here"> <input type="submit" value="Search"></p></form>'
        f"<h2>Reddit</h2><ul>{links}</ul>"
        + (f"<h2>Bookmarks</h2><ul>{marks}</ul>" if marks else "")
        + '<p>Type a link number and press Enter to follow it. Press <a href="about:help">?</a> for help, '
        '<a href="about:config">c</a> for settings, o to open an address.</p>'
    )
    return _page("about:home", "PyBrowse", body)


def bookmarks_page(browser: "Browser") -> Document:
    if not browser.bookmarks:
        return _page("about:bookmarks", "Bookmarks", "<p>No bookmarks yet. Press a on any page to add one.</p>")
    items = "".join(
        f'<li><a href="{escape(b["url"])}">{escape(b["title"])}</a><br>{escape(b["url"])}<br>'
        f'<a href="about:bookmarks?remove={i}">remove</a></li>'
        for i, b in enumerate(browser.bookmarks)
    )
    return _page("about:bookmarks", "Bookmarks", f"<ul>{items}</ul>")


def history_page(browser: "Browser") -> Document:
    if not browser.history:
        return _page("about:history", "History", "<p>Nothing visited yet in this session.</p>")
    items = "".join(f'<li><a href="{escape(u)}">{escape(t or u)}</a><br>{escape(u)}</li>'
                    for u, t in browser.history)
    return _page("about:history", "History", f"<p>Pages visited in this session, newest first.</p><ul>{items}</ul>")


def tabs_page(browser: "Browser") -> Document:
    items = "".join(
        f'<li><a href="about:tabs?switch={t.id}">{escape(t.title)}</a>{" (this tab)" if t is browser.tab else ""}</li>'
        for t in browser.tabs
    )
    return _page("about:tabs", "Tabs", f"<ol>{items}</ol>")


def config_page(browser: "Browser", notice: str = "") -> Document:
    rows = []
    for key, (_, description) in SETTINGS.items():
        value = browser.config.get(key)
        if isinstance(value, list):
            value = ", ".join(value)
        rows.append(f'<p>{escape(key)}: <input type="text" name="{escape(key)}" value="{escape(str(value))}">'
                    f"<br>{escape(description)}</p>")
    body = (
        (f"<p><b>{escape(notice)}</b></p>" if notice else "")
        + f"<p>Settings are stored in {escape(str(browser.config.path))}.</p>"
        + '<form action="about:config" method="post">' + "".join(rows)
        + '<p><input type="submit" value="Save settings"></p></form>'
        + '<p><a href="about:config?reset=1">Reset all settings to defaults</a></p>'
    )
    return _page("about:config", "Settings", body)
