"""Drive the real full-screen program in a pseudo-terminal and read the screen back."""

import json
import random
import sys
import time
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("pseudo-terminals are POSIX-only", allow_module_level=True)
pexpect = pytest.importorskip("pexpect")
pyte = pytest.importorskip("pyte")

ROOT = Path(__file__).resolve().parent.parent


class Terminal:
    def __init__(self, home, args, cols=90, rows=24):
        env = {"PYBROWSE_HOME": str(home), "TERM": "xterm-256color", "LANG": "en_US.UTF-8",
               "PATH": "/usr/bin:/bin"}
        self.child = pexpect.spawn(sys.executable, [str(ROOT / "main.py"), *args], cwd=str(ROOT), env=env,
                                   dimensions=(rows, cols), encoding="utf-8", codec_errors="replace")
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    def pump(self, seconds=0.3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.stream.feed(self.child.read_nonblocking(65536, timeout=0.05))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                return

    @property
    def text(self):
        return "\n".join(line.rstrip() for line in self.screen.display)

    def wait_for(self, needle, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.pump(0.1)
            if needle in self.text:
                return
        raise AssertionError(f"{needle!r} not on screen:\n{self.text}")

    def keys(self, data, settle=0.3):
        self.child.send(data)
        self.pump(settle)

    def resize(self, cols, rows):
        self.screen.resize(rows, cols)
        self.child.setwinsize(rows, cols)
        self.pump(0.5)

    def exit_status(self, seconds=10):
        self.child.expect(pexpect.EOF, timeout=seconds)
        self.child.close()
        return self.child.exitstatus


@pytest.fixture
def pages(site):
    site.page("/", "<title>Start</title><h1>Start page</h1><p>Go to <a href='/second'>the second page</a> "
                   "or <a href='/long'>a long page</a>.</p>")
    site.page("/second", "<title>Second</title><p>You reached the second page.</p>")
    site.page("/long", "<title>Long</title>" + "".join(f"<p>paragraph {i} " + "filler " * 20 + "</p>"
                                                      for i in range(80)) + "<p>needle at the end</p>")
    return site


def configure(home, site):
    (home / "config.json").write_text(json.dumps({
        "homepage": site.url + "/", "search_url": site.url + "/?q={query}"}))


def assert_no_logged_errors(home):
    log = home / "pybrowse.log"
    if log.exists():
        assert "Traceback" not in log.read_text(), log.read_text()


def test_read_follow_links_tabs_find_and_quit(home, pages):
    configure(home, pages)
    t = Terminal(home, [])
    t.wait_for("Start page")
    assert t.screen.display[0].startswith("[1:Start]")
    assert "the second page[1]" in t.text

    t.keys("1")
    assert "Link 1: the second page ->" in t.text
    t.keys("\r")
    t.wait_for("You reached the second page.")
    t.keys("h")
    t.wait_for("Start page")

    t.keys("2t")  # open link 2 in a new tab
    t.wait_for("paragraph 0")
    assert "1:Start" in t.screen.display[0] and "[2:Long]" in t.screen.display[0]
    t.keys("/")
    t.keys("needle\r")
    t.wait_for('Found "needle"')
    assert "needle at the end" in t.text
    t.keys("G")
    assert t.screen.display[-1].startswith("100%")

    t.resize(50, 20)  # the page re-wraps to the new width
    t.wait_for("needle at the end")
    assert all(len(line.rstrip()) <= 50 for line in t.screen.display)

    t.keys("]")
    t.wait_for("Start page")
    t.keys("q")
    t.wait_for("Quit PyBrowse? (y/n)")
    t.keys("y\r")
    assert t.exit_status() == 0
    assert_no_logged_errors(home)


def test_random_keys_never_crash_the_browser(home, pages):
    configure(home, pages)
    t = Terminal(home, [])
    t.wait_for("Start page")
    rng = random.Random(1234)
    keys = list("0123456789jkbgGhlrnNaTBHc?/st[]x \t\r\x7f\x1b") + [
        "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "\x1b[5~", "\x1b[6~", "\x1b[H", "\x1b[F", "\x1b[Z",
        "o", "O", "é", "漢", "\U0001f600", "\x15"]
    for _ in range(400):
        t.child.send(rng.choice(keys))
        if rng.random() < 0.1:
            t.pump(0.05)
    for size in ((30, 10), (120, 40), (20, 5), (80, 24)):
        t.resize(*size)
    t.keys("\x1b\x1b\x1b", 0.5)
    t.keys("?")
    t.wait_for("Links and forms")
    assert t.child.isalive()
    t.keys("\x03")  # Ctrl+C quits cleanly
    assert t.exit_status() == 0
    assert_no_logged_errors(home)


def test_refuses_to_start_without_a_terminal(home):
    import subprocess

    result = subprocess.run([sys.executable, str(ROOT / "main.py")], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, env={"PYBROWSE_HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "Terminal tab" in result.stderr


def test_home_backspace_and_escape_keys(home, pages):
    configure(home, pages)
    pages.routes["/slow"] = lambda h: (time.sleep(8), h.send_response(200), h.end_headers())
    t = Terminal(home, [pages.url + "/second"])
    t.wait_for("You reached the second page.")
    t.keys("\x1b[H")  # Home key: the home page
    t.wait_for("Start page")
    t.keys("\x7f")  # Backspace: back to where we were
    t.wait_for("You reached the second page.")

    t.keys("?")
    t.wait_for("Links and forms")
    t.keys("\x1b", 0.5)  # Esc leaves the help page
    t.wait_for("You reached the second page.")

    t.keys("o")
    t.keys(pages.url + "/slow\r")
    t.wait_for("Loading")
    t.keys("\x1b", 0.5)  # Esc stops a slow load
    t.wait_for("Stopped.")
    assert "You reached the second page." in t.text
    t.keys("\x03")
    assert t.exit_status() == 0
    assert_no_logged_errors(home)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific crash")
def test_system_browser_still_opens_after_a_failed_lookup_on_macos():
    import subprocess

    # Run in a fresh process: the trigger breaks fork() for the rest of that process.
    script = """
import socket, urllib.request
from pybrowse import app
urllib.request.getproxies(); urllib.request.proxy_bypass("example.com")  # read macOS proxy settings
try:
    socket.getaddrinfo("nosuch.pybrowse-tests.invalid", 80)
except socket.gaierror:
    pass
app.MAC_OPEN = "/usr/bin/true"  # stands in for /usr/bin/open so no browser window appears
print(app.open_external("https://example.com/"))
"""
    env = {"PATH": "/usr/bin:/bin", "PYBROWSE_HOME": "/tmp"}
    result = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT), env=env, capture_output=True, text=True)
    assert result.stdout.strip() == "True", result.stderr
