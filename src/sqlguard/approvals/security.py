"""Who may talk to the approval server, and what a request must prove.

Layers, outermost first (the `Host` check is Starlette's `TrustedHostMiddleware`, wired in `app`):

1. The server binds to loopback only. Another address is refused unless the person says they
   know it has no real authentication behind it.
2. The whole app lives under a random path prefix (`/s/<128+ bits>`) that exists only in the link
   printed to the terminal of the person who started the server. Browsers do not keep cookies
   apart by port, so any other server on 127.0.0.1 that the person visits receives the session
   cookie; scoping the cookie to the secret path means it is only ever sent to URLs that
   already know the prefix. Anything outside the prefix is a bare 404.
   Opening the printed link and confirming trades the one-time token for a session cookie:
   `HttpOnly`, `SameSite=Strict`, `Path=<prefix>/`, random, held only in this process. Showing
   the confirmation page does not spend the token, so a link preview or prefetch cannot use it
   up; only the confirming POST does. Any later use of the token is refused, a new sign-in
   replaces the session it was made from, and signing out ends the session.
3. Every request that is not a GET needs a same-origin `Origin` header, and every POST needs the
   session's CSRF token in its form.
4. Pages carry headers that stop framing, sniffing, referrers, caching and any script that is
   not the vendored htmx.

None of this separates two programs running as the same OS user: an agent that can open a
browser, read this terminal or call the CLI is not stopped by it (see `docs/limitations.md`).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

SESSION_COOKIE = "sqlguard_session"
PREFIX_BYTES = 24  # 192 bits
CSRF_FIELD = "csrf_token"
SESSION_SECONDS = 8 * 3600
MAX_SESSIONS = 8
MAX_FORM_BYTES = 16 * 1024
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
UNSAFE_FLAG = "--i-know-this-is-unauthenticated"
PUBLIC_PATHS = frozenset({"/login"})  # relative to the secret prefix
PUBLIC_PREFIXES = ("/static/",)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def check_bind_host(host: str, *, unsafe: bool = False) -> str:
    """`host` if it may be bound; ValueError for anything but loopback without `unsafe`."""
    if host in LOOPBACK_HOSTS:
        return "127.0.0.1" if host == "localhost" else host
    if not unsafe:
        raise ValueError(
            f"refusing to listen on {host!r}: this server has no real authentication and must "
            f"stay on 127.0.0.1 (pass {UNSAFE_FLAG} to override)"
        )
    return host


def allowed_hosts(bind_host: str) -> list[str]:
    """`Host` header values the server answers to: loopback names, plus the bind host if unsafe."""
    hosts = list(LOOPBACK_HOSTS)
    if bind_host not in LOOPBACK_HOSTS:
        hosts.append("*" if bind_host in ("0.0.0.0", "::") else bind_host)
    return hosts


@dataclass
class Session:
    id: str
    csrf: str
    created: float
    flashes: list[tuple[str, str]] = field(default_factory=list)

    def flash(self, kind: str, text: str) -> None:
        self.flashes.append((kind, text))

    def take_flashes(self) -> list[tuple[str, str]]:
        taken, self.flashes = self.flashes, []
        return taken


class AccessControl:
    """The secret path prefix, the one-time token and the sessions it buys (memory only)."""

    def __init__(
        self,
        token: str | None = None,
        *,
        prefix: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._token = token or secrets.token_urlsafe(32)
        self._prefix = prefix or secrets.token_urlsafe(PREFIX_BYTES)
        self._used = False
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def token(self) -> str:
        return self._token

    @property
    def base(self) -> str:
        """The path every URL of the app starts with, without a trailing slash."""
        return f"/s/{self._prefix}"

    def __repr__(self) -> str:
        return "AccessControl(<token and prefix hidden>)"

    def redeem(self, candidate: str, replacing: str | None = None) -> Session | None:
        """A new session for the right token, once. Any other attempt gets None.

        `replacing` is the session the request came with; it ends when the new one starts.
        """
        with self._lock:
            if self._used or not hmac.compare_digest(
                candidate.encode("utf-8", "replace"), self._token.encode()
            ):
                return None
            self._used = True
            self._drop_expired()
            if replacing:
                self._sessions.pop(replacing, None)
            while len(self._sessions) >= MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), self._clock())
            self._sessions[session.id] = session
            return session

    def session(self, cookie: str | None) -> Session | None:
        if not cookie:
            return None
        with self._lock:
            self._drop_expired()
            return self._sessions.get(cookie)

    def end(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, s in self._sessions.items() if now - s.created > SESSION_SECONDS]:
            del self._sessions[key]


def same_origin(request: Request) -> bool:
    """True for a request a page of this very server made (the browser sets `Origin`)."""
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host or origin != f"{request.url.scheme}://{host}":
        return False
    return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")


class SecurityMiddleware(BaseHTTPMiddleware):
    """Sessions, the `Origin` check and the response headers; CSRF is checked with the form."""

    def __init__(self, app, access: AccessControl) -> None:
        super().__init__(app)
        self.access = access

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        base = self.access.base
        path = request.url.path
        if path != base and not path.startswith(base + "/"):
            return _deny(404, "Not Found")  # no hint that anything is here
        relative = path[len(base) :] or "/"
        public = relative in PUBLIC_PATHS or relative.startswith(PUBLIC_PREFIXES)
        if not public:
            session = self.access.session(request.cookies.get(SESSION_COOKIE))
            if session is None:
                return _deny(401, "Not signed in. Open the link that `sqlguard serve` printed.")
            request.state.session = session
        if request.method not in ("GET", "HEAD"):
            if request.method != "POST":
                return _deny(405, "Method not allowed.")
            if not same_origin(request):
                return _deny(403, "Cross-origin request refused.")
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response


def _deny(status: int, message: str) -> Response:
    response = Response(message, status_code=status, media_type="text/plain")
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


async def parse_form(request: Request) -> dict[str, str]:
    """The urlencoded form of a POST (no CSRF check: for the sign-in, which has no session yet).

    Raises HTTPException: 415 for another content type, 413 for a large body, 400 for a body
    that is not valid.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        raise HTTPException(415, "A form is required.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(413, "The form is too large.")
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=bool(body),
            max_num_fields=20,
        )
    except (UnicodeDecodeError, ValueError) as err:
        raise HTTPException(400, "The form could not be read.") from err
    return {key: values[-1] for key, values in parsed.items()}


async def read_form(request: Request) -> dict[str, str]:
    """`parse_form`, after the session's CSRF token was checked (403 when missing or wrong)."""
    form = await parse_form(request)
    session: Session = request.state.session
    if not hmac.compare_digest(
        form.get(CSRF_FIELD, "").encode("utf-8", "replace"), session.csrf.encode()
    ):
        raise HTTPException(403, "The CSRF token is missing or wrong; reload the page.")
    return form
