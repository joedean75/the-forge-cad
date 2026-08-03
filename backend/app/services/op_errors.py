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
    #: What to do about it. A code tells a caller *which* problem it has; the
    #: remedy tells it what the next call should be, which is otherwise
    #: something every agent has to re-derive from prose every time. Written
    #: once, per code, rather than repeated in a system prompt that will drift.
    #: `fillet_edges` already did this inside its message ("call list_edges for
    #: the 12 available on this body") and it was the exception; this makes it
    #: the rule without every handler having to remember.
    remedy = "Check the argument against the operation's schema and call again."

    def __init__(self, message: str, *, arg: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.arg = arg

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message, "remedy": self.remedy}
        if self.arg:
            out["arg"] = self.arg
        return out


class UnknownOperation(OperationError):
    """No such operation in the catalog."""

    code = "UNKNOWN_OP"
    remedy = ("Use one of the operation names in the catalog exactly as written.")
    http_status = 400


class MissingArgument(OperationError):
    """A required argument was absent. Distinct from anything being 'not found'."""

    code = "MISSING_ARG"
    remedy = ("Supply the named argument. The operation's schema marks it required; it has no default because there is no answer that would be right.")
    http_status = 422


class BadArgument(OperationError):
    """An argument was present but unusable — wrong type, range or enum member."""

    code = "BAD_ARG"
    remedy = ("Fix the named argument's type, range or spelling. Lengths are in metres and the schema lists every field an object accepts.")
    http_status = 422


class NoSuchDocument(OperationError):
    code = "NO_SUCH_DOCUMENT"
    remedy = ("Nothing can be done with this id — the document does not exist or is not yours.")
    http_status = 404


class NoSuchFeature(OperationError):
    code = "NO_SUCH_FEATURE"
    remedy = ("Call list_features and use an id from it. Feature ids change when features are deleted.")
    http_status = 404


class PreconditionFailed(OperationError):
    """The call was well-formed but the document is not in a state that allows it.

    e.g. asking for a flat pattern on a part that has no sheet-metal feature. The
    fix is a different *previous* step, not different arguments — worth its own
    code so an agent learns to build the precondition rather than retrying.
    """

    code = "PRECONDITION"
    remedy = ("Different arguments will not help: a different *earlier* step is needed. Build what this operation needs first, then call it again.")
    http_status = 409


class EvaluationFailed(OperationError):
    """The edit applied but the resulting tree does not evaluate to geometry."""

    code = "EVAL_FAILED"
    remedy = ("The edit applied but the body will not evaluate. Undo it with set_feature_params or delete_feature, or fix the parameter it depends on.")
    http_status = 422


class InternalError(OperationError):
    """An unexpected exception. Surfaced as structured JSON, never a bare 500."""

    code = "INTERNAL"
    remedy = ("Not your call\u2019s fault. Try a different approach; repeating this one will fail the same way.")
    http_status = 500


def require(args: dict, key: str):
    """Fetch a required argument, or raise MissingArgument naming it."""
    value = (args or {}).get(key)
    if value is None:
        raise MissingArgument(f"'{key}' is required", arg=key)
    return value
