"""Settings, data directory and crash-safe JSON storage."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("pybrowse")

DEFAULT_FRONTENDS = [
    "https://safereddit.com",
    "https://red.artemislena.eu",
    "https://redlib.privacyredirect.com",
    "https://redlib.catsarch.com",
    "https://redlib.cow.rip",
    "https://redlib.nadeko.net",
    "https://redlib.r4fo.com",
    "https://redlib.privadency.com",
]

# name: (default, description)
SETTINGS: dict[str, tuple[object, str]] = {
    "homepage": ("about:home", "Page opened at startup and in new tabs (a URL or about:home)."),
    "search_url": ("https://html.duckduckgo.com/html/?q={query}",
                   "Search engine URL. {query} is replaced with your search terms."),
    "reddit_frontends": (DEFAULT_FRONTENDS,
                         "Redlib servers used to read Reddit, tried in order (comma-separated). "
                         "Empty means fetch reddit.com directly, which Reddit currently blocks."),
    "width": (0, "Maximum text width in columns. 0 uses the full terminal width."),
    "timeout": (20, "Network timeout in seconds (1-300)."),
    "max_page_mb": (5, "Largest page to download, in megabytes (1-100)."),
    "history_limit": (100, "Back/forward entries kept per tab (1-10000)."),
    "user_agent": ("PyBrowse/0.1 (text-mode browser; +https://github.com/lben/PyBrowse)",
                   "User-Agent sent to websites. Some sites block browsers that pretend to be Chrome."),
    "proxy": ("", "Proxy for all connections, e.g. http://proxy.example.com:8080 or "
                  "http://user:password@proxy.example.com:8080. Empty uses the system proxy settings "
                  "(HTTP_PROXY/HTTPS_PROXY variables, or the Windows proxy settings)."),
}
_INT_RANGES = {"width": (0, 1000), "timeout": (1, 300), "max_page_mb": (1, 100), "history_limit": (1, 10000)}


def data_dir() -> Path:
    override = os.environ.get("PYBROWSE_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "PyBrowse"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "PyBrowse"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pybrowse"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path, default):
    """Read JSON; a missing file gives `default`, a corrupt one is set aside as *.bad."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        try:
            os.replace(path, path.with_suffix(path.suffix + ".bad"))
        except OSError:
            pass
        return default


def save_json(path: Path, data) -> None:
    """Write JSON atomically so a crash or power loss never leaves a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return (parts.scheme in ("http", "https") and bool(parts.hostname)
            and not any(ch.isspace() for ch in parts.hostname))


def validate(name: str, value):
    """Return the cleaned value or raise ValueError with a message for the user."""
    if name not in SETTINGS:
        raise ValueError(f"unknown setting {name!r}")
    default = SETTINGS[name][0]
    if isinstance(default, int):
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValueError(f"{name} must be a whole number") from None
        low, high = _INT_RANGES[name]
        if not low <= number <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
        return number
    if isinstance(default, list):
        items = value if isinstance(value, list) else str(value).split(",")
        items = [str(item).strip().rstrip("/") for item in items if str(item).strip()]
        bad = [item for item in items if not _http_url(item)]
        if bad:
            raise ValueError(f"not an http(s) URL: {bad[0]}")
        return items
    text = str(value).strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(f"{name} contains control characters")
    if name == "search_url" and ("{query}" not in text or not _http_url(text.replace("{query}", "x"))):
        raise ValueError("search_url must be an http(s) URL containing {query}")
    if name == "homepage" and not (text.startswith("about:") or _http_url(text)):
        raise ValueError("homepage must be an http(s) URL or an about: page")
    if name == "user_agent" and not text:
        raise ValueError("user_agent cannot be empty")
    if name == "proxy" and text:
        if "://" not in text:
            text = "http://" + text  # corporate proxies are usually given as host:port
        if not urlsplit(text).scheme in ("http", "https") or not _http_url(text):
            raise ValueError("proxy must be an http(s) address like http://proxy.example.com:8080")
    return text


def mask_password(url: str) -> str:
    """The URL with any password replaced by ***, for showing on screen."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=f"{parts.username or ''}:***@{host}"))


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.values = {name: default for name, (default, _) in SETTINGS.items()}
        stored = load_json(path, {})
        if isinstance(stored, dict):
            for name, value in stored.items():
                try:
                    self.values[name] = validate(name, value)
                except ValueError as exc:
                    log.warning("ignoring setting %s: %s", name, exc)

    def get(self, name: str):
        return self.values[name]

    def update(self, changes: dict[str, str]) -> list[str]:
        """Validate and apply changes; save if all are valid. Returns error messages."""
        cleaned, errors = {}, []
        for name, value in changes.items():
            try:
                cleaned[name] = validate(name, value)
            except ValueError as exc:
                errors.append(str(exc))
        if errors:
            return errors
        self.values.update(cleaned)
        return self.save()

    def reset(self) -> list[str]:
        self.values = {name: default for name, (default, _) in SETTINGS.items()}
        return self.save()

    def save(self) -> list[str]:
        try:
            save_json(self.path, self.values)
        except OSError as exc:
            return [f"could not save settings: {exc}"]
        return []
