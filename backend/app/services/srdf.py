"""An assembly as SRDF — the semantic half a URDF cannot carry.

A URDF says what the machine *is*: links, masses, joints, axes. A motion planner
needs two things it deliberately does not say. Which chains of links are worth
planning for (MoveIt calls them groups), and which pairs of links may be allowed
to touch — because a planner that treats every contact as a collision refuses to
move a machine whose parts are bolted together by design.

**Scope is deliberately narrow, and the omissions are the interesting part.**
SRDF can also carry end effectors, virtual joints and named states. Every one of
those is a *planning* decision — which link is the tool, what the robot is
mounted to, what "home" means — and nothing in an assembly answers them. Writing
a guess into a file a planner obeys is worse than leaving it out, so this writes
only what the geometry actually determines:

- **Groups** are the kinematic chains, one per leaf link, walked over the same
  parent/child edges the URDF tree uses. A chain with no articulated joint in it
  is not emitted: there is nothing to plan.
- **`disable_collisions`** comes from two sources with different confidence.
  Mated pairs are `Adjacent` and exact — a mate names two components by id.
  Pairs the interference check reports as touching *at the current pose* are
  `Default`, and those are the ones a planner would otherwise refuse to move
  through.

**A pair is skipped rather than guessed when two components share a name.**
`interference.check_assembly` reports pairs by component name, and link names are
de-duplicated (`arm`, `arm_2`), so an ambiguous name cannot be matched back to a
link with certainty. Disabling the wrong pair tells a planner it may drive one
part through another, which is exactly the silent wrongness worth refusing — so
the pair is dropped and the ambiguity is named in `warnings`.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape, quoteattr

from app.services import assembly_service
from app.services.urdf import _safe, link_names_for


def _articulated(mate) -> bool:
    """A mate that leaves something free, rather than one that only places."""
    return bool(getattr(mate, "base_connector", None)
                and getattr(mate, "moved_connector", None))


def export_srdf(asm_id: str, *, name: Optional[str] = None,
                clearance_mm: float = 1.0) -> Dict[str, Any]:
    """The assembly as SRDF, alongside what it could not determine."""
    from app.services import interference

    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)
    if not asm.components:
        raise ValueError("an empty assembly has no links to describe")

    links = link_names_for(asm)
    parent_of: Dict[str, str] = {}
    joint_kind: Dict[str, bool] = {}
    mated: List[tuple] = []
    for mate in asm.mates:
        if mate.base not in links or mate.moved not in links:
            continue
        mated.append((mate.base, mate.moved))
        if mate.moved in parent_of:
            # The second parent closes a loop. URDF's tree cannot hold it and a
            # chain cannot either, so the chain walk keeps the first edge — the
            # same rule `export_urdf` applies, so the two agree about the tree.
            continue
        parent_of[mate.moved] = mate.base
        joint_kind[mate.moved] = _articulated(mate)

    if not any(_articulated(m) for m in asm.mates):
        raise ValueError(
            "no connector mates, so every joint is fixed and there is nothing to "
            "plan: an SRDF for an assembly that cannot move is an empty file. "
            "Add mate connectors and a revolute, slider or ball mate first.")

    warnings: List[str] = []

    # ---- groups: one chain per leaf ---------------------------------------
    parents = set(parent_of.values())
    groups: List[str] = []
    for component in asm.components:
        cid = component.id
        if cid in parents or cid not in parent_of:
            continue  # not a leaf, or not attached to anything
        chain, walker, moves = [cid], cid, False
        while walker in parent_of:
            moves = moves or joint_kind.get(walker, False)
            walker = parent_of[walker]
            chain.append(walker)
        if not moves:
            # Every joint between here and the base is fixed: a "chain" a
            # planner cannot move is a group it would silently ignore.
            warnings.append(f"{links[cid]}: reached only by fixed joints, so no "
                            f"group was written for it")
            continue
        base, tip = links[chain[-1]], links[cid]
        groups.append("\n".join([
            f'  <group name={quoteattr("chain_" + tip)}>',
            f'    <chain base_link={quoteattr(base)} tip_link={quoteattr(tip)}/>',
            "  </group>",
        ]))

    # ---- collisions a planner may ignore ----------------------------------
    disabled: List[str] = []
    seen: set = set()

    def _disable(a: str, b: str, reason: str) -> None:
        key = tuple(sorted((a, b)))
        if a == b or key in seen:
            return
        seen.add(key)
        disabled.append(f'  <disable_collisions link1={quoteattr(a)} '
                        f'link2={quoteattr(b)} reason={quoteattr(reason)}/>')

    for base_id, moved_id in mated:
        _disable(links[base_id], links[moved_id], "Adjacent")

    # Names are how the interference report identifies a component, so a name
    # shared by two of them cannot be resolved back to one link.
    counts = Counter(c.name for c in asm.components)
    by_name = {c.name: links[c.id] for c in asm.components if counts[c.name] == 1}
    try:
        report = interference.check_assembly(asm_id, clearance_mm=clearance_mm)
    except (KeyError, ValueError) as exc:  # a body that will not evaluate
        report = {"pairs": []}
        warnings.append(f"touching pairs were not measured: {exc}")
    for pair in report.get("pairs") or []:
        a, b = by_name.get(pair.get("a")), by_name.get(pair.get("b"))
        if a is None or b is None:
            warnings.append(
                f"{pair.get('a')!r} and {pair.get('b')!r} touch, but at least one "
                f"name belongs to more than one component — no collision pair was "
                f"disabled, because disabling the wrong one lets a planner drive "
                f"a part through another")
            continue
        _disable(a, b, "Default")

    robot = _safe(name or asm.name, "robot")
    xml = "\n".join([
        '<?xml version="1.0"?>',
        f"<!-- Generated from Forge assembly {escape(asm_id)} -->",
        f"<robot name={quoteattr(robot)}>",
        *groups,
        *disabled,
        "</robot>",
        "",
    ])
    return {"srdf": xml, "robot": robot, "groups": len(groups),
            "disabled_pairs": len(disabled), "warnings": warnings}
