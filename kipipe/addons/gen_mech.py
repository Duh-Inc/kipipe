"""Mechanical generators: printable enclosure (OpenSCAD), STEP model, PDF drawings, panel plan."""
from __future__ import annotations

import math
import re
import shutil
import subprocess

from .. import kicad_lib as kl
from .core import addon
from .draw import esc

GROUP = "Mechanical"

# Height above the board (mm) by footprint name, first match wins. Estimates for sizing a case, not for tolerance work.
HEIGHTS = [
    (r"TerminalBlock.*5\.08|TerminalBlock_Phoenix_MKDS", 10.5), (r"TerminalBlock", 9.0),
    (r"BarrelJack|Barrel_Jack", 11.0), (r"USB_C", 3.3), (r"USB_B_Micro|USB_Micro", 3.0), (r"USB_B_Mini|USB_Mini", 4.0),
    (r"USB_B(?!_M)", 11.5), (r"USB_A", 7.2), (r"RJ45|RJ12|8P8C", 13.5), (r"Jack_3\.5mm", 6.0), (r"SMA_", 9.0),
    (r"PinHeader.*Vertical(?!_SMD)|PinSocket.*Vertical", 8.6), (r"PinHeader.*Horizontal|PinSocket.*Horizontal", 6.0),
    (r"JST_XH.*Vertical|JST_XH_B", 7.0), (r"JST_PH.*Vertical|JST_PH_B", 6.0), (r"JST_(XH|PH)_S", 6.0), (r"JST_SH", 3.0),
    (r"CP_Radial_D(\d+(?:\.\d+)?)", "radial"), (r"C_Radial|CP_Axial|C_Disc|C_Rect", 8.0),
    (r"DIP-\d+", 4.5), (r"TO-220.*Vertical", 16.0), (r"TO-220", 5.0), (r"TO-92", 6.5), (r"TO-252|DPAK", 2.4), (r"TO-263|D2PAK", 4.6),
    (r"SW_PUSH_6mm|SW_Push_1P1T_NO_6x6", 5.0), (r"SW_(SPST|SPDT|Slide|DIP)", 4.5), (r"SW_", 4.0),
    (r"Relay", 16.0), (r"ESP32-WROOM|ESP32-WROVER|ESP32-S3-WROOM|ESP32-C3", 3.3), (r"ESP-12", 3.2), (r"RFM|XBee", 4.0),
    (r"Crystal_HC49-4H|Crystal_HC49-U_Vertical", 13.5), (r"Crystal_HC49", 4.5), (r"L_.*(Bourns|Radial|Toroid|SRR|SRN|CDRH|12x12|10x10)", 6.0),
    (r"LED_D5", 9.0), (r"LED_D3", 5.5), (r"SOT-223|SOT-89", 1.8), (r"CP_EIA|Tantalum", 2.2), (r"D_SM[ABC]", 2.4),
    (r"Fuseholder|Battery|BatteryHolder", 12.0), (r"Buzzer|Speaker", 9.5), (r"Potentiometer|Trimmer|RV_", 7.0),
    (r"R_Axial.*Vertical", 12.0), (r"R_Axial|D_DO-|D_A-", 3.5), (r"_THT", 8.0),
    (r"QFN|DFN|LGA|BGA|SON", 1.0), (r"SOIC|SOP|TSSOP|MSOP|QFP", 1.8), (r"0402|0603|0805|1206|1210", 1.2),
]
SCREW = {"2": (2.2, 1.7, 4.5), "2.5": (2.7, 2.1, 5.2), "3": (3.2, 2.6, 6.0), "4": (4.3, 3.4, 8.0)}   # clearance hole, tap hole, boss


def part_height(footprint: str, opts=None) -> float:
    name = footprint.split(":")[-1]
    for pat, h in HEIGHTS:
        m = re.search(pat, name, re.I)
        if m:
            if h == "radial":
                return max(7.0, float(m.group(1)) * 1.5 + 1.5)
            return h
    return 2.0


@addon(id="gen.enclosure", title="Printable enclosure", kind="generate", group=GROUP, status="beta", needs=("spec", "layout"),
       options=[{"key": "wall", "label": "Wall thickness (mm)", "type": "number", "default": 2.0},
                {"key": "gap", "label": "Gap around the board (mm)", "type": "number", "default": 0.6},
                {"key": "standoff", "label": "Space under the board (mm)", "type": "number", "default": 4.0},
                {"key": "lid_holes", "label": "Openings in the lid over LEDs and buttons", "type": "bool", "default": True}],
       summary="A parametric two-part case (OpenSCAD) sized to this board: standoffs under the mounting holes, wall openings for edge connectors, lid holes over LEDs and buttons.")
def gen_enclosure(r):
    j = r.job
    w, h = j.size()
    wall, gap, so = r.num("wall", 2.0), r.num("gap", 0.6), r.num("standoff", 4.0)
    t = float(j.board.get("thickness_mm") or 1.6)
    heights = {ref: part_height(c.get("footprint", "")) for ref, c in j.comps.items() if j.placed(ref) and not j.placed(ref).get("back")}
    tallest = max(heights.values(), default=2.0)
    top = tallest + 1.5
    fy = lambda y: h - y        # noqa: E731  layout y runs down, OpenSCAD y runs up
    holes = []
    for p in j.holes():
        m = re.search(r"_M(\d(?:\.\d)?)", p.get("lib", ""))
        holes.append((p["x"], fy(p["y"]), m.group(1) if m and m.group(1) in SCREW else "3"))
    cuts, notes = [], []
    for ref, c in j.comps.items():
        b = j.box(ref)
        if not b or j.placed(ref).get("back"):
            continue
        if not (c.get("edge") or j.kind(ref) == "J"):
            continue
        d = {"left": b[0], "right": w - b[2], "top": b[1], "bottom": h - b[3]}
        side = c.get("edge") if c.get("edge") in d else min(d, key=d.get)
        if d[side] > 2.5:
            if j.kind(ref) == "J":
                notes.append(f"{ref} is {d[side]:.1f} mm from the nearest edge: no opening made")
            continue
        hh = heights.get(ref, 3.0) + 0.6
        if side in ("left", "right"):
            lo, hi = fy(b[3]) - 0.6, fy(b[1]) + 0.6
        else:
            lo, hi = b[0] - 0.6, b[2] + 0.6
        cuts.append((ref, side, lo, hi, hh))
    lid = []
    if r.opts.get("lid_holes") not in (False, "false", "0", 0):
        for ref in j.comps:
            p = j.placed(ref)
            if p and not p.get("back") and j.kind(ref) in ("LED", "SW"):
                lid.append((ref, p["x"], fy(p["y"]), 1.6 if j.kind(ref) == "LED" else 2.6))
    f = lambda v: f"{v:.2f}".rstrip("0").rstrip(".")       # noqa: E731
    s = [f"// Enclosure for {j.name}, generated by kipipe. Open in OpenSCAD, press F6, export STL.",
         "// Heights of parts are estimates from footprint names: check `top_clear` against your tallest part before printing.",
         'part = "both";      // "base", "lid" or "both" (both: side by side for one print)', "",
         f"board_w = {f(w)}; board_h = {f(h)}; board_t = {f(t)};",
         f"wall = {f(wall)}; floor_t = {f(max(wall, 1.6))}; gap = {f(gap)};",
         f"standoff = {f(so)};      // space under the board for solder joints and through-hole pins",
         f"top_clear = {f(top)};    // space above the board: tallest part ({f(tallest)} mm) plus margin",
         "lid_t = 2; lip = 1.6; fit = 0.2; $fn = 40;",
         "slot_open = true;   // true: connector openings run to the top so the board drops straight in; false: closed windows (tilt the board in)", "",
         "in_w = board_w + 2*gap; in_h = board_h + 2*gap;",
         "out_w = in_w + 2*wall; out_h = in_h + 2*wall;",
         "base_z = floor_t + standoff + board_t + top_clear;",
         "board_z = floor_t + standoff;      // underside of the board",
         "ox = wall + gap; oy = wall + gap;  // board origin inside the case", "",
         "// [x, y, clearance hole, tap hole, boss diameter] at each mounting hole",
         "holes = [" + ", ".join(f"[{f(x)}, {f(y)}, {SCREW[m][0]}, {SCREW[m][1]}, {SCREW[m][2]}]" for x, y, m in holes) + "];",
         "// [side, from, to, height above the board]  side: 0 left, 1 right, 2 top (far), 3 bottom (near)",
         "cuts = [" + ", ".join(f"[{('left', 'right', 'top', 'bottom').index(sd)}, {f(lo)}, {f(hi)}, {f(hh)}]" for _, sd, lo, hi, hh in cuts) + "];"
         + ("   // " + ", ".join(c[0] for c in cuts) if cuts else ""),
         "// [x, y, radius] openings in the lid",
         "lid_holes = [" + ", ".join(f"[{f(x)}, {f(y)}, {rad}]" for _, x, y, rad in lid) + "];" + ("   // " + ", ".join(c[0] for c in lid) if lid else ""), "",
         "module rbox(w, h, z, r = 2) { hull() for (x = [r, w - r], y = [r, h - r]) translate([x, y, 0]) cylinder(r = r, h = z); }", "",
         "module base() {",
         "  difference() {",
         "    rbox(out_w, out_h, base_z);",
         "    translate([wall, wall, floor_t]) cube([in_w, in_h, base_z]);",
         "    for (c = cuts) {",
         "      z0 = board_z + board_t - 0.3;",
         "      zh = slot_open ? base_z : c[3] + 0.6;",
         "      if (c[0] == 0) translate([-1, oy + c[1], z0]) cube([wall + 2, c[2] - c[1], zh]);",
         "      if (c[0] == 1) translate([out_w - wall - 1, oy + c[1], z0]) cube([wall + 2, c[2] - c[1], zh]);",
         "      if (c[0] == 2) translate([ox + c[1], out_h - wall - 1, z0]) cube([c[2] - c[1], wall + 2, zh]);",
         "      if (c[0] == 3) translate([ox + c[1], -1, z0]) cube([c[2] - c[1], wall + 2, zh]);",
         "    }",
         "  }",
         "  for (p = holes) translate([ox + p[0], oy + p[1], floor_t - 0.01]) difference() {",
         "    cylinder(d = p[4], h = standoff);",
         "    translate([0, 0, -1]) cylinder(d = p[3], h = standoff + 2);",
         "  }",
         "}", "",
         "module lid() {",
         "  difference() {",
         "    union() {",
         "      rbox(out_w, out_h, lid_t);",
         "      translate([wall + fit, wall + fit, lid_t - 0.01]) difference() {",
         "        cube([in_w - 2*fit, in_h - 2*fit, lip]);",
         "        translate([1.2, 1.2, -1]) cube([in_w - 2*fit - 2.4, in_h - 2*fit - 2.4, lip + 2]);",
         "      }",
         "    }",
         "    // the lid is printed face down, so x is mirrored to line up with the board when fitted",
         "    for (p = lid_holes) translate([out_w - (ox + p[0]), oy + p[1], -1]) cylinder(r = p[2], h = lid_t + lip + 2);",
         "  }",
         "}", "",
         'if (part == "base" || part == "both") base();',
         'if (part == "lid" || part == "both") translate([part == "both" ? out_w + 6 : 0, 0, 0]) lid();', ""]
    scad = r.write(f"{j.name}-enclosure.scad", "\n".join(s), "Enclosure (OpenSCAD)")
    exe = shutil.which("openscad")
    if exe:
        for part in ("base", "lid"):
            stl = r.out / f"{j.name}-enclosure-{part}.stl"
            try:
                p = subprocess.run([exe, "-o", str(stl), "-D", f'part="{part}"', str(scad)], capture_output=True, text=True, timeout=180)
                if stl.exists() and stl.stat().st_size > 200:
                    r.attach(stl, f"STL: {part}")
                else:
                    r.finding("warning", f"OpenSCAD could not render the {part}", (p.stderr or "")[-300:])
            except Exception as e:
                r.finding("warning", f"OpenSCAD failed on the {part}", f"{type(e).__name__}: {e}")
    else:
        r.finding("note", "OpenSCAD is not installed, so no STL was made", "Open the .scad file in OpenSCAD (sudo apt install openscad) and export it, or install it and run this again.")
    for n in notes:
        r.finding("note", n, "Add a `cuts` entry by hand if it needs one.")
    if not holes:
        r.finding("warning", "The board has no mounting holes", "The case has no standoffs: the board would lie loose. Set mounting holes in the board options and rerun.")
    r.finding("note", f"Inside height above the board is {top:.1f} mm", f"From the tallest part estimate ({max(heights, key=heights.get) if heights else 'none'}: {tallest:g} mm). "
              "Heights come from a footprint-name table: measure the real part.")
    r.data.update(outer=[round(w + 2 * (gap + wall), 1), round(h + 2 * (gap + wall), 1), round(max(wall, 1.6) + so + t + top + 2, 1)],
                  cuts=[c[0] for c in cuts], lid_holes=[c[0] for c in lid], tallest=tallest)
    r.summary = (f"{r.data['outer'][0]:g} × {r.data['outer'][1]:g} × {r.data['outer'][2]:g} mm outside, {len(holes)} standoff(s), "
                 f"{len(cuts)} wall opening(s), {len(lid)} lid hole(s)")


# ---------------------------------------------------------------------------
def _cli(r, *args, timeout=240):
    env = kl.detect()
    p = subprocess.run([*env.cli_cmd, *map(str, args)], capture_output=True, text=True, cwd=r.job.dir, env=kl.clean_env(), timeout=timeout)
    return p


@addon(id="gen.step", title="3D model (STEP)", kind="generate", group=GROUP, needs=("pcb", "kicad"),
       summary="The assembled board as a STEP file for mechanical CAD (FreeCAD, Fusion, Onshape): design a case around the real thing.")
def gen_step(r):
    j = r.job
    r.out.mkdir(parents=True, exist_ok=True)
    out = r.out / f"{j.name}.step"
    p = _cli(r, "pcb", "export", "step", "--subst-models", "--force", "-o", out, j.pcb)
    if not out.exists():
        p = _cli(r, "pcb", "export", "step", "-f", "-o", out, j.pcb)
    if out.exists() and out.stat().st_size > 1000:
        r.attach(out, "STEP model")
        miss = len(re.findall(r"(?i)could not|not found|missing", (p.stdout or "") + (p.stderr or "")))
        if miss:
            r.finding("note", "Some parts have no 3D model", "They are missing from the STEP file; the board and the other parts are there.")
        r.summary = f"{out.stat().st_size // 1024} kB"
    else:
        raise RuntimeError("kicad-cli did not write a STEP file: " + ((p.stderr or p.stdout or "").strip()[-200:] or "no message"))


@addon(id="gen.docpack", title="PDF drawings", kind="generate", group="Share", needs=("pcb", "kicad"),
       summary="Schematic and board drawings as PDF (schematic, assembly drawing per side, copper per layer) for printing or a lab notebook.")
def gen_docpack(r):
    j = r.job
    r.out.mkdir(parents=True, exist_ok=True)
    made = 0
    if j.sch:
        out = r.out / f"{j.name}-schematic.pdf"
        _cli(r, "sch", "export", "pdf", "-o", out, j.sch)
        if out.exists():
            r.attach(out, "Schematic (PDF)")
            made += 1
    sets = [("assembly-top", "F.Fab,F.Silkscreen,Edge.Cuts", []), ("copper-top", "F.Cu,Edge.Cuts", []),
            ("copper-bottom", "B.Cu,Edge.Cuts", ["--mirror"]), ("assembly-bottom", "B.Fab,B.Silkscreen,Edge.Cuts", ["--mirror"])]
    for name, layers, extra in sets:
        out = r.out / f"{j.name}-{name}.pdf"
        p = _cli(r, "pcb", "export", "pdf", "-l", layers, *extra, "-o", out, j.pcb)
        if out.exists() and out.stat().st_size > 800:
            r.attach(out, name.replace("-", " ").capitalize() + " (PDF)")
            made += 1
        elif name == "assembly-top":
            r.finding("warning", "kicad-cli could not plot the board to PDF", (p.stderr or p.stdout or "").strip()[-200:])
    r.summary = f"{made} PDF(s)"


# ---------------------------------------------------------------------------
@addon(id="gen.panel", title="Panel plan", kind="generate", group="Build and order", status="preview", needs=("spec", "layout"),
       options=[{"key": "panel_w", "label": "Panel width (mm)", "type": "number", "default": 100},
                {"key": "panel_h", "label": "Panel height (mm)", "type": "number", "default": 100},
                {"key": "gap", "label": "Gap between boards (mm; 0 for V-cut)", "type": "number", "default": 2},
                {"key": "rail", "label": "Edge rails (mm)", "type": "number", "default": 5}],
       summary="How many copies fit on a panel and how, with a drawing and the KiKit command that would build it. Does not write the panel board itself yet.")
def gen_panel(r):
    j = r.job
    w, h = j.size()
    pw, ph, gap, rail = r.num("panel_w", 100), r.num("panel_h", 100), r.num("gap", 2), r.num("rail", 5)
    best = None
    for rot, (bw, bh) in enumerate(((w, h), (h, w))):
        nx = int((pw - 2 * 0 + gap) // (bw + gap))
        ny = int((ph - 2 * rail + gap) // (bh + gap))
        if nx > 0 and ny > 0 and (best is None or nx * ny > best[0]):
            best = (nx * ny, nx, ny, rot, bw, bh)
    if not best:
        r.summary = f"one board ({w:.0f} × {h:.0f} mm) does not fit a {pw:g} × {ph:g} mm panel with {rail:g} mm rails"
        r.finding("warning", r.summary, "Use a larger panel or thinner rails.")
        return
    n, nx, ny, rot, bw, bh = best
    used_w, used_h = nx * bw + (nx - 1) * gap, ny * bh + (ny - 1) * gap + 2 * rail
    sc = 4
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 -2 {pw + 4} {ph + 4}" width="{(pw + 4) * sc}" height="{(ph + 4) * sc}" font-family="DejaVu Sans, Arial" font-size="3">'
         f'<rect x="0" y="0" width="{pw}" height="{ph}" fill="none" stroke="#999" stroke-dasharray="2 1" stroke-width="0.3"/>'
         f'<rect x="0" y="0" width="{used_w}" height="{used_h}" fill="#e9e4c9" stroke="#555" stroke-width="0.3"/>']
    for iy in range(ny):
        for ix in range(nx):
            x, y = ix * (bw + gap), rail + iy * (bh + gap)
            o.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bw:.2f}" height="{bh:.2f}" fill="#1f6b3a" stroke="#0d3d20" stroke-width="0.2"/>'
                     f'<text x="{x + bw / 2:.2f}" y="{y + bh / 2 + 1:.2f}" text-anchor="middle" fill="#fff">{iy * nx + ix + 1}</text>')
    o.append(f'<text x="1" y="{rail - 1.5:.1f}" fill="#555">rail</text></svg>')
    r.write(f"{j.name}-panel.svg", "".join(o), "Panel drawing")
    pcb = j.pcb.name if j.pcb else f"{j.name}.kicad_pcb"
    cut = "vcuts" if gap == 0 else "mousebites"
    cmd = (f"kikit panelize --layout 'grid; rows: {ny}; cols: {nx}; space: {gap:g}mm" + ("; rotation: 90deg" if rot else "") + "' "
           + ("--tabs 'fixed; width: 3mm; vcount: 2' " if gap else "--tabs full ")
           + f"--cuts '{cut}" + ("; drill: 0.5mm; spacing: 1mm; offset: 0.2mm" if gap else "") + "' "
           + f"--framing 'railstb; width: {rail:g}mm; space: {gap:g}mm' --post 'millradius: 1mm' {pcb} {j.name}-panel.kicad_pcb")
    r.write("kikit-command.txt", cmd + "\n", "KiKit command")
    if not shutil.which("kikit"):
        r.finding("note", "KiKit is not installed", "The command is written out but was not run. KiKit installs into KiCad's Python (see its documentation).")
    r.finding("note", "Check with your fab before panelising yourself", "Many fabs panelise for free or charge extra for customer panels with several designs.")
    r.data.update(count=n, cols=nx, rows=ny, rotated=bool(rot), size=[round(used_w, 1), round(used_h, 1)], command=cmd)
    r.summary = f"{n} board(s): {nx} × {ny}" + (", turned 90°" if rot else "") + f", panel {used_w:.0f} × {used_h:.0f} mm"
