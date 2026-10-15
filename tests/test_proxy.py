"""All traffic goes through the configured proxy, and proxy problems are explained."""

import base64
import json
import select
import socket
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import spnego
import spnego.exceptions
from conftest import FIXTURES, Site, text_of, wait

from pybrowse.config import mask_password, validate
from pybrowse.document import Link


class Proxy:
    """A forwarding HTTP proxy that optionally demands a login.

    `password`: require Basic authentication for user "user".
    `windows_login`: require the NTLM/Negotiate handshake instead, checked against the
    accounts in the NTLM_USER_FILE that the `ntlm_proxy` fixture sets up. `close_first`
    makes it drop the connection after its first refusal, as some proxies do.
    """

    def __init__(self, password=None, windows_login=False, close_first=False):
        self.requests = []
        self.schemes_used = []
        proxy = self
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # the login handshake needs the connection kept open

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                self.context = None
                self.logged_in = False

            def refuse(self, *challenges, close=False):
                self.send_response(407)
                for challenge in challenges:
                    self.send_header("Proxy-Authenticate", challenge)
                self.send_header("Content-Length", "0")
                if close:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                return True

            def refused(self):
                if windows_login:
                    return self.refused_windows_login()
                if not password:
                    return False
                expected = "Basic " + base64.b64encode(f"user:{password}".encode()).decode()
                if self.headers.get("Proxy-Authorization") == expected:
                    return False
                return self.refuse('Basic realm="corp"')

            def refused_windows_login(self):
                if self.logged_in:  # the handshake authenticates the whole connection
                    return False
                scheme, _, token = (self.headers.get("Proxy-Authorization") or "").partition(" ")
                if scheme not in ("NTLM", "Negotiate") or not token:
                    return self.refuse("Negotiate", "NTLM", close=close_first)
                proxy.schemes_used.append(scheme)
                if self.context is None:
                    self.context = spnego.server(hostname="127.0.0.1", service="HTTP", protocol=scheme.lower())
                try:
                    reply = self.context.step(base64.b64decode(token))
                except spnego.exceptions.SpnegoError:  # wrong password or domain
                    self.context = None
                    return self.refuse("Negotiate", "NTLM")
                if self.context.complete:
                    self.logged_in = True
                    return False
                return self.refuse(f"{scheme} {base64.b64encode(reply).decode()}")

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


@pytest.fixture
def ntlm_proxy(tmp_path, monkeypatch, request):
    accounts = tmp_path / "accounts.txt"
    accounts.write_text("CORP:user:s3cret\n")
    monkeypatch.setenv("NTLM_USER_FILE", str(accounts))  # pyspnego's server-side account list
    p = Proxy(windows_login=True, close_first=getattr(request, "param", False))
    yield p
    p.server.shutdown()
    p.server.server_close()


@pytest.fixture
def https_site():
    s = Site(tls=True)
    yield s
    s.server.shutdown()
    s.server.server_close()


LOGIN = "http://CORP%5Cuser:s3cret@"  # DOMAIN\user, with the backslash URL-encoded
CERT = str(FIXTURES / "localhost-cert.pem")


@pytest.mark.parametrize("ntlm_proxy", [False, True], ids=["keeps connection", "closes after first refusal"],
                         indirect=True)
def test_https_through_a_proxy_that_needs_the_windows_login(https_site, ntlm_proxy, make_browser):
    https_site.page("/secure", "<title>Secure</title><p>intranet</p>")
    b = make_browser(proxy=LOGIN + ntlm_proxy.url[len("http://"):], ca_bundle=CERT)
    tab = b.new_tab(https_site.url + "/secure")
    assert wait(b, tab).title == "Secure"
    assert ntlm_proxy.schemes_used[:1] == ["Negotiate"]  # preferred over plain NTLM
    assert ntlm_proxy.requests[0].startswith("CONNECT ")
    b.navigate(tab, https_site.url + "/secure")  # a second page reuses the logged-in connection
    assert wait(b, tab).title == "Secure"


def test_plain_http_through_a_proxy_that_needs_the_windows_login(site, ntlm_proxy, make_browser):
    site.page("/page", "<title>Plain</title>")
    b = make_browser(proxy=LOGIN + ntlm_proxy.url[len("http://"):])
    assert wait(b, b.new_tab(site.url + "/page")).title == "Plain"
    assert ntlm_proxy.schemes_used and site.requests[-1][1] == "/page"


def test_wrong_windows_login_is_explained_for_https_and_plain_http(https_site, site, ntlm_proxy, make_browser):
    https_site.page("/secure", "<title>Secure</title>")
    site.page("/page", "<title>Plain</title>")
    b = make_browser(proxy="http://CORP%5Cuser:wrong@" + ntlm_proxy.url[len("http://"):], ca_bundle=CERT)
    for url in (https_site.url + "/secure", site.url + "/page"):
        text = text_of(wait(b, b.new_tab(url)))
        assert "the proxy did not accept the username and password in the proxy setting" in text, text
        assert "Negotiate login" in text and "wrong" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="Windows always has a login to offer")
def test_missing_login_details_are_explained(https_site, ntlm_proxy):
    import os
    import subprocess
    from pathlib import Path

    # In a separate process: the test proxy's account file would otherwise be found by the
    # browser side too, since pyspnego reads NTLM_USER_FILE for both roles.
    script = """
import pathlib, sys, tempfile
from pybrowse.config import Config
from pybrowse.net import FetchError, Fetcher
home = pathlib.Path(tempfile.mkdtemp())
config = Config(home / "config.json")
assert config.update({"proxy": sys.argv[1], "ca_bundle": sys.argv[2]}) == []
try:
    Fetcher(config, home / "cookies.txt").fetch(sys.argv[3])
    print("LOADED")
except FetchError as exc:
    print(exc)
"""
    env = {k: v for k, v in os.environ.items() if k != "NTLM_USER_FILE"}
    result = subprocess.run([sys.executable, "-c", script, ntlm_proxy.url, CERT, https_site.url + "/secure"],
                            cwd=str(Path(__file__).resolve().parent.parent), env=env, capture_output=True, text=True)
    assert "this system has none to offer" in result.stdout and "DOMAIN\\user:password" in result.stdout, result


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


def test_configured_proxy_wins_over_system_proxy_settings(site, proxy, make_browser, monkeypatch):
    # On Windows the system proxy comes from the registry; the variables stand in for it here.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    site.page("/page", "<title>Via configured proxy</title>")
    b = make_browser(proxy=proxy.url.replace("http://", "http://user:s3cret@"))
    assert wait(b, b.new_tab(site.url + "/page")).title == "Via configured proxy"
    assert proxy.requests == [site.url + "/page"]


def test_name_lookup_failures_say_which_server_could_not_be_found(site, make_browser, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")  # no system proxy either
    b = make_browser(proxy="http://proxy.pybrowse-test.invalid:8080")
    text = text_of(wait(b, b.new_tab(site.url + "/page")))
    assert "could not find the proxy server proxy.pybrowse-test.invalid" in text
    assert "Using proxy http://proxy.pybrowse-test.invalid:8080" in text

    # A broken proxy breaks every Reddit mirror alike: report it once instead of trying them all.
    b.config.update({"reddit_frontends": ["https://a.pybrowse-test.invalid", "https://b.pybrowse-test.invalid"]})
    text = text_of(wait(b, b.new_tab("https://www.reddit.com/r/test/")))
    assert text.count("could not find the proxy server") == 1 and "mirrors" not in text

    b2 = make_browser(proxy="")
    text = text_of(wait(b2, b2.new_tab("http://site.pybrowse-test.invalid/")))
    assert "could not find the server site.pybrowse-test.invalid" in text
    assert "No proxy is in use" in text and "press c and set proxy" in text
