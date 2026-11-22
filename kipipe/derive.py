"""Derive a board's mechanics from another board of the same project.

A spec may carry

    "derive": {"board": "power_board",            # a board already designed in this project
               "outline": true,                   # same size, shape and mounting holes
               "mates": [{"ref": "J1", "with": "J2", "side": "bottom"}]}

"outline" copies the other board's size, shape and mounting-hole type, so the holes land in the same places.
A mate fixes this board's connector J1 exactly over that board's J2 so the two plug together when stacked:
  side "bottom": this board sits above; J1 goes on its underside, seen mirrored from the top.
  side "top":    J1 sits on top at the same spot (pass-through stacking headers, the same connector on every board).

The designer wires the mate logically: J1 pin k carries what J2 pin k carries. On the underside a part is
mirrored, so pad k of J1 is not over pad k of J2. Code works out which pad lies over which from the footprint
geometry and moves the nets onto the pads that actually touch. Nothing here calls a model.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import kicad_lib as kl

CONTEXT = {"project": None, "jobs": None, "job": None}     # set by the pipeline before a spec is validated
ROTS = (0, 90, 180, 270)
TOL = 0.06                                                 # mm: pads this close are the same spot


def set_context(project, jobs_dir, job_name=None):
    CONTEXT.update(project=project, jobs=Path(jobs_dir) if jobs_dir else None, job=job_name)


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def source_board(name: str):
    """(job dir, spec, layout, exact size) of a board in the current project, or (None, why)."""
    from .addons import projects
    p = projects.load(CONTEXT["project"])
    if not p:
        return None, "this run is not in a project, so there is no other board to derive from"
    boards = p.get("boards") or {}
    key = next((k for k in boards if _norm(k) == _norm(name)), None) or next((k for k in boards if _norm(name) in _norm(k) or _norm(k) in _norm(name)), None)
    if not key:
        return None, f"no board called {name!r} in project {p['name']} (boards: {', '.join(boards) or 'none yet'})"
    d = CONTEXT["jobs"] / boards[key]
    try:
        spec = json.loads((d / "spec.json").read_text())
        layout = json.loads((d / "out/layout.json").read_text())
    except Exception:
        return None, f"board {key} has no finished layout to derive from (its run is gone or stopped before placement)"
    try:
        rep = json.loads((d / "out/build.json").read_text())
        w, h = float(rep["width"]), float(rep["height"])
    except Exception:
        w, h = float(layout["board"][0]) - 0.1, float(layout["board"][1]) - 0.1
    off = (float(layout["board"][0]) - w) / 2            # the layout dump measures from the outside of the edge line
    return {"dir": d, "name": key, "spec": spec, "layout": layout, "w": w, "h": h, "off": off}, ""


def _source_pads(src, ref):
    """[(pad number, net, x, y)] of a placed part on the source board, board coordinates."""
    p = next((x for x in src["layout"]["parts"] if x["ref"] == ref), None)
    if not p:
        return None, None
    g = (p.get("geo") or {}).get(str(p.get("rot", "0"))) or {}
    pads = [(str(n), str(net).lstrip("/"), p["x"] + (x0 + x1) / 2 - src["off"], p["y"] + (y0 + y1) / 2 - src["off"])
            for n, net, x0, y0, x1, y1 in g.get("pads", []) if str(n)]
    return pads, p


def _local_pads(fp_id: str):
    return [(str(n), (x0 + x1) / 2, (y0 + y1) / 2) for n, x0, y0, x1, y1 in kl.footprint_pad_rects(fp_id) if str(n)]


def _rot(x, y, deg):
    """KiCad's footprint rotation: positive angles turn counter-clockwise on screen (y runs down)."""
    a = math.radians(deg)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def align(local, target, back: bool):
    """Lay a footprint's pads over target pad positions. Returns (rotation, origin x, origin y, {pad: target pad})
    for the best fit, preferring the fit where each pad lies over the pad of the same number. None if no fit."""
    tgt = [(n, x, y) for n, _, x, y in target]
    if len(local) != len(tgt) or not tgt:
        return None
    best = None
    for rot in ROTS:
        pts = []
        for n, x, y in local:
            if back:
                x = -x                              # a part on the underside is seen mirrored from the top
            rx, ry = _rot(x, y, rot)
            pts.append((n, rx, ry))
        for tn, tx, ty in tgt:                      # try putting the first pad on each target pad
            dx, dy = tx - pts[0][1], ty - pts[0][2]
            mp = {}
            for n, x, y in pts:
                hit = next((t for t in tgt if abs(t[1] - (x + dx)) < TOL and abs(t[2] - (y + dy)) < TOL), None)
                if not hit:
                    break
                mp[n] = hit[0]
            if len(mp) == len(pts) and len(set(mp.values())) == len(tgt):
                same = sum(1 for a, b in mp.items() if a == b)
                cand = (same, -rot, rot, dx, dy, mp)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    if not best:
        return None
    return best[2], best[3], best[4], best[5]


def apply(spec: dict, report) -> None:
    """Resolve spec["derive"]: copy the outline, fix mates in place, move nets onto the pads that touch.
    Problems go into the validation report so the designer can correct them."""
    d = spec.get("derive")
    if not isinstance(d, dict) or d.get("_applied"):
        return
    if not CONTEXT["project"]:
        report.warnings.append("the spec has a derive section but this run is not in a project; it was ignored")
        return
    src, why = source_board(str(d.get("board") or ""))
    if not src:
        report.errors.append("derive: " + why)
        return
    b = spec.setdefault("board", {})
    notes = []
    if d.get("outline", True):
        sb = src["spec"].get("board") or {}
        b["width_mm"], b["height_mm"] = round(src["w"], 3), round(src["h"], 3)
        for k in ("shape", "mounting_holes", "thickness_mm"):
            if sb.get(k) is not None:
                b[k] = sb[k]
        if src["layout"].get("outline"):
            b["shape"] = {"polygon": [[round(x - src["off"], 3), round(y - src["off"], 3)] for x, y in src["layout"]["outline"]]}
        notes.append(f"outline and mounting holes copied from {src['name']}: {src['w']:.1f} x {src['h']:.1f} mm")
    comps = {c.get("ref"): c for c in spec.get("components", [])}
    presets = {}
    for m in d.get("mates") or []:
        ref, other, side = str(m.get("ref") or ""), str(m.get("with") or ""), str(m.get("side") or "bottom").lower()
        back = side in ("bottom", "back", "under", "underside")
        if ref not in comps:
            report.errors.append(f"derive: mate {ref!r} is not a component of this board")
            continue
        target, placed = _source_pads(src, other)
        if not target:
            conns = [p["ref"] for p in src["layout"]["parts"] if p["ref"].startswith(("J", "P"))]
            report.errors.append(f"derive: {src['name']} has no placed part {other!r} to mate with (its connectors: {', '.join(conns)})")
            continue
        if placed.get("back"):
            report.errors.append(f"derive: {other} is on the underside of {src['name']}; a board stacked above it cannot reach it")
            continue
        local = _local_pads(comps[ref].get("footprint", ""))
        fit = align(local, target, back)
        if not fit:
            report.errors.append(
                f"derive: {ref} ({comps[ref].get('footprint')}, {len(local)} pads) cannot be laid over {other} of {src['name']} "
                f"({placed.get('lib')}, {len(target)} pads): the pad patterns differ. Use a footprint with the same pad count, pitch and rows "
                f"(for a pin header, the PinSocket of the same size)")
            continue
        rot, x, y, pad_over = fit
        moved = sum(1 for a, k in pad_over.items() if a != k)
        if moved:                                   # nets follow the pads that physically meet
            old = {}
            for net, pins in spec["nets"].items():
                for p in list(pins):
                    if p.startswith(ref + "."):
                        old[p.split(".", 1)[1]] = net
                        pins.remove(p)
            for pad, over in pad_over.items():
                net = old.get(over)
                if net:
                    spec["nets"][net].append(f"{ref}.{pad}")
            nc = spec.get("no_connect") or []
            if any(p.startswith(ref + ".") for p in nc):
                inv = {v: k for k, v in pad_over.items()}
                spec["no_connect"] = [f"{ref}.{inv.get(p.split('.', 1)[1], p.split('.', 1)[1])}" if p.startswith(ref + ".") else p for p in nc]
        comps[ref]["edge"] = None
        presets[ref] = {"x": round(x, 4), "y": round(y, 4), "rot": rot, "back": back, "over": other, "board": src["name"],
                        "target": [[n, round(tx, 4), round(ty, 4)] for n, _, tx, ty in target], "pad_over": pad_over}
        notes.append(f"{ref} fixed {'under' if back else 'on'} the board over {src['name']} {other}"
                     + (f"; {moved} of {len(pad_over)} pads renumbered because the part is mirrored there" if moved else ""))
    d["presets"] = presets
    d["source_job"] = src["dir"].name
    d["notes"] = notes
    d["_applied"] = True
    for n in notes:
        report.warnings.append("derive: " + n)


def verify(job) -> list:
    """After layout: does the finished board really match? [(severity, title, detail)]"""
    d = job.spec.get("derive") or {}
    if not d.get("_applied"):
        return []
    out = []
    src, why = source_board(str(d.get("board") or ""))
    if not src:
        return [("warning", "The board this one was derived from can no longer be read", why)]
    size = job.size()
    lay = job.layout
    if not lay.get("parts"):
        return []
    try:
        rep = json.loads((job.dir / "out/build.json").read_text())
        w, h = float(rep["width"]), float(rep["height"])
    except Exception:
        w, h = size[0] - 0.1, size[1] - 0.1
    off = (float(lay["board"][0]) - w) / 2
    if d.get("outline", True):
        if abs(w - src["w"]) > 0.05 or abs(h - src["h"]) > 0.05:
            out.append(("error", f"Outline differs from {src['name']}: {w:.2f} x {h:.2f} mm here, {src['w']:.2f} x {src['h']:.2f} mm there", "The boards will not line up in a stack."))
        else:
            out.append(("note", f"Same outline as {src['name']}: {w:.1f} x {h:.1f} mm", ""))
        holes = lambda L, o: sorted((round(p["x"] - o, 2), round(p["y"] - o, 2)) for p in L["parts"] if str(p.get("lib", "")).startswith("MountingHole"))  # noqa: E731
        a, b = holes(lay, off), holes(src["layout"], src["off"])
        if a != b and (a or b):
            far = [p for p in a if not any(abs(p[0] - q[0]) < 0.06 and abs(p[1] - q[1]) < 0.06 for q in b)]
            if far or len(a) != len(b):
                out.append(("error", f"Mounting holes do not line up with {src['name']}", f"here {a}, there {b}"))
            else:
                out.append(("note", f"{len(a)} mounting holes line up with {src['name']}", ""))
        elif a:
            out.append(("note", f"{len(a)} mounting holes line up with {src['name']}", ""))
    for ref, pre in (d.get("presets") or {}).items():
        p = next((x for x in lay["parts"] if x["ref"] == ref), None)
        if not p:
            out.append(("error", f"{ref} is missing from the placed board", ""))
            continue
        g = (p.get("geo") or {}).get(str(p.get("rot", "0"))) or {}
        mine = {str(n): (p["x"] + (x0 + x1) / 2 - off, p["y"] + (y0 + y1) / 2 - off, str(net).lstrip("/")) for n, net, x0, y0, x1, y1 in g.get("pads", []) if str(n)}
        tgt = {str(n): (x, y) for n, x, y in pre["target"]}
        bad = [f"pad {n} is {math.hypot(mine[n][0] - tgt[k][0], mine[n][1] - tgt[k][1]):.2f} mm off" for n, k in pre["pad_over"].items()
               if n in mine and k in tgt and math.hypot(mine[n][0] - tgt[k][0], mine[n][1] - tgt[k][1]) > 0.08]
        if bool(p.get("back")) != bool(pre["back"]):
            bad.append("it is on the wrong side of the board")
        if bad:
            out.append(("error", f"{ref} does not sit over {pre['board']} {pre['over']}", "; ".join(bad[:6])))
            continue
        spads, _ = _source_pads(src, pre["over"])
        snet = {n: net for n, net, _, _ in spads or []}
        diff = [f"pad {n} ({mine[n][2] or 'unconnected'}) meets {pre['over']} pad {k} ({snet.get(k) or 'unconnected'})"
                for n, k in pre["pad_over"].items() if n in mine and mine[n][2] and snet.get(k) and _norm(mine[n][2]) != _norm(snet.get(k, ""))]
        unused = [f"{k} ({snet[k]})" for n, k in pre["pad_over"].items() if n in mine and not mine[n][2] and snet.get(k)]
        if diff:
            out.append(("warning", f"{ref} sits over {pre['board']} {pre['over']}, but {len(diff)} of the pads that meet carry different net names",
                        "; ".join(diff[:8]) + ". Fine if the names differ on purpose; otherwise the stack shorts or misconnects."))
        else:
            out.append(("note", f"{ref} sits {'under this board ' if pre['back'] else ''}exactly over {pre['board']} {pre['over']}, and every connected pad meets one with the same net",
                        (f"Not used on this board: {pre['over']} pin(s) {', '.join(unused)}." if unused else "")))
    return out
