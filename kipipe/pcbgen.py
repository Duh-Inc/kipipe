"""Board operations that need KiCad's pcbnew bindings.

Runs under the system interpreter that has `pcbnew` (KiCad's SWIG bindings,
present through KiCad 10). Invoked as a subprocess by the pipeline:

    python3 -m kipipe.pcbgen <phase> job.json

phases:
    build   netlist -> footprints, nets, placement, outline, holes
    dsn     export Specctra DSN for the autorouter
    finish  import the routed SES, pour ground, fill zones, report
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pcbnew

from . import outline as oshape
from . import placer
from . import sexpr
from .sexpr import find, find_all

MM = pcbnew.FromMM
ORIGIN = (100.0, 100.0)     # board top-left in page coordinates (mm)
HOLES = {"M2": ("MountingHole_2.2mm_M2", 5.5), "M2.5": ("MountingHole_2.7mm_M2.5", 6.0),
         "M3": ("MountingHole_3.2mm_M3", 7.0)}


def log(msg):
    print(msg, flush=True)


def remove(board, item):
    """Detach an item. KiCad 7's Python Remove() wrapper breaks on some
    proxies; RemoveNative is the underlying call."""
    fn = getattr(board, "RemoveNative", None)
    if fn is not None:
        fn(item)
    else:
        remove(board, item)


def zones(board):
    # index access returns typed ZONE objects on every KiCad version
    return [board.GetArea(i) for i in range(board.GetAreaCount())]


def V(x_mm, y_mm):
    return pcbnew.VECTOR2I(MM(x_mm), MM(y_mm))


def to_mm(v):
    return pcbnew.ToMM(v)


# ---------------------------------------------------------------------------

def parse_netlist(path):
    tree = sexpr.parse(Path(path).read_text())
    comps = []
    for c in find_all(find(tree, "components"), "comp"):
        ref = str(find(c, "ref")[1])
        fp = find(c, "footprint")
        val = find(c, "value")
        ts = find(c, "tstamps") or find(c, "tstamp")
        sp = find(c, "sheetpath")
        sheet_ts = str(find(sp, "tstamps")[1]) if sp and find(sp, "tstamps") else "/"
        comps.append({"ref": ref, "footprint": str(fp[1]) if fp else "", "value": str(val[1]) if val else "",
                      "path": sheet_ts + (str(ts[1]) if ts else "")})
    pin_net = {}
    nets = []
    for n in find_all(find(tree, "nets"), "net"):
        name = str(find(n, "name")[1])
        if name.startswith("unconnected-") or name.startswith("Net-("):
            if name.startswith("unconnected-"):
                continue
        nets.append(name)
        for node in find_all(n, "node"):
            pin_net[(str(find(node, "ref")[1]), str(find(node, "pin")[1]))] = name
    return comps, nets, pin_net


_FP_BASES: list = []      # directories found to hold <Lib>.pretty, reused for later libs


def _fp_candidates(lib):
    import glob
    import os
    yield from (str(Path(b, lib + ".pretty")) for b in _FP_BASES)
    for k, v in sorted(os.environ.items()):
        if re.match(r"KICAD\d*_FOOTPRINT_DIR$", k):
            yield str(Path(v, lib + ".pretty"))
    for base in ("/app/share/kicad/footprints", "/usr/share/kicad/footprints", "/usr/local/share/kicad/footprints"):
        yield str(Path(base, lib + ".pretty"))
    # flatpak library extensions are mounted under /app/extensions (layout varies by version)
    for pat in ("/app/extensions/*/{l}", "/app/extensions/*/*/{l}", "/app/extensions/*/*/*/{l}",
                "/app/extensions/*/*/*/*/{l}", "/app/*/footprints/{l}", "/app/share/kicad/*/{l}"):
        yield from sorted(glob.glob(pat.format(l=lib + ".pretty")))


def fp_lib_path(lib, libs):
    p = libs.get(lib)
    tried = []
    for cand in ([p] if p else []) + list(_fp_candidates(lib)):
        if cand in tried:
            continue
        tried.append(cand)
        if Path(cand).is_dir():
            base = str(Path(cand).parent)
            if base not in _FP_BASES:
                _FP_BASES.insert(0, base)
            return cand
    import os
    envs = {k: v for k, v in os.environ.items() if k.startswith("KICAD")}
    exts = sorted(str(x) for x in Path("/app/extensions").glob("*/*")) if Path("/app/extensions").exists() else []
    raise RuntimeError(f"footprint library {lib} not found.\n  tried: {tried[:12]}\n  KICAD vars: {envs}\n"
                       f"  /app/extensions: {exts[:20]}")

def bbox_of(fp):
    try:
        fp.BuildCourtyardCaches()
    except Exception:
        pass
    layer = pcbnew.B_CrtYd if fp.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
    try:
        poly = fp.GetCourtyard(layer)
        if poly.OutlineCount() > 0:
            b = poly.BBox()
            return b.GetLeft(), b.GetTop(), b.GetRight(), b.GetBottom()
    except Exception:
        pass
    try:
        b = fp.GetBoundingBox(False, False)
    except TypeError:
        b = fp.GetBoundingBox(False)
    return b.GetLeft(), b.GetTop(), b.GetRight(), b.GetBottom()


def flip_fp(fp):
    """Move a footprint to the other side of the board. The call changed between KiCad versions."""
    was = fp.GetLayer()
    pos = fp.GetPosition()
    for arg in (False, getattr(pcbnew, "FLIP_DIRECTION_TOP_BOTTOM", None), getattr(pcbnew, "FLIP_DIRECTION_LEFT_RIGHT", None), 0):
        if arg is None:
            continue
        try:
            fp.Flip(pos, arg)
        except Exception:
            continue
        if fp.GetLayer() != was:
            return True
    return False


def fit_preset(fp, pre):
    """Rotation and position (board mm) at which each pad of fp lies over the target pad it must meet."""
    target = {str(n): (x, y) for n, x, y in pre.get("target", [])}
    over = pre.get("pad_over") or {}
    for rot in placer.ROTS:
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(V(0, 0))
        pads = {}
        for pad in fp.Pads():
            if pad.GetNumber():
                pos = pad.GetPosition()
                pads.setdefault(str(pad.GetNumber()), (to_mm(pos.x), to_mm(pos.y)))
        n0 = next((n for n in pads if over.get(n) in target), None)
        if n0 is None:
            return None
        dx, dy = target[over[n0]][0] - pads[n0][0], target[over[n0]][1] - pads[n0][1]
        if all(over.get(n) in target and abs(pads[n][0] + dx - target[over[n]][0]) < 0.08 and abs(pads[n][1] + dy - target[over[n]][1]) < 0.08
               for n in pads):
            return rot, dx, dy
    return None


def measure(fp, pad_net):
    """Geometry at each rotation, measured with the footprint at the origin."""
    geo = {}
    for rot in placer.ROTS:
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(V(0, 0))
        l, t, r, b = bbox_of(fp)
        pads = []
        pb = None
        for pad in fp.Pads():
            pos = pad.GetPosition()
            pads.append((pad_net.get(pad.GetNumber()), to_mm(pos.x), to_mm(pos.y)))
            bb = pad.GetBoundingBox()
            box = (to_mm(bb.GetLeft()), to_mm(bb.GetTop()), to_mm(bb.GetRight()), to_mm(bb.GetBottom()))
            pb = box if pb is None else (min(pb[0], box[0]), min(pb[1], box[1]), max(pb[2], box[2]), max(pb[3], box[3]))
        geo[rot] = placer.Geo(to_mm(l), to_mm(t), to_mm(r), to_mm(b), pads, pb)
    fp.SetOrientationDegrees(0)
    return geo


def edge_bbox(board):
    xs, ys = [], []
    for d in board.GetDrawings():
        if d.GetLayer() == pcbnew.Edge_Cuts:
            b = d.GetBoundingBox()
            xs += [b.GetLeft(), b.GetRight()]
            ys += [b.GetTop(), b.GetBottom()]
    if not xs:
        return None
    return to_mm(min(xs)), to_mm(min(ys)), to_mm(max(xs)), to_mm(max(ys))


def add_rect_outline(board, x0, y0, x1, y1):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(MM(0.1))
        s.SetStart(V(*pts[i]))
        s.SetEnd(V(*pts[(i + 1) % 4]))
        board.Add(s)


def add_shape_outline(board, ox, oy, shape, w, h):
    """Edge.Cuts for a shaped board: a true circle for circles, closed segments for everything else."""
    for prim in oshape.kicad_primitives(shape, w, h):
        if prim[0] == "circle":
            _, cx, cy, r = prim
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_CIRCLE)
            s.SetLayer(pcbnew.Edge_Cuts)
            s.SetWidth(MM(0.1))
            s.SetStart(V(ox + cx, oy + cy))
            s.SetEnd(V(ox + cx + r, oy + cy))
            board.Add(s)
        else:
            pts = prim[1]
            for i in range(len(pts)):
                s = pcbnew.PCB_SHAPE(board)
                s.SetShape(pcbnew.SHAPE_T_SEGMENT)
                s.SetLayer(pcbnew.Edge_Cuts)
                s.SetWidth(MM(0.1))
                s.SetStart(V(ox + pts[i][0], oy + pts[i][1]))
                s.SetEnd(V(ox + pts[(i + 1) % len(pts)][0], oy + pts[(i + 1) % len(pts)][1]))
                board.Add(s)


def board_outline_poly(board):
    """The board outline as a polygon relative to its bounding box (or None if it is a plain rectangle)."""
    try:
        ps = pcbnew.SHAPE_POLY_SET()
        board.GetBoardPolygonOutlines(ps)
        if ps.OutlineCount() < 1:
            return None
        ol = ps.Outline(0)
        pts = [(to_mm(ol.CPoint(i).x), to_mm(ol.CPoint(i).y)) for i in range(ol.PointCount())]
    except Exception:
        return None
    if len(pts) < 3:
        return None
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    pts = [(round(x - x0, 4), round(y - y0, 4)) for x, y in pts]
    w, h = max(p[0] for p in pts), max(p[1] for p in pts)
    corners = {(0, 0), (round(w, 4), 0), (round(w, 4), round(h, 4)), (0, round(h, 4))}
    if len(pts) == 4 and set(pts) == corners:
        return None
    if oshape._area(pts) < 0:
        pts = pts[::-1]
    return pts


# ---------------------------------------------------------------------------

GROUND = {"GND", "GNDA", "GNDD", "AGND", "DGND", "VSS", "0V", "GNDPWR"}
ANTENNA_RE = re.compile(r"ESP32|ESP8266|ESP-WROOM|WROOM|WROVER|ESP32-C|nRF52|nRF91|RFM9|RFM6|LoRa|E22|E220|SX12|CC1101|"
                        r"Antenna|Bluetooth|BT_|BLE|XBee|Wi-?Fi|RAK", re.I)


def _farads(value):
    m = re.match(r"\s*([0-9.]+)\s*([pnuµm]?)", value or "")
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return v * {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}[m.group(2)]


def apply_floorplan(parts, by_ref, want, pad_nums, plan, job):
    """Turn the spec floorplan plus automatic rules into placer constraints."""
    out = {"log": [], "macros": [], "auto_near": [], "antennas": []}
    clr = job["placement"]["clearance"]
    # grids: declared, else detected
    grids = list(plan.get("grids") or [])
    declared = {r for g in grids for r in g.get("refs", [])}
    if job.get("auto_rules", True):
        for g in placer.find_matrices([p for _, p in parts if p.ref not in declared],
                                      {r: c["footprint"] for r, c in want.items()}):
            grids.append(g)
    place_list = [p for _, p in parts]
    member_of = {}
    for i, g in enumerate(grids):
        kids = [by_ref[r] for r in g["refs"] if r in by_ref]
        if len(kids) != len(g["refs"]) or any(k.locked for k in kids):
            out["log"].append(f"grid {i + 1}: skipped (missing or locked members)")
            continue
        m = placer.make_grid(f"GRID{i + 1}", kids, int(g["cols"]), g.get("pitch_mm"), clr)
        edges_ = {k.edge for k in kids}
        if len(edges_) == 1 and None not in edges_:
            m.edge = edges_.pop()               # a row of edge buttons stays on its edge as a block
        out["macros"].append(m)
        for k in kids:
            member_of[k.ref] = m
        place_list = [p for p in place_list if p not in kids] + [m]
        out["log"].append(f"grid {i + 1}: {len(kids)} parts in {g['cols']} columns"
                          + (f" (detected {g['auto']})" if g.get("auto") else "")
                          + f", block {m.geo[0].xmax - m.geo[0].xmin:.1f} x {m.geo[0].ymax - m.geo[0].ymin:.1f} mm")
    placeable = {p.ref: p for p in place_list}
    for p in place_list:
        if not p.members and len(p.geo[0].pads) >= 8 and not p.edge:
            p.halo = 0.8
    # parts on high-clearance nets (high voltage) keep that clearance from their neighbours
    ncl = job.get("net_clearance") or {}
    base = job["placement"].get("clearance", 0.5)
    hv = []
    for p in place_list:
        nets = {n for (n, _, _) in p.geo[0].pads if n} if not p.members else set()
        need = max([ncl.get(n.lstrip("/"), 0.0) for n in nets] or [0.0])
        if need > base:
            # room for a track between two such parts: clearance + track + clearance, split between both parts
            p.halo = max(p.halo, need + 0.3 - base / 2)
            p.pad_edge = max(p.pad_edge, need + 0.6 + job["placement"].get("edge_clr", 0.4))
            hv.append(p.ref)
    if hv:
        out["log"].append(f"high-clearance spacing around {len(hv)} parts: " + ", ".join(hv[:12]) + ("..." if len(hv) > 12 else ""))

    # regions (a grid inherits a region if any member is listed)
    for reg in plan.get("regions") or []:
        rect = tuple(reg["rect"])
        for r in reg.get("refs", []):
            tgt = member_of.get(r) or placeable.get(r)
            if tgt is not None and not tgt.locked and not tgt.edge:
                tgt.region = rect

    def pad_index(ref, pin):
        nums = pad_nums.get(ref, [])
        return nums.index(pin) if pin in nums else None

    def same_net_pad(part_ref, target_ref, tidx):
        tnet = by_ref[target_ref].geo[0].pads[tidx][0]
        for i, (n, _, _) in enumerate(by_ref[part_ref].geo[0].pads):
            if n and n == tnet:
                return i
        return None

    user_near = set()
    for n in plan.get("near") or []:
        ref, (tref, _, pin) = n["ref"], str(n["to"]).partition(".")
        if ref in member_of or tref in member_of or ref not in placeable or tref not in placeable:
            out["log"].append(f"near {ref} -> {n['to']}: skipped (grid member or unknown)")
            continue
        tidx = pad_index(tref, pin) if pin else 0
        if tidx is None:
            out["log"].append(f"near {ref} -> {n['to']}: no such pad")
            continue
        placeable[ref].near.append((tref, tidx, float(n["max_mm"]), same_net_pad(ref, tref, tidx)))
        user_near.add(ref)

    for k in plan.get("keepouts") or []:
        tgt = member_of.get(k["ref"]) or placeable.get(k["ref"])
        if tgt is not None:
            tgt.keepout = max(tgt.keepout, float(k.get("mm", 0)))

    if job.get("auto_rules", True):
        # decoupling: caps <= 1 uF between a supply net and ground go next to an IC pin on that supply
        ics = [p for p in place_list if p.ref.rstrip("0123456789") in ("U", "IC") and len(p.geo[0].pads) >= 6]
        load = {}
        for p in place_list:
            if not p.ref.startswith("C") or p.ref in user_near or p.members or p.locked:
                continue
            nets = [n for (n, _, _) in p.geo[0].pads]
            f = _farads(want.get(p.ref, {}).get("value", ""))
            if len(nets) != 2 or f is None or f > 1.01e-6:
                continue
            gnd = [n for n in nets if n and n.lstrip("/").upper() in GROUND]
            sup = [n for n in nets if n and n.lstrip("/").upper() not in GROUND]
            if len(gnd) != 1 or len(sup) != 1:
                continue
            cands = [(ic, i) for ic in ics for i, (n, _, _) in enumerate(ic.geo[0].pads) if n == sup[0]]
            power = job.get("power_pins") or {}
            pw = [c for c in cands if pad_nums[c[0].ref][c[1]] in power.get(c[0].ref, [])]
            cands = pw or cands
            if not cands:
                continue
            ic, idx = min(cands, key=lambda c: load.get((c[0].ref, c[1]), 0))
            k = load.get((ic.ref, idx), 0)
            load[(ic.ref, idx)] = k + 1
            is_module = want.get(ic.ref, {}).get("footprint", "").startswith("RF_Module:") or ANTENNA_RE.search(want.get(ic.ref, {}).get("footprint", ""))
            p.near.append((ic.ref, idx, (6.0 if is_module else 4.0) + 1.5 * k, nets.index(sup[0])))
            out["auto_near"].append(f"{p.ref}->{ic.ref}.{pad_nums[ic.ref][idx]}")
        if out["auto_near"]:
            out["log"].append(f"decoupling: {', '.join(out['auto_near'])}")
        # antenna modules: edge, antenna overhanging, clear band beside it
        for ref, c in want.items():
            p = placeable.get(ref)
            if p is None or p.locked:
                continue
            fpid = c["footprint"]
            if fpid.startswith("RF_Module:") or ANTENNA_RE.search(fpid):
                rot, over = placer.edge_orientation(p, p.edge or "top")
                if over:
                    placer.narrow_to_body(p)
                    p.antenna = True
                    p.edge = p.edge or "top"
                    out["antennas"].append(ref)
                    out["log"].append(f"antenna module {ref}: {p.edge} edge, antenna overhanging, "
                                      f"{placer.ANTENNA_SIDE:.0f} mm clear beside it")
    out["place_list"] = place_list
    return out


def add_rule_area(board, rect, name):
    """Copper-pour keepout on all copper layers (tracks and pads still allowed)."""
    x0, y0, x1, y1 = rect
    for lid in board.GetEnabledLayers().CuStack():
        z = pcbnew.ZONE(board)
        z.SetIsRuleArea(True)
        z.SetLayer(lid)
        for fn, val in (("SetDoNotAllowCopperPour", True), ("SetDoNotAllowZoneFills", True),
                        ("SetDoNotAllowTracks", False), ("SetDoNotAllowVias", False),
                        ("SetDoNotAllowPads", False), ("SetDoNotAllowFootprints", False)):
            if hasattr(z, fn):
                getattr(z, fn)(val)
        try:
            z.SetZoneName(name)
        except Exception:
            pass
        o = z.Outline()
        o.NewOutline()
        for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
            o.Append(MM(x), MM(y))
        board.Add(z)


def add_edge_strips(board, ox, oy, strips):
    """Edge keepout that follows a shaped outline: one thin rule area per outline edge."""
    for quad in strips:
        for lid in board.GetEnabledLayers().CuStack():
            z = pcbnew.ZONE(board)
            z.SetIsRuleArea(True)
            z.SetLayer(lid)
            for fn, val in (("SetDoNotAllowTracks", True), ("SetDoNotAllowVias", True),
                            ("SetDoNotAllowCopperPour", False), ("SetDoNotAllowZoneFills", False),
                            ("SetDoNotAllowPads", False), ("SetDoNotAllowFootprints", False)):
                if hasattr(z, fn):
                    getattr(z, fn)(val)
            try:
                z.SetZoneName("kipipe_edge")
            except Exception:
                pass
            o = z.Outline()
            o.NewOutline()
            for (x, y) in quad:
                o.Append(MM(ox + x), MM(oy + y))
            board.Add(z)


def add_edge_ring(board, x0, y0, x1, y1, width):
    """Track/via keepout band inside the outline. Freerouting only keeps the normal
    clearance from the board edge; fabs want more (JLC: 0.3 mm). Rule areas are exported
    to the router as keepouts, so this makes it honour the edge rule."""
    for (a, b, c, d) in ((x0, y0, x1, y0 + width), (x0, y1 - width, x1, y1),
                         (x0, y0, x0 + width, y1), (x1 - width, y0, x1, y1)):
        for lid in board.GetEnabledLayers().CuStack():
            z = pcbnew.ZONE(board)
            z.SetIsRuleArea(True)
            z.SetLayer(lid)
            for fn, val in (("SetDoNotAllowTracks", True), ("SetDoNotAllowVias", True),
                            ("SetDoNotAllowCopperPour", False), ("SetDoNotAllowZoneFills", False),
                            ("SetDoNotAllowPads", False), ("SetDoNotAllowFootprints", False)):
                if hasattr(z, fn):
                    getattr(z, fn)(val)
            try:
                z.SetZoneName("kipipe_edge")
            except Exception:
                pass
            o = z.Outline()
            o.NewOutline()
            for (x, y) in [(a, b), (c, b), (c, d), (a, d)]:
                o.Append(MM(x), MM(y))
            board.Add(z)


def set_board_only(fp):
    for fn in ("SetBoardOnly",):
        if hasattr(fp, fn):
            getattr(fp, fn)(True)
            return
    try:
        fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_BOARD_ONLY)
    except Exception:
        pass


def phase_build(job):
    board_path = job["board"]
    existing = Path(board_path).exists()
    if not existing:
        Path(board_path).write_text("(kicad_pcb (version 20221018) (generator kipipe))\n")
    try:
        board = pcbnew.LoadBoard(board_path)
    except Exception:
        if existing:
            raise
        board = pcbnew.CreateEmptyBoard()
        pcbnew.SaveBoard(board_path, board)
        board = pcbnew.LoadBoard(board_path)
    if not existing:
        board.SetCopperLayerCount(int(job["layers"]))

    comps, nets, pin_net = parse_netlist(job["netlist"])
    netinfo = {}
    for name in nets:
        ni = board.FindNet(name)
        if ni is None:
            ni = pcbnew.NETINFO_ITEM(board, name)
            board.Add(ni)
        netinfo[name] = ni

    fp_libs = job["fp_libs"]
    want = {c["ref"]: c for c in comps if c["footprint"]}
    have = {fp.GetReference(): fp for fp in board.GetFootprints()}
    kept = []
    for ref, fp in have.items():
        if ref not in want:
            libname = str(fp.GetFPID().GetUniStringLibId())
            board_only = fp.IsLocked() or any(k in libname for k in ("MountingHole", "Fiducial", "Symbol:", "Logo")) \
                or not any(p.GetNumber() for p in fp.Pads())
            if board_only:
                kept.append(fp)   # holes, fiducials, logos: keep, and keep clear of them
                continue
            log(f"removing {ref} (not in schematic)")
            remove(board, fp)
    edges = job.get("edges", {})
    presets = job.get("presets") or {}
    preset_done = {}
    parts = []
    pad_nums = {}
    locked_count = 0
    for ref, c in want.items():
        lib, _, name = c["footprint"].partition(":")
        fp = have.get(ref)
        if fp is not None and str(fp.GetFPID().GetUniStringLibId()) != c["footprint"]:
            log(f"{ref}: footprint changed to {c['footprint']}, replacing")
            remove(board, fp)
            fp = None
        if fp is None:
            fp = pcbnew.FootprintLoad(fp_lib_path(lib, fp_libs), name)
            if fp is None:
                raise RuntimeError(f"cannot load footprint {c['footprint']}")
            fp.SetFPID(pcbnew.LIB_ID(lib, name))
            board.Add(fp)
        fp.SetReference(ref)
        fp.SetValue(c["value"])
        try:
            fp.SetPath(pcbnew.KIID_PATH(c["path"]))
        except Exception:
            pass
        pad_net = {}
        for pad in fp.Pads():
            net = pin_net.get((ref, pad.GetNumber()))
            if net:
                pad.SetNet(netinfo[net])
                pad_net[pad.GetNumber()] = net
            else:
                pad.SetNetCode(0)
        pad_nums[ref] = [pad.GetNumber() for pad in fp.Pads()]
        pre = presets.get(ref)
        if pre:                         # a connector fixed over another board's connector (derive.py)
            want_back = bool(pre.get("back"))
            if (fp.GetLayer() == pcbnew.B_Cu) != want_back:
                flip_fp(fp)
            geo = measure(fp, pad_net)
            sol = fit_preset(fp, pre)
            if sol:
                rot, px, py = sol
                fp.SetOrientationDegrees(rot)
                preset_done[ref] = (fp, px, py, rot)
                locked_count += 1
                parts.append((fp, placer.Part(ref, geo, locked=True, x=px, y=py, rot=rot, placed=True)))
                log(f"{ref}: fixed {'under' if want_back else 'on'} the board at {px:.2f}, {py:.2f} mm, over {pre.get('board')} {pre.get('over')}")
                continue
            log(f"warning: {ref} could not be laid over {pre.get('board')} {pre.get('over')}; placed freely instead")
            if want_back:
                flip_fp(fp)
        locked = fp.IsLocked()
        if locked:
            locked_count += 1
            pos, rot = fp.GetPosition(), fp.GetOrientationDegrees()
            geo = measure(fp, pad_net)
            fp.SetPosition(pos)
            fp.SetOrientationDegrees(rot)
            parts.append((fp, placer.Part(ref, geo, locked=True, x=to_mm(pos.x), y=to_mm(pos.y),
                                          rot=int(round(rot)) % 360, placed=True)))
        else:
            parts.append((fp, placer.Part(ref, measure(fp, pad_net), edge=edges.get(ref), fp=c["footprint"])))

    # remove our previous pours first (zone objects lose their SWIG type after track removal on KiCad 7)
    for z in zones(board):
        if z.GetZoneName() in ("kipipe_pour", "kipipe_antenna", "kipipe_edge"):
            remove(board, z)
    # remove unlocked copper from a previous run; keep locked (hand-routed) tracks
    removed = 0
    for t in list(board.GetTracks()):
        if not t.IsLocked():
            remove(board, t)
            removed += 1
    if removed:
        log(f"removed {removed} unlocked track/via segments from the previous layout")

    plan = job.get("floorplan") or {}
    by_ref = {p.ref: p for _, p in parts}
    notes = apply_floorplan(parts, by_ref, want, pad_nums, plan, job)
    place_list, macros = notes.pop("place_list"), notes.pop("macros")
    for n in notes["log"]:
        log(n)

    recorder = [] if job.get("frames") else None
    holes = job.get("mounting_holes", "none")
    hole_fp, keepout = HOLES.get(holes, (None, 0.0))
    outline = edge_bbox(board)
    ox, oy = ORIGIN
    shape = job.get("shape") if not oshape.is_rect(job.get("shape")) else None
    hole_pts = None
    poly = None
    if outline:
        existing = board_outline_poly(board)
        shape = {"type": "polygon", "points": existing} if existing else None
        ox, oy = outline[0], outline[1]
        width, height = outline[2] - outline[0], outline[3] - outline[1]
        log(f"using existing board outline {width:.1f} x {height:.1f} mm")
        for _, p in parts:
            if p.locked and p.ref not in preset_done:
                p.x -= ox
                p.y -= oy
        obst = []
        for fp in kept:
            l, t, r, b = bbox_of(fp)
            obst.append((to_mm(l) - ox, to_mm(t) - oy, to_mm(r) - ox, to_mm(b) - oy))
        if any("MountingHole" in str(fp.GetFPID().GetUniStringLibId()) for fp in kept):
            keepout = 0.0           # existing holes are obstacles already
        if shape:
            log(f"existing outline is not a rectangle ({len(existing)} points); placing inside its shape")
        w, h = placer.run(place_list, width, height, hole_keepout=keepout, obstacles=obst,
                          clearance=job["placement"]["clearance"], edge_margin=job["placement"]["edge_margin"],
                          iterations=job["placement"]["iterations"], edge_clr=job["placement"].get("edge_clr", 0.5),
                          recorder=recorder, log=log, shape=shape, holes=[] if keepout == 0 else None)
        hole_pts = getattr(placer.run, "holes", None) if shape else None
    else:
        w, h = placer.run(place_list, job.get("width"), job.get("height"), hole_keepout=keepout,
                          clearance=job["placement"]["clearance"], edge_margin=job["placement"]["edge_margin"],
                          iterations=job["placement"]["iterations"], edge_clr=job["placement"].get("edge_clr", 0.5),
                          recorder=recorder, log=log, shape=shape)
        if shape:
            add_shape_outline(board, ox, oy, shape, w, h)
            hole_pts = getattr(placer.run, "holes", None)
            log(f"board outline: {shape['type']} {w:.0f} x {h:.0f} mm, {board.GetCopperLayerCount()} layers")
        else:
            add_rect_outline(board, ox, oy, ox + w, oy + h)
            log(f"board outline {w:.0f} x {h:.0f} mm, {board.GetCopperLayerCount()} layers")
    if shape:
        poly = oshape.polygon(shape, w, h)

    for ref, (fp, px, py, rot) in preset_done.items():
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(V(ox + px, oy + py))
        fp.SetLocked(True)
    for m in macros:
        placer.expand(m)
    relaxed = getattr(placer.run, "relaxed", [])
    # (antenna copper keepouts come from the module footprints themselves; an extra rule
    #  area here would be exported to the router as a track keepout and wall off the module pins)
    for fp, p in parts:
        if p.locked:
            r = p.rect()
            if r[0] < -0.01 or r[1] < -0.01 or r[2] > w + 0.01 or r[3] > h + 0.01:
                log(f"warning: locked {p.ref} extends outside the board outline; it will likely be unroutable")
            continue
        fp.SetOrientationDegrees(p.rot)
        fp.SetPosition(V(ox + p.x, oy + p.y))

    if hole_fp and not any("MountingHole" in str(fp.GetFPID().GetUniStringLibId()) for fp in board.GetFootprints()):
        k = keepout / 2
        spots = hole_pts if hole_pts is not None else [(k, k), (w - k, k), (k, h - k), (w - k, h - k)]
        for i, (hx, hy) in enumerate(spots, 1):
            fp = pcbnew.FootprintLoad(fp_lib_path("MountingHole", fp_libs), hole_fp)
            fp.SetFPID(pcbnew.LIB_ID("MountingHole", hole_fp))
            fp.SetReference(f"H{i}")
            fp.SetPosition(V(ox + hx, oy + hy))
            fp.SetLocked(True)
            set_board_only(fp)
            board.Add(fp)

    ring = job["placement"].get("edge_clr", 0.4) - 0.05
    if ring > 0 and poly:
        add_edge_strips(board, ox, oy, oshape.edge_strips(poly, ring))
    elif ring > 0:
        add_edge_ring(board, ox, oy, ox + w, oy + h, ring)
    for fp in board.GetFootprints():
        if "MountingHole" in str(fp.GetFPID().GetUniStringLibId()):
            set_board_only(fp)
    ds = board.GetDesignSettings()
    try:
        ds.SetAuxOrigin(V(ox, oy + h))
    except Exception:
        pass
    if recorder is not None:
        geo = {p.ref: {str(r): {"box": [g.xmin, g.ymin, g.xmax, g.ymax], "pads": g.pads} for r, g in p.geo.items()}
               for p in place_list}
        Path(job["frames"]).write_text(json.dumps({"geo": geo, "frames": recorder, "size": [w, h], "outline": poly}))
    pcbnew.SaveBoard(board_path, board)
    report = {"width": w, "height": h, "origin": [ox, oy], "locked": locked_count, "relaxed": relaxed,
              "grids": len(macros), "antennas": notes.get("antennas", []), "decoupling": len(notes.get("auto_near", [])),
              "footprints": len(parts), "nets": len(nets), "rules": list(notes.get("log", [])),
              "shape": shape.get("type") if shape else "rect", "outline": poly}
    Path(job["report"]).write_text(json.dumps(report, indent=1))
    log(f"placed {len(parts) - locked_count} footprints ({locked_count} locked kept)")


def phase_dsn(job):
    board = pcbnew.LoadBoard(job["board"])
    for z in zones(board):
        if z.GetZoneName() == "kipipe_plane":
            remove(board, z)
    plane = job.get("plane_net")
    if plane and board.GetCopperLayerCount() >= 4:
        name = next((n for n in (plane, "/" + plane) if board.FindNet(n)), None)
        outline = edge_bbox(board)
        if name and outline:
            add_pour(board, name, ["In1.Cu"], outline, zone_name="kipipe_plane")
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
            pcbnew.SaveBoard(job["board"], board)
            log(f"plane: {name} on In1.Cu")
    ok = pcbnew.ExportSpecctraDSN(board, job["dsn"])
    if not ok or not Path(job["dsn"]).exists():
        raise RuntimeError("DSN export failed (is the board outline closed?)")
    log(f"exported {job['dsn']}")


# ---------------------------------------------------------------------------
# SES import (own implementation: works headless on every KiCad version)
# ---------------------------------------------------------------------------

def _res_scale(node):
    """Specctra (resolution unit n) -> factor to millimetres."""
    r = find(node, "resolution")
    unit, n = (str(r[1]).lower(), float(r[2])) if r else ("um", 1.0)
    per = {"um": 1e-3, "mm": 1.0, "mil": 0.0254, "inch": 25.4, "cm": 10.0}[unit]
    return per / n


def import_ses(board, path):
    tree = sexpr.parse(Path(path).read_text())
    routes = find(tree, "routes")
    if routes is None:
        raise RuntimeError("SES has no routes")
    k = _res_scale(routes)
    padstacks = {}
    lib = find(routes, "library_out")
    if lib:
        for ps in find_all(lib, "padstack"):
            name = str(ps[1])
            dia, layers = None, []
            for sh in find_all(ps, "shape"):
                c = find(sh, "circle")
                if c:
                    layers.append(str(c[1]))
                    dia = float(c[2]) * k
            padstacks[name] = (dia, layers)
    ds = board.GetDesignSettings()
    n_tracks = n_vias = 0
    for net in find_all(find(routes, "network_out") or [], "net"):
        name = str(net[1])
        ni = board.FindNet(name)
        if ni is None:
            log(f"SES net {name} not on board, skipped")
            continue
        code = ni.GetNetCode()
        for w in find_all(net, "wire"):
            path_ = find(w, "path")
            if not path_:
                continue
            layer = board.GetLayerID(str(path_[1]))
            width = float(path_[2]) * k
            coords = [float(v) for v in path_[3:] if not isinstance(v, list)]
            pts = [(coords[i] * k, -coords[i + 1] * k) for i in range(0, len(coords) - 1, 2)]
            for a, b in zip(pts, pts[1:]):
                if a == b:
                    continue
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(V(*a))
                t.SetEnd(V(*b))
                t.SetWidth(MM(width))
                t.SetLayer(layer)
                t.SetNetCode(code)
                board.Add(t)
                n_tracks += 1
        for v in find_all(net, "via"):
            name_ = str(v[1])
            x, y = float(v[2]) * k, -float(v[3]) * k
            m = re.search(r"_(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)_um", name_)
            if m:
                dia, drill = float(m.group(1)) / 1000, float(m.group(2)) / 1000
            else:
                dia = padstacks.get(name_, (None,))[0] or to_mm(ds.GetCurrentViaSize())
                drill = to_mm(ds.GetCurrentViaDrill())
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(V(x, y))
            try:
                via.SetViaType(pcbnew.VIATYPE_THROUGH)
            except Exception:
                pass
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            try:
                via.SetWidth(pcbnew.F_Cu, MM(dia))      # KiCad 9+: padstack, layer argument
            except TypeError:
                via.SetWidth(MM(dia))
            via.SetDrill(MM(drill))
            via.SetNetCode(code)
            board.Add(via)
            n_vias += 1
    return n_tracks, n_vias


def add_pour(board, net_name, layers, outline, zone_name="kipipe_pour"):
    ni = board.FindNet(net_name)
    if ni is None:
        return 0
    x0, y0, x1, y1 = outline
    n = 0
    for lname in layers:
        z = pcbnew.ZONE(board)
        z.SetLayer(board.GetLayerID(lname))
        z.SetNetCode(ni.GetNetCode())
        try:
            z.SetZoneName(zone_name)
        except Exception:
            pass
        o = z.Outline()
        o.NewOutline()
        for (x, y) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
            o.Append(MM(x), MM(y))
        try:
            z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)
        except Exception:
            pass
        try:
            z.SetMinThickness(MM(0.25))
            z.SetThermalReliefGap(MM(0.5))
            z.SetThermalReliefSpokeWidth(MM(0.5))
        except Exception:
            pass
        board.Add(z)
        n += 1
    return n


def unconnected(board):
    try:
        board.BuildConnectivity()
    except Exception:
        pass
    conn = board.GetConnectivity()
    for args in ((False,), ()):
        try:
            return conn.GetUnconnectedCount(*args)
        except TypeError:
            continue
    return -1


def _is_track(t):
    return t.GetClass() in ("PCB_TRACK", "TRACK")


def _is_via(t):
    return t.GetClass() in ("PCB_VIA", "VIA")


def _on_segment(px, py, ax, ay, bx, by, tol):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return (px - ax) ** 2 + (py - ay) ** 2 <= tol * tol
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2 <= tol * tol


def cleanup_tracks(board):
    """What KiCad's Cleanup Tracks does for router leftovers: drop exact duplicates and zero-length segments, then
    repeatedly remove any segment with an end that touches nothing of its net (no pad, via or other track).
    Locked tracks (kept from an existing board) are never touched. Returns how many segments were removed."""
    removed = 0
    seen = set()
    for t in list(board.GetTracks()):
        if not _is_track(t) or t.IsLocked():
            continue
        a, b = t.GetStart(), t.GetEnd()
        key = (t.GetLayer(), t.GetNetCode(), t.GetWidth(), *sorted([(a.x, a.y), (b.x, b.y)]))
        if (a.x, a.y) == (b.x, b.y) or key in seen:
            remove(board, t)
            removed += 1
            continue
        seen.add(key)
    pads = [(pad, pad.GetNetCode()) for fp in board.GetFootprints() for pad in fp.Pads()]
    for _ in range(50):
        tracks = [t for t in board.GetTracks() if _is_track(t)]
        vias = [v for v in board.GetTracks() if _is_via(v)]
        cut = []
        for t in tracks:
            if t.IsLocked():
                continue
            net, layer = t.GetNetCode(), t.GetLayer()
            for end in (t.GetStart(), t.GetEnd()):
                ok = False
                for o in tracks:
                    if o is t or o.GetNetCode() != net or o.GetLayer() != layer:
                        continue
                    s2, e2 = o.GetStart(), o.GetEnd()
                    if _on_segment(end.x, end.y, s2.x, s2.y, e2.x, e2.y, max(o.GetWidth() / 2, 1000)):
                        ok = True
                        break
                if not ok:
                    for v in vias:
                        if v.GetNetCode() == net and (v.GetPosition() - end).EuclideanNorm() <= max(v.GetWidth(pcbnew.F_Cu) if _takes_layer(v) else v.GetWidth(), 2000) / 2:
                            ok = True
                            break
                if not ok:
                    for pad, pn in pads:
                        if pn == net and pad.IsOnLayer(layer) and pad.HitTest(end):
                            ok = True
                            break
                if not ok:
                    cut.append(t)
                    break
        if not cut:
            break
        for t in cut:
            remove(board, t)
        removed += len(cut)
    return removed


def _takes_layer(v):
    try:
        v.GetWidth(pcbnew.F_Cu)
        return True
    except TypeError:
        return False


def phase_finish(job):
    board = pcbnew.LoadBoard(job["board"])
    nt, nv = import_ses(board, job["ses"])
    log(f"imported {nt} track segments, {nv} vias")
    cleaned = cleanup_tracks(board)
    if cleaned:
        log(f"removed {cleaned} leftover track segment(s) (dead-end spurs and duplicates from the router)")
    for z in zones(board):
        if z.GetZoneName() == "kipipe_plane":       # replaced by the full pour below
            remove(board, z)
    outline = edge_bbox(board)
    poured = 0
    if job.get("pour_net"):
        names = [n for n in (job["pour_net"], "/" + job["pour_net"]) if board.FindNet(n)]
        if names:
            cu = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
            poured = add_pour(board, names[0], cu, outline)
            filler = pcbnew.ZONE_FILLER(board)
            filler.Fill(board.Zones())
            log(f"poured {names[0]} on {', '.join(cu)}")
    left = unconnected(board)
    pcbnew.SaveBoard(job["board"], board)
    Path(job["report"]).write_text(json.dumps({"tracks": nt, "vias": nv, "unrouted": left, "pours": poured}))
    log(f"unrouted connections: {left}")


def phase_geometry(job):
    """Dump pad positions and track widths per net (for the checks)."""
    board = pcbnew.LoadBoard(job["board"])
    out = {"tracks": [], "pads": [], "outline": edge_bbox(board)}
    out["vias"] = []
    for t in board.GetTracks():
        if t.GetClass() in ("PCB_VIA", "VIA"):
            try:
                d = to_mm(t.GetWidth(pcbnew.F_Cu))      # KiCad 9+: per-layer padstack
            except TypeError:
                d = to_mm(t.GetWidth())
            out["vias"].append({"net": t.GetNetname(), "x": to_mm(t.GetPosition().x), "y": to_mm(t.GetPosition().y), "d": d})
        if t.GetClass() in ("PCB_TRACK", "TRACK"):
            out["tracks"].append({"net": t.GetNetname(), "w": to_mm(t.GetWidth()), "layer": board.GetLayerName(t.GetLayer()),
                                  "a": [to_mm(t.GetStart().x), to_mm(t.GetStart().y)],
                                  "b": [to_mm(t.GetEnd().x), to_mm(t.GetEnd().y)]})
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            p = pad.GetPosition()
            out["pads"].append({"ref": fp.GetReference(), "pin": pad.GetNumber(), "net": pad.GetNetname(),
                                "x": to_mm(p.x), "y": to_mm(p.y), "back": fp.GetLayer() == pcbnew.B_Cu})
    Path(job["out"]).write_text(json.dumps(out))


def phase_drc(job):
    """Fallback DRC through pcbnew for kicad-cli versions without `pcb drc`."""
    board = pcbnew.LoadBoard(job["board"])
    txt = Path(job["out"]).with_suffix(".txt")
    units = getattr(pcbnew, "EDA_UNITS_MILLIMETRES", None)
    if units is None:
        units = pcbnew.EDA_UNITS_MM
    pcbnew.WriteDRCReport(board, str(txt), units, True)
    viol, unconn, section = [], [], "v"
    cur = None
    for line in txt.read_text().splitlines():
        m = re.match(r"\*\* Found (\d+) (DRC violations|unconnected pads|Footprint errors)", line)
        if m:
            section = {"DRC violations": "v", "unconnected pads": "u", "Footprint errors": "f"}[m.group(2)]
            continue
        m = re.match(r"\[(\w+)\]: (.*)", line)
        if m:
            cur = {"type": m.group(1), "description": m.group(2), "severity": "error", "items": []}
            (unconn if section == "u" else viol).append(cur)
            continue
        if cur is not None:
            m = re.search(r"Severity: (\w+)", line)
            if m:
                cur["severity"] = m.group(1).lower()
            elif line.strip().startswith("@"):
                cur["items"].append({"description": line.strip()})
    Path(job["out"]).write_text(json.dumps({"violations": viol, "unconnected_items": unconn, "source": "pcbnew"}, indent=1))


def _rkey(r):
    return f"{round(r % 360, 1):g}"


def phase_layout(job):
    """Placement snapshot for the web editor: every footprint with its geometry at its
    current rotation and the three 90-degree steps from it (measured by pcbnew, so the
    editor never has to know KiCad's rotation convention)."""
    board = pcbnew.LoadBoard(job["board"])
    ob = edge_bbox(board) or (0, 0, 100, 100)
    ox, oy = ob[0], ob[1]
    parts = []
    for fp in board.GetFootprints():
        pos, cur = fp.GetPosition(), fp.GetOrientationDegrees()
        geo = {}
        for k in range(4):
            r = (cur + 90 * k) % 360
            fp.SetOrientationDegrees(r)
            fp.SetPosition(V(0, 0))
            l, t, rr, b = bbox_of(fp)
            pads = []
            for pad in fp.Pads():
                bb = pad.GetBoundingBox()
                pads.append([pad.GetNumber(), pad.GetNetname(), round(to_mm(bb.GetLeft()), 3), round(to_mm(bb.GetTop()), 3),
                             round(to_mm(bb.GetRight()), 3), round(to_mm(bb.GetBottom()), 3)])
            geo[_rkey(r)] = {"box": [round(to_mm(v), 3) for v in (l, t, rr, b)], "pads": pads}
        fp.SetOrientationDegrees(cur)
        fp.SetPosition(pos)
        lib = str(fp.GetFPID().GetUniStringLibId())
        parts.append({"ref": fp.GetReference(), "value": fp.GetValue(), "lib": lib,
                      "x": round(to_mm(pos.x) - ox, 3), "y": round(to_mm(pos.y) - oy, 3), "rot": _rkey(cur),
                      "locked": bool(fp.IsLocked()), "back": fp.GetLayer() == pcbnew.B_Cu,
                      "fixed": "MountingHole" in lib, "geo": geo})
    Path(job["out"]).write_text(json.dumps({"board": [ob[2] - ob[0], ob[3] - ob[1]], "parts": parts,
                                            "outline": board_outline_poly(board)}))


def phase_apply(job):
    """Apply editor moves, lock moved parts, clear unlocked copper and our pours."""
    board = pcbnew.LoadBoard(job["board"])
    ob = edge_bbox(board) or (0, 0, 0, 0)
    ox, oy = ob[0], ob[1]
    for z in zones(board):
        if z.GetZoneName() in ("kipipe_pour", "kipipe_antenna"):
            remove(board, z)
    moved = 0
    for ref, m in (job.get("moves") or {}).items():
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            log(f"move: {ref} not on the board, skipped")
            continue
        fp.SetOrientationDegrees(float(m.get("rot", fp.GetOrientationDegrees())))
        fp.SetPosition(V(ox + float(m["x"]), oy + float(m["y"])))
        fp.SetLocked(True)
        moved += 1
    removed = 0
    for t in list(board.GetTracks()):
        if not t.IsLocked():
            remove(board, t)
            removed += 1
    pcbnew.SaveBoard(job["board"], board)
    log(f"applied {moved} move(s), cleared {removed} unlocked track/via segments")


def phase_revise(job):
    """Prepare the previous board for a revised design: lock unchanged parts where they are, unlock the
    rest so they get placed again, delete parts the revision removed, and clear copper and our pours."""
    board = pcbnew.LoadBoard(job["board"])
    keep, drop = set(job.get("lock") or []), set(job.get("remove") or [])
    for z in zones(board):
        if z.GetZoneName() in ("kipipe_pour", "kipipe_antenna", "kipipe_edge"):
            remove(board, z)
    locked = deleted = 0
    temp = []                     # locked only for this revision; unlocked again after placement
    for fp in list(board.GetFootprints()):
        ref = fp.GetReference()
        if ref in drop:
            remove(board, fp)
            deleted += 1
        elif ref in keep:
            if not fp.IsLocked():
                temp.append(ref)
            fp.SetLocked(True)
            locked += 1
        elif "MountingHole" not in str(fp.GetFPID().GetUniStringLibId()):
            fp.SetLocked(False)
    for t in list(board.GetTracks()):
        remove(board, t)
    pcbnew.SaveBoard(job["board"], board)
    if job.get("report"):
        Path(job["report"]).write_text(json.dumps({"temp_locked": temp}))
    log(f"revision: kept {locked} parts in place, removed {deleted}")


def phase_unlock(job):
    board = pcbnew.LoadBoard(job["board"])
    n = 0
    for ref in job.get("refs") or []:
        fp = board.FindFootprintByReference(ref)
        if fp is not None and fp.IsLocked():
            fp.SetLocked(False)
            n += 1
    pcbnew.SaveBoard(job["board"], board)


def main():
    phase, job_path = sys.argv[1], sys.argv[2]
    job = json.loads(Path(job_path).read_text())
    # Fixed UUID sequence per board and phase: KiCad saves items sorted by UUID, so random UUIDs reorder the file,
    # the Freerouting input, and with it the routing. Seeded, the same spec gives the same board.
    import zlib
    try:
        pcbnew.KIID.SeedGenerator(zlib.crc32(f"{Path(job.get('board', '')).name}:{phase}".encode()))
    except Exception:
        pass
    {"build": phase_build, "dsn": phase_dsn, "finish": phase_finish, "geometry": phase_geometry, "drc": phase_drc,
     "layout": phase_layout, "apply": phase_apply, "revise": phase_revise, "unlock": phase_unlock}[phase](job)


if __name__ == "__main__":
    main()
