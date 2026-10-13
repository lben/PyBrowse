"""Reddit support.

Reddit blocks non-browser clients (its JSON API and old.reddit.com both refuse
anonymous programmatic access), so Reddit URLs are read through Redlib
(https://github.com/redlib-org/redlib), an open-source Reddit front end that
serves plain HTML without JavaScript. Several public Redlib instances are tried
in order; the page is shown with its real reddit.com URL.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from .document import collapse
from .render import Renderer

CANONICAL = "https://www.reddit.com"
_CONTENT_PATH = re.compile(
    r"^/(?:$|(?:r|u|user|comments|search|subreddits|best|hot|new|top|rising|controversial|"
    r"duplicates|wiki|w)(?:/|$))"
)


def reddit_path(url: str) -> str | None:
    """Path and query to request from a Redlib instance, or None if not a Reddit URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return None
    if host == "redd.it":
        post_id = parts.path.strip("/")
        return f"/comments/{post_id}" if re.fullmatch(r"[A-Za-z0-9]+", post_id) else None
    path = parts.path or "/"
    if host in ("redditmedia.com", "www.redditmedia.com"):  # embed domain search engines link to
        if not _CONTENT_PATH.match(path):
            return None
    elif host != "reddit.com" and not host.endswith(".reddit.com"):
        return None
    gallery = re.fullmatch(r"/gallery/([A-Za-z0-9]+)/?", path)
    if gallery:
        path = f"/comments/{gallery.group(1)}"
    return path + (f"?{parts.query}" if parts.query else "")


def canonical(url: str, instance: str) -> str:
    """Map a URL on a Redlib instance back to reddit.com when it is Reddit content."""
    parts = urlsplit(url)
    inst = urlsplit(instance)
    if parts.hostname != inst.hostname or not _CONTENT_PATH.match(parts.path or "/"):
        return url
    return CANONICAL + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def unusable(status: int, body: bytes) -> str | None:
    """Reason a Redlib response cannot be shown, or None if it is a real Redlib page.

    Anti-bot challenges and proxy error pages are served instead of content by some
    instances; every genuine Redlib page carries its navigation bar.
    """
    if status != 200:
        return f"HTTP {status}"
    if b'class="fixed_navbar"' not in body:
        return "not a Redlib page (probably an anti-bot challenge)"
    return None


def prepare(soup: BeautifulSoup) -> None:
    """Drop Redlib's own interface (menus, settings, pop-ups) before rendering."""
    for nav in soup.select("nav"):  # keep only the search box from the top bar
        forms = [form.extract() for form in nav.find_all("form")]
        nav.clear()
        for form in forms:
            nav.append(form)
    for selector in (".popup", "body > footer", "#sub_actions", "#sub_icon", ".comment_left .line",
                     "#post_links", "hr.sep"):
        for tag in soup.select(selector):
            tag.decompose()


def _points(score: str) -> str:
    if score == "1":
        return "1 point"
    return f"{score} points" if score.lstrip("-").isdigit() else score


def _text(tag: Tag | None) -> str:
    return collapse(tag.get_text(" ")).strip() if tag else ""


def _classes(tag: Tag) -> list[str]:
    return tag.get("class") or []


def _post(tag: Tag, r: Renderer) -> bool:
    classes = _classes(tag)
    if tag.name != "div" or "post" not in classes:
        return False
    header = tag.find(class_="post_header")
    sub = header.find(class_="post_subreddit") if header else None
    author = header.find(class_="post_author") if header else None
    created = header.find(class_="created") if header else None
    title_tag = tag.find(class_="post_title")
    flair = title_tag.find(class_="post_flair") if title_tag else None
    flair_text = _text(flair)
    if flair:
        flair.extract()
    title_link = title_tag.find("a", href=True) if title_tag else None
    title = _text(title_tag) or "(untitled)"
    if flair_text:
        title = f"[{flair_text}] {title}"
    score_tag = tag.find(class_="post_score")
    score = (score_tag.get("title") or "").strip() if score_tag else ""
    score = score if score.lstrip("-").isdigit() else _text(score_tag).replace(" Upvotes", "")
    comments = tag.find(class_="post_comments")
    body = tag.find(class_="post_body")

    r.blank()
    if "highlighted" in classes:  # the post itself on a comments page
        r.text(title)
        r.flush(underline="=")
        if sub:
            r.add_link(sub.get("href"), _text(sub))
            r.text(" | ")
        if author:
            r.add_link(author.get("href"), _text(author))
            r.text(" | ")
        r.text(f"{(created.get('title') or _text(created)) if created else ''} | {_points(score)}")
        r.flush()
        r.blank()
        for child in tag.find_all(recursive=False):
            if not {"post_header", "post_title", "post_score", "post_footer", "post_body"} & set(_classes(child)):
                r.render(child)  # media, galleries, polls, link previews
        if body:
            r.render(body)
        r.blank()
        r.text("Comments")
        r.flush(underline="-")
        return True

    r.add_link(title_link.get("href") if title_link else None, title)
    r.flush()
    with r.indented(3):
        r.text(f"{_points(score)} | ")
        if sub:
            r.text(f"{_text(sub)} | ")
        if author:
            r.text(f"{_text(author)} | ")
        if created:
            r.text(f"{_text(created)} | ")
        if comments:
            r.add_link(comments.get("href"), _text(comments))
        r.flush()
        thumb = tag.find("a", class_="post_thumbnail")
        if thumb and thumb.get("href"):
            r.add_link(thumb.get("href"), f"link: {_text(thumb.find('span')) or 'external'}")
            r.flush()
        if body and _text(body):
            r.render(body)
    return True


def _comment(tag: Tag, r: Renderer) -> bool:
    if tag.name != "div" or "comment" not in _classes(tag):
        return False
    left = tag.find(class_="comment_left", recursive=False)
    right = tag.find(class_="comment_right", recursive=False)
    if right is None:
        return False
    score_tag = left.find(class_="comment_score") if left else None
    score = (score_tag.get("title") or _text(score_tag)).strip() if score_tag else ""
    data = right.find(class_="comment_data", recursive=False)
    body = right.find(class_="comment_body", recursive=False)
    replies = right.find(class_="replies", recursive=False)

    meta = []
    if data:
        author = data.find(class_="comment_author")
        post_link = data.find(class_="comment_link")  # comments listed on a user page
        if author:
            meta.append(_text(author))
        if score:
            meta.append(_points(score))
        created = data.find(class_="created")
        if created:
            meta.append(_text(created))
        if data.find(class_="edited"):
            meta.append("edited")
        where = data.find(class_="comment_subreddit")
        if where:
            meta.append(f"in {_text(where)}")
        r.text(" | ".join(meta))
        if post_link:
            r.text(" | ")
            r.add_link(post_link.get("href"), _text(post_link))
        r.flush()
    if body:
        r.render(body)
    r.blank()
    if replies:
        with r.indented(4):
            r.render(replies)
    return True


HOOKS = (_post, _comment)
