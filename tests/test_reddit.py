"""Reddit is the first site that must work: posts, comment trees, listings, paging."""

import re

from conftest import FIXTURES, Site, text_of, wait

from pybrowse.document import Link

POST_URL = "https://www.reddit.com/r/testsub/comments/abc123/which_editor/"
POST_PATH = "/r/testsub/comments/abc123/which_editor/"


def mirrors():
    challenge, down, good = Site(), Site(), Site()
    challenge.page(POST_PATH, "<html><title>Making sure you're not a bot!</title><p>Anubis</p></html>")
    down.page(POST_PATH, "<p>Forbidden</p>", status=403)
    good.page(POST_PATH, (FIXTURES / "redlib_post.html").read_bytes())
    good.page("/r/testsub", (FIXTURES / "redlib_listing.html").read_bytes())
    return challenge, down, good


def test_post_with_comment_tree_reads_as_plain_text_via_first_working_mirror(make_browser):
    challenge, down, good = mirrors()
    try:
        b = make_browser(reddit_frontends=[challenge.url, down.url, good.url])
        tab = b.new_tab(POST_URL)
        doc = wait(b, tab)
        text = text_of(doc)

        assert doc.url == POST_URL  # the real Reddit address is shown, not the mirror
        assert doc.note == "via 127.0.0.1"
        assert "[Question] Which editor do you use?\n=================================" in text
        assert "r/testsub[" in text and "Oct 12 2026, 07:58:33 UTC | 149 points" in text
        assert "I am choosing between & two editors." in text
        assert "> A quoted line" in text
        assert 'print("hello")\n    indented()' in text  # code keeps its layout
        assert "\nu/bob | 95 points | 1d ago\n\nTop level comment text." in text
        assert "\n    u/carol | 1 point | 20h ago | edited\n\n    A nested reply with a link[" in text
        assert "\n        \u2192 More replies (9)[" in text
        assert "u/dave | 7 points" in text
        for noise in ("MAIN FEEDS", "settings", "You are about to leave", "redlib.", "About", "\u200b"):
            assert noise not in text

        links = {item.text: item.url for item in doc.links if isinstance(item, Link)}
        assert links["\u2192 More replies (9)"] == "https://www.reddit.com/r/testsub/comments/abc123/which_editor/c2"
        assert links["link"] == "https://example.com/doc"
        assert links["[image: Screenshot of an editor]"] == good.url + "/img/abc.png"  # media stays on the mirror

        # The mirror that worked is tried first next time.
        challenge_hits = len(challenge.requests)
        b.reload(tab)
        wait(b, tab)
        assert len(challenge.requests) == challenge_hits
    finally:
        for s in (challenge, down, good):
            s.server.shutdown()


def test_comment_sort_and_subreddit_search_forms_stay_on_reddit_addresses(make_browser):
    challenge, down, good = mirrors()
    try:
        b = make_browser(reddit_frontends=[good.url])
        tab = b.new_tab(POST_URL)
        doc = wait(b, tab)
        fields = [item for item in doc.links if not isinstance(item, Link)]
        sort = next(f for f in fields if f.kind == "select")
        b.set_value(tab, sort, "new")
        b.submit(tab, sort.form)
        assert tab.loading == POST_URL + "?sort=new"
        search = next(f for f in fields if f.kind == "text")
        assert search.form.action == "https://www.reddit.com/r/testsub/search"
    finally:
        for s in (challenge, down, good):
            s.server.shutdown()


def test_subreddit_listing_shows_posts_sorting_and_next_page(make_browser):
    challenge, down, good = mirrors()
    try:
        b = make_browser(reddit_frontends=[good.url])
        tab = b.new_tab()
        b.open("r/testsub", tab)
        doc = wait(b, tab)
        text = text_of(doc)
        links = {item.text: item.url for item in doc.links if isinstance(item, Link)}

        assert re.search(r"Hot\[\d+\] New\[\d+\]", text)
        assert "First post title[" in text
        assert "   9112 points | r/testsub | u/erin | 1d ago | 5.0k comments[" in text
        assert "   1 point | r/testsub | u/frank | 2d ago | 98 comments[" in text
        assert links["link: news.example.org"] == "https://news.example.org/story"
        assert links["First post title"] == "https://www.reddit.com/r/testsub/comments/p1/first_post/"
        assert links["NEXT"] == "https://www.reddit.com/r/testsub?sort=hot&t=&after=t3_p2"
        assert "A community for tests." in text
        assert "Subscribe" not in text and "-" * 50 not in text
    finally:
        for s in (challenge, down, good):
            s.server.shutdown()


def test_all_mirrors_failing_gives_an_explanation_not_a_crash(make_browser):
    challenge, down, good = mirrors()
    try:
        b = make_browser(reddit_frontends=[challenge.url, down.url])
        doc = wait(b, b.new_tab(POST_URL))
        text = text_of(doc)
        assert "None of the configured mirrors could load this page" in text
        assert "not a Redlib page" in text and "HTTP 403" in text
        assert "reddit_frontends" in text
    finally:
        for s in (challenge, down, good):
            s.server.shutdown()


def test_reddit_address_forms_are_recognised(make_browser):
    from pybrowse.reddit import reddit_path

    assert reddit_path("https://old.reddit.com/r/x/comments/1/t/?sort=new") == "/r/x/comments/1/t/?sort=new"
    assert reddit_path("https://redd.it/abc12") == "/comments/abc12"
    assert reddit_path("https://www.reddit.com/gallery/abc12") == "/comments/abc12"
    assert reddit_path("https://www.redditmedia.com/r/x/comments/1/t/") == "/r/x/comments/1/t/"
    assert reddit_path("https://i.redditmedia.com/image.png") is None
    assert reddit_path("https://notreddit.com/r/x") is None
    assert reddit_path("https://evilreddit.com/r/x") is None
