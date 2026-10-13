"""Convert HTML into a plain-text Document with numbered links and form fields."""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Callable, Iterable
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag

from .document import Block, Document, Field, Form, Link, collapse, sanitize

SKIP = {
    "script", "style", "template", "head", "svg", "canvas", "object", "embed", "link",
    "meta", "title", "base", "map", "area", "param", "source", "track", "noembed", "frameset",
}
# Block elements that get a blank line before and after them.
SPACED = {"p", "blockquote", "pre", "table", "ul", "ol", "dl", "figure", "fieldset", "hr",
          "h1", "h2", "h3", "h4", "h5", "h6"}
# Block elements that start and end a line.
BLOCK = SPACED | {
    "address", "article", "aside", "center", "dd", "details", "dialog", "dir", "div", "dt",
    "figcaption", "footer", "form", "header", "hgroup", "li", "main", "menu", "nav",
    "section", "summary", "tr", "caption", "legend", "option",
}
LISTS = {"ul", "ol", "menu", "dir"}
TEXT_INPUTS = {"text", "search", "email", "url", "tel", "number", "date", "datetime-local",
               "month", "week", "time", "color", "range", ""}
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_NOT_TEXT = (Comment, Declaration, Doctype, ProcessingInstruction)

Hook = Callable[[Tag, "Renderer"], bool]


class Renderer:
    """Walks a parsed HTML tree and builds Blocks.

    Site-specific hooks are called for every element; a hook that returns True has
    rendered the element itself (using the helper methods below) and its children
    are skipped.
    """

    def __init__(self, page_url: str, *, allow_about: bool = False, hooks: Iterable[Hook] = ()):
        self.page_url = page_url
        self.base = page_url
        self.allow_about = allow_about
        self.hooks = tuple(hooks)
        self.blocks: list[Block] = []
        self.links: list = []
        self.parts: list = []
        self.indent = 0
        self.quote = 0
        self.prefix = ""
        self.pre = 0
        self.cell = 0
        self.anchor: list | None = None  # [Link, text pieces, tag]
        self.forms: list[Form] = []
        self.orphans = Form(action=None)
        self.lists: list[list] = []  # [ordered, next number, indent added]
        self.rows: list[int] = []  # cells seen in each open table row

    # ----- helpers available to hooks -----

    def resolve(self, href: str | None) -> str | None:
        if not href:
            return None
        href = sanitize(href).strip()
        if not href or href.lower().startswith(("javascript:", "data:", "mailto:", "tel:", "blob:")):
            return None
        try:
            url = urljoin(self.base, href)
            parts = urlsplit(url)
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme == "about":
            return url if self.allow_about else None
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.hostname.endswith("duckduckgo.com") and parts.path == "/l/":
            target = parse_qs(parts.query).get("uddg")
            if target and target[0].startswith(("http://", "https://")):
                return target[0]
        return url

    def text(self, text: str) -> None:
        if self.pre:
            text = sanitize(text).replace("\r\n", "\n").replace("\r", "\n")
        else:
            text = collapse(text)
            if not self.parts or (isinstance(self.parts[-1], str) and self.parts[-1].endswith(" ")):
                text = text.lstrip()
        if not text:
            return
        self.parts.append(text)
        if self.anchor is not None:
            self.anchor[1].append(text)

    def add_link(self, href: str | None, label: str) -> None:
        """Append `label` followed by a numbered link marker (or just the label)."""
        url = self.resolve(href)
        self.text(label)
        if url:
            self.parts.append(self._number(Link(url=url, text=collapse(label).strip())))

    def flush(self, underline: str = "") -> None:
        if self.cell or self.anchor is not None:  # table cells and links stay on one line
            self.text(" ")
            return
        if any(not isinstance(p, str) or p.strip() for p in self.parts):
            self.blocks.append(Block(parts=self.parts, indent=self.indent, prefix=self.prefix,
                                     quote=self.quote, underline=underline))
            self.prefix = ""
        self.parts = []

    def blank(self) -> None:
        self.flush()
        if not self.cell and self.anchor is None:
            self.blocks.append(Block(blank=True))

    @contextmanager
    def indented(self, amount: int):
        self.flush()
        self.indent += amount
        try:
            yield
        finally:
            self.flush()
            self.indent -= amount

    def render(self, root: Tag) -> None:
        stack: list[tuple[object, bool]] = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                self._end(node)
                continue
            if isinstance(node, NavigableString):
                if not isinstance(node, _NOT_TEXT):
                    self.text(str(node))
                continue
            if not isinstance(node, Tag) or self._hidden(node):
                continue
            if any(hook(node, self) for hook in self.hooks):
                continue
            if self._start(node):
                stack.append((node, True))
                stack.extend((child, False) for child in reversed(node.contents))

    def finish(self, title: str, note: str = "") -> Document:
        self.flush()
        while self.blocks and self.blocks[0].blank:
            self.blocks.pop(0)
        return Document(url=self.page_url, title=title, blocks=self.blocks, links=self.links, note=note)

    # ----- element handling -----

    def _number(self, item):
        self.links.append(item)
        item.number = len(self.links)
        return item

    def _hidden(self, tag: Tag) -> bool:
        if tag.name in SKIP or tag.has_attr("hidden"):
            return True
        if tag.name == "dialog" and not tag.has_attr("open"):
            return True
        if (tag.get("aria-hidden") or "").lower() == "true":
            return True
        return bool(_HIDDEN_STYLE.search(tag.get("style") or ""))

    def _form(self) -> Form:
        return self.forms[-1] if self.forms else self.orphans

    def _start(self, tag: Tag) -> bool:
        """Handle an opening tag. Return True to descend into its children."""
        name = tag.name
        if name == "a":
            url = self.resolve(tag.get("href"))
            if url and self.anchor is None:
                self.anchor = [Link(url=url), [], tag]
            return True
        if name == "img":
            self._image(tag)
            return False
        if name in ("video", "audio", "iframe"):
            src = tag.get("src")
            if not src:
                source = tag.find("source")
                src = source.get("src") if source else None
            title = collapse(tag.get("title") or "").strip()
            self.add_link(src, f"[{name}{': ' + title if title else ''}]")
            return False
        if name == "br":
            if self.pre:
                self.parts.append("\n")
            elif self.parts:
                self.flush()
            elif not self.cell:
                self.blocks.append(Block(blank=True))
            return False
        if name == "hr":
            self.flush()
            if not self.cell:
                self.blocks.append(Block(rule="-", indent=self.indent))
            return False
        if name in ("input", "select", "textarea", "button"):
            self._control(tag)
            return False
        if name == "base":
            return False
        if name == "pre":
            self.blank()
            self.pre += 1
            return True
        if name in LISTS:
            self.flush() if self.lists else self.blank()
            added = 2 if self.lists else 0
            self.indent += added
            try:
                start = int(tag.get("start", 1))
            except ValueError:
                start = 1
            self.lists.append([name == "ol", start, added])
            return True
        if name == "li":
            self.flush()
            if self.lists and self.lists[-1][0]:
                self.prefix = f"{self.lists[-1][1]}. "
                self.lists[-1][1] += 1
            else:
                self.prefix = "* "
            return True
        if name == "blockquote":
            self.blank()
            self.quote += 1
            return True
        if name == "dd":
            self.flush()
            self.indent += 4
            return True
        if name == "form":
            self.flush()
            action = self.resolve(tag.get("action") or self.page_url)
            method = (tag.get("method") or "GET").upper()
            self.forms.append(Form(action=action, method="POST" if method == "POST" else "GET"))
            return True
        if name == "tr":
            self.flush()
            self.rows.append(0)
            return True
        if name in ("td", "th"):
            if self.rows:
                if self.rows[-1]:
                    self.text(" | ")
                self.rows[-1] += 1
            self.cell += 1
            return True
        if name in SPACED:
            self.blank()
        elif name in BLOCK:
            self.flush()
        return True

    def _end(self, tag: Tag) -> None:
        name = tag.name
        if name == "a":
            if self.anchor is not None and self.anchor[2] is tag:
                link, pieces, _ = self.anchor
                link.text = "".join(pieces).strip()
                if not link.text:
                    link.text = collapse(tag.get("aria-label") or tag.get("title") or "").strip() or "link"
                    self.text(link.text)
                self.anchor = None
                trailing = bool(self.parts) and isinstance(self.parts[-1], str) and self.parts[-1].endswith(" ")
                if trailing:  # keep the number right after the link text: "Hot[4] New[5]"
                    self.parts[-1] = self.parts[-1].rstrip()
                self.parts.append(self._number(link))
                if trailing:
                    self.parts.append(" ")
            return
        if name == "pre":
            self.pre -= 1
            if self.parts and isinstance(self.parts[0], str) and self.parts[0].startswith("\n"):
                self.parts[0] = self.parts[0][1:]
            if self.parts and isinstance(self.parts[-1], str):
                self.parts[-1] = self.parts[-1].rstrip("\n")
            if self.parts:
                self.blocks.append(Block(parts=self.parts, indent=self.indent, quote=self.quote, pre=True))
            self.parts = []
            self.blank()
            return
        if name in LISTS:
            if self.lists:
                self.indent -= self.lists.pop()[2]
            self.flush() if self.lists else self.blank()
            return
        if name == "blockquote":
            self.blank()
            self.quote = max(0, self.quote - 1)
            return
        if name == "dd":
            self.flush()
            self.indent = max(0, self.indent - 4)
            return
        if name == "form":
            self.flush()
            if self.forms:
                self.forms.pop()
            return
        if name == "tr":
            if self.rows:
                self.rows.pop()
            self.flush()
            return
        if name in ("td", "th"):
            self.cell = max(0, self.cell - 1)
            return
        if name in ("h1", "h2"):
            self.flush(underline="=" if name == "h1" else "-")
            self.blank()
            return
        if name in SPACED:
            self.blank()
        elif name in BLOCK:
            self.flush()

    def _image(self, tag: Tag) -> None:
        alt = tag.get("alt")
        if alt is not None and not alt.strip():
            return  # decorative image
        if tag.get("width") in ("0", "1") or tag.get("height") in ("0", "1"):
            return  # tracking pixel
        label = collapse(alt or tag.get("title") or "").strip()
        if self.anchor is not None:
            self.text(f" [image: {label}]" if label else " [image]")
            return
        self.text(" ")
        self.add_link(tag.get("src") or tag.get("data-src"), f"[image: {label}]" if label else "[image]")
        self.text(" ")

    def _control(self, tag: Tag) -> None:
        form = self._form()
        name = tag.get("name") or ""
        label = collapse(tag.get("placeholder") or tag.get("aria-label") or tag.get("title") or "").strip()
        if tag.name == "button":
            kind = (tag.get("type") or "submit").lower()
            if kind != "submit":
                return
            text = collapse(tag.get_text(" ")).strip()
            if not any(ch.isalnum() for ch in text):  # icon-only buttons such as an arrow
                text = tag.get("value") or label or "Submit"
            field = Field(form, "submit", name=name, value=tag.get("value") or "", label=text)
        elif tag.name == "select":
            options = []
            selected = None
            for opt in tag.find_all("option"):
                text = collapse(opt.get_text(" ")).strip()
                value = opt.get("value", text)
                options.append((value, text))
                if selected is None and opt.has_attr("selected"):
                    selected = value
            if not options:
                return
            value = selected if selected is not None else options[0][0]
            field = Field(form, "select", name=name, value=value, label=label, options=options)
        elif tag.name == "textarea":
            field = Field(form, "textarea", name=name, value=sanitize(tag.get_text()), label=label)
        else:
            kind = (tag.get("type") or "text").lower()
            value = sanitize(tag.get("value") or "")
            if kind == "hidden":
                form.fields.append(Field(form, "hidden", name=name, value=value))
                return
            if kind in ("submit", "image"):
                field = Field(form, "submit", name=name, value=value,
                              label=value or tag.get("alt") or "Submit")
            elif kind in ("checkbox", "radio"):
                field = Field(form, kind, name=name, value=value or "on", checked=tag.has_attr("checked"))
            elif kind == "password":
                field = Field(form, "password", name=name, value=value, label=label)
            elif kind in TEXT_INPUTS:
                field = Field(form, "text", name=name, value=value, label=label or name)
            else:  # file, reset, button: need JavaScript or a file picker
                return
        if tag.has_attr("disabled"):
            field.name = ""  # disabled controls are never submitted
        form.fields.append(field)
        self.parts.append(self._number(field))
        self.text(" ")


def render_html(body: bytes | str, url: str, *, encoding: str | None = None, allow_about: bool = False,
                hooks: Iterable[Hook] = (), prepare: Callable[[BeautifulSoup], None] | None = None,
                note: str = "") -> Document:
    if isinstance(body, bytes):
        soup = BeautifulSoup(body, "html.parser", from_encoding=encoding)
    else:
        soup = BeautifulSoup(body, "html.parser")
    title_tag = soup.find("title")
    title = collapse(title_tag.get_text()).strip() if title_tag else ""
    if prepare:
        prepare(soup)
    renderer = Renderer(url, allow_about=allow_about, hooks=hooks)
    base = soup.find("base", href=True)
    if base:
        renderer.base = renderer.resolve(base["href"]) or url
    renderer.render(soup)
    return renderer.finish(title, note)


def render_text(text: str, url: str, title: str = "", note: str = "") -> Document:
    block = Block(parts=[sanitize(text.replace("\r\n", "\n"))], pre=True)
    return Document(url=url, title=title or url, blocks=[block], note=note)
