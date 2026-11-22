"""Shared drawing and table helpers for generators: board SVG from the layout, BOM grouping, PCB statistics."""
from __future__ import annotations

import html
import re

from .core import Job

esc = html.escape

ORDER = ("R", "C", "FB", "L", "D", "LED", "F", "Q", "Y", "U", "SW", "K", "BT", "TP", "J")
KIND_NAME = {"R": "Resistor", "C": "Capacitor", "L": "Inductor", "D": "Diode", "LED": "LED", "Q": "Transistor", "U": "IC / module",
             "J": "Connector", "SW": "Switch", "Y": "Crystal", "F": "Fuse", "FB": "Ferrite bead", "BT": "Battery", "TP": "Test point",
             "K": "Relay", "H": "Mounting hole", "ANT": "Antenna"}


def natkey(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def is_tht(footprint: str) -> bool:
    return bool(re.search(r"THT|PinHeader|PinSocket|TerminalBlock|_Radial|_Axial|DIP-|TO-220|TO-92|Barrel|_Vertical|_Horizontal|"
                          r"Package_DIP|Button_Switch_THT|Connector_Phoenix|Connector_JST:JST_(XH|PH|EH)_[BS]\d", footprint))


def bom(job: Job) -> list:
    """Grouped bill of materials: [{refs, qty, value, footprint, mpn, kind, tht}], in a sensible reading order."""
    groups = {}
    for ref, c in job.comps.items():
        k = job.kind(ref)
        if k == "H":
            continue
        key = (k, str(c.get("value", "")), c.get("footprint", ""), str(c.get("mpn") or ""))
        groups.setdefault(key, []).append(ref)
    rows = []
    for (k, value, fp, mpn), refs in groups.items():
        rows.append({"refs": sorted(refs, key=natkey), "qty": len(refs), "value": value, "footprint": fp.split(":")[-1],
                     "footprint_id": fp, "mpn": mpn, "kind": k, "kind_name": KIND_NAME.get(k, k), "tht": is_tht(fp)})
    rows.sort(key=lambda g: (ORDER.index(g["kind"]) if g["kind"] in ORDER else 99, natkey(g["refs"][0])))
    return rows


def csv_line(cells) -> str:
    return ",".join('"' + str(c).replace('"', '""') + '"' for c in cells)


def md_table(head, rows) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in r) + " |" for r in rows]
    return "\n".join(out)


_PAL = {"R": "#c9b48a", "C": "#d8a25a", "U": "#3b3f46", "J": "#8f969e", "LED": "#e0564a", "D": "#5a5f66", "Q": "#3b3f46",
        "SW": "#b8bcc2", "Y": "#c8ccd2", "L": "#6d7279"}


def board_svg(job: Job, side="top", scale=10.0, labels=True, cls="brd") -> str:
    """The placed board as SVG. Every part is <g class="part" data-ref="R1">, so pages can highlight parts."""
    size = job.size()
    if not size:
        return ""
    w, h = size
    back = side != "top"
    fx = (lambda x: w - x) if back else (lambda x: x)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" class="{cls}" viewBox="-1 -1 {w + 2:.2f} {h + 2:.2f}" '
         f'width="{(w + 2) * scale:.0f}" height="{(h + 2) * scale:.0f}" font-family="DejaVu Sans, Arial, sans-serif">']
    outline = job.layout.get("outline")
    if outline and isinstance(outline, list) and len(outline) > 2:
        pts = " ".join(f"{fx(x):.2f},{y:.2f}" for x, y in outline)
        o.append(f'<polygon points="{pts}" fill="#1f6b3a" stroke="#0d3d20" stroke-width="0.2"/>')
    else:
        o.append(f'<rect x="0" y="0" width="{w:.2f}" height="{h:.2f}" rx="0.8" fill="#1f6b3a" stroke="#0d3d20" stroke-width="0.2"/>')
    for p in job.layout.get("parts", []):
        ref = p["ref"]
        g = (p.get("geo") or {}).get(str(p.get("rot", "0")))
        if not g:
            continue
        if str(p.get("lib", "")).startswith("MountingHole"):
            o.append(f'<circle cx="{fx(p["x"]):.2f}" cy="{p["y"]:.2f}" r="1.6" fill="#fff" stroke="#c9a23c" stroke-width="0.5"/>')
            continue
        if bool(p.get("back")) != back:
            continue
        b = g["box"]
        x0, x1 = sorted((fx(p["x"] + b[0]), fx(p["x"] + b[2])))
        y0, y1 = p["y"] + b[1], p["y"] + b[3]
        k = job.kind(ref) if ref in job.comps else "U"
        o.append(f'<g class="part" data-ref="{esc(ref)}">')
        o.append(f'<rect class="body" x="{x0:.2f}" y="{y0:.2f}" width="{x1 - x0:.2f}" height="{y1 - y0:.2f}" rx="0.25" '
                 f'fill="{_PAL.get(k, "#4a4f57")}" fill-opacity="0.85" stroke="#0b0d10" stroke-width="0.08"/>')
        for n, net, px0, py0, px1, py1 in g.get("pads", []):
            a, c = sorted((fx(p["x"] + px0), fx(p["x"] + px1)))
            first = ' class="pad1"' if str(n) in ("1", "A1") else ""
            o.append(f'<rect{first} x="{a:.2f}" y="{p["y"] + py0:.2f}" width="{c - a:.2f}" height="{py1 - py0:.2f}" '
                     f'fill="{"#f2d675" if first else "#c9a23c"}"><title>{esc(ref)}.{esc(str(n))} {esc(str(net))}</title></rect>')
        if labels:
            fs = max(0.7, min(1.6, (x1 - x0) / max(len(ref), 2) * 1.3, (y1 - y0) * 0.7))
            o.append(f'<text x="{(x0 + x1) / 2:.2f}" y="{(y0 + y1) / 2 + fs * 0.35:.2f}" font-size="{fs:.2f}" text-anchor="middle" '
                     f'fill="#fff" stroke="#000" stroke-width="0.04" paint-order="stroke">{esc(ref)}</text>')
        o.append("</g>")
    o.append("</svg>")
    return "\n".join(o)


def pcb_stats(job: Job) -> dict:
    """Numbers a fab order form asks for, read from the .kicad_pcb text. Empty if there is no board file."""
    f = job.pcb
    if not f:
        return {}
    t = f.read_text(encoding="utf-8", errors="replace")
    seg_w = [float(x) for x in re.findall(r"\(segment\b[^\n]*?\(width ([\d.]+)\)", t)]
    vias = re.findall(r"\(via\b[^\n]*?\(size ([\d.]+)\)\s*\(drill ([\d.]+)\)", t)
    pads = re.findall(r'\(pad "[^"]*" (smd|thru_hole|np_thru_hole|connect)', t)
    drills = [float(x) for x in re.findall(r"\(drill (?:oval )?([\d.]+)", t)]
    back = len(re.findall(r'\(footprint "[^"]*"\s*(?:\(locked\)\s*)?\(layer "B\.Cu"\)', t))
    return {"tracks": len(seg_w), "min_track_mm": min(seg_w) if seg_w else None, "vias": len(vias),
            "min_via_drill_mm": min((float(d) for _, d in vias), default=None),
            "min_via_size_mm": min((float(s) for s, _ in vias), default=None),
            "smd_pads": pads.count("smd"), "tht_pads": pads.count("thru_hole"), "npth": pads.count("np_thru_hole"),
            "min_drill_mm": min(drills) if drills else None, "max_drill_mm": max(drills) if drills else None,
            "parts_on_back": back, "zones": len(re.findall(r"\(zone\b", t))}


PAGE_CSS = """
:root{--bg:#f6f5f0;--ink:#1c1e21;--dim:#676b72;--line:#d9d6cc;--card:#fff;--acc:#1f6b3a;--warn:#b26a00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#14171a;--ink:#e8e6df;--dim:#9aa0a8;--line:#2c3137;--card:#1c2024;--acc:#5fbf82;--warn:#e0a84a;--bad:#ef7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
p.lead{color:var(--dim);margin:0 0 18px}table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;color:var(--dim);font-weight:500}
th,td{padding:6px 10px 6px 0;border-bottom:1px solid var(--line);vertical-align:top}code,.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;overflow:auto}.card svg,.card img{max-width:100%;height:auto;display:block;margin:0 auto}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.facts div{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.facts b{display:block;font-size:18px}.facts span{color:var(--dim);font-size:12.5px}
.sev{display:inline-block;font-size:11.5px;padding:1px 7px;border-radius:9px;border:1px solid currentColor;margin-right:6px}
.sev.error{color:var(--bad)}.sev.warning{color:var(--warn)}.sev.note{color:var(--dim)}.dim{color:var(--dim)}
@media print{body{background:#fff}.noprint{display:none}}
"""


def page(title: str, body: str, extra_css: str = "", script: str = "") -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{esc(title)}</title><style>{PAGE_CSS}{extra_css}</style></head><body><main>{body}</main>"
            + (f"<script>{script}</script>" if script else "") + "</body></html>")
