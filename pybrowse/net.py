"""HTTP fetching with size limits, timeouts and persistent cookies."""

from __future__ import annotations

import logging
import os
import re
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from http.cookiejar import LoadError, LWPCookieJar
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import Config, mask_password

log = logging.getLogger("pybrowse")


class FetchError(Exception):
    """A page could not be fetched; the message is shown to the user.

    `hint` is advice on fixing the cause (shown once even when several servers failed).
    `proxy_failed` means the proxy itself failed, so every other server would fail too.
    """

    def __init__(self, message: str, hint: str = "", proxy_failed: bool = False):
        super().__init__(message)
        self.hint = hint
        self.proxy_failed = proxy_failed


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
        proxy = self.config.get("proxy")
        # Passed per request on purpose: requests lets the HTTP(S)_PROXY / REQUESTS_CA_BUNDLE
        # variables and the Windows proxy settings override session-level values, but not
        # per-request ones. Empty settings fall back to those system defaults.
        proxies = {"http": proxy, "https": proxy} if proxy else None
        verify = self.config.get("ca_bundle") or True
        try:
            with self._session().request(method, url, data=data, timeout=(timeout, timeout), stream=True,
                                         allow_redirects=True, proxies=proxies, verify=verify) as resp:
                if resp.status_code == 407:  # a plain-http request refused by the proxy itself
                    raise FetchError("the proxy asked for a username and password", self._proxy_hint(url),
                                     proxy_failed=True)
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
            raise FetchError(f"secure connection failed: {_root_cause(exc)}", self._tls_hint()) from None
        except requests.exceptions.ConnectionError as exc:  # includes ProxyError
            log.warning("could not fetch %s: %r", url, exc)
            to_proxy = isinstance(exc, requests.exceptions.ProxyError)
            lookup = _name_lookup_failure(exc)
            if lookup:
                host, reason = lookup
                if host is None:
                    host = urlsplit(self._proxy_for(url) if to_proxy else url).hostname
                server = "the proxy server" if to_proxy else "the server"
                message = f"could not find {server} {host} (name lookup failed: {reason})"
            else:
                message = f"{'proxy error' if to_proxy else 'could not connect'}: {_root_cause(exc)}"
            raise FetchError(message, self._proxy_hint(url), proxy_failed=to_proxy) from None
        except (requests.exceptions.RequestException, ValueError, OSError) as exc:
            # OSError: requests could not read the ca_bundle file
            raise FetchError(f"request failed: {exc}", self._tls_hint()) from None
        self.save_cookies()
        return result

    def _proxy_for(self, url: str) -> str:
        """The proxy that applies to `url`: the setting, else the system proxy, else ''."""
        if self.config.get("proxy"):
            return self.config.get("proxy")
        try:
            proxies = requests.utils.get_environ_proxies(url)  # variables, or OS settings
        except Exception:  # reading OS proxy settings must never break a page load
            return ""
        return proxies.get(urlsplit(url).scheme) or proxies.get("all") or ""

    def _proxy_hint(self, url: str) -> str:
        if self.config.get("proxy"):
            return f"Using proxy {mask_password(self.config.get('proxy'))}. Press c to change the proxy setting."
        system = self._proxy_for(url)
        if system:
            return (f"Using the system proxy {mask_password(system)} (from the HTTP(S)_PROXY variables or the "
                    "operating system's proxy settings). Press c and set proxy to use a different one.")
        return "No proxy is in use. On a corporate network, press c and set proxy to your proxy's address."

    def _tls_hint(self) -> str:
        bundle = self.config.get("ca_bundle")
        if bundle:
            return f"HTTPS sites are verified with the certificate file {bundle}. Press c to change ca_bundle."
        return ("If your network inspects HTTPS traffic with its own certificate, press c and set "
                "ca_bundle to your organisation's certificate file.")

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


def _chain(exc: BaseException):
    """The exception and everything it wraps (causes, contexts and urllib3 `reason`s)."""
    seen, pending = set(), [exc]
    while pending and len(seen) < 20:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for inner in (current.__cause__, current.__context__, getattr(current, "reason", None), *current.args):
            if isinstance(inner, BaseException):
                pending.append(inner)


def _name_lookup_failure(exc: BaseException) -> tuple[str | None, str] | None:
    """(host if known, reason) when a DNS lookup failed, e.g. Windows error 11001."""
    chain = list(_chain(exc))
    gai = next((e for e in chain if isinstance(e, socket.gaierror)), None)
    if gai is None:
        return None
    named = (re.search(r"Failed to resolve '([^']+)'", str(e)) for e in chain)
    host = next((m.group(1) for m in named if m), None)
    return host, str(gai)


def _root_cause(exc: BaseException) -> str:
    for _ in range(10):
        inner = exc.__cause__ or exc.__context__
        if inner is None:
            break
        exc = inner
    text = str(exc)
    return text.split(": ", 1)[-1] if len(text) > 120 else text
