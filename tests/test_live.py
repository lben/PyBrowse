"""The real-world flow over the internet. Run with: pytest -m live"""

import pytest
from conftest import text_of, wait

from pybrowse.document import Link

pytestmark = pytest.mark.live


def test_search_then_read_a_reddit_thread_then_next_results_page(make_browser):
    b = make_browser()
    tab = b.new_tab()
    b.open("reddit python text user interface library", tab)
    results = wait(b, tab, timeout=60)
    reddit_links = [l for l in results.links if isinstance(l, Link) and "reddit.com/r/" in l.url]
    assert reddit_links, text_of(results)

    b.navigate(tab, reddit_links[0].url)
    thread = wait(b, tab, timeout=300)
    text = text_of(thread)
    assert thread.note.startswith("via "), text
    assert "Comments\n--------" in text and " points" in text

    b.back(tab)
    next_button = next(f for f in tab.doc.links if not isinstance(f, Link) and f.label == "Next")
    b.submit(tab, next_button.form, next_button)
    page2 = wait(b, tab, timeout=60)
    assert len([l for l in page2.links if isinstance(l, Link)]) >= 5, text_of(page2)
