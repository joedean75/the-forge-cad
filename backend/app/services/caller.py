"""Who is running the current operation.

``cad_operations.execute(op, doc_id, args)`` takes no account, deliberately: an
operation acts on a *document*, and the HTTP layer has already decided the
caller may act on it (`run_operation` checks `can_access`, then `can_edit` for a
mutation). Threading an account through every handler would put an access
decision inside each one, which is exactly where it should not live.

That works until an operation's answer depends on *who asked* rather than on
which document. There is one: `list_standard_parts` returns the purchased-parts
catalog, half of which is the caller's own imported supplier parts. Scoping that
to the document's owner is wrong the moment a document is shared — a
collaborator on Alice's part would be shown the designations, manufacturers and
part numbers of everything Alice imported.

So the account rides in a ContextVar set by the router, the same shape
`services/rollout` and `services/trajectories` already use. Three properties
make that safe rather than a hidden global:

- **It is a scope, not an assignment.** `acting_as` is a context manager that
  restores the previous token, so a nested call cannot leak its identity
  outward, and a background task started inside one does not inherit it by
  accident (a ContextVar copy is taken at task creation, which is the behaviour
  wanted here — the job genuinely is that caller's).
- **Unset is a real answer, not a default.** `acting_account_id()` returns
  `None` for a direct in-process call (tests, rollouts, the decompiler) rather
  than pretending somebody asked. Callers decide what that means; nothing here
  invents an identity.
- **It is never an authorisation input.** Access is decided at the router
  against the document, before `execute` runs. This narrows what a permitted
  call returns; it can never widen it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_ACCOUNT_ID: ContextVar[Optional[str]] = ContextVar("forge_acting_account", default=None)


@contextmanager
def acting_as(account_id: Optional[str]) -> Iterator[None]:
    """Run the block attributed to `account_id`."""
    token = _ACCOUNT_ID.set(account_id)
    try:
        yield
    finally:
        _ACCOUNT_ID.reset(token)


def acting_account_id() -> Optional[str]:
    """The account on whose behalf the current operation runs, if known."""
    return _ACCOUNT_ID.get()
