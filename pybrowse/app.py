"""Terminal user interface and program entry point."""

from __future__ import annotations

import argparse
import locale
import logging
import os
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from urllib.parse import quote_plus

try:
    import curses
except ImportError:  # Windows without the windows-curses package
    curses = None

from . import __version__
from .browser import Browser, resolve_input
from .config import Config, data_dir
from .document import Field, clip, sanitize, text_width

log = logging.getLogger("pybrowse")

ESC = "\x1b"
CTRL_C = "\x03"
ENTER_KEYS = ("\n", "\r")  # plus curses.KEY_ENTER and, on Windows, PADENTER (see _is_enter)
BACKSPACE_KEYS = ("\x7f", "\b")
DIGITS = tuple("0123456789")


class UI:
    def __init__(self, scr, browser: Browser):
        self.scr = scr
        self.b = browser
        self.number = ""  # link number being typed
        self.find_term = ""
        self.status = ""
        self.overlay: list[str] | None = None  # temporary lines shown instead of the page
        self.last_view = 0  # page lines shown by the previous draw
        self.running = True

    # ----- main loop -----

    def run(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)
        dirty = True
        while self.running:
            if self.b.poll():
                dirty = True
            if self.b.message:
                self.status, self.b.message = self.b.message, ""
                dirty = True
            if dirty:
                self._safe(self.draw)
                dirty = False
            try:
                key = self.scr.get_wch()
            except curses.error:
                continue  # no key within the timeout
            except KeyboardInterrupt:
                return
            dirty = True
            try:
                self.handle(key)
            except KeyboardInterrupt:
                return
            except Exception as exc:  # never crash on a key press; report and continue
                log.exception("handling key %r", key)
                self.status = f"Error: {exc} (details in pybrowse.log)"

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("drawing failed")

    # ----- drawing -----

    def size(self) -> tuple[int, int]:
        return self.scr.getmaxyx()

    def page_width(self) -> int:
        _, w = self.size()
        limit = self.b.config.get("width")
        return min(limit, w) if limit else w

    def lines(self, entry) -> list[str]:
        """The entry's lines at the current width, keeping the reading position when the
        text re-wraps (terminal resized or width setting changed)."""
        lines = entry.doc.lines(self.page_width())
        if entry.line_count and entry.line_count != len(lines):
            if entry.scroll + self.last_view >= entry.line_count:
                entry.scroll = len(lines)  # stay at the bottom; clamped when drawn
            else:
                entry.scroll = entry.scroll * len(lines) // entry.line_count
        entry.line_count = len(lines)
        return lines

    def view_height(self) -> int:
        return max(1, self.size()[0] - 2)

    def put(self, y: int, x: int, text: str) -> None:
        h, w = self.size()
        if not 0 <= y < h or x >= w:
            return
        # Last line of defence: nothing reaches the terminal with control characters.
        # Never write the bottom-right cell: curses raises when the cursor would scroll.
        text = clip(sanitize(text).replace("\n", " ").replace("\t", " "), w - x - (1 if y == h - 1 else 0))
        try:
            self.scr.addstr(y, x, text)
        except curses.error:
            pass
        except (UnicodeEncodeError, ValueError):
            try:
                self.scr.addstr(y, x, text.encode("ascii", "replace").decode("ascii"))
            except curses.error:
                pass

    def draw(self) -> None:
        h, w = self.size()
        self.scr.erase()
        if h < 3 or w < 20:
            self.put(0, 0, "Window too small")
            self.scr.refresh()
            return
        self.put(0, 0, self.tab_bar(w))
        view = self.view_height()
        if self.overlay is not None:
            for i, line in enumerate(self.overlay[:view]):
                self.put(1 + i, 0, line)
        else:
            tab = self.b.tab
            entry = tab.current
            if entry and entry.doc:
                lines = self.lines(entry)
                entry.scroll = max(0, min(entry.scroll, len(lines) - view))
                for i, line in enumerate(lines[entry.scroll:entry.scroll + view]):
                    self.put(1 + i, 0, line)
            elif tab.loading:
                self.put(1, 0, "Loading...")
        self.put(h - 1, 0, self.status_line())
        self.scr.refresh()
        self.last_view = view

    def tab_bar(self, w: int) -> str:
        tabs = self.b.tabs
        each = max(12, w // len(tabs))
        labels = []
        for i, tab in enumerate(tabs):
            title = sanitize(tab.title).replace("\n", " ")
            label = clip(f"{i + 1}:{title}", each - 3)
            labels.append(f"[{label}]" if i == self.b.active else f" {label} ")
        start = 0
        while start < self.b.active and text_width(" ".join(labels[start:self.b.active + 1])) > w:
            start += 1
        return " ".join(labels[start:])

    def status_line(self) -> str:
        tab = self.b.tab
        if self.number:
            return f"Link {self.number}: {self.describe(int(self.number))}  (Enter=go t=new tab e=external Esc=cancel)"
        if self.status:
            return self.status
        if tab.loading:
            return f"Loading {tab.loading} ... (Esc to stop)"
        entry = tab.current
        if not entry or not entry.doc:
            return "Press o to open an address, ? for help."
        doc = entry.doc
        lines = self.lines(entry)
        end = min(len(lines), entry.scroll + self.view_height())
        position = "All" if len(lines) <= self.view_height() else f"{100 * end // max(1, len(lines))}%"
        note = f"  ({doc.note})" if doc.note else ""
        return f"{position}  {doc.url}{note}  (? help)"

    def describe(self, number: int) -> str:
        doc = self.b.tab.doc
        target = doc.target(number) if doc else None
        if target is None:
            return "no such link"
        if isinstance(target, Field):
            return f"form field {target.display()}"
        return f"{target.text} -> {target.url}" if target.text else target.url

    # ----- prompts -----

    def prompt(self, label: str, initial: str = "", mask: bool = False) -> str | None:
        """Read a line of text on the status line. Returns None if cancelled with Esc."""
        buf, pos = list(initial), len(initial)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                h, w = self.size()
                self._safe(self.draw)
                text = "".join(buf)
                shown_text = "*" * len(text) if mask else text
                avail = max(1, w - text_width(label) - 2)
                start = 0
                while text_width(shown_text[start:pos]) > avail:
                    start += 1
                shown = clip(shown_text[start:], avail)
                self.put(h - 1, 0, " " * (w - 1))
                self.put(h - 1, 0, clip(label, w - 2) + shown)
                try:
                    self.scr.move(h - 1, min(w - 2, text_width(label) + text_width(shown_text[start:pos])))
                except curses.error:
                    pass
                self.scr.refresh()
                try:
                    key = self.scr.get_wch()
                except curses.error:
                    continue
                if _is_enter(key):
                    return text
                if key == ESC:
                    return None
                if key == CTRL_C:
                    raise KeyboardInterrupt
                if key in BACKSPACE_KEYS or key == curses.KEY_BACKSPACE:
                    if pos > 0:
                        del buf[pos - 1]
                        pos -= 1
                elif key == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif key == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif key == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif key == curses.KEY_HOME:
                    pos = 0
                elif key == curses.KEY_END:
                    pos = len(buf)
                elif key == "\x15":  # Ctrl+U clears the line
                    buf, pos = [], 0
                elif key == curses.KEY_RESIZE:
                    self.resized()
                elif isinstance(key, str) and key.isprintable():
                    buf.insert(pos, key)
                    pos += 1
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def ask(self, question: str) -> bool:
        answer = self.prompt(f"{question} (y/n) ")
        return answer is not None and answer.strip().lower() in ("y", "yes")

    def resized(self) -> None:
        try:
            curses.resize_term(0, 0)  # needed on Windows; harmless elsewhere
        except (curses.error, AttributeError):
            pass

    # ----- keys -----

    def handle(self, key) -> None:
        self.status = ""
        if key in DIGITS:
            if len(self.number) < 6:
                self.number += key
            return
        if self.number:
            number, self.number = int(self.number), ""
            if _is_enter(key):
                self.follow(number)
            elif key == "t":
                url = self.b.link_url(self.b.tab, number)
                if url:
                    self.b.new_tab(url)
            elif key == "e":
                self.external(self.b.link_url(self.b.tab, number))
            elif key == "i":
                self.status = f"Link {number}: {self.describe(number)}"
            elif key in BACKSPACE_KEYS or key == curses.KEY_BACKSPACE:
                self.number = str(number)[:-1]
            return  # any other key just cancels the number

        b, tab = self.b, self.b.tab
        view = self.view_height()
        if key in (curses.KEY_DOWN, "j"):
            self.scroll(1)
        elif key in (curses.KEY_UP, "k"):
            self.scroll(-1)
        elif key in (curses.KEY_NPAGE, " "):
            self.scroll(max(1, view - 1))
        elif key in (curses.KEY_PPAGE, "b"):
            self.scroll(-max(1, view - 1))
        elif key in (curses.KEY_HOME, "g"):
            self.scroll(-10**9)
        elif key in (curses.KEY_END, "G"):
            self.scroll(10**9)
        elif key in (curses.KEY_LEFT, "h", curses.KEY_BACKSPACE) or key in BACKSPACE_KEYS:
            b.back(tab)
        elif key in (curses.KEY_RIGHT, "l"):
            b.forward(tab)
        elif key == "o":
            text = self.prompt("Open (address, r/name, or search): ")
            if text:
                b.open(text, tab)
        elif key == "O":
            entry = tab.current
            text = self.prompt("Open: ", entry.url if entry else "")
            if text:
                b.open(text, tab)
        elif key == "s":
            text = self.prompt("Search the web: ")
            if text and text.strip():
                b.navigate(tab, b.config.get("search_url").replace("{query}", quote_plus(text.strip())))
        elif key == "t":
            text = self.prompt("New tab (address or search; Enter for home page): ")
            if text is not None:
                b.new_tab(resolve_input(text, b.config.get("search_url")))
        elif key in ("]", "\t"):
            b.switch_tab(1)
        elif key in ("[", curses.KEY_BTAB):
            b.switch_tab(-1)
        elif key == "x":
            b.close_tab()
        elif key == "T":
            b.navigate(tab, "about:tabs")
        elif key == "r":
            b.reload(tab)
        elif key == ESC:
            b.stop(tab)
        elif key == "e":
            entry = tab.current
            self.external(entry.url if entry else None)
        elif key == "/":
            term = self.prompt("Find: ", self.find_term)
            if term:
                self.find_term = term
                self.find(1, include_current=True)
        elif key == "n":
            self.find(1)
        elif key == "N":
            self.find(-1)
        elif key == "a":
            b.add_bookmark(tab)
        elif key == "B":
            b.navigate(tab, "about:bookmarks")
        elif key == "H":
            b.navigate(tab, "about:history")
        elif key == "c":
            b.navigate(tab, "about:config")
        elif key == "?":
            b.navigate(tab, "about:help")
        elif key == "q":
            if self.ask("Quit PyBrowse?"):
                self.running = False
        elif key == CTRL_C:
            self.running = False
        elif key == curses.KEY_RESIZE:
            self.resized()

    def scroll(self, amount: int) -> None:
        entry = self.b.tab.current
        if entry and entry.doc:
            lines = self.lines(entry)
            entry.scroll = max(0, min(entry.scroll + amount, len(lines) - self.view_height()))

    def find(self, direction: int, include_current: bool = False) -> None:
        entry = self.b.tab.current
        if not self.find_term:
            self.status = "Press / to search this page."
            return
        if not entry or not entry.doc:
            return
        lines = self.lines(entry)
        needle = self.find_term.casefold()
        start = entry.scroll if include_current else entry.scroll + direction
        for step in range(len(lines)):
            i = (start + step * direction) % len(lines)
            if needle in lines[i].casefold():
                entry.scroll = i
                wrapped = (i < start) if direction > 0 else (i > start)
                self.status = f"Found \"{self.find_term}\" on line {i + 1}" + (" (wrapped)" if wrapped else "")
                return
        self.status = f"\"{self.find_term}\" not found on this page."

    def follow(self, number: int) -> None:
        tab = self.b.tab
        field = self.b.follow(tab, number)
        if field is not None:
            self.use_field(field)

    def use_field(self, field: Field) -> None:
        b, tab = self.b, self.b.tab
        if field.kind in ("checkbox", "radio"):
            b.toggle(tab, field)
        elif field.kind == "submit":
            b.submit(tab, field.form, field)
        elif field.kind == "select":
            self.overlay = ["Choose an option, by number or by typing part of its name:", ""] + [
                f"  {i}. {label}" for i, (_, label) in enumerate(field.options, 1)]
            try:
                answer = self.prompt("Option: ")
            finally:
                self.overlay = None
            if answer:
                value = _choose(field.options, answer)
                if value is None:
                    self.status = "No matching option."
                else:
                    b.set_value(tab, field, value)
        else:
            value = self.prompt(f"{field.label or field.name or 'Text'}: ", field.value,
                                mask=field.kind == "password")
            if value is None:
                return
            b.set_value(tab, field, value)
            editable = [f for f in field.form.fields if f.kind in ("text", "password", "textarea")]
            if editable == [field]:  # a single-field form (like a search box) submits on Enter
                buttons = [f for f in field.form.fields if f.kind == "submit"]
                b.submit(tab, field.form, buttons[0] if buttons else None)

    def external(self, url: str | None) -> None:
        if not url or not url.startswith(("http://", "https://")):
            if url is not None:
                self.status = "Only web addresses can be opened in the system browser."
            return
        try:
            opened = webbrowser.open(url)
        except Exception as exc:
            opened = False
            log.warning("could not open %s: %s", url, exc)
        self.scr.clear()  # a console browser could have drawn over the screen
        self.status = f"Opened in system browser: {url}" if opened else "Could not open a system browser."


def _is_enter(key) -> bool:
    return key in ENTER_KEYS or key in (curses.KEY_ENTER, getattr(curses, "PADENTER", None))


def _choose(options: list[tuple[str, str]], answer: str) -> str | None:
    answer = answer.strip()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return options[int(answer) - 1][0]
    needle = answer.casefold()
    for value, label in options:
        if label.casefold().startswith(needle):
            return value
    for value, label in options:
        if needle in label.casefold():
            return value
    return None


def _setup_logging(home) -> None:
    try:
        handler = RotatingFileHandler(home / "pybrowse.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def dump(browser: Browser, address: str, width: int) -> int:
    """Print a page as plain text followed by its link list, like `lynx -dump`."""
    tab = browser.new_tab(resolve_input(address, browser.config.get("search_url")))
    # Worst case: every Reddit mirror is tried, each up to its full download deadline.
    attempts = len(browser.config.get("reddit_frontends")) + 1
    deadline = time.monotonic() + browser.config.get("timeout") * 3 * attempts + 5
    while tab.loading and time.monotonic() < deadline:
        browser.poll()
        time.sleep(0.05)
    doc = tab.doc
    if doc is None:
        print("Timed out.", file=sys.stderr)
        return 1
    print(sanitize("\n".join(doc.lines(width))))  # sanitize keeps newlines and tabs only
    links = [item for item in doc.links if hasattr(item, "url")]
    if links:
        print("\nLinks")
        for item in links:
            print(f"  {item.number}. {sanitize(item.url)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pybrowse", description="A text-only web browser for the terminal.")
    parser.add_argument("address", nargs="?", help="address, r/subreddit, or search terms to open")
    parser.add_argument("--dump", action="store_true", help="print the page as plain text and exit")
    parser.add_argument("--width", type=int, default=100, help="text width for --dump (default 100)")
    parser.add_argument("--version", action="version", version=f"PyBrowse {__version__}")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # e.g. emoji on a Windows code-page console
        except (AttributeError, ValueError):
            pass
    try:
        home = data_dir()
    except OSError as exc:
        print(f"Cannot create the PyBrowse data folder: {exc}", file=sys.stderr)
        return 1
    _setup_logging(home)
    config = Config(home / "config.json")

    if args.dump:
        browser = Browser(config, home)
        try:
            return dump(browser, args.address or config.get("homepage"), max(20, args.width))
        finally:
            browser.shutdown()

    if curses is None:
        print("PyBrowse needs the windows-curses package on Windows:\n    pip install windows-curses",
              file=sys.stderr)
        return 2
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("PyBrowse needs a real terminal. In PyCharm, run `python main.py` from the Terminal tab,\n"
              "or tick 'Emulate terminal in output console' in the run configuration.", file=sys.stderr)
        return 2
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")  # make Esc respond quickly
    browser = Browser(config, home)
    browser.new_tab(args.address and resolve_input(args.address, config.get("search_url")))
    try:
        curses.wrapper(lambda scr: UI(scr, browser).run())
    except KeyboardInterrupt:
        pass
    finally:
        browser.shutdown()
    return 0
