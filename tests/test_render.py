"""Pages must read like a text file and must never be able to attack the terminal."""

from pybrowse.document import Link, text_width
from pybrowse.render import render_html


def lines(html, width=60, url="https://site.test/page"):
    doc = render_html(html, url)
    return doc, doc.lines(width)


def test_page_text_cannot_inject_terminal_control_sequences():
    html = ("<title>t\x1b]0;owned\x07</title><p>safe\x1b[2J\x1b]52;c;ZXZpbA==\x07 text\x9b31m"
            "\u202eevil\u202c\x00end</p><pre>code\x1b[31m\r\nline</pre>")
    doc, out = lines(html)
    for line in out + [doc.title]:
        assert not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in line), repr(line)
        assert "\u202e" not in line
    assert "safe[2J]52;c;ZXZpbA== text31mevilend" in out[0]


def test_invisible_and_scripted_content_is_not_shown_and_unsafe_links_are_not_followable():
    html = """<script>alert(1)</script><style>p{}</style>
    <p>visible <span style="display: none">hidden1</span><span hidden>hidden2</span>
    <span aria-hidden="true">hidden3</span></p>
    <a href="javascript:alert(1)">js</a> <a href="data:text/html,x">data</a>
    <a href="file:///etc/passwd">file</a> <a href="about:config">about</a>
    <a href="/next">next</a> <a href="mailto:a@b.c">mail</a>"""
    doc, out = lines(html)
    text = "\n".join(out)
    for hidden in ("alert", "p{}", "hidden1", "hidden2", "hidden3"):
        assert hidden not in text
    assert [item.url for item in doc.links] == ["https://site.test/next"]
    assert "js data file about next[1] mail" in text


def test_text_wraps_to_the_width_including_wide_characters():
    html = "<p>" + "word " * 50 + "</p><p>" + "\u6f22\u5b57" * 40 + "</p><ul><li>" + "item " * 30 + "</li></ul>"
    for width in (20, 33, 80):
        doc, out = lines(html, width)
        assert all(text_width(line) <= width for line in out), width
    _, out = lines(html, 40)
    first = next(i for i, line in enumerate(out) if line.startswith("* item"))
    assert out[first + 1].startswith("  item")  # continuation lines hang under the bullet text


def test_links_images_lists_tables_and_headings_become_plain_text():
    html = """<h1>Title</h1><p>Read <a href="/a">the docs</a> or
    <a href="https://other.test/"><img src="/i.png" alt="logo"></a>.</p>
    <img src="/photo.jpg" alt="A cat"><img src="/spacer.gif" alt=""><img src="/t.gif" width="1">
    <ol><li>one</li><li>two<ul><li>nested</li></ul></li></ol>
    <table><tr><th>Name</th><th>Score</th></tr><tr><td>a</td><td>1</td></tr></table>"""
    doc, out = lines(html, 60)
    assert out[:2] == ["Title", "====="]
    text = "\n".join(out)
    assert "Read the docs[1] or [image: logo][2]." in text
    assert "[image: A cat][3]" in text and "spacer" not in text and "t.gif" not in text
    assert "1. one\n2. two\n  * nested" in text
    assert "Name | Score\na | 1" in text
    assert [l.url for l in doc.links if isinstance(l, Link)] == [
        "https://site.test/a", "https://other.test/", "https://site.test/photo.jpg"]


def test_deeply_nested_markup_does_not_crash():
    html = "<div>" * 5000 + "deep" + "</div>" * 5000 + "<b>" * 3000 + "bold"
    doc, out = lines(html)
    assert "deep" in "\n".join(out) and "bold" in "\n".join(out)


def test_form_control_attributes_cannot_inject_terminal_control_sequences():
    html = ('<input name="a\x1b]0;PWNED\x07b"><input type="image" alt="c\x1b[2Jd">'
            '<button value="e\x1b[31mf">&#8594;</button><select name="s"><option>g\x9bh</option></select>')
    _, out = lines(html)
    assert out and not any(ord(c) < 32 or 127 <= ord(c) < 160 for line in out for c in line), out


def test_icon_link_with_blank_accessible_name_still_renders_the_page():
    html = '<p>Article text.</p><p><a href="/home" aria-label=" "><img src="logo.png" alt=""></a></p>'
    doc, out = lines(html)
    assert out == ["Article text.", "", "link[1]"]
    assert "code" in "\n".join(lines('<a href="/x"><pre>code</pre></a>')[1])
