# 3D printing with The Forge

The Machine mode turns a part into a running print without leaving the app:
**slice → preview → send to your printer**. The slicer is built directly on the
CAD kernel's signed distance field, and its output is standard RepRap-flavor
G-code (relative extrusion) that runs unmodified on Klipper (via Moonraker) and
Marlin machines.

## In the app

Open a part → **Machine** mode:

1. **Slice for printing** — set layer height, infill %, walls, top/bottom
   layers, and temperatures, then *Slice*. You get layer count, estimated print
   time, and filament grams, plus a to-scale layer preview you can scrub:
   walls in ember, solid skins in hot, sparse infill in steel.
2. **Download G-code**, or connect the **Moonraker endpoint** (status,
   temperatures, jog, progress are live) and **Upload / Upload + start**.

## Settings reference

| Setting | Default | Notes |
|---|---|---|
| `layer_height` | 0.2 mm | 0.05–0.6 |
| `nozzle_diameter` | 0.4 mm | extrusion width is 1.1× nozzle |
| `wall_count` | 2 | perimeters per layer |
| `skin_layers` | 3 | solid top/bottom shells; 0 disables |
| `infill` | 0.15 | 0–1 density, rectilinear 45°/135° |
| `speed_print` / `speed_first_layer` / `speed_travel` | 45 / 20 / 150 mm/s | |
| `temp_nozzle` / `temp_bed` | 210 / 60 °C | PLA defaults |
| `fan_percent` | 100 | part cooling from layer 3 |
| `retract_mm` / `retract_speed` | 1.0 mm / 35 mm/s | on travels > 1.5 mm |
| `skirt_loops` | 2 | first-layer adhesion/prime |
| `bed_center_x/y` | 110 / 110 | part is auto-centered here, bottom on the plate |

## For agents (API / MCP)

```bash
# Slice (stats return; G-code is stored on the document)
curl -s -X POST $BASE/api/documents/$DOC/op -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' \
  -d '{"op":"slice_part","args":{"settings":{"layer_height":0.2,"infill":0.15}}}'

# Print (auto-slices with defaults if needed; SSRF-guarded URL)
curl -s -X POST $BASE/api/documents/$DOC/op -H 'X-Api-Key: fk_…' \
  -H 'Content-Type: application/json' \
  -d '{"op":"print_part","args":{"printer_url":"http://printer.local:7125","start":true}}'

# Raw G-code download
curl -s $BASE/api/documents/$DOC/gcode -H 'X-Api-Key: fk_…' -o part.gcode
```

MCP tools `slice_part` and `print_part` wrap the same operations, so an
MCP-connected assistant can go *prompt → part → printing* in two calls.
HTTP equivalents: `POST /api/documents/{id}/slice` (returns stats + layer
preview polylines) and `GET /api/documents/{id}/gcode`.

## How the slicer works

Mesh slicers spend most of their complexity on polygon offsetting (perimeters)
and boolean clipping (infill regions). The Forge slices the **signed distance
field** instead:

1. Each layer's occupancy is sampled from the 3D SDF at the layer's mid-height,
   then rebuilt into a true **in-plane 2D signed distance** via an EDT. (The 3D
   field can't be used directly: just above the floor it measures distance to
   the floor, not the outline — first/last layers would lose their walls.)
2. **Perimeters are iso-contours** of that field at `-(w/2 + i·w)` — exact
   insets with no clipping library.
3. **Solid skins**: any inner region not covered `skin_layers` deep both above
   and below is filled dense — that's what closes top and bottom surfaces.
4. **Sparse infill** fills the remaining interior at the requested density,
   alternating 45°/135° per layer.
5. Paths are simplified (Douglas-Peucker, 0.05 mm), travel is ordered greedily,
   retraction on long travels, part-cooling fan from layer 3.

Estimates: print time integrates move lengths at their feed rates; filament
grams assume PLA (1.24 g/cm³).

## Printer configuration

- The Machine panel accepts any Moonraker URL directly. Preconfigure a fleet
  with `MOONRAKER_URLS="Voron|http://voron.local:7125,Ender|http://ender.local:7125"`
  in `backend/.env`.
- Outbound printer URLs pass an SSRF guard; unroutable/multicast targets are
  refused.
- Uploads land in Moonraker's G-code folder as `forge_<part>.gcode`; *Upload +
  start* begins the job immediately.

## Current limits

No support generation (print overhangs ≤ ~50° or orient accordingly), no
brim/raft (skirt only), single extruder, uniform layer height. Estimates do not
model acceleration, so real prints run slightly longer than quoted.
