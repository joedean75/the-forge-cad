# Sprint 2: CAD part appearance

## Implemented

- Separate rendering vertices at labelled CAD face boundaries before computing
  normals. Flat faces retain flat shading and vertices within a curved face
  remain shared for smooth shading.
- Keep transport coordinates, triangle order, feature IDs and face IDs intact.
  Measurements and exports continue to use the original welded mesh.
- Disable shadow maps and part self-shadowing in document and assembly views
  to eliminate shadow-map stripes.
- Share lighting and material defaults between the two modeling views.
- Preserve computed normals when cloning geometry and skip normals for unlit
  face highlights.
- Add numeric regression tests for a welded box, capped cylinder, picking,
  highlights, unchanged coordinates and unlabelled mesh compatibility.

## Validation

The frontend CI runs these tests through the existing Node test runner, plus
lint, TypeScript, production build and the entry-bundle budget. Browser visual
verification remains pending because the development environment is unavailable.

Before closing the sprint, inspect a box, cylinder, filleted bracket, thin plate
and multi-part assembly at several zoom levels. Check sharp edges, tangent face
joins, selected faces, dimmed parts, clipping, Fit and the ground grid. Numeric
tests cannot establish the absence of every visual artifact.

## Remaining work

- Verify tangent joins and imported models visually; face boundaries alone do
  not encode analytic normals or continuity across adjacent curved CAD faces.
- Tune display quality against frame time using representative large models.
- Add user-facing display controls only after the default appearance is verified.

Sprint 3 follows with measured code simplification and performance work. The
shared material/lighting and removal of redundant normal calculations here are
small prerequisites, not completion of that sprint.
