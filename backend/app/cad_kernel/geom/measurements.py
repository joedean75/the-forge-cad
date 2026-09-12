"""Engineering measurements of a B-rep, independent of its display mesh.

All lengths are metres. The unit-density inertia is about the centre of mass;
multiply it by material density to obtain kg m². Sheets have area and an area
centroid, but no enclosed volume or mass inertia.
"""


def exact_measurements(shape, kind: str) -> dict:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    bounds = Bnd_Box()
    # Do not use triangulation or control-polygon bounds for curved surfaces.
    BRepBndLib.AddOptimal_s(shape, bounds, False, False)
    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = bounds.Get()
    surface = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface)
    props = surface
    volume = 0.0
    inertia = None
    if kind == "solid":
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props)
        signed_volume = float(props.Mass())
        volume = abs(signed_volume)
        matrix = props.MatrixOfInertia()
        sign = -1.0 if signed_volume < 0 else 1.0
        inertia = [[sign * matrix.Value(i, j) for j in range(1, 4)]
                   for i in range(1, 4)]
    center = props.CentreOfMass()
    return {
        "kind": kind,
        "volume": volume,
        "area": float(surface.Mass()),
        "center": [center.X(), center.Y(), center.Z()],
        "inertia": inertia,
        "bbox": {"min": [lo_x, lo_y, lo_z], "max": [hi_x, hi_y, hi_z]},
    }
