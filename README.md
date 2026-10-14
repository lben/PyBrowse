# PyBrowse

A text-only web browser for the terminal, written in Python. Pages read like a
plain `.txt` file: no colors, no bold, no images, no JavaScript. Every link and
form field gets a number like `[12]`; type the number and press Enter to follow it.

It is built first and foremost for **searching the web and reading Reddit threads**,
with tabs, history, bookmarks, cookies and a built-in settings page.

```
 1:reddit python tui at DuckDuckGo  [2:Best TUI Library/Framework - r/Python]

[Discussion] Best TUI Library/Framework
=======================================
r/Python[4] | u/LucidCrumple[5] | Oct 22 2021, 15:14:03 UTC | 27 points

I've been working on a project to streamline my interaction with multiple
independent QEMU-KVM hosts.

Comments
--------

u/MahitDzmare | 4 points | Oct 22 '21

May be textual[20] can be a good option?

    u/LucidCrumple | 1 point | Oct 22 '21

    Yeah that looks very nice and just from skimming - (maybe) easy to implement.

8%  https://www.reddit.com/r/Python/comments/qdj1xr/...  (via safereddit.com)  (? help)
```

## Install and run

Requires Python 3.10 or newer.

```
git clone https://github.com/lben/PyBrowse.git
cd PyBrowse
./pybrowse.sh          # macOS / Linux
pybrowse.cmd           # Windows (Command Prompt or PowerShell)
```

The launcher sets up a private Python environment in `.venv` and installs the
dependencies on the first run (a few seconds); after that it starts immediately.
Arguments are passed through, e.g. `./pybrowse.sh r/Python`.

To start it from any folder by typing `pybrowse`, link the script into a folder on
your `PATH` (macOS/Linux), or add the PyBrowse folder to `PATH` (Windows):

```
ln -s "$PWD/pybrowse.sh" /usr/local/bin/pybrowse
```

Prefer to manage Python yourself? `pip install -r requirements.txt` and
`python main.py` work too. On Windows, `requirements.txt` also installs
`windows-curses`, which provides the terminal library Python lacks there.

### PyCharm (Windows 11, macOS, Linux)

1. Open the `PyBrowse` folder as a project and let PyCharm create a virtual environment.
2. Open `requirements.txt` and click *Install requirements* (or run `pip install -r requirements.txt`).
3. Open the **Terminal** tab at the bottom and run `python main.py`.

PyCharm's *Run* window is not a real terminal, so a full-screen program cannot draw
there. If you prefer the Run button, edit the run configuration for `main.py` and
tick *Modify options > Emulate terminal in output console*.

You can also pass something to open right away:

```
python main.py r/Python
python main.py "best mechanical keyboard reddit"
python main.py https://example.com
```

`python main.py --dump ADDRESS` prints a page as text followed by its list of links,
which is handy for piping into other tools.

## Keys

| Keys | Action |
| --- | --- |
| Up/Down, `j`/`k` | scroll one line |
| PgDn/Space, PgUp/`b` | scroll one screen |
| Home/`g`, End/`G` | top / bottom of page |
| `12` Enter | follow link 12, or edit/toggle/press form field 12 |
| `12` `t` | open link 12 in a new tab |
| `12` `e` | open link 12 in your normal browser |
| `12` `i` | show where link 12 goes |
| `o` | open an address, `r/subreddit`, or search the web |
| `O` | edit the current address |
| `s` | search the web |
| Left/`h`/Backspace, Right/`l` | back, forward |
| `r`, Esc | reload, stop loading |
| `e` | open the current page in your normal browser |
| `/`, `n`, `N` | find on page, next, previous |
| `t`, `x` | new tab, close tab |
| `]`/Tab, `[`/Shift+Tab | next tab, previous tab |
| `T` | list tabs |
| `a`, `B` | bookmark page, show bookmarks |
| `H` | history |
| `c` | settings |
| `?` | help |
| `q` or Ctrl+C | quit |

## How search and Reddit work

Two sites in the intended workflow now block programs that are not full browsers:

* **Google** only returns results to browsers that run JavaScript and pass its bot
  checks, and its search API is closed to new users. PyBrowse searches with
  DuckDuckGo's JavaScript-free page (`html.duckduckgo.com`), which is made for text
  browsers. Reddit threads show up there just as they do on Google. You can point
  `search_url` at any other engine that works without JavaScript.
* **Reddit** returns "blocked by network security" to anything that is not a real
  browser, `old.reddit.com` requires a login, and new API keys need Reddit's manual
  approval. PyBrowse therefore reads Reddit through
  [Redlib](https://github.com/redlib-org/redlib), an open-source Reddit front end that
  serves plain HTML. Any `reddit.com`, `old.reddit.com`, `redd.it` or
  `redditmedia.com` address is fetched from the first Redlib server that answers,
  and is shown with its real `reddit.com` address. Links, sorting, subreddit search,
  "More replies" and the next-page link all work.

Public Redlib servers come and go. When one fails PyBrowse tries the next and
remembers the one that worked. If they all fail, the error page says so; update the
`reddit_frontends` setting from the
[current instance list](https://github.com/redlib-org/redlib-instances), or run your
own Redlib server (it is a single Docker container) and put its address first.

## Settings

Press `c` to open the settings page, edit a field by its number, then press the
*Save settings* button. Settings are stored as JSON in:

* Windows: `%APPDATA%\PyBrowse\config.json`
* macOS: `~/Library/Application Support/PyBrowse/config.json`
* Linux: `~/.config/pybrowse/config.json`

Set `PYBROWSE_HOME` to use another folder.

| Setting | Default | Meaning |
| --- | --- | --- |
| `homepage` | `about:home` | page for startup and new tabs |
| `search_url` | DuckDuckGo HTML | `{query}` is replaced by your search terms |
| `reddit_frontends` | public Redlib servers | tried in order; empty means fetch reddit.com directly |
| `width` | `0` | maximum text width; `0` uses the whole terminal |
| `timeout` | `20` | network timeout in seconds |
| `max_page_mb` | `5` | largest page downloaded |
| `history_limit` | `100` | back/forward entries kept per tab |
| `user_agent` | `PyBrowse/0.1 ...` | sent to websites |
| `proxy` | empty | proxy for all connections; empty uses the system proxy settings |
| `ca_bundle` | empty | certificate file for networks that inspect HTTPS; empty uses the built-in certificates |

### Behind a corporate proxy

Press `c`, set `proxy` to your proxy address, for example
`http://proxy.example.com:8080` or `http://user:password@proxy.example.com:8080`,
and save. Every connection, including searches and the Reddit mirrors, then goes
through it. The password is shown as `***` on the settings page and the settings
file is readable only by your user account.

When `proxy` is empty, PyBrowse uses the `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`
environment variables, or on Windows and macOS the system proxy settings.

If pages fail with "secure connection failed", your network probably inspects HTTPS
traffic with its own certificate. Set `ca_bundle` to your organisation's certificate
file (a `.pem`/`.crt` file your IT department can provide), for example
`C:\certs\company.pem` or `~/certs/company.pem`. The `REQUESTS_CA_BUNDLE`
environment variable also works when `ca_bundle` is empty.

Bookmarks (`bookmarks.json`), cookies (`cookies.txt`) and a small rotating log
(`pybrowse.log`) live in the same folder.

## Safety and reliability

* Page text is stripped of terminal control and escape sequences and of Unicode
  direction overrides, so a page cannot change your terminal or disguise text.
* No JavaScript is ever run. Only `http` and `https` addresses are fetched;
  `javascript:`, `data:` and `file:` links are shown as plain text.
* Downloads stop at `max_page_mb`, every request has a timeout and an overall time
  limit, and redirects are capped.
* Pages load on background threads; Esc cancels a load, and quitting never waits
  for a slow server.
* Each tab keeps rendered pages only near the current history position and older
  ones are fetched again when needed, so memory stays bounded however long you browse.
* Errors become an error page or a status-line message, never a crash; details go to
  `pybrowse.log`. Settings, bookmarks and cookies are written atomically, and a
  corrupt file is set aside as `*.bad` instead of stopping the program.
* The cookie file is readable only by your user account.

## Limitations

* No JavaScript, so sites that build their pages with JavaScript show little or
  nothing. Use `e` to open such a page in your normal browser.
* Reddit depends on public Redlib servers (see above). Logging in to Reddit is not
  supported yet.
* Images, video and file uploads are not shown; images appear as `[image: ...]`
  links you can open with `e`.

## Development

```
pip install -e ".[test]"
pytest              # offline tests, including driving the real UI in a pseudo-terminal
pytest -m live      # end-to-end over the internet: search, Reddit thread, next page
```

The pseudo-terminal UI tests need `pexpect` and run on macOS and Linux.

## License

MIT. See [LICENSE](LICENSE).
