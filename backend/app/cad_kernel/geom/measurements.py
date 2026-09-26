"""Engineering measurements of a B-rep, independent of its display mesh.

All lengths are metres. The unit-density inertia is about the centre of mass;
multiply it by material density to obtain kg m². Sheets have area and an area
centroid, but no enclosed volume or mass inertia.
"""


def exact_bounds(shape):
    """The optimal axis-aligned box of a shape, as (lo_x, lo_y, lo_z, hi_x, hi_y, hi_z).

    The same answer as `BRepBndLib.AddOptimal` over the whole shape, which is
    the union of each face's optimal box — but that call optimises *every*
    face, and on a supplier motor with 1,658 B-spline faces it was 48 of the
    59 seconds it took to open the part. Only a face that can reach an extreme
    can change the result. Each face's conservative box (analytic bounds, or the
    control polygon for a spline, which contains the surface by the convex-hull
    property) is cheap; per side of the box, faces are visited in order of how
    far that conservative bound reaches, the optimal box is computed only for
    those, and the walk stops at the first face whose conservative bound cannot
    beat the best exact value so far. Triangulation is still never used: a
    tessellated box is short on every curved face by the chordal sag, and the
    drawing prints its dimension figures from this box.
    """
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    from app.cad_kernel.core import naming

    faces = naming.faces(shape)
    if not faces:
        box = Bnd_Box()
        BRepBndLib.AddOptimal_s(shape, box, False, False)
        if box.IsVoid():
            # A shape with no faces is a body with nothing in it — a subtract
            # that removed everything. `Bnd_Box.Get` raises about *itself*
            # there, and that message reached the caller as the whole
            # explanation: "Bnd_Box is void", naming neither the body nor what
            # emptied it.
            raise naming.EmptyBody(
                "this body is empty — nothing is left to measure. An operation "
                "removed all of the material; undo it or add some back")
        return box.Get()

    loose = []
    for face in faces:
        box = Bnd_Box()
        BRepBndLib.Add_s(face, box, False)
        loose.append(box.Get())

    exact: dict = {}

    def optimal(i):
        if i not in exact:
            box = Bnd_Box()
            BRepBndLib.AddOptimal_s(faces[i], box, False, False)
            exact[i] = box.Get()
        return exact[i]

    result = []
    for k in range(6):             # lo_x, lo_y, lo_z, hi_x, hi_y, hi_z
        high = k >= 3
        order = sorted(range(len(faces)), key=lambda i: loose[i][k], reverse=high)
        best = None
        for i in order:
            reach = loose[i][k]
            if best is not None and (reach <= best if high else reach >= best):
                break
            value = optimal(i)[k]
            if best is None or (value > best if high else value < best):
                best = value
        result.append(best)
    return tuple(result)


def exact_measurements(shape, kind: str) -> dict:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = exact_bounds(shape)
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
