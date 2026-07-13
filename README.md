# Forge Design Replica

A browser-based replica of the **The Forge (Forge)** design module. This project reproduces the end-to-end generative-design workflow — requirements, topology optimization preview, simulation review, additive-manufacturing constraints, and node-based assembly — as a modern web app.

> **Note:** The UI is a workflow mockup / presentation-layer interpretation of Forge. The backend topology optimizer now uses a real 3D SIMP (Solid Isotropic Material with Penalization) compliance-minimization solver on a hexahedral voxel mesh. Density operations are structured to run on GPU via PyTorch when CUDA is available, while the sparse linear solve uses scipy for stability. Forge itself is proprietary; this implementation is for educational/demonstration purposes only.

## Tech Stack

- **Frontend:** React 19 + TypeScript + Vite + Tailwind CSS v4 + React Three Fiber / Three.js
- **Backend:** Python + FastAPI + trimesh + numpy/scipy
- **Dev ops:** Vite dev proxy to FastAPI; optional Docker Compose

## Quick Start

### 1. Start the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 2. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 in your browser.

## Workflow

1. **Requirements** — define design space, preserve/interface regions, loads, constraints, material, and objective, or use the **Text-to-CAD** prompt to generate a starting spec from a natural-language description.
2. **Generative** — click *Run Generative Study* to generate candidate designs.
3. **Topology** — select a candidate and inspect the organic mesh, load paths, and measurements.
4. **Simulation** — re-solves the linear-elastic FEA problem with real material properties and reports von Mises stress, displacement, safety factor, and compliance.
5. **Manufacturing** — analyzes the selected mesh for build orientation, overhang area, support volume, printability score, and estimated LPBF print time.
6. **Assembly** — see the node/strut bill of materials.
7. **Export** — download STL and design report.

## Project Structure

```
/home/joed/cad
├── frontend/          # React app
├── backend/           # FastAPI app
├── shared/            # TypeScript type definitions
└── README.md
```

## License

MIT — for educational/demonstration purposes only. Divergent and Forge are trademarks of Divergent Technologies, Inc.
