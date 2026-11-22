"""Fallback converters for legacy KiCad 5 library files (.lib symbols, .mod footprints), used when kicad-cli
can't upgrade them (KiCad 7's CLI only reads the new formats). They keep what the pipeline needs: pins, pads,
fields and simple graphics. Arcs become polylines; De Morgan alternates and footprint polygons are dropped."""
from __future__ import annotations

import math
import re
import shlex

from .sexpr import Q

MIL = 0.0254
ETYPE = {"I": "input", "O": "output", "B": "bidirectional", "T": "tri_state", "P": "passive", "U": "unspecified",
         "W": "power_in", "w": "power_out", "C": "open_collector", "E": "open_emitter", "N": "no_connect"}
PIN_ANGLE = {"R": 0, "U": 90, "L": 180, "D": 270}
SHAPE = {"I": "inverted", "C": "clock", "CI": "inverted_clock", "L": "input_low", "V": "output_low", "F": "falling_edge_clock",
         "X": "non_logic"}


def _f(v) -> str:
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _split(line: str) -> list:
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def _stroke():
    return ["stroke", ["width", "0"], ["type", "default"]]


def _fill(c):
    return ["fill", ["type", {"F": "outline", "f": "background"}.get(c, "none")]]


def _hidden_effects(hide: bool, size=1.27):
    e = ["effects", ["font", ["size", _f(size), _f(size)]]]
    if hide:
        e.append("hide")         # old syntax: every KiCad from 7 on reads it
    return e


def convert_lib(text: str) -> list:
    """Legacy .lib text -> kicad_symbol_lib tree (version 20211014, which KiCad 7+ reads)."""
    out = ["kicad_symbol_lib", ["version", "20211014"], ["generator", Q("kipipe")]]
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        t = _split(line)
        k = t[0]
        if k == "DEF":
            name = t[1].lstrip("~")
            cur = {"name": name, "ref": t[2], "units": int(t[7]) if len(t) > 7 else 1,
                   "power": len(t) > 9 and t[9] == "P", "fields": [], "items": {}, "pin_names_hidden": len(t) > 6 and t[6] == "N",
                   "pin_numbers_hidden": len(t) > 5 and t[5] == "N", "offset": float(t[4]) * MIL if len(t) > 4 else 0.508}
        elif cur is None:
            continue
        elif re.match(r"F\d+$", k):
            idx = int(k[1:])
            val = t[1]
            x, y, size = float(t[2]) * MIL, float(t[3]) * MIL, float(t[4]) * MIL
            hide = len(t) > 6 and t[6] == "I"
            key = {0: "Reference", 1: "Value", 2: "Footprint", 3: "Datasheet"}.get(idx, t[-1] if len(t) > 9 else f"Field{idx}")
            cur["fields"].append((key, val, x, y, size, hide or idx >= 2))
        elif k == "DRAW" or k == "ENDDRAW" or k in ("$FPLIST", "$ENDFPLIST", "ALIAS"):
            continue
        elif k == "ENDDEF":
            out.append(_emit_symbol(cur))
            cur = None
        elif k in ("S", "P", "C", "A", "X", "T"):
            try:
                _draw(cur, t)
            except (IndexError, ValueError):
                pass
    return out


def _bucket(cur, unit, conv):
    return cur["items"].setdefault((int(unit), 1 if int(conv) <= 1 else int(conv)), [])


def _draw(cur, t):
    k = t[0]
    if k == "S":   # S x1 y1 x2 y2 unit convert width fill
        if int(t[6]) > 1:
            return
        _bucket(cur, t[5], t[6]).append(["rectangle", ["start", _f(float(t[1]) * MIL), _f(float(t[2]) * MIL)],
                                         ["end", _f(float(t[3]) * MIL), _f(float(t[4]) * MIL)], _stroke(), _fill(t[8] if len(t) > 8 else "N")])
    elif k == "C":   # C x y r unit convert width fill
        if int(t[5]) > 1:
            return
        _bucket(cur, t[4], t[5]).append(["circle", ["center", _f(float(t[1]) * MIL), _f(float(t[2]) * MIL)],
                                         ["radius", _f(float(t[3]) * MIL)], _stroke(), _fill(t[7] if len(t) > 7 else "N")])
    elif k == "P":   # P n unit convert width x y ... fill
        n = int(t[1])
        if int(t[3]) > 1:
            return
        xy = t[5:5 + 2 * n]
        pts = ["pts"] + [["xy", _f(float(xy[i]) * MIL), _f(float(xy[i + 1]) * MIL)] for i in range(0, 2 * n, 2)]
        fill = t[5 + 2 * n] if len(t) > 5 + 2 * n else "N"
        _bucket(cur, t[2], t[3]).append(["polyline", pts, _stroke(), _fill(fill)])
    elif k == "A":   # A x y r start end unit convert width fill ...
        if int(t[7]) > 1:
            return
        cx, cy, r = float(t[1]) * MIL, float(t[2]) * MIL, float(t[3]) * MIL
        a0, a1 = float(t[4]) / 10, float(t[5]) / 10
        if a1 < a0:
            a0, a1 = a1, a0
        if a1 - a0 > 180:      # legacy arcs take the short way round
            a0, a1 = a1, a0 + 360
        pts = ["pts"]
        for i in range(9):
            a = math.radians(a0 + (a1 - a0) * i / 8)
            pts.append(["xy", _f(cx + r * math.cos(a)), _f(cy + r * math.sin(a))])
        _bucket(cur, t[6], t[7]).append(["polyline", pts, _stroke(), _fill("N")])
    elif k == "T":   # text: decorative, skip
        return
    elif k == "X":   # X name num x y len orient snum sname unit convert etype [shape]
        if int(t[10]) > 1:
            return
        name, num = t[1], t[2]
        x, y, ln = float(t[3]) * MIL, float(t[4]) * MIL, float(t[5]) * MIL
        etype = ETYPE.get(t[11], "passive")
        shape = t[12] if len(t) > 12 else ""
        hidden = "N" in shape
        gshape = SHAPE.get(shape.replace("N", ""), "line")
        pin = ["pin", etype, gshape, ["at", _f(x), _f(y), str(PIN_ANGLE.get(t[6], 0))], ["length", _f(ln)]]
        if hidden:
            pin.append("hide")
        pin += [["name", Q(name if name != "~" else ""), _hidden_effects(False, float(t[8]) * MIL or 1.27)],
                ["number", Q(num), _hidden_effects(False, float(t[7]) * MIL or 1.27)]]
        _bucket(cur, t[9], t[10]).append(pin)


def _emit_symbol(cur) -> list:
    name = cur["name"]
    s = ["symbol", Q(name)]
    if cur["power"]:
        s.append(["power"])
    if cur["pin_numbers_hidden"]:
        s.append(["pin_numbers", "hide"])
    s.append(["pin_names", ["offset", _f(cur["offset"])]] + (["hide"] if cur["pin_names_hidden"] else []))
    s += [["in_bom", "yes"], ["on_board", "yes"]]
    keys = set()
    for key, val, x, y, size, hide in cur["fields"]:
        if key == "Value" and val in ("", "~"):
            val = name
        if val == "~":
            val = ""
        keys.add(key)
        s.append(["property", Q(key), Q(val), ["at", _f(x), _f(y), "0"], _hidden_effects(hide, size or 1.27)])
    for key in ("Reference", "Value", "Footprint", "Datasheet"):
        if key not in keys:
            s.append(["property", Q(key), Q({"Reference": cur["ref"], "Value": name}.get(key, "")), ["at", "0", "0", "0"],
                      _hidden_effects(key in ("Footprint", "Datasheet"))])
    for (unit, conv), items in sorted(cur["items"].items()):
        s.append(["symbol", Q(f"{name}_{unit}_{conv}")] + items)
    return s


# ---------------------------------------------------------------------------
# footprints
# ---------------------------------------------------------------------------

LAYER = {21: "F.SilkS", 20: "B.SilkS", 24: "Dwgs.User", 25: "Cmts.User", 26: "Eco1.User", 27: "Eco2.User", 28: "Edge.Cuts"}


def convert_mod(text: str) -> list:
    """Legacy .mod library text -> [footprint trees] in the KiCad 7 format."""
    scale = 0.00254              # old files are in 1/10000 inch
    out, cur, pad = [], None, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Units mm"):
            scale = 1.0
            continue
        t = _split(line)
        if not t:
            continue
        k = t[0]
        v = lambda i: _f(float(t[i]) * scale)
        try:
            if k == "$MODULE":
                cur = {"name": " ".join(t[1:]), "items": [], "smd": False, "th": False}
            elif cur is None:
                continue
            elif k == "$EndMODULE":
                out.append(_emit_fp(cur))
                cur = None
            elif k == "$PAD":
                pad = {}
            elif k == "$EndPAD" and pad is not None:
                cur["items"].append(_emit_pad(pad, cur))
                pad = None
            elif pad is not None:
                if k == "Sh":    # Sh "name" shape sx sy dsx dsy orient
                    pad.update(name=t[1], shape=t[2], sx=v(3), sy=v(4), rot=float(t[7]) / 10 if len(t) > 7 else 0)
                elif k == "Dr":  # Dr diam ox oy [O dx dy]
                    pad.update(drill=v(1), oval=(v(5), v(6)) if len(t) > 6 and t[4] == "O" else None)
                elif k == "At":
                    pad.update(kind=t[1], mask=int(t[3], 16) if len(t) > 3 else 0x00888000)
                elif k == "Po":
                    pad.update(x=v(1), y=v(2))
            elif k in ("T0", "T1"):   # T0 x y sy sx rot width mirror visible layer italic "text"
                kind = "reference" if k == "T0" else "value"
                layer = LAYER.get(int(t[9]), "F.SilkS") if kind == "reference" else "F.Fab"
                cur["items"].append(["fp_text", kind, Q(t[-1]), ["at", v(1), v(2)], ["layer", Q(layer)],
                                     ["effects", ["font", ["size", v(3), v(4)], ["thickness", v(6)]]]])
            elif k == "DS":   # DS x1 y1 x2 y2 width layer
                cur["items"].append(["fp_line", ["start", v(1), v(2)], ["end", v(3), v(4)],
                                     ["stroke", ["width", v(5)], ["type", "solid"]], ["layer", Q(LAYER.get(int(t[6]), "F.Fab"))]])
            elif k == "DC":   # DC cx cy px py width layer
                cur["items"].append(["fp_circle", ["center", v(1), v(2)], ["end", v(3), v(4)],
                                     ["stroke", ["width", v(5)], ["type", "solid"]], ["fill", "none"],
                                     ["layer", Q(LAYER.get(int(t[6]), "F.Fab"))]])
            elif k == "DA":   # DA cx cy sx sy angle(0.1deg) width layer
                cx, cy, sx, sy = (float(t[i]) * scale for i in range(1, 5))
                ang = math.radians(float(t[5]) / 10)
                r, a0 = math.hypot(sx - cx, sy - cy), math.atan2(sy - cy, sx - cx)
                mid = (cx + r * math.cos(a0 + ang / 2), cy + r * math.sin(a0 + ang / 2))
                end = (cx + r * math.cos(a0 + ang), cy + r * math.sin(a0 + ang))
                cur["items"].append(["fp_arc", ["start", _f(sx), _f(sy)], ["mid", _f(mid[0]), _f(mid[1])], ["end", _f(end[0]), _f(end[1])],
                                     ["stroke", ["width", v(6)], ["type", "solid"]], ["layer", Q(LAYER.get(int(t[7]), "F.Fab"))]])
            elif k == "At" and pad is None:
                cur["smd"] = cur["smd"] or t[1] == "SMD"
        except (IndexError, ValueError):
            continue
    return out


def _emit_pad(p, cur):
    kind = p.get("kind", "STD")
    shape = {"R": "rect", "C": "circle", "O": "oval", "T": "rect"}.get(p.get("shape", "R"), "rect")
    back = (p.get("mask", 0) & 0x00008000) == 0 and (p.get("mask", 0) & 0x00000001) != 0
    side = "B" if back else "F"
    if kind in ("SMD", "CONN"):
        typ, layers = "smd", [f"{side}.Cu", f"{side}.Mask"] + ([f"{side}.Paste"] if kind == "SMD" else [])
        cur["smd"] = True
    elif kind == "HOLE":
        typ, layers = "np_thru_hole", ["*.Cu", "*.Mask"]
    else:
        typ, layers = "thru_hole", ["*.Cu", "*.Mask"]
        cur["th"] = True
    at = ["at", p.get("x", "0"), p.get("y", "0")] + ([_f(p["rot"])] if p.get("rot") else [])
    node = ["pad", Q(p.get("name", "")), typ, shape, at, ["size", p.get("sx", "1"), p.get("sy", "1")]]
    if typ != "smd":
        node.append(["drill", "oval", *p["oval"]] if p.get("oval") else ["drill", p.get("drill") or p.get("sx", "1")])
    node.append(["layers", *[Q(x) for x in layers]])
    return node


def _emit_fp(cur):
    fp = ["footprint", Q(cur["name"]), ["version", "20221018"], ["generator", "kipipe"], ["layer", Q("F.Cu")]]
    if cur["smd"] and not cur["th"]:
        fp.append(["attr", "smd"])
    elif cur["th"]:
        fp.append(["attr", "through_hole"])
    return fp + cur["items"]
