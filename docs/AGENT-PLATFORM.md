# The Forge — Agent Platform

The Forge exposes its entire CAD engine as an **agent-native API**: every
geometry mutation the UI performs goes through one serializable operation
endpoint, so an AI agent (or your own code) can do anything a user can —
create parts from text, edit feature trees, run the multi-step Copilot,
import/export files, and get manufacturing quotes.

Start here: `GET /api/platform` (discovery manifest) → `GET /api/operations`
(the op catalog) → `GET /docs` (interactive OpenAPI).

## Authentication

| Mode | Header | Where it comes from |
|---|---|---|
| API key | `X-Api-Key: fk_…` | Account → API keys (Pro plan and above) |
| Session | `Authorization: Bearer <jwt>` | `POST /api/auth/login` |

In development (no `FORGE_AUTH_REQUIRED=1`) requests without credentials fall
back to a shared dev account. Production deployments enforce auth; mutating
operations are metered against the account's plan quota — including each
sub-step the Copilot takes.

## The operation API

```bash
# What can I call?
curl -s $BASE/api/operations | jq '.operations[].name'

# One operation (lengths in metres)
curl -s -X POST $BASE/api/documents/$DOC/op \
  -H 'X-Api-Key: fk_…' -H 'Content-Type: application/json' \
  -d '{"op":"add_primitive","args":{"shape":"cylinder","operation":"subtract",
       "params":{"radius":0.005,"height":0.05,"center":{"x":0,"y":0,"z":0}}}}'
```

Every mutating call returns the updated document (feature tree + derived
mesh/bbox/mass), so agents can verify each step.

Add `?envelope=1` and failures come back as `200` bodies instead of raising —
`{ok, error:{code,message,arg}, delta, state, warnings}`. That is the shape to
drive a loop from: `ok` is computed, `error.code` is one of a stable set
(`MISSING_ARG`, `BAD_ARG`, `NO_SUCH_FEATURE`, `PRECONDITION`, `EVAL_FAILED`, …),
and `delta` reports what actually changed about the geometry.

`GET /api/operations/tools` returns the same catalog as OpenAI-style function
definitions with strict JSON Schema, for constrained decoding.

## Selecting faces and edges

The operations above address the body as a whole. These address its topology,
and need the exact kernel (`pip install cadquery-ocp`) — without it they return
a `PRECONDITION` error saying so.

```bash
# What can I select? Each entry has a stable id, kind, size and position.
curl -s -X POST $BASE/api/documents/$DOC/op -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' -d '{"op":"list_edges","args":{"kind":"circle"}}'

# Round the ones you picked — or pass "all" instead of a list.
-d '{"op":"fillet_edges","args":{"edges":["E1a2b3c4"],"radius":0.001}}'
```

`fillet_edges`, `chamfer_edges` and `shell_faces` are real edge/face treatments,
not smooth blends. They roll the edit back and return a typed `PRECONDITION` if
OCC cannot build them, so a failed fillet never leaves the document unevaluatable.

A selection is stored as an *identity*, not a measurement, and is followed
through the construction history on every rebuild — so an upstream edit that
splits the edge you filleted leaves the reference naming **both** segments, and
one that destroys it says so instead of quietly moving the fillet to whatever was
nearest. Ask which happened:

```bash
-d '{"op":"check_references","args":{}}'
# -> {"lost":1,"split":0,"ok":false,"features":[{"feature_id":"f4",
#     "references":[{"id":"E5523fd09","status":"deleted","resolves_to":0,
#                    "meaning":"an upstream edit destroyed it — reselect and reapply"}]}]}
```

`status` is `matched`, `split`, `deleted` or `unresolved`. Any mutating operation
that leaves a reference unresolved also says so in the envelope's `warnings` —
worth reading, because the body it leaves behind is a perfectly valid solid that
`check_geometry` will pass.

## Checking the part

Four different questions, deliberately separate:

```bash
# Is it a well-formed solid? (watertight, winding, connected bodies)
-d '{"op":"check_geometry","args":{}}'

# Can it be made? process = additive | milling | molding | casting
-d '{"op":"dfm","args":{"process":"milling"}}'

# Will it hold? Real hex-element FEA — check `solver` in the result;
# "closed-form estimate" means no real solve ran.
-d '{"op":"simulate","args":{}}'

# Is it the RIGHT part? Requirements you set, scored.
-d '{"op":"set_spec","args":{"requirements":[
      {"kind":"bounding_box","axis":"x","value":80,"tolerance":0.5},
      {"kind":"hole","diameter":10,"count":4},
      {"kind":"mass","max":200}]}}'
-d '{"op":"verify_against_spec","args":{}}'
```

`verify_against_spec` returns a pass/fail row per requirement plus `score`, the
fraction met — that is the reward signal. An unmeasurable requirement fails
loudly rather than being skipped, so the score cannot be inflated by a spec the
kernel could not check.

## Rollouts and training data

`services/trajectories.recording(doc_id, goal=…)` records every operation as
`(goal, obs_before, op, args, result, delta, obs_after)`;
`GET /api/trajectories/export.jsonl` exports them in fine-tuning shape.
`services/decompiler.py` goes the other way — it recovers the op sequence for
any existing feature tree and verifies it by replay.

Two things bite at volume, and `services/rollout.py` is the switch:

```python
from app.services import rollout
with rollout.rollout(resolution=192):
    ...   # metering off, resolution pinned
```

Metering is obvious. **Pinning matters more**: resolution is otherwise capped by
the account's plan, so the same document evaluates differently for different
callers — which destroys the one property a dataset needs. `FORGE_ROLLOUT=1`
sets it process-wide. Neither switch touches access control.

## Copilot (multi-step agentic loop)

```bash
JOB=$(curl -s -X POST $BASE/api/documents/$DOC/copilot \
  -H 'X-Api-Key: fk_…' -H 'Content-Type: application/json' \
  -d '{"goal":"turn this into an 80x40x10 mm plate with 4 corner M4 holes","max_steps":8}' \
  | jq -r .job_id)

# A run can span many LLM turns, so it executes as a background job — poll it:
curl -s $BASE/api/documents/$DOC/copilot/$JOB -H 'X-Api-Key: fk_…'
```

Poll until `status` is no longer `"running"`. While running, `steps` is the
live transcript (op, rationale, ok/error); when done, the response adds the
summary and the final document. The backend plans with its configured LLM;
every step is undoable and metered.

## 3D printing (slice + print)

The built-in FDM slicer turns a part into printable G-code (RepRap flavor,
Klipper/Marlin compatible) straight from its signed-distance field:

```bash
# Slice: returns layer/time/filament stats; G-code is stored on the document
curl -s -X POST $BASE/api/documents/$DOC/op -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' \
  -d '{"op":"slice_part","args":{"settings":{"layer_height":0.2,"infill":0.15}}}'

# Download the G-code, or send it to a Moonraker/Klipper printer:
curl -s $BASE/api/documents/$DOC/gcode -H 'X-Api-Key: fk_…' -o part.gcode
curl -s -X POST $BASE/api/documents/$DOC/op -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' \
  -d '{"op":"print_part","args":{"printer_url":"http://printer.local:7125","start":true}}'
```

`print_part` slices with defaults first if the part has no G-code yet, so an
agent can go prompt → part → print in two operations. MCP tools: `slice_part`,
`print_part`.

## Import / export / quotes

```bash
# STEP/IGES → exact editable solid (addressable faces, exact measurement).
# Meshes (STL/OBJ/PLY/GLB/OFF) import as a voxel body instead — an STL has
# nothing better to recover. Units come from a STEP/IGES header, not a guess.
curl -s -X POST $BASE/api/documents/import -H 'X-Api-Key: fk_…' \
  -F file=@bracket.step -F units=auto

# Instant manufacturing quotes, then an order
curl -s -X POST $BASE/api/documents/$DOC/quotes -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' -d '{"quantity":5}'
curl -s -X POST $BASE/api/documents/$DOC/orders -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' -d '{"process":"cnc-3axis","quantity":5}'

# STEP export
curl -s $BASE/api/documents/$DOC/step -H 'X-Api-Key: fk_…' -o part.step
```

## MCP server (Claude Code / Desktop / any MCP client)

The stdio server in `backend/mcp_server.py` wraps the HTTP API as MCP tools:
`list_operations`, `create_part`, `list_parts`, `get_part`, `run_operation`,
`copilot`, `import_part`, `quote_part`, `order_part`, `export_part_step`.

```jsonc
// .mcp.json / Claude Desktop config
{
  "mcpServers": {
    "forge-cad": {
      "command": "/path/to/backend/.venv/bin/python",
      "args": ["/path/to/backend/mcp_server.py"],
      "env": {
        "FORGE_API": "https://your-forge.example.com/api",
        "FORGE_API_KEY": "fk_…"
      }
    }
  }
}
```

## Conventions

- **Units:** all op args are metres (`0.1` = 100 mm); the UI displays mm.
- **Meshes:** `derived.mesh_url` is a binary-STL data URI — decode it directly.
- **Errors:** 404 = not yours/doesn't exist (never leaks existence), 403 = role
  too low, 422 = bad arguments (message says what to fix), 429 = plan quota.
