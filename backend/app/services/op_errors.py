"""Typed failures for the operation API.

Every operation failure used to reach the caller as one of three shapes:
``KeyError`` → HTTP 404, ``ValueError`` → HTTP 422, or an unhandled exception →
a bare non-JSON 500. That collapsed genuinely different problems together — most
damagingly, a *missing required argument* raised ``KeyError`` and so was reported
as ``404 Not found: 'sketch'``, telling the caller the resource did not exist when
in fact the call was malformed. An agent cannot learn to correct itself from that.

Each error here carries a stable machine-readable ``code`` and the HTTP status it
maps to, so both a model and a human get the same unambiguous classification.
"""

from __future__ import annotations

from typing import Optional


class OperationError(ValueError):
    """Base class: a failure attributable to the call, not to the server.

    Subclasses ``ValueError`` deliberately. Every one of these is "the value you
    passed, or the state you passed it in, is wrong", which is what ValueError
    means — and it keeps the many existing ``except ValueError`` handlers in the
    routers working unchanged, so adding structure here is purely additive. The
    ``code`` is what callers that want precision branch on.
    """

    code = "BAD_ARG"
    http_status = 422

    def __init__(self, message: str, *, arg: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.arg = arg

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.arg:
            out["arg"] = self.arg
        return out


class UnknownOperation(OperationError):
    """No such operation in the catalog."""

    code = "UNKNOWN_OP"
    http_status = 400


class MissingArgument(OperationError):
    """A required argument was absent. Distinct from anything being 'not found'."""

    code = "MISSING_ARG"
    http_status = 422


class BadArgument(OperationError):
    """An argument was present but unusable — wrong type, range or enum member."""

    code = "BAD_ARG"
    http_status = 422


class NoSuchDocument(OperationError):
    code = "NO_SUCH_DOCUMENT"
    http_status = 404


class NoSuchFeature(OperationError):
    code = "NO_SUCH_FEATURE"
    http_status = 404


class PreconditionFailed(OperationError):
    """The call was well-formed but the document is not in a state that allows it.

    e.g. asking for a flat pattern on a part that has no sheet-metal feature. The
    fix is a different *previous* step, not different arguments — worth its own
    code so an agent learns to build the precondition rather than retrying.
    """

    code = "PRECONDITION"
    http_status = 409


class EvaluationFailed(OperationError):
    """The edit applied but the resulting tree does not evaluate to geometry."""

    code = "EVAL_FAILED"
    http_status = 422


class InternalError(OperationError):
    """An unexpected exception. Surfaced as structured JSON, never a bare 500."""

    code = "INTERNAL"
    http_status = 500


def require(args: dict, key: str):
    """Fetch a required argument, or raise MissingArgument naming it."""
    value = (args or {}).get(key)
    if value is None:
        raise MissingArgument(f"'{key}' is required", arg=key)
    return value
