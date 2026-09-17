"""A ceiling on request-body size, enforced before the body is in memory.

Every upload route does ``data = await file.read()`` and *then* checks the
length against ``import_service.MAX_FILE_BYTES``. By that point the bytes are
already resident: a caller who POSTs a 2 GB file has spent 2 GB of the worker's
memory to be told the file is too large. The same applies to every JSON route —
``POST /op`` will happily buffer whatever it is handed before Pydantic looks at
it. The operation surface is reachable by anyone who can open a document, which
makes this a remote memory-exhaustion vector rather than a hygiene point.

Written as raw ASGI rather than a ``BaseHTTPMiddleware`` because the check has to
sit *upstream* of the body being assembled — `BaseHTTPMiddleware` receives a
request whose body is already being buffered by the framework, which is the
thing being defended against.

Two enforcement points, because either alone is bypassable:

- **``Content-Length``**, when present, is rejected before a single byte of body
  is read. This is the common case and costs nothing.
- **The streamed byte count**, because ``Transfer-Encoding: chunked`` carries no
  ``Content-Length`` at all. Bytes are counted as they arrive and the request is
  failed the moment the running total crosses the cap, so a chunked upload is
  bounded by the cap plus one chunk rather than by the client's patience.

The cap is per-path-prefix: a STEP import is legitimately 50 MB and a JSON
operation call is not, and one number for both would either refuse real files or
leave the JSON surface wide open.
"""

from __future__ import annotations

import logging
from typing import Iterable, Tuple

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.import_limits import UPLOAD_MAX_BYTES

_LOGGER = logging.getLogger(__name__)

MB = 1024 * 1024

#: Anything not matched below. Generous enough for a large feature tree on
#: ``PUT /api/documents/{id}`` and far below what it takes to hurt a worker.
DEFAULT_MAX_BYTES = 8 * MB

# File and transport caps share app.import_limits so a supported STEP cannot
# be rejected by an independently maintained request limit. Uploads still
# become resident bytes and OCC needs additional memory to parse them; see
# DEPLOY.md for deployment sizing.

#: (path substring, cap). Substring rather than prefix because the document
#: upload routes sit under `/api/documents/{id}/...` and `/api/documents/meta/...`.
UPLOAD_PATHS: Tuple[str, ...] = (
    "/api/documents/import",
    "/api/documents/from-image",
    "/api/documents/meta/supplier-parts",
    "/api/assemblies/import",
)


def limit_for(path: str, uploads: Iterable[str] = UPLOAD_PATHS) -> int:
    return UPLOAD_MAX_BYTES if any(u in path for u in uploads) else DEFAULT_MAX_BYTES


async def _reject(send: Send, limit: int) -> None:
    body = (
        b'{"detail":{"code":"BAD_ARG","message":"request body too large '
        b'(limit ' + str(limit // MB).encode() + b' MB)",'
        b'"remedy":"send a smaller body, or upload a smaller file"}}'
    )
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Refuse over-sized request bodies with 413 before buffering them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = limit_for(scope.get("path", ""))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    _LOGGER.warning("rejected %s: content-length %s > %d",
                                    scope.get("path"), declared, limit)
                    await _reject(send, limit)
                    return
            except ValueError:
                pass  # malformed; the streamed count below still applies

        received = 0
        too_large = False

        async def counting_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > limit:
                    too_large = True
                    # Stop the body here. The app sees a truncated request and
                    # whatever it does with it is discarded — `send_wrapper`
                    # replaces the response with the 413.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if too_large and not started:
                # The app is mid-response on a body we refused. Its output is
                # dropped and the 413 goes out instead; anything after the first
                # message would be a protocol error, so it is swallowed.
                if message["type"] == "http.response.start":
                    started = True
                    await _reject(send, limit)
                return
            if too_large:
                return
            await send(message)

        await self.app(scope, counting_receive, send_wrapper)
