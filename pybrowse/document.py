"""Text document model: blocks of inline parts, laid out into plain lines."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from wcwidth import wcwidth

# Control characters (C0, DEL, C1) and bidirectional overrides. Web content must
# never reach the terminal with these: they can move the cursor, change terminal
# state, or visually reorder text. Invisible zero-width spaces are dropped too.
_UNSAFE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b\u200e\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]"
)
_SPACES = re.compile(r"\s+")


def sanitize(text: str) -> str:
    """Remove characters that are unsafe to send to a terminal. Keeps \\t and \\n."""
    return _UNSAFE.sub("", text)


def collapse(text: str) -> str:
    return _SPACES.sub(" ", sanitize(text))


def char_width(ch: str) -> int:
    w = wcwidth(ch)
    return w if w > 0 else 0


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def clip(text: str, width: int) -> str:
    """Longest prefix of `text` that fits in `width` terminal columns."""
    used = 0
    for i, ch in enumerate(text):
        used += char_width(ch)
        if used > width:
            return text[:i]
    return text


def _hard_split(word: str, width: int) -> list[str]:
    pieces, current, used = [], [], 0
    for ch in word:
        w = char_width(ch)
        if current and used + w > width:
            pieces.append("".join(current))
            current, used = [], 0
        current.append(ch)
        used += w
    if current:
        pieces.append("".join(current))
    return pieces


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap by terminal display width."""
    width = max(1, width)
    lines: list[str] = []
    line, used = "", 0
    for word in text.split(" "):
        if not word:
            continue
        w = text_width(word)
        if line and used + 1 + w <= width:
            line, used = f"{line} {word}", used + 1 + w
            continue
        if line:
            lines.append(line)
        if w <= width:
            line, used = word, w
        else:
            *full, line = _hard_split(word, width)
            lines.extend(full)
            used = text_width(line)
    if line:
        lines.append(line)
    return lines or [""]


@dataclass(eq=False)
class Link:
    url: str
    text: str = ""
    number: int = 0


@dataclass(eq=False)
class Form:
    action: str | None  # None when the controls are not inside a <form>
    method: str = "GET"
    fields: list["Field"] = field(default_factory=list)


@dataclass(eq=False)
class Field:
    form: Form
    kind: str  # text, password, textarea, checkbox, radio, select, submit, hidden
    name: str = ""
    value: str = ""
    label: str = ""
    checked: bool = False
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    number: int = 0

    def display(self) -> str:
        # Labels and values come from page attributes or typed text: never trust them.
        return collapse(self._display())

    def _display(self) -> str:
        if self.kind in ("text", "textarea"):
            shown = self.value or (f"({self.label})" if self.label else "")
            if len(shown) > 40:
                shown = shown[:37] + "..."
            return f"[input: {shown}]"
        if self.kind == "password":
            return f"[password: {'*' * min(len(self.value), 12)}]"
        if self.kind == "checkbox":
            return "[x]" if self.checked else "[ ]"
        if self.kind == "radio":
            return "(*)" if self.checked else "( )"
        if self.kind == "select":
            label = next((lab for val, lab in self.options if val == self.value), self.value)
            return f"[select: {label}]"
        return f"[button: {self.label or 'Submit'}]"


@dataclass(eq=False)
class Block:
    parts: list = field(default_factory=list)  # str, Link (marker after its text), Field
    indent: int = 0
    prefix: str = ""  # first-line prefix such as "* " or "1. "
    quote: int = 0  # blockquote nesting, shown as "> " per level
    pre: bool = False
    rule: str = ""  # a horizontal rule drawn with this character
    underline: str = ""  # underline the text with this character (headings)
    blank: bool = False


def part_text(part) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, Link):
        return f"[{part.number}]"
    return f"{part.display()}[{part.number}]"


@dataclass(eq=False)
class Document:
    url: str
    title: str = ""
    blocks: list[Block] = field(default_factory=list)
    links: list = field(default_factory=list)  # Link | Field; index = number - 1
    note: str = ""  # shown in the status line, e.g. which Reddit mirror served the page
    _cache: tuple[int, list[str]] | None = field(default=None, repr=False)

    def target(self, number: int):
        if 1 <= number <= len(self.links):
            return self.links[number - 1]
        return None

    def invalidate(self) -> None:
        self._cache = None

    def lines(self, width: int) -> list[str]:
        width = max(20, width)
        if self._cache is None or self._cache[0] != width:
            self._cache = (width, layout(self.blocks, width))
        return self._cache[1]


def layout(blocks: list[Block], width: int) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if block.blank:
            if out and out[-1] != "":
                out.append("")
            continue
        lead = " " * min(block.indent, width // 2) + "> " * min(block.quote, 5)
        avail = max(10, width - text_width(lead))
        if block.rule:
            out.append(lead + block.rule * avail)
            continue
        text = "".join(part_text(p) for p in block.parts)
        if block.pre:
            for raw in text.expandtabs(4).split("\n"):
                pieces = _hard_split(raw, avail) or [""]
                out.extend((lead + piece).rstrip() for piece in pieces)
            continue
        text = text.strip()
        if not text:
            continue
        hang = " " * text_width(block.prefix)
        body = wrap(text, max(1, avail - len(hang)))
        lines = [lead + (block.prefix if i == 0 else hang) + ln for i, ln in enumerate(body)]
        out.extend(lines)
        if block.underline:
            longest = max(text_width(ln) for ln in lines) - text_width(lead)
            out.append(lead + block.underline * max(1, longest))
    while out and out[-1] == "":
        out.pop()
    return out
