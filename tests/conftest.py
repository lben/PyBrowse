import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pybrowse.browser import Browser
from pybrowse.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


class Site:
    """A local web server. Tests register routes: path -> (status, content type, body)."""

    def __init__(self, tls=False):
        self.routes = {}
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                site.requests.append((self.command, self.path, body, self.headers.get("Cookie")))
                route = site.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<title>Not found</title><p>nothing here</p>")
                    return
                if callable(route):
                    route(self)
                    return
                status, ctype, payload = route
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:  # self-signed certificate for 127.0.0.1, see fixtures/localhost-cert.pem
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(FIXTURES / "localhost-cert.pem", FIXTURES / "localhost-key.pem")
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def page(self, path, html, status=200, ctype="text/html; charset=utf-8"):
        self.routes[path] = (status, ctype, html.encode() if isinstance(html, str) else html)


@pytest.fixture(autouse=True)
def no_system_proxy_settings(monkeypatch):
    """Keep tests independent of this machine's proxy settings.

    With any *_proxy variable set, Python reads proxies from the environment instead of
    the OS. Reading the macOS settings in the test process, plus a failed DNS lookup, makes
    every later fork crash (see open_external), which would break the terminal tests.
    """
    monkeypatch.setenv("NO_PROXY", "pybrowse-tests.invalid")


@pytest.fixture
def site():
    s = Site()
    yield s
    s.server.shutdown()
    s.server.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PYBROWSE_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def make_browser(home):
    browsers = []

    def make(**settings):
        config = Config(home / "config.json")
        assert config.update({k: str(v) if not isinstance(v, list) else v for k, v in settings.items()}) == []
        b = Browser(config, home)
        browsers.append(b)
        return b

    yield make
    for b in browsers:
        b.shutdown()


def wait(browser, tab, timeout=15):
    """Poll until the tab has finished loading, as the UI loop does."""
    end = time.monotonic() + timeout
    while tab.loading and time.monotonic() < end:
        browser.poll()
        time.sleep(0.01)
    browser.poll()
    assert not tab.loading, "page did not finish loading"
    return tab.doc


def text_of(doc, width=100):
    return "\n".join(doc.lines(width))
