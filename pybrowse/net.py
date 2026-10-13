"""HTTP fetching with size limits, timeouts and persistent cookies."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from http.cookiejar import LoadError, LWPCookieJar
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import Config

log = logging.getLogger("pybrowse")


class FetchError(Exception):
    """A page could not be fetched; the message is shown to the user."""


@dataclass
class Response:
    url: str
    status: int
    content_type: str
    encoding: str | None  # charset from the Content-Type header, if any
    body: bytes
    truncated: bool


class Fetcher:
    def __init__(self, config: Config, cookie_path: Path):
        self.config = config
        self.cookie_path = cookie_path
        self.jar = LWPCookieJar(str(cookie_path))
        try:
            self.jar.load(ignore_discard=True)
        except FileNotFoundError:
            pass
        except (OSError, LoadError) as exc:
            log.warning("ignoring unreadable cookie file: %s", exc)
        self._local = threading.local()
        self._save_lock = threading.Lock()

    def _session(self) -> requests.Session:
        # One session per worker thread; all share the (internally locked) cookie jar.
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.cookies = self.jar
            session.max_redirects = 10
            session.headers.update({
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.8",
            })
            self._local.session = session
        session.headers["User-Agent"] = self.config.get("user_agent")
        return session

    def fetch(self, url: str, method: str = "GET", data=None, cancelled=lambda: False) -> Response:
        try:
            parts = urlsplit(url)
        except ValueError:
            raise FetchError(f"invalid address: {url}") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError(f"PyBrowse can only open http and https addresses, not {url}")
        timeout = self.config.get("timeout")
        limit = self.config.get("max_page_mb") * 1024 * 1024
        deadline = time.monotonic() + timeout * 3
        try:
            with self._session().request(method, url, data=data, timeout=(timeout, timeout),
                                         stream=True, allow_redirects=True) as resp:
                chunks, size, truncated = [], 0, False
                for chunk in resp.iter_content(64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        truncated = True
                        break
                    if cancelled():
                        raise FetchError("cancelled")
                    if time.monotonic() > deadline:
                        raise FetchError(f"the server took longer than {timeout * 3} seconds to send the page")
                content_type = resp.headers.get("Content-Type", "")
                encoding = requests.utils.get_encoding_from_headers(resp.headers)
                if encoding == "ISO-8859-1" and "charset" not in content_type.lower():
                    encoding = None  # requests' text/* default; let the page's <meta> decide
                result = Response(url=resp.url, status=resp.status_code,
                                  content_type=content_type.split(";")[0].strip().lower(),
                                  encoding=encoding, body=b"".join(chunks)[:limit], truncated=truncated)
        except requests.exceptions.Timeout:
            raise FetchError(f"the server did not answer within {timeout} seconds") from None
        except requests.exceptions.TooManyRedirects:
            raise FetchError("too many redirects") from None
        except requests.exceptions.SSLError as exc:
            raise FetchError(f"secure connection failed: {exc}") from None
        except requests.exceptions.ConnectionError as exc:
            raise FetchError(f"could not connect: {_root_cause(exc)}") from None
        except (requests.exceptions.RequestException, ValueError) as exc:
            raise FetchError(f"request failed: {exc}") from None
        self.save_cookies()
        return result

    def save_cookies(self) -> None:
        with self._save_lock:
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=self.cookie_path.parent, prefix="cookies", suffix=".tmp")
                os.close(fd)  # mkstemp creates the file readable only by this user
                self.jar.save(tmp, ignore_discard=True)
                os.replace(tmp, self.cookie_path)
                tmp = None
            except (OSError, RuntimeError) as exc:  # RuntimeError: jar changed by another thread
                log.warning("could not save cookies: %s", exc)
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass


def _root_cause(exc: BaseException) -> str:
    for _ in range(10):
        inner = exc.__cause__ or exc.__context__
        if inner is None:
            break
        exc = inner
    text = str(exc)
    return text.split(": ", 1)[-1] if len(text) > 120 else text
