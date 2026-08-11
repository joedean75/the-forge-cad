# Forge frontend

React + TypeScript + Vite. A screen router around one persistent document —
there is no react-router; `useNavStore` moves between `signin → library →
editor/assembly` and syncs the hash.

```bash
npm install
npm run dev      # :5173, proxies /api → :8000
npm run build    # tsc -b && vite build — the type-check IS the gate
npm run lint     # oxlint
```

There is no frontend test suite. `npm run build` is what has to pass: `tsc -b`
runs first, so a type error fails the build.

## Layout

| Path | What lives there |
|---|---|
| `src/api/client.ts` | Every server call. Add endpoints here, never an ad-hoc `fetch`. |
| `src/store/` | Zustand: `useDocumentStore` (the open part), `useAssemblyStore`, `useAuthStore`, `useNavStore`. |
| `src/features/` | One directory per editor mode — model, optimize, analysis, make, validate, machine, sketch, assembly. |
| `src/components/` | The viewport, the ribbon, and everything shared between modes. |
| `src/util/` | Pure helpers. `featureTree.ts` is the one that matters — see below. |
| `src/landing/` | The marketing page, a separate entry point. |

## Three things that will bite

- **Types are mirrored by hand.** `../shared/types.ts` duplicates the Pydantic
  models in `backend/app/models/schemas.py`. Changing a response shape means
  editing both — nothing generates one from the other.
- **Read geometry through `featureTree.geometryTree(doc)`, not
  `doc.feature_tree`.** A parameter is stored as its expression (`"=length/2"`),
  so arithmetic on the stored tree throws; `derived.numeric_tree` carries the
  numbers. The inspector is the exception — it *should* show the expression.
- **The mesh is a URL, not a payload.** `derived.mesh_url` is content-addressed
  and immutable; `util/forgeMesh.fetchForgeMesh` handles the one case that
  matters, a 404 after the hash rolled out of the server's LRU.

The backend contract, the operation catalog and the reasoning behind both are in
[`../CLAUDE.md`](../CLAUDE.md).
