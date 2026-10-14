"""All traffic goes through the configured proxy, and proxy problems are explained."""

import base64
import json
import select
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import text_of, wait

from pybrowse.config import mask_password, validate
from pybrowse.document import Link


class Proxy:
    """A forwarding HTTP proxy that optionally demands Basic authentication."""

    def __init__(self, password=None):
        self.requests = []
        proxy = self
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def refused(self):
                if not password:
                    return False
                expected = "Basic " + base64.b64encode(f"user:{password}".encode()).decode()
                if self.headers.get("Proxy-Authorization") == expected:
                    return False
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="corp"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True

            def do_CONNECT(self):  # https goes through a tunnel
                proxy.requests.append("CONNECT " + self.path)
                if self.refused():
                    return
                host, _, port = self.path.rpartition(":")
                try:
                    upstream = socket.create_connection((host, int(port)), timeout=10)
                except OSError:
                    self.send_error(502)
                    return
                self.send_response(200, "Connection Established")
                self.end_headers()
                with upstream:
                    pair = {self.connection: upstream, upstream: self.connection}
                    while True:
                        ready, _, _ = select.select(list(pair), [], [], 30)
                        if not ready:
                            return
                        for sock in ready:
                            data = sock.recv(65536)
                            if not data:
                                return
                            pair[sock].sendall(data)

            def do_GET(self):
                proxy.requests.append(self.path)  # a proxy receives the absolute URL
                if self.refused():
                    return
                with direct.open(self.path, timeout=5) as upstream:
                    body = upstream.read()
                self.send_response(upstream.status)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "text/html"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def proxy():
    p = Proxy(password="s3cret")
    yield p
    p.server.shutdown()
    p.server.server_close()


def test_pages_are_fetched_through_the_proxy_with_its_credentials(site, proxy, make_browser):
    site.page("/page", "<title>Via proxy</title><p>hello</p>")
    b = make_browser(proxy=proxy.url.replace("http://", "http://user:s3cret@"))
    doc = wait(b, b.new_tab(site.url + "/page"))
    assert doc.title == "Via proxy"
    assert proxy.requests == [site.url + "/page"]
    assert site.requests[-1][1] == "/page"


def test_proxy_refusal_and_unreachable_proxy_are_explained(site, proxy, make_browser):
    site.page("/page", "<p>hello</p>")
    b = make_browser(proxy=proxy.url)  # no password
    text = text_of(wait(b, b.new_tab(site.url + "/page")))
    assert "the proxy asked for a username and password" in text
    assert "Using proxy " + proxy.url in text and "Press c" in text

    b2 = make_browser(proxy="http://user:pw@127.0.0.1:9")  # nothing listens on the discard port
    text = text_of(wait(b2, b2.new_tab(site.url + "/page")))
    assert "proxy error" in text
    assert "Using proxy http://user:***@127.0.0.1:9" in text and "pw@" not in text


def test_settings_page_masks_the_proxy_password_but_keeps_it_on_save(home, make_browser):
    b = make_browser(proxy="http://me:topsecret@proxy.corp.test:3128")
    tab = b.new_tab("about:config")
    assert "topsecret" not in text_of(tab.doc)
    fields = {f.name: f for f in tab.doc.links if not isinstance(f, Link) and f.name}
    assert fields["proxy"].value == "http://me:***@proxy.corp.test:3128"
    b.set_value(tab, fields["width"], "80")
    b.submit(tab, fields["width"].form)
    assert "Settings saved." in text_of(tab.doc)
    saved = json.loads((home / "config.json").read_text())
    assert saved["proxy"] == "http://me:topsecret@proxy.corp.test:3128" and saved["width"] == 80

    fields = {f.name: f for f in tab.doc.links if not isinstance(f, Link) and f.name}
    b.set_value(tab, fields["proxy"], "")
    b.submit(tab, fields["proxy"].form)
    assert b.config.get("proxy") == ""


@pytest.mark.live
def test_reddit_over_https_goes_through_the_proxy_tunnel(proxy, make_browser):
    b = make_browser(proxy=proxy.url.replace("http://", "http://user:s3cret@"))
    tab = b.new_tab()
    b.open("r/Python", tab)
    doc = wait(b, tab, timeout=300)
    assert doc.note.startswith("via "), text_of(doc)
    assert any(r.startswith("CONNECT ") and r.endswith(":443") for r in proxy.requests), proxy.requests


def test_proxy_setting_is_validated():
    assert validate("proxy", "proxy.corp.test:8080") == "http://proxy.corp.test:8080"
    assert validate("proxy", " ") == ""
    assert mask_password("http://proxy.corp.test:8080") == "http://proxy.corp.test:8080"
    for bad in ("socks5://proxy.corp.test:1080", "http://", "not a proxy"):
        with pytest.raises(ValueError):
            validate("proxy", bad)
