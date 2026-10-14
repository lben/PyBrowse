"""Browser state: tabs, history, bookmarks, forms and background page loading."""

from __future__ import annotations

import itertools
import logging
import queue
import re
import threading
from collections import deque
from pathlib import Path
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

from . import duckduckgo, pages, reddit
from .config import Config, load_json, save_json
from .document import Block, Document, Field, Form, Link
from .net import FetchError, Fetcher, Response
from .render import render_html, render_text

log = logging.getLogger("pybrowse")

CACHE_RADIUS = 5  # rendered pages kept in memory on each side of the current history entry
WORKERS = 4


def message_page(url: str, title: str, lines: list[str]) -> Document:
    blocks = [Block(parts=[title], underline="="), Block(blank=True)]
    for line in lines:
        blocks.append(Block(parts=[line]) if line else Block(blank=True))
    return Document(url=url, title=title, blocks=blocks)


def error_page(url: str, reason: str) -> Document:
    return message_page(url, "Page could not be loaded", [
        f"Address: {url}", "", *reason.split("\n"), "",
        "Press r to try again, or h to go back.",
    ])


def to_document(resp: Response, max_mb: int) -> Document:
    ctype = resp.content_type
    note = f"HTTP {resp.status}" if resp.status >= 400 else ""
    looks_html = not ctype and resp.body.lstrip()[:1] == b"<"
    if ctype in ("text/html", "application/xhtml+xml") or looks_html:
        if urlsplit(resp.url).hostname in duckduckgo.HOSTS:
            doc = render_html(resp.body, resp.url, encoding=resp.encoding, note=note,
                              hooks=duckduckgo.HOOKS, prepare=duckduckgo.prepare)
        else:
            doc = render_html(resp.body, resp.url, encoding=resp.encoding, note=note)
    elif (ctype.startswith("text/") or ctype.endswith(("+json", "+xml"))
          or ctype in ("application/json", "application/xml", "application/javascript")):
        try:
            text = resp.body.decode(resp.encoding or "utf-8", "replace")
        except LookupError:
            text = resp.body.decode("utf-8", "replace")
        doc = render_text(text, resp.url, note=note)
    else:
        doc = message_page(resp.url, "Not a text page", [
            f"This address returned {ctype or 'unknown content'} ({len(resp.body):,} bytes).",
            "PyBrowse shows text only. Press e to open it in your system browser.",
        ])
    if resp.truncated:
        doc.blocks += [Block(blank=True), Block(parts=[
            f"[Page cut off at {max_mb} MB. Raise max_page_mb in the settings to see more.]"])]
    return doc


class Loader:
    """Turns an address into a Document. Never raises: failures become error pages."""

    def __init__(self, config: Config, fetcher: Fetcher):
        self.config = config
        self.fetcher = fetcher
        self.preferred = ""  # the Redlib instance that worked last

    def load(self, url: str, method: str = "GET", data=None, cancelled=lambda: False) -> Document:
        try:
            path = reddit.reddit_path(url)
            instances = self.config.get("reddit_frontends")
            if path is not None and instances:
                return self._load_reddit(url, path, method, data, instances, cancelled)
            return to_document(self.fetcher.fetch(url, method, data, cancelled), self.config.get("max_page_mb"))
        except FetchError as exc:
            return error_page(url, f"{exc}\n\n{exc.hint}" if exc.hint else str(exc))
        except Exception as exc:  # a bug must never take the browser down
            log.exception("loading %s", url)
            return error_page(url, f"Internal error: {exc!r}\nDetails are in pybrowse.log.")

    def _load_reddit(self, url, path, method, data, instances, cancelled) -> Document:
        failures, hints = [], []
        for instance in sorted(instances, key=lambda inst: inst != self.preferred):
            host = urlsplit(instance).hostname
            if cancelled():
                raise FetchError("cancelled")
            try:
                resp = self.fetcher.fetch(instance + path, method, data, cancelled)
            except FetchError as exc:
                if str(exc) == "cancelled":
                    raise
                failures.append(f"  {host}: {exc}")
                if exc.hint and exc.hint not in hints:
                    hints.append(exc.hint)
                if exc.proxy_failed:
                    return error_page(url, f"{exc}\n\n{exc.hint}")
                continue
            reason = reddit.unusable(resp.status, resp.body)
            if reason:
                failures.append(f"  {host}: {reason}")
                continue
            self.preferred = instance
            doc = render_html(resp.body, resp.url, encoding=resp.encoding, hooks=reddit.HOOKS,
                              prepare=reddit.prepare, note=f"via {host}")
            doc.url = reddit.canonical(resp.url, instance)
            for item in doc.links:
                if isinstance(item, Link):
                    item.url = reddit.canonical(item.url, instance)
                elif item.form.action:
                    item.form.action = reddit.canonical(item.form.action, instance)
            return doc
        return error_page(url, "\n".join([
            "Reddit blocks direct access, so PyBrowse reads it through Redlib mirrors.",
            "None of the configured mirrors could load this page:", *failures, "", *hints,
            "Mirrors come and go. Press c to edit reddit_frontends in the settings",
            "(see https://github.com/redlib-org/redlib-instances for a current list).",
        ]))


class Entry:
    def __init__(self, url: str, method: str = "GET", data=None, doc: Document | None = None):
        self.url = url
        self.method = method
        self.data = data
        self.doc = doc
        self.scroll = 0
        self.line_count = 0  # lines in the layout `scroll` refers to


class Tab:
    _ids = itertools.count(1)

    def __init__(self):
        self.id = next(Tab._ids)
        self.entries: list[Entry] = []
        self.index = -1
        self.generation = 0  # bumped to cancel the pending load
        self.loading: str | None = None

    @property
    def current(self) -> Entry | None:
        return self.entries[self.index] if 0 <= self.index < len(self.entries) else None

    @property
    def doc(self) -> Document | None:
        entry = self.current
        return entry.doc if entry else None

    @property
    def title(self) -> str:
        doc = self.doc
        if doc:
            return doc.title or doc.url
        return self.loading or "New tab"


def resolve_input(text: str, search_url: str) -> str | None:
    """Turn what the user typed into an address: a URL, a Reddit shortcut, or a search."""
    text = text.strip()
    if not text:
        return None
    if re.match(r"(?i)^(https?|about):", text):
        return text
    if re.match(r"^/?(r|u|user)/[\w-]+", text):
        return "https://www.reddit.com/" + text.lstrip("/")
    host = text.split("/", 1)[0]
    if " " not in text and re.fullmatch(r"(localhost|[\w-]+(\.[\w-]+)+)(:\d+)?", host):
        return ("http://" if host.startswith("localhost") else "https://") + text
    return search_url.replace("{query}", quote_plus(text))


def form_data(form: Form, submitter: Field | None) -> list[tuple[str, str]]:
    data = []
    for field in form.fields:
        if not field.name:
            continue
        if field.kind == "submit":
            if field is submitter:
                data.append((field.name, field.value))
        elif field.kind in ("checkbox", "radio"):
            if field.checked:
                data.append((field.name, field.value))
        else:
            data.append((field.name, field.value))
    return data


class Browser:
    def __init__(self, config: Config, home: Path):
        self.config = config
        self.loader = Loader(config, Fetcher(config, home / "cookies.txt"))
        self.bookmarks_path = home / "bookmarks.json"
        stored = load_json(self.bookmarks_path, [])
        self.bookmarks = [b for b in stored if isinstance(b, dict) and isinstance(b.get("url"), str)
                          and isinstance(b.get("title"), str)] if isinstance(stored, list) else []
        self.history: deque[tuple[str, str]] = deque(maxlen=1000)
        self.tabs: list[Tab] = []
        self.active = 0
        self.message = ""
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._done: queue.SimpleQueue = queue.SimpleQueue()
        # Daemon threads: quitting never waits for a slow server.
        for i in range(WORKERS):
            threading.Thread(target=self._work, name=f"fetch-{i}", daemon=True).start()

    # ----- tabs -----

    @property
    def tab(self) -> Tab:
        return self.tabs[self.active]

    def new_tab(self, address: str | None = None) -> Tab:
        tab = Tab()
        self.tabs.insert(self.active + 1 if self.tabs else 0, tab)
        self.active = self.tabs.index(tab)
        self.navigate(tab, address or self.config.get("homepage"))
        return tab

    def close_tab(self) -> None:
        tab = self.tabs.pop(self.active)
        tab.generation += 1
        if not self.tabs:
            self.active = 0
            self.new_tab()
            return
        self.active = min(self.active, len(self.tabs) - 1)

    def switch_tab(self, delta: int) -> None:
        self.active = (self.active + delta) % len(self.tabs)

    def switch_to(self, tab_id: int) -> bool:
        for i, tab in enumerate(self.tabs):
            if tab.id == tab_id:
                self.active = i
                return True
        return False

    # ----- navigation -----

    def open(self, text: str, tab: Tab | None = None) -> None:
        url = resolve_input(text, self.config.get("search_url"))
        if url:
            self.navigate(tab or self.tab, url)

    def navigate(self, tab: Tab, url: str, method: str = "GET", data=None, target: Entry | None = None) -> None:
        if url.lower().startswith("about:"):
            tab.generation += 1
            tab.loading = None
            result = pages.handle(self, tab, url, method, data)
            if result is not None:
                doc, replace = result
                current = tab.current
                if replace and current is not None and current.url.startswith("about:"):
                    target = current
                self._show(tab, doc, "GET", None, target)
            return
        tab.generation += 1
        tab.loading = url
        self._jobs.put((tab, tab.generation, url, method, data, target))

    def back(self, tab: Tab) -> None:
        if tab.index > 0:
            self._go(tab, tab.index - 1)
        else:
            self.message = "No earlier page in this tab."

    def forward(self, tab: Tab) -> None:
        if tab.index < len(tab.entries) - 1:
            self._go(tab, tab.index + 1)
        else:
            self.message = "No later page in this tab."

    def reload(self, tab: Tab) -> None:
        entry = tab.current
        if entry:
            self.navigate(tab, entry.url, entry.method, entry.data, target=entry)

    def stop(self, tab: Tab) -> None:
        if tab.loading:
            tab.generation += 1
            tab.loading = None
            self.message = "Stopped."

    def _go(self, tab: Tab, index: int) -> None:
        entry = tab.entries[index]
        if entry.doc is not None and not entry.url.startswith("about:"):
            tab.generation += 1
            tab.loading = None
            tab.index = index
        else:
            self.navigate(tab, entry.url, entry.method, entry.data, target=entry)

    def _work(self) -> None:
        while True:
            tab, generation, url, method, data, target = self._jobs.get()
            if generation != tab.generation:
                continue  # cancelled while queued
            try:
                doc = self.loader.load(url, method, data, cancelled=lambda: generation != tab.generation)
            except BaseException as exc:  # keep the worker alive whatever happens
                log.exception("worker failed on %s", url)
                doc = error_page(url, f"Internal error: {exc!r}")
            self._done.put((tab, generation, method, data, target, doc))

    def poll(self) -> bool:
        """Apply finished loads. Returns True if anything visible changed."""
        changed = False
        while True:
            try:
                tab, generation, method, data, target, doc = self._done.get_nowait()
            except queue.Empty:
                return changed
            if tab in self.tabs and generation == tab.generation:
                tab.loading = None
                self._show(tab, doc, method, data, target)
                changed = True

    def _show(self, tab: Tab, doc: Document, method: str, data, target: Entry | None) -> None:
        if target is not None and target in tab.entries:
            target.doc = doc
            target.url = doc.url
            tab.index = tab.entries.index(target)
        else:
            del tab.entries[tab.index + 1:]
            tab.entries.append(Entry(doc.url, method, data, doc))
            excess = len(tab.entries) - self.config.get("history_limit")
            if excess > 0:
                del tab.entries[:excess]
            tab.index = len(tab.entries) - 1
            if not doc.url.startswith("about:"):
                self.history.appendleft((doc.url, doc.title))
        for i, entry in enumerate(tab.entries):
            if abs(i - tab.index) > CACHE_RADIUS:
                entry.doc = None

    # ----- links and forms -----

    def follow(self, tab: Tab, number: int):
        """Follow link `number`. Returns the Field if it is a form control, else None."""
        doc = tab.doc
        target = doc.target(number) if doc else None
        if target is None:
            self.message = f"There is no link {number} on this page."
            return None
        if isinstance(target, Link):
            self.navigate(tab, target.url)
            return None
        return target

    def link_url(self, tab: Tab, number: int) -> str | None:
        doc = tab.doc
        target = doc.target(number) if doc else None
        if isinstance(target, Link):
            return target.url
        self.message = f"Link {number} is not a web address." if target else f"There is no link {number}."
        return None

    def toggle(self, tab: Tab, field: Field) -> None:
        if field.kind == "checkbox":
            field.checked = not field.checked
        elif field.kind == "radio":
            for other in field.form.fields:
                if other.kind == "radio" and other.name == field.name:
                    other.checked = other is field
        if tab.doc:
            tab.doc.invalidate()

    def set_value(self, tab: Tab, field: Field, value: str) -> None:
        field.value = value
        if tab.doc:
            tab.doc.invalidate()

    def submit(self, tab: Tab, form: Form, submitter: Field | None = None) -> None:
        if not form.action:
            self.message = "This form cannot be submitted (it has no address)."
            return
        data = form_data(form, submitter)
        if form.method == "POST":
            self.navigate(tab, form.action, "POST", data)
            return
        parts = urlsplit(form.action)
        self.navigate(tab, urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(data), "")))

    # ----- bookmarks -----

    def add_bookmark(self, tab: Tab) -> None:
        doc = tab.doc
        if doc is None or doc.url.startswith("about:"):
            self.message = "Only web pages can be bookmarked."
            return
        if any(b["url"] == doc.url for b in self.bookmarks):
            self.message = "Already bookmarked."
            return
        self.bookmarks.append({"url": doc.url, "title": doc.title or doc.url})
        self.message = self._save_bookmarks() or "Bookmarked."

    def remove_bookmark(self, index: int) -> None:
        if 0 <= index < len(self.bookmarks):
            del self.bookmarks[index]
            self.message = self._save_bookmarks() or "Bookmark removed."

    def _save_bookmarks(self) -> str:
        try:
            save_json(self.bookmarks_path, self.bookmarks)
        except OSError as exc:
            return f"Could not save bookmarks: {exc}"
        return ""

    def shutdown(self) -> None:
        for tab in self.tabs:
            tab.generation += 1
        self.loader.fetcher.save_cookies()
