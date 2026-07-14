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

## Copilot (multi-step agentic loop)

```bash
curl -s -X POST $BASE/api/documents/$DOC/copilot \
  -H 'X-Api-Key: fk_…' -H 'Content-Type: application/json' \
  -d '{"goal":"turn this into an 80x40x10 mm plate with 4 corner M4 holes","max_steps":8}'
```

The response contains the executed step transcript (op, rationale, ok/error)
and the final document. The backend plans with its configured LLM; every step
is undoable and metered.

## Import / export / quotes

```bash
# Mesh file → editable part (STL/OBJ/PLY/GLB/OFF, faceted STEP)
curl -s -X POST $BASE/api/documents/import -H 'X-Api-Key: fk_…' \
  -F file=@bracket.stl -F units=auto

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
