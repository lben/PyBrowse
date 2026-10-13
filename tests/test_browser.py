"""Browsing behaviour through the public Browser API against a real local web server."""

import json
import time

from conftest import text_of, wait

from pybrowse.browser import CACHE_RADIUS, resolve_input
from pybrowse.document import Link


def test_search_box_then_follow_result_then_back_and_forward(site, make_browser):
    site.page("/search", "<form action='/results'><input name='q'><input type='submit' value='Go'></form>")
    site.routes["/results"] = lambda h: (h.send_response(200), h.send_header("Content-Type", "text/html"),
                                         h.end_headers(), h.wfile.write(
        f"<p>results for {h.path.split('=')[1]}</p><a href='/r1'>Result one</a>".encode()))
    site.page("/r1", "<title>R1</title><p>result page</p>")
    b = make_browser(search_url=site.url + "/search?q={query}", homepage=site.url + "/search")
    tab = b.new_tab()
    doc = wait(b, tab)
    field = b.follow(tab, 1)
    b.set_value(tab, field, "cats & dogs")
    b.submit(tab, field.form)
    doc = wait(b, tab)
    assert "results for cats+%26+dogs" in text_of(doc)
    assert b.follow(tab, 1) is None
    assert "result page" in text_of(wait(b, tab))
    b.back(tab)
    assert "results for" in text_of(tab.doc)  # cached, no reload needed
    b.forward(tab)
    assert tab.doc.title == "R1"
    assert [u for u, _ in b.history][:1] == [site.url + "/r1"]


def test_tabs_open_switch_and_close(site, make_browser):
    site.page("/a", "<title>A</title><a href='/b'>to b</a>")
    site.page("/b", "<title>B</title>")
    b = make_browser(homepage=site.url + "/a")
    first = b.new_tab()
    wait(b, first)
    second = b.new_tab(b.link_url(first, 1))
    assert b.tab is second
    assert wait(b, second).title == "B"
    b.switch_tab(1)
    assert b.tab is first
    b.close_tab()
    assert b.tabs == [second]
    b.close_tab()  # closing the last tab leaves a fresh home tab
    assert len(b.tabs) == 1 and wait(b, b.tab).title == "A"


def test_bookmarks_and_cookies_survive_a_restart(site, home, make_browser):
    def login(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/html")
        h.send_header("Set-Cookie", "session=abc123; Path=/")
        h.end_headers()
        h.wfile.write(b"<title>Logged in</title>")

    site.routes["/login"] = login
    site.page("/me", "<p>me</p>")
    b = make_browser()
    tab = b.new_tab(site.url + "/login")
    wait(b, tab)
    b.add_bookmark(tab)
    b.shutdown()

    b2 = make_browser()
    assert b2.bookmarks == [{"url": site.url + "/login", "title": "Logged in"}]
    wait(b2, b2.new_tab(site.url + "/me"))
    assert site.requests[-1][3] == "session=abc123"


def test_corrupt_saved_files_are_set_aside_instead_of_crashing(home, make_browser):
    (home / "config.json").write_text("{not json")
    (home / "bookmarks.json").write_text(json.dumps([{"url": 5}, "junk", {"url": "https://x.test/", "title": "X"}]))
    (home / "cookies.txt").write_text("garbage\x00\x01")
    b = make_browser()
    assert b.config.get("width") == 0
    assert b.bookmarks == [{"url": "https://x.test/", "title": "X"}]
    assert (home / "config.json.bad").exists()


def test_settings_page_saves_valid_values_and_rejects_invalid_ones(home, make_browser):
    b = make_browser()
    tab = b.new_tab("about:config")
    fields = {f.name: f for f in tab.doc.links if not isinstance(f, Link) and f.name}
    b.set_value(tab, fields["width"], "72")
    save = next(f for f in tab.doc.links if not isinstance(f, Link) and f.kind == "submit")
    b.submit(tab, save.form, save)
    assert b.config.get("width") == 72
    assert json.loads((home / "config.json").read_text())["width"] == 72
    assert "Settings saved." in text_of(tab.doc)

    fields = {f.name: f for f in tab.doc.links if not isinstance(f, Link) and f.name}
    b.set_value(tab, fields["timeout"], "never")
    b.submit(tab, fields["timeout"].form)
    assert "Not saved: timeout must be a whole number" in text_of(tab.doc)
    assert b.config.get("timeout") == 20


def test_network_failures_become_error_pages(site, make_browser):
    def slow(h):
        time.sleep(3)

    site.routes["/slow"] = slow
    site.page("/file.zip", b"PK\x03\x04" + b"\x00" * 100, ctype="application/zip")
    b = make_browser(timeout=1)
    tab = b.new_tab(site.url + "/slow")
    assert "did not answer within 1 seconds" in text_of(wait(b, tab))
    b.navigate(tab, "http://127.0.0.1:9/")  # nothing listens on the discard port
    assert "could not connect" in text_of(wait(b, tab))
    b.navigate(tab, site.url + "/file.zip")
    assert "returned application/zip" in text_of(wait(b, tab))
    b.navigate(tab, "ftp://example.com/")
    assert "only open http and https" in text_of(wait(b, tab))


def test_oversized_pages_are_cut_off(site, make_browser):
    site.page("/big", "<p>" + "x" * (3 * 1024 * 1024) + "</p>")
    b = make_browser(max_page_mb=1)
    doc = wait(b, b.new_tab(site.url + "/big"))
    assert "[Page cut off at 1 MB." in text_of(doc)


def test_memory_stays_bounded_with_long_history(site, make_browser):
    for i in range(30):
        site.page(f"/p{i}", f"<title>{i}</title>")
    b = make_browser(history_limit=10)
    tab = b.new_tab()
    for i in range(30):
        b.navigate(tab, f"{site.url}/p{i}")
        wait(b, tab)
    assert len(tab.entries) == 10
    assert sum(e.doc is not None for e in tab.entries) <= CACHE_RADIUS + 1
    for _ in range(9):
        b.back(tab)
        wait(b, tab)
    assert tab.doc.title == "20"  # evicted pages are fetched again


def test_stopped_load_is_ignored_when_it_finishes(site, make_browser):
    site.page("/fast", "<title>fast</title>")
    site.routes["/slow"] = lambda h: (time.sleep(1), h.send_response(200), h.send_header("Content-Type", "text/html"),
                                       h.end_headers(), h.wfile.write(b"<title>slow</title>"))
    b = make_browser()
    tab = b.new_tab(site.url + "/fast")
    wait(b, tab)
    b.navigate(tab, site.url + "/slow")
    b.stop(tab)
    time.sleep(1.5)
    b.poll()
    assert tab.doc.title == "fast" and tab.loading is None


def test_post_forms_send_their_fields(site, make_browser):
    site.page("/form", "<form method='post' action='/submit'><input name='a' value='1'>"
                       "<input type='checkbox' name='c' value='yes'><input type='hidden' name='h' value='z'>"
                       "<select name='s'><option>x</option><option value='yv'>y</option></select>"
                       "<button name='go' value='ok'>Send</button></form>")
    site.page("/submit", "<p>thanks</p>")
    b = make_browser()
    tab = b.new_tab(site.url + "/form")
    doc = wait(b, tab)
    fields = [f for f in doc.links if not isinstance(f, Link)]
    text_field, checkbox, select, button = fields
    b.toggle(tab, checkbox)
    b.set_value(tab, select, "yv")
    assert "[input: 1][1] [x][2] [select: y][3] [button: Send][4]" in text_of(tab.doc)
    b.submit(tab, button.form, button)
    wait(b, tab)
    method, path, body, _ = site.requests[-1]
    assert (method, path, body) == ("POST", "/submit", b"a=1&c=yes&h=z&s=yv&go=ok")


def test_typed_addresses():
    search = "https://s.test/?q={query}"
    assert resolve_input("example.com/x", search) == "https://example.com/x"
    assert resolve_input("localhost:8000", search) == "http://localhost:8000"
    assert resolve_input("r/Python", search) == "https://www.reddit.com/r/Python"
    assert resolve_input("best tui library", search) == "https://s.test/?q=best+tui+library"
    assert resolve_input("python", search) == "https://s.test/?q=python"
    assert resolve_input("  ", search) is None
