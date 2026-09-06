"""`shared/types.ts` mirrors the Pydantic models by hand. This checks it (API-01).

The duplication is deliberate and documented — the frontend imports
`../../../shared/types` rather than generating from the schema — but "kept in
sync by hand" is a policy, not a mechanism, and a response shape that grows a
field on one side only fails at runtime in the browser with `undefined`, which
is the least legible way to find out.

What this compares is **field names on the models the API actually returns**. It
deliberately does not compare *types*: TypeScript and Pydantic disagree about
optionality, unions and dictionaries in ways that would need a translator, and a
translator is a third definition of the same thing. Names catch the drift that
actually happens — a field added on the server that the client never reads, or a
client field the server stopped sending.

Two kinds of difference are legitimate and are declared, not inferred:

- **Client-only fields**, computed in the browser and never sent.
- **Server-only fields**, deliberately withheld from the wire (a job's `inputs`,
  which the caller that submitted them already has).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.models import schemas

_TYPES_TS = Path(__file__).resolve().parents[2] / "shared" / "types.ts"


def _ts_interface(name: str) -> set:
    """The field names declared on one exported TypeScript interface.

    A small parser rather than a real one: the file is hand-written in a
    consistent style, and the alternative (a TypeScript toolchain invoked from
    pytest) is a dependency this suite should not grow to check a naming
    convention. It reads the brace-balanced body and takes the identifiers at
    depth one that are followed by `?:` or `:`.
    """
    source = _TYPES_TS.read_text()
    match = re.search(rf"export interface {name}\s*\{{", source)
    if match is None:
        raise AssertionError(f"shared/types.ts has no interface {name}")

    depth = 0
    body = []
    for index in range(match.end() - 1, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        body.append((depth, char))

    fields = set()
    line_depth = 1
    line: list = []
    for depth, char in body[1:]:
        if char == "\n":
            text = "".join(line).strip()
            # Only depth-1 declarations: a nested object literal's keys belong
            # to the nested type, not to this one.
            if line_depth == 1:
                found = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\??\s*:", text)
                if found:
                    fields.add(found.group(1))
            line = []
            line_depth = depth
        else:
            line.append(char)
    return fields


#: Pydantic model -> TypeScript interface, with the differences that are
#: intentional. Anything not listed here must match name for name.
CONTRACTS = [
    ("DesignDocument", "DesignDocument", {"client_only": set(), "server_only": set()}),
    ("DerivedCache", "DerivedCache", {"client_only": set(), "server_only": set()}),
    ("AnalysisProduct", "AnalysisProduct", {"client_only": set(), "server_only": set()}),
    ("DocumentSummary", "DocumentSummary", {"client_only": set(), "server_only": set()}),
    ("OptimizeJob", "OptimizeJob", {"client_only": set(), "server_only": set()}),
    ("DocumentParameter", "DocumentParameter", {"client_only": set(), "server_only": set()}),
]


@pytest.mark.parametrize("model_name,ts_name,allowed", CONTRACTS,
                         ids=[c[0] for c in CONTRACTS])
def test_the_hand_mirrored_type_matches_its_model(model_name, ts_name, allowed):
    model = getattr(schemas, model_name)
    server = set(model.model_fields)
    client = _ts_interface(ts_name)

    missing_on_client = server - client - allowed["server_only"]
    extra_on_client = client - server - allowed["client_only"]

    assert not missing_on_client, (
        f"{model_name} has fields {sorted(missing_on_client)} that "
        f"shared/types.ts {ts_name} does not declare — the browser will read "
        f"them as undefined")
    assert not extra_on_client, (
        f"shared/types.ts {ts_name} declares {sorted(extra_on_client)}, which "
        f"{model_name} does not send")


def test_the_job_record_matches_what_the_job_route_publishes():
    """`JobRecord` mirrors `Job.public()`, not a Pydantic model.

    The job row is a plain object rather than a schema — it is written and read
    by the queue, not validated at a route boundary — so the thing to compare
    against is the dict the route actually returns.
    """
    from app.services import jobs

    published = set(jobs.Job(kind="x", subject_id="y").public())
    declared = _ts_interface("JobRecord")
    assert published == declared, (
        f"JobRecord drifted from Job.public(): "
        f"only on the server {sorted(published - declared)}, "
        f"only in TypeScript {sorted(declared - published)}")


def test_a_jobs_inputs_are_never_published():
    """Withheld deliberately: the client that submitted them already has them,
    and echoing an optimization setup back on every poll is the payload mistake
    this codebase keeps undoing."""
    from app.services import jobs

    assert "inputs" not in jobs.Job(kind="x", subject_id="y").public()


def test_the_parser_finds_a_field_that_is_actually_there():
    """A parser that silently returned nothing would make every check vacuous."""
    fields = _ts_interface("DesignDocument")
    assert {"id", "name", "feature_tree", "derived", "revision"} <= fields
