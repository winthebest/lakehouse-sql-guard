"""The approval web app: Starlette routes over `ReviewService`, Jinja2 pages, vendored htmx."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jinja2
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from sqlguard.approvals.security import (
    SECURITY_HEADERS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    AccessControl,
    SecurityMiddleware,
    Session,
    allowed_hosts,
    parse_form,
    read_form,
)
from sqlguard.approvals.service import ReviewError, ReviewService
from sqlguard.safety.guard import ActionNotPending, Outcome, ProofRejected, UnknownAction
from sqlguard.safety.store import StoreError

_log = logging.getLogger("sqlguard.approvals")
_HERE = Path(__file__).parent
_ID = re.compile(r"[0-9a-f]{32}")
DEFAULT_HOUSEKEEPING_SECONDS = 60.0
REFRESH_SECONDS = 3

# outcome -> (flash kind, sentence)
_OUTCOME_TEXT: dict[Outcome, tuple[str, str]] = {
    Outcome.PUBLISHED: ("ok", "Approved: the change was published and verified."),
    Outcome.NO_CHANGE: ("ok", "Approved: the statement changed no rows."),
    Outcome.MAINTENANCE_DONE: ("ok", "Approved: the maintenance action ran."),
    Outcome.REJECTED: ("ok", "Rejected: the audit branch was dropped."),
    Outcome.STALE: (
        "warn",
        "Stale: the table, the policy or the diff changed since the review, "
        "so nothing was applied.",
    ),
    Outcome.EXPIRED: ("warn", "Expired: nobody decided in time, so nothing was applied."),
    Outcome.ROLLED_BACK: (
        "warn",
        "The change was published, failed its checks and was rolled back.",
    ),
    Outcome.ROLLBACK_BLOCKED: ("bad", "Needs attention: the change cannot be undone safely."),
    Outcome.ROLLBACK_FAILED: (
        "bad",
        "Needs attention: the change failed its checks and the rollback failed.",
    ),
    Outcome.EXECUTION_FAILED: ("bad", "The engine rejected the statement; nothing was published."),
    Outcome.ERROR: (
        "bad",
        "Unexpected error. The action stays in publishing until housekeeping settles it.",
    ),
}


def make_environment() -> jinja2.Environment:
    """Jinja2 with autoescape on for every template and no silent undefined variables."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_HERE / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ts"] = _ts
    env.filters["short"] = lambda value, n=8: str(value)[: int(n)]
    env.filters["cell"] = _cell
    return env


def _ts(value: datetime | None) -> str:
    return "" if value is None else value.strftime("%Y-%m-%d %H:%M:%SZ")


def _cell(value: Any, limit: int = 120) -> str:
    if isinstance(value, (bytes, bytearray)):
        text = "0x" + bytes(value[: limit // 2]).hex()
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def create_app(
    service: ReviewService,
    access: AccessControl,
    *,
    bind_host: str = "127.0.0.1",
    housekeeping_seconds: float | None = DEFAULT_HOUSEKEEPING_SECONDS,
) -> Starlette:
    """The app. Housekeeping runs when the server starts and then every `housekeeping_seconds`
    (None turns the loop off; `service.housekeeping()` can still be called)."""
    env = make_environment()

    def render(request: Request, name: str, status: int = 200, **context: Any) -> HTMLResponse:
        session: Session | None = getattr(request.state, "session", None)
        context.update(
            approver=service.approver,
            base=access.base,
            csrf_token=session.csrf if session else "",
            flashes=session.take_flashes() if session else [],
            refresh=REFRESH_SECONDS,
        )
        return HTMLResponse(env.get_template(name).render(**context), status_code=status)

    def message(request: Request, status: int, title: str, text: str) -> HTMLResponse:
        return render(request, "message.html", status, title=title, text=text)

    def action_id(request: Request) -> str:
        found = request.path_params["action_id"]
        if not _ID.fullmatch(found):
            raise HTTPException(404, "No such action.")
        return found

    def sealed(response: Response) -> Response:
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    async def login_page(request: Request) -> Response:
        """Show a confirmation; only the POST below spends the token (a link preview or a
        prefetch that merely fetches this page must not)."""
        token = request.query_params.get("token", "")
        if not token:
            return sealed(Response("A sign-in link is required.", 401, media_type="text/plain"))
        return render(request, "login.html", token=token)

    async def login(request: Request) -> Response:
        form = await parse_form(request)
        session = access.redeem(form.get("token", ""), request.cookies.get(SESSION_COOKIE))
        if session is None:
            return sealed(
                Response(
                    "The token is wrong or was already used. "
                    "Restart `sqlguard serve` for a new one.",
                    status_code=401,
                    media_type="text/plain",
                )
            )
        response = RedirectResponse(f"{access.base}/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session.id,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            path=f"{access.base}/",  # only URLs under the secret prefix ever get the cookie
        )
        return sealed(response)

    async def logout(request: Request) -> Response:
        await read_form(request)  # CSRF
        session: Session = request.state.session
        access.end(session.id)
        response = message(
            request,
            200,
            "Signed out",
            "The session ended. Restart `sqlguard serve` to sign in again.",
        )
        response.delete_cookie(SESSION_COOKIE, path=f"{access.base}/")
        return response

    async def queue_page(request: Request) -> Response:
        view = await run_in_threadpool(service.queue)
        return render(request, "queue.html", view=view, now=datetime.now(UTC))

    async def queue_fragment(request: Request) -> Response:
        view = await run_in_threadpool(service.queue)
        return render(request, "_queue.html", view=view, now=datetime.now(UTC))

    async def detail(request: Request) -> Response:
        ident = action_id(request)
        try:
            page = int(request.query_params.get("page", "1"))
        except ValueError:
            page = 1
        try:
            view = await run_in_threadpool(lambda: service.view(ident, page=page, mint_proofs=True))
        except ReviewError:
            raise HTTPException(404, "No such action.") from None
        return render(request, "action.html", view=view)

    async def decide(request: Request, decision: Any) -> Response:
        ident = action_id(request)
        form = await read_form(request)  # CSRF is checked here, before anything else is read
        session: Session = request.state.session
        try:
            decided = await run_in_threadpool(
                service.decide, ident, decision, form.get("proof", ""), form.get("note")
            )
        except ReviewError as err:
            return message(request, 400, "Not decided", str(err))
        except ProofRejected:
            return message(
                request,
                403,
                "Proof refused",
                "The proof on this page is invalid, expired or was already used. "
                "Reload the action and try again.",
            )
        except ActionNotPending as err:
            return message(request, 409, "Already decided", f"This action is {err.state.value}.")
        except UnknownAction:
            raise HTTPException(404, "No such action.") from None
        except StoreError:
            _log.error("the store failed while deciding action %s", ident, exc_info=True)
            return message(request, 500, "Store error", "The action store could not be used.")
        kind, text = _OUTCOME_TEXT.get(
            decided.result.outcome, ("warn", f"Outcome: {decided.result.outcome.value}.")
        )
        session.flash(kind, text)
        for reason in decided.result.reasons[:5]:
            session.flash("note", reason)
        if not decided.note_recorded:
            session.flash("warn", "The note could not be written to the audit log.")
        return RedirectResponse(f"{access.base}/a/{ident}", status_code=303)

    async def approve(request: Request) -> Response:
        return await decide(request, "approve")

    async def reject(request: Request) -> Response:
        return await decide(request, "reject")

    async def audit_page(request: Request) -> Response:
        entries = await run_in_threadpool(service.audit_tail)
        return render(request, "audit.html", entries=entries)

    async def http_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, HTTPException):
            raise exc
        return message(request, exc.status_code, f"Error {exc.status_code}", str(exc.detail))

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        task = None
        if housekeeping_seconds:
            task = asyncio.create_task(_housekeeping_loop(service, housekeeping_seconds))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return Starlette(
        routes=[
            Mount(
                access.base,
                routes=[
                    Route("/login", login_page, methods=["GET"]),
                    Route("/login", login, methods=["POST"]),
                    Route("/logout", logout, methods=["POST"]),
                    Route("/", queue_page, methods=["GET"]),
                    Route("/queue", queue_fragment, methods=["GET"]),
                    Route("/a/{action_id}", detail, methods=["GET"]),
                    Route("/a/{action_id}/approve", approve, methods=["POST"]),
                    Route("/a/{action_id}/reject", reject, methods=["POST"]),
                    Route("/audit", audit_page, methods=["GET"]),
                    Mount("/static", StaticFiles(directory=_HERE / "static"), name="static"),
                ],
            )
        ],
        middleware=[
            Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts(bind_host)),
            Middleware(SecurityMiddleware, access=access),
        ],
        exception_handlers={HTTPException: http_error},
        lifespan=lifespan,
    )


async def _housekeeping_loop(service: ReviewService, seconds: float) -> None:
    while True:
        await run_in_threadpool(service.housekeeping)
        await asyncio.sleep(seconds)
