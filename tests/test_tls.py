"""HTTPS sites signed by an organisation's own certificate work once ca_bundle points at it."""

import pytest
from conftest import FIXTURES, Site, text_of, wait

from pybrowse.config import validate

CERT = str(FIXTURES / "localhost-cert.pem")


@pytest.fixture
def https_site():
    s = Site(tls=True)
    yield s
    s.server.shutdown()
    s.server.server_close()


def test_ca_bundle_setting_makes_a_privately_signed_https_site_load(https_site, make_browser):
    https_site.page("/secure", "<title>Secure</title><p>intranet page</p>")

    b = make_browser()  # built-in certificates only: the private certificate is not trusted
    text = text_of(wait(b, b.new_tab(https_site.url + "/secure")))
    assert "secure connection failed" in text
    assert "press c and set ca_bundle" in text

    b2 = make_browser(ca_bundle=CERT)
    assert wait(b2, b2.new_tab(https_site.url + "/secure")).title == "Secure"
    assert b2.config.get("ca_bundle") == CERT


def test_reddit_mirror_failures_show_the_certificate_hint_once(https_site, make_browser):
    b = make_browser(reddit_frontends=[https_site.url, https_site.url + "/other"])
    text = text_of(wait(b, b.new_tab("https://www.reddit.com/r/test/")))
    assert text.count("secure connection failed") == 2
    assert text.count("press c and set ca_bundle") == 1


def test_ca_bundle_must_be_an_existing_file(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        validate("ca_bundle", str(tmp_path / "missing.pem"))
    assert validate("ca_bundle", "") == ""
    assert validate("ca_bundle", CERT) == CERT


def test_ca_bundle_setting_wins_over_the_certificate_environment_variable(https_site, make_browser,
                                                                          monkeypatch):
    import certifi

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certifi.where())  # trusts public CAs only
    https_site.page("/secure", "<title>Secure</title>")
    b = make_browser(ca_bundle=CERT)
    assert wait(b, b.new_tab(https_site.url + "/secure")).title == "Secure"
