//! Native SDF grid sampler for Forge.
//!
//! Evaluates a compiled feature-tree program over a regular grid in parallel.
//! Covers primitives (box/sphere/cylinder/cone), booleans, smooth booleans
//! (fillet/chamfer), shell, transform, and extrude. Pattern/hole are compiled to
//! these on the Python side; unsupported nodes fall back to the numpy evaluator.

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use rayon::prelude::*;
use serde::Deserialize;

#[derive(Deserialize)]
#[serde(tag = "type")]
enum Profile {
    #[serde(rename = "rect")]
    Rect { width: f64, height: f64, #[serde(default)] cx: f64, #[serde(default)] cy: f64 },
    #[serde(rename = "circle")]
    Circle { radius: f64, #[serde(default)] cx: f64, #[serde(default)] cy: f64 },
    #[serde(rename = "polygon")]
    Polygon { points: Vec<[f64; 2]> },
}

#[derive(Deserialize)]
#[serde(tag = "op")]
enum Node {
    #[serde(rename = "box")]
    Box_ { center: [f64; 3], half: [f64; 3] },
    #[serde(rename = "sphere")]
    Sphere { center: [f64; 3], radius: f64 },
    #[serde(rename = "cylinder")]
    Cylinder { center: [f64; 3], radius: f64, half_h: f64, rot: [f64; 9] },
    #[serde(rename = "cone")]
    Cone { center: [f64; 3], base_radius: f64, height: f64, rot: [f64; 9] },
    #[serde(rename = "boolean")]
    Boolean { operation: String, children: Vec<Node> },
    #[serde(rename = "smooth")]
    Smooth { operation: String, radius: f64, #[serde(default)] chamfer: bool, children: Vec<Node> },
    #[serde(rename = "shell")]
    Shell { thickness: f64, child: Box<Node> },
    #[serde(rename = "transform")]
    Transform { translate: [f64; 3], rot: [f64; 9], scale: f64, child: Box<Node> },
    #[serde(rename = "extrude")]
    Extrude { profile: Profile, normal: String, offset: f64, depth: f64 },
}

#[inline(always)]
fn local_from(rot: &[f64; 9], d: [f64; 3]) -> [f64; 3] {
    // M^T · d  (rot = M row-major)
    [
        rot[0] * d[0] + rot[3] * d[1] + rot[6] * d[2],
        rot[1] * d[0] + rot[4] * d[1] + rot[7] * d[2],
        rot[2] * d[0] + rot[5] * d[1] + rot[8] * d[2],
    ]
}

#[inline(always)]
fn smin(a: f64, b: f64, k: f64, chamfer: bool) -> f64 {
    if k <= 1e-9 {
        return a.min(b);
    }
    if chamfer {
        return a.min(b).min((a - k + b) * 0.70710678);
    }
    let h = (0.5 + 0.5 * (b - a) / k).clamp(0.0, 1.0);
    b * (1.0 - h) + a * h - k * h * (1.0 - h)
}

#[inline(always)]
fn smax(a: f64, b: f64, k: f64, chamfer: bool) -> f64 {
    -smin(-a, -b, k, chamfer)
}

fn profile_sdf(profile: &Profile, u: f64, v: f64) -> f64 {
    match profile {
        Profile::Rect { width, height, cx, cy } => {
            let qx = (u - cx).abs() - width / 2.0;
            let qy = (v - cy).abs() - height / 2.0;
            (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt() + qx.max(qy).min(0.0)
        }
        Profile::Circle { radius, cx, cy } => ((u - cx).powi(2) + (v - cy).powi(2)).sqrt() - radius,
        Profile::Polygon { points } => {
            let n = points.len();
            if n == 0 {
                return 1e9;
            }
            let mut d = (u - points[0][0]).powi(2) + (v - points[0][1]).powi(2);
            let mut s = 1.0f64;
            for i in 0..n {
                let j = (i + n - 1) % n;
                let e = [points[j][0] - points[i][0], points[j][1] - points[i][1]];
                let w = [u - points[i][0], v - points[i][1]];
                let t = ((w[0] * e[0] + w[1] * e[1]) / (e[0] * e[0] + e[1] * e[1] + 1e-12)).clamp(0.0, 1.0);
                let b = [w[0] - e[0] * t, w[1] - e[1] * t];
                d = d.min(b[0] * b[0] + b[1] * b[1]);
                let c1 = v >= points[i][1];
                let c2 = v < points[j][1];
                let c3 = e[0] * w[1] - e[1] * w[0] > 0.0;
                if (c1 && c2 && c3) || (!c1 && !c2 && !c3) {
                    s = -s;
                }
            }
            s * d.sqrt()
        }
    }
}

fn eval(node: &Node, p: [f64; 3]) -> f64 {
    match node {
        Node::Box_ { center, half } => {
            let qx = (p[0] - center[0]).abs() - half[0];
            let qy = (p[1] - center[1]).abs() - half[1];
            let qz = (p[2] - center[2]).abs() - half[2];
            (qx.max(0.0).powi(2) + qy.max(0.0).powi(2) + qz.max(0.0).powi(2)).sqrt()
                + qx.max(qy).max(qz).min(0.0)
        }
        Node::Sphere { center, radius } => {
            let d = [p[0] - center[0], p[1] - center[1], p[2] - center[2]];
            (d[0] * d[0] + d[1] * d[1] + d[2] * d[2]).sqrt() - radius
        }
        Node::Cylinder { center, radius, half_h, rot } => {
            let l = local_from(rot, [p[0] - center[0], p[1] - center[1], p[2] - center[2]]);
            let radial = (l[0] * l[0] + l[1] * l[1]).sqrt() - radius;
            let axial = l[2].abs() - half_h;
            (radial.max(0.0).powi(2) + axial.max(0.0).powi(2)).sqrt() + radial.max(axial).min(0.0)
        }
        Node::Cone { center, base_radius, height, rot } => {
            let l = local_from(rot, [p[0] - center[0], p[1] - center[1], p[2] - center[2]]);
            let r = (l[0] * l[0] + l[1] * l[1]).sqrt();
            let cone_radius = base_radius * (0.5 - l[2] / height);
            let outside_cone = r - cone_radius;
            let outside_slab = (-l[2] - height / 2.0).max(l[2] - height / 2.0);
            outside_cone.max(outside_slab)
        }
        Node::Boolean { operation, children } => {
            if children.is_empty() {
                return 1e9;
            }
            let mut r = eval(&children[0], p);
            for c in &children[1..] {
                let d = eval(c, p);
                r = match operation.as_str() {
                    "union" => r.min(d),
                    "subtract" => r.max(-d),
                    _ => r.max(d),
                };
            }
            r
        }
        Node::Smooth { operation, radius, chamfer, children } => {
            if children.is_empty() {
                return 1e9;
            }
            let mut r = eval(&children[0], p);
            for c in &children[1..] {
                let d = eval(c, p);
                r = match operation.as_str() {
                    "union" => smin(r, d, *radius, *chamfer),
                    "subtract" => smax(r, -d, *radius, *chamfer),
                    _ => smax(r, d, *radius, *chamfer),
                };
            }
            r
        }
        Node::Shell { thickness, child } => eval(child, p).abs() - thickness / 2.0,
        Node::Transform { translate, rot, scale, child } => {
            let d = [p[0] - translate[0], p[1] - translate[1], p[2] - translate[2]];
            let l = local_from(rot, d);
            let s = if *scale != 0.0 { *scale } else { 1e-6 };
            eval(child, [l[0] / s, l[1] / s, l[2] / s]) * s
        }
        Node::Extrude { profile, normal, offset, depth } => {
            let (u, v, w) = match normal.as_str() {
                "z" => (p[0], p[1], p[2]),
                "y" => (p[0], p[2], p[1]),
                _ => (p[1], p[2], p[0]),
            };
            let d2 = profile_sdf(profile, u, v);
            let slab = (w - offset).abs() - depth / 2.0;
            d2.max(slab)
        }
    }
}

/// Sample a compiled SDF program over an ij-indexed grid (C-order: i*ny*nz+j*nz+k).
#[pyfunction]
fn sample_grid(
    py: Python<'_>,
    program: &str,
    min: [f64; 3],
    max: [f64; 3],
    dims: (usize, usize, usize),
) -> PyResult<Py<PyArray1<f64>>> {
    let node: Node = serde_json::from_str(program)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let (nx, ny, nz) = dims;
    let total = nx * ny * nz;
    let sx = if nx > 1 { (max[0] - min[0]) / (nx as f64 - 1.0) } else { 0.0 };
    let sy = if ny > 1 { (max[1] - min[1]) / (ny as f64 - 1.0) } else { 0.0 };
    let sz = if nz > 1 { (max[2] - min[2]) / (nz as f64 - 1.0) } else { 0.0 };

    let vals: Vec<f64> = py.allow_threads(|| {
        (0..total)
            .into_par_iter()
            .map(|idx| {
                let i = idx / (ny * nz);
                let rem = idx % (ny * nz);
                let j = rem / nz;
                let k = rem % nz;
                eval(&node, [min[0] + i as f64 * sx, min[1] + j as f64 * sy, min[2] + k as f64 * sz])
            })
            .collect()
    });

    Ok(vals.into_pyarray_bound(py).unbind())
}

#[pymodule]
fn forge_sdf(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sample_grid, m)?)?;
    Ok(())
}
