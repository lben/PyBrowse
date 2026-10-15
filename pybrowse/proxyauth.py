"""Logging in to proxies that use the Windows login handshake (NTLM or Negotiate/Kerberos).

Many corporate proxies refuse plain username/password (Basic) logins and instead run the
multi-step handshake Windows uses. The pyspnego library performs that handshake: on
Windows with the account you are logged in as, so no password is needed, and elsewhere
with the username and password given in the proxy address.

For HTTPS pages the handshake has to happen on the CONNECT tunnel, below the layer where
requests sees responses, so it is done in a connection class. For plain HTTP pages it is
done by repeating the request with the next token, like a browser does.
"""

from __future__ import annotations

import base64
import http.client
import logging
from urllib.parse import unquote, urlsplit

import requests.adapters
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import ProxyManager

try:
    import spnego
    import spnego.exceptions
except ImportError:  # pragma: no cover - pyspnego is in requirements.txt
    spnego = None

log = logging.getLogger("pybrowse")

SCHEMES = ("Negotiate", "NTLM")  # in order of preference; Negotiate can carry Kerberos or NTLM
MAX_STEPS = 5


class ProxyLoginError(OSError):
    """The proxy login handshake failed; the message is for the user."""


class ProxyLoginRestart(OSError):
    """The proxy closed the connection after naming its login scheme. The next
    connection starts the handshake immediately, so the request is simply retried."""


def challenges(values) -> dict[str, bytes | None]:
    """Parse Proxy-Authenticate header values into {scheme: token or None}."""
    offered: dict[str, bytes | None] = {}
    for value in values:
        for part in value.split(","):
            scheme, _, token = part.strip().partition(" ")
            scheme = {"ntlm": "NTLM", "negotiate": "Negotiate", "basic": "Basic"}.get(scheme.lower(), scheme)
            try:
                offered[scheme] = base64.b64decode(token.strip()) if token.strip() else None
            except ValueError:
                continue
    return offered


def choose(offered: dict) -> str | None:
    return next((scheme for scheme in SCHEMES if scheme in offered), None)


def _token(data: bytes | None) -> str:
    return base64.b64encode(data or b"").decode("ascii")


class ProxyLogin:
    """What is known about one proxy's login, shared by all connections to it."""

    def __init__(self, proxy_url: str, proxy_headers: dict | None = None):
        parts = urlsplit(proxy_url)
        self.host = parts.hostname or ""
        self.username = unquote(parts.username) if parts.username else None
        self.password = unquote(parts.password) if parts.password else None
        self.proxy_headers = proxy_headers if proxy_headers is not None else {}
        self.scheme: str | None = None  # learned from the proxy's first refusal

    def use(self, scheme: str) -> None:
        self.scheme = scheme
        # urllib3 copies these headers over every plain-HTTP request's own headers, so the
        # Basic login built from the address would replace the handshake token.
        self.proxy_headers.pop("Proxy-Authorization", None)

    def new_context(self, scheme: str):
        if spnego is None:
            raise ProxyLoginError("this proxy needs the Windows login handshake, which requires the pyspnego "
                                  "package: run  pip install -r requirements.txt")
        try:
            return spnego.client(self.username, self.password, hostname=self.host, service="HTTP",
                                 protocol=scheme.lower())
        except spnego.exceptions.SpnegoError as exc:
            raise ProxyLoginError(self.describe_failure(exc)) from None

    def step(self, context, token: bytes | None = None) -> bytes | None:
        """One handshake step; library errors become a ProxyLoginError for the user.

        (urllib3 only converts OSErrors raised while connecting, so anything else would
        escape as an internal error.)
        """
        try:
            return context.step(token)
        except spnego.exceptions.SpnegoError as exc:
            raise ProxyLoginError(self.describe_failure(exc)) from None

    def describe_failure(self, exc: Exception | None = None) -> str:
        who = "the username and password in the proxy setting" if self.username else "the current login"
        detail = f" ({exc})" if exc else ""
        if not self.username:
            return (f"the proxy wants a Windows-style ({self.scheme or 'NTLM'}) login and this system has none "
                    f"to offer{detail}. Add DOMAIN\\user:password to the proxy address in the settings.")
        return f"the proxy did not accept {who} for its {self.scheme or 'NTLM'} login{detail}"


class ProxyLoginHTTPSConnection(HTTPSConnection):
    """Performs the login handshake on the CONNECT tunnel when the proxy asks for it."""

    login: ProxyLogin

    def _tunnel(self) -> None:
        login = self.login
        headers = dict(self._tunnel_headers)
        context = None
        scheme = login.scheme
        if scheme:  # known from an earlier connection: start the handshake straight away
            context = login.new_context(scheme)
            headers["Proxy-Authorization"] = f"{scheme} {_token(login.step(context))}"
        for _ in range(MAX_STEPS):
            status, reason, offered, will_close = self._connect_once(headers)
            if status == 200:
                return
            if status != 407:
                self.close()
                raise OSError(f"Tunnel connection failed: {status} {reason}")
            if context is None:
                scheme = choose(offered)
                if scheme is None:  # a Basic-only proxy: nothing more we can do here
                    self.close()
                    raise OSError(f"Tunnel connection failed: {status} {reason}")
                login.use(scheme)
                if will_close:
                    self.close()
                    raise ProxyLoginRestart()
                context = login.new_context(scheme)
                token = login.step(context)
            else:
                challenge = offered.get(scheme)
                if challenge is None or context.complete:
                    self.close()
                    raise ProxyLoginError(login.describe_failure())
                if will_close:
                    self.close()
                    raise ProxyLoginError("the proxy closed the connection in the middle of the login handshake")
                try:
                    token = login.step(context, challenge)
                except ProxyLoginError:
                    self.close()
                    raise
            headers["Proxy-Authorization"] = f"{scheme} {_token(token)}"
        self.close()
        raise ProxyLoginError("the proxy kept asking for a login")

    def _connect_once(self, headers: dict) -> tuple[int, str, dict, bool]:
        host = self._tunnel_host
        if host and ":" in host:
            host = f"[{host}]"
        lines = [f"CONNECT {host}:{self._tunnel_port} {self._http_vsn_str}\r\n"]
        lines += [f"{name}: {value}\r\n" for name, value in headers.items()]
        self.send("".join(lines + ["\r\n"]).encode("latin-1"))
        response = http.client.HTTPResponse(self.sock, method="CONNECT")
        try:
            response.begin()
            offered = challenges(response.headers.get_all("Proxy-Authenticate") or [])
            if response.status != 200:
                response.read()  # discard the body so the connection can carry the next step
            return response.status, response.reason, offered, response.will_close
        finally:
            response.close()  # closes only the reader, not the socket


class ProxyLoginHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = ProxyLoginHTTPSConnection
    login: ProxyLogin

    def _new_conn(self):
        conn = super()._new_conn()
        conn.login = self.login
        return conn


class ProxyLoginManager(ProxyManager):
    def __init__(self, proxy_url: str, **kwargs):
        super().__init__(proxy_url, **kwargs)
        self.login = ProxyLogin(proxy_url, self.proxy_headers)
        self.pool_classes_by_scheme = {"http": HTTPConnectionPool, "https": ProxyLoginHTTPSConnectionPool}

    def _new_pool(self, scheme, host, port, request_context=None):
        pool = super()._new_pool(scheme, host, port, request_context)
        if isinstance(pool, ProxyLoginHTTPSConnectionPool):
            pool.login = self.login
        return pool


class ProxyLoginAdapter(requests.adapters.HTTPAdapter):
    def proxy_manager_for(self, proxy: str, **proxy_kwargs):
        if proxy not in self.proxy_manager and not proxy.lower().startswith("socks"):
            self.proxy_manager[proxy] = ProxyLoginManager(
                proxy, proxy_headers=self.proxy_headers(proxy), num_pools=self._pool_connections,
                maxsize=self._pool_maxsize, block=self._pool_block, **proxy_kwargs)
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def response_hook(response, **kwargs):
    """requests response hook: complete the login handshake for a plain-HTTP request."""
    if response.status_code != 407 or response.request.url.lower().startswith("https:"):
        return response
    offered = challenges(response.headers.get("Proxy-Authenticate", "").split("\n"))
    scheme = choose(offered)
    proxy = requests.utils.select_proxy(response.request.url, kwargs.get("proxies") or {})
    if scheme is None or not proxy:
        return response
    manager = response.connection.proxy_manager.get(requests.utils.prepend_scheme_if_needed(proxy, "http"))
    login = manager.login if isinstance(manager, ProxyLoginManager) else ProxyLogin(proxy)
    login.use(scheme)
    try:
        context = login.new_context(scheme)
        token = login.step(context)
        for _ in range(MAX_STEPS):
            response.content  # finish reading so the same connection carries the next step
            request = response.request.copy()
            request.headers["Proxy-Authorization"] = f"{scheme} {_token(token)}"
            response = response.connection.send(request, **kwargs)
            if response.status_code != 407:
                return response
            challenge = challenges(response.headers.get("Proxy-Authenticate", "").split("\n")).get(scheme)
            if challenge is None or context.complete:
                return response  # rejected; the caller reports the 407
            token = login.step(context, challenge)
    except ProxyLoginError as exc:
        log.warning("proxy login failed: %s", exc)
    return response
