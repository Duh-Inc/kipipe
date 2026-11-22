"""Spec -> .kicad_sch.

Layout strategy: every symbol unit is placed on a grid; every connected pin
gets a short wire stub ending in a net label. Same-named labels connect, so the
schematic is electrically exact by construction and easy to read as a
connection list. Power nets that only feed power_in pins get a PWR_FLAG so ERC
passes. Pins listed in no_connect get an X.
"""
from __future__ import annotations

import math
import uuid as _uuid
from dataclasses import dataclass

from . import kicad_lib as kl
from . import sexpr
from .sexpr import Q, find, find_all, fmt
from .spec import _natkey

GRID = 2.54
STUB = 2.54
FONT = ["font", ["size", "1.27", "1.27"]]


_UID = {"seed": None, "n": 0}
_NS = _uuid.UUID("6b1f1c2e-5d4a-4c1b-9a7e-6b6970697065")


def uid_reset(seed: str | None):
    """Make the next UUIDs a fixed sequence for this design, so regenerating the same spec gives a byte-identical
    schematic (clean git diffs). None goes back to random."""
    _UID.update(seed=seed, n=0)


def new_uuid() -> str:
    if _UID["seed"] is None:
        return str(_uuid.uuid4())
    _UID["n"] += 1
    return str(_uuid.uuid5(_NS, f"{_UID['seed']}:{_UID['n']}"))


def U() -> list:
    return ["uuid", Q(new_uuid())]


def snap(v: float, g: float = GRID) -> float:
    return round(v / g) * g


@dataclass
class UnitBox:
    ref: str
    lib_id: str
    unit: int
    pins: list
    xmin: float
    xmax: float
    ymin: float     # schematic coords (y down)
    ymax: float
    lpad: float     # room for labels on the left/right
    rpad: float
    tpad: float
    bpad: float
    text_above: bool = False


def _graphics_bbox(sym: list, unit: int):
    xs, ys = [], []
    name = str(sym[1])
    for sub in find_all(sym, "symbol"):
        parts = str(sub[1])[len(name) + 1:].split("_")
        try:
            u, s = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue
        if u not in (0, unit) or s not in (0, 1):
            continue
        for node in sexpr.walk(sub):
            h = node[0] if node else None
            if h in ("start", "end", "xy", "mid", "center", "at") and len(node) >= 3:
                try:
                    xs.append(float(node[1]))
                    ys.append(-float(node[2]))
                except ValueError:
                    pass
    return xs, ys


def _label_len(net: str) -> float:
    return STUB + 1.27 * 0.8 * len(net) + 2.0


def _outward(angle: float):
    a = math.radians(angle + 180)
    dx, dy = round(math.cos(a)), round(math.sin(a))
    return dx, -dy   # to schematic coords


def build_units(spec: dict, pin_net: dict[str, str]):
    boxes: list[UnitBox] = []
    for c in sorted(spec["components"], key=lambda c: (_order(c["ref"]), _natkey(c["ref"]))):
        sym = kl.get_symbol(c["symbol"])
        allpins = [p for p in kl.symbol_pins(sym) if p.style in (0, 1)]
        for unit in kl.symbol_units(sym):
            pins = [p for p in allpins if p.unit in (0, unit)]
            gx, gy = _graphics_bbox(sym, unit)
            xs = gx + [p.x for p in pins]
            ys = gy + [-p.y for p in pins]
            pads = {"l": 2.54, "r": 2.54, "t": 2.54, "b": 2.54}
            for p in pins:
                net = pin_net.get(f"{c['ref']}.{p.number}")
                ext = _label_len(net) if net else 3.0
                dx, dy = _outward(p.angle)
                sy = -p.y
                if dx < 0:
                    pads["l"] = max(pads["l"], ext - (p.x - min(xs)))
                elif dx > 0:
                    pads["r"] = max(pads["r"], ext - (max(xs) - p.x))
                elif dy < 0:
                    pads["t"] = max(pads["t"], ext - (sy - min(ys)))
                else:
                    pads["b"] = max(pads["b"], ext - (max(ys) - sy))
            right_labels = any(_outward(p.angle)[0] > 0 and f"{c['ref']}.{p.number}" in pin_net for p in pins)
            boxes.append(UnitBox(c["ref"], c["symbol"], unit, pins, min(xs), max(xs), min(ys), max(ys),
                                 pads["l"], pads["r"], pads["t"] + (7.62 if right_labels else 3.0),
                                 pads["b"] + 3.0, right_labels))
    return boxes


def _order(ref: str) -> int:
    prefix = "".join(ch for ch in ref if ch.isalpha())
    return {"J": 0, "U": 1, "Q": 2, "D": 4, "R": 5, "C": 6, "L": 7}.get(prefix, 3)


PAPERS = [("A4", 297, 210), ("A3", 420, 297), ("A2", 594, 420), ("A1", 841, 594), ("A0", 1189, 841)]


def layout(boxes: list[UnitBox]):
    """Shelf-pack unit boxes; returns positions and paper size."""
    total = sum((b.xmax - b.xmin + b.lpad + b.rpad) * (b.ymax - b.ymin + b.tpad + b.bpad) for b in boxes)
    for paper, W, H in PAPERS:
        if total * 1.8 < (W - 40) * (H - 50):
            break
    margin_x, margin_y = 25.4, 25.4
    usable = W - 2 * margin_x
    pos = {}
    x, y, row_h = margin_x, margin_y, 0.0
    for b in boxes:
        w = b.xmax - b.xmin + b.lpad + b.rpad + 5.08
        h = b.ymax - b.ymin + b.tpad + b.bpad + 2.54
        if x + w > margin_x + usable and x > margin_x:
            x, y, row_h = margin_x, y + row_h, 0.0
        ox = snap(x + b.lpad - b.xmin)
        oy = snap(y + b.tpad - b.ymin)
        pos[(b.ref, b.unit)] = (ox, oy)
        x += w
        row_h = max(row_h, h)
    need_h = y + row_h + margin_y
    while need_h > H and paper != "A0":
        idx = [p[0] for p in PAPERS].index(paper) + 1
        paper, W, H = PAPERS[idx]
    return pos, paper


def _hide(env, effects: list) -> list:
    if env.new_hide_syntax:
        return effects + [["hide", "yes"]]
    return effects + ["hide"]


def _prop(env, key, val, x, y, hidden=False, angle=0, justify=None):
    eff = ["effects", FONT] + ([["justify", justify]] if justify else [])
    if hidden:
        eff = _hide(env, eff)
    return ["property", Q(key), Q(val), ["at", fmt(x), fmt(y), fmt(angle)], eff]


def generate(spec: dict, project: str) -> tuple[str, dict]:
    """Return (.kicad_sch text, info dict)."""
    env = kl.detect()
    pin_net = {p: net for net, pins in spec["nets"].items() for p in pins}
    nc = set(spec.get("no_connect", []))
    comps = {c["ref"]: c for c in spec["components"]}
    uid_reset(project)
    root = new_uuid()

    # PWR_FLAG on nets with power_in pins but no power_out source
    flag_nets = []
    for net, pins in spec["nets"].items():
        types = []
        for p in pins:
            ref, _, num = p.partition(".")
            sym = kl.get_symbol(comps[ref]["symbol"])
            for sp in kl.symbol_pins(sym):
                if sp.number == num:
                    types.append(sp.etype)
                    break
        if "power_in" in types and "power_out" not in types:
            flag_nets.append(net)

    flag_comp = []
    for i, net in enumerate(flag_nets, 1):
        ref = f"#FLG{i:02d}"
        flag_comp.append({"ref": ref, "symbol": "power:PWR_FLAG", "value": "PWR_FLAG", "footprint": ""})
        pin_net[f"{ref}.1"] = net
    all_comps = spec["components"] + flag_comp
    spec_for_layout = dict(spec, components=all_comps)

    boxes = build_units(spec_for_layout, pin_net)
    pos, paper = layout(boxes)

    body: list = []
    lib_syms = {}
    for c in all_comps:
        if c["symbol"] not in lib_syms:
            s = kl.get_symbol(c["symbol"])
            s[1] = Q(c["symbol"])
            lib_syms[c["symbol"]] = s

    labels = 0
    for b in boxes:
        c = next(cc for cc in all_comps if cc["ref"] == b.ref)
        ox, oy = pos[(b.ref, b.unit)]
        is_flag = b.ref.startswith("#")
        node = ["symbol", ["lib_id", Q(c["symbol"])], ["at", fmt(ox), fmt(oy), "0"], ["unit", str(b.unit)]]
        if env.major >= 8:
            node.append(["exclude_from_sim", "no"])
        node += [["in_bom", "no" if is_flag else "yes"], ["on_board", "no" if is_flag else "yes"], ["dnp", "no"], U()]
        if b.text_above:
            ty = oy + b.ymin - (b.tpad - 7.62) - 1.27
            tx = ox + (b.xmin + b.xmax) / 2
            node.append(_prop(env, "Reference", b.ref, tx, ty - 2.54, hidden=is_flag))
            node.append(_prop(env, "Value", c["value"], tx, ty, hidden=False))
        else:
            node.append(_prop(env, "Reference", b.ref, ox + b.xmax + 1.27, oy + b.ymin - 1.27, hidden=is_flag, justify="left"))
            node.append(_prop(env, "Value", c["value"], ox + b.xmax + 1.27, oy + b.ymin + 1.27, hidden=False, justify="left"))
        node.append(_prop(env, "Footprint", c.get("footprint", ""), ox, oy, hidden=True))
        node.append(_prop(env, "Datasheet", "~", ox, oy, hidden=True))
        for p in b.pins:
            if p.raw and p.number != kl.expand_pin_number(p.raw)[0]:
                continue                    # a stacked pin is one pin in the drawing
            node.append(["pin", Q(p.raw or p.number), U()])
        node.append(["instances", ["project", Q(project),
                                   ["path", Q("/" + root), ["reference", Q(b.ref)], ["unit", str(b.unit)]]]])
        body.append(node)

        for p in b.pins:
            key = f"{b.ref}.{p.number}"
            px, py = ox + p.x, oy - p.y
            if key in nc:
                body.append(["no_connect", ["at", fmt(px), fmt(py)], U()])
                continue
            net = pin_net.get(key)
            if not net or p.hidden:
                continue
            dx, dy = _outward(p.angle)
            ex, ey = px + dx * STUB, py + dy * STUB
            body.append(["wire", ["pts", ["xy", fmt(px), fmt(py)], ["xy", fmt(ex), fmt(ey)]],
                         ["stroke", ["width", "0"], ["type", "default"]], U()])
            ang = {(-1, 0): 180, (1, 0): 0, (0, -1): 90, (0, 1): 270}[(dx, dy)]
            just = "right" if ang in (180, 270) else "left"
            body.append(["label", Q(net), ["at", fmt(ex), fmt(ey), str(ang)],
                         ["effects", FONT, ["justify", just, "bottom"]], U()])
            labels += 1

    tree = ["kicad_sch", ["version", env.sch_version], ["generator", Q("kipipe")]]
    if env.major >= 8:
        tree.append(["generator_version", Q(f"{env.major}.0")])
    tree += [["uuid", Q(root)], ["paper", Q(paper)],
             ["title_block", ["title", Q(spec["name"])], ["comment", "1", Q(spec.get("description", "")[:120])],
              ["comment", "2", Q("Generated by kipipe")]],
             ["lib_symbols"] + list(lib_syms.values())]
    tree += body
    tree.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
    info = {"paper": paper, "units": len(boxes), "labels": labels, "pwr_flags": flag_nets}
    return sexpr.dumps(tree) + "\n", info


# ---------------------------------------------------------------------------
# Netlist cross-check
# ---------------------------------------------------------------------------

def parse_netlist(text: str) -> tuple[dict, dict]:
    """KiCad sexpr netlist -> (components {ref: {value, footprint}}, nets {name: set('REF.PIN')})."""
    tree = sexpr.parse(text)
    comps = {}
    for comp in find_all(find(tree, "components") or [], "comp"):
        ref = str(find(comp, "ref")[1])
        val = find(comp, "value")
        fp = find(comp, "footprint")
        comps[ref] = {"value": str(val[1]) if val else "", "footprint": str(fp[1]) if fp else ""}
    nets = {}
    for net in find_all(find(tree, "nets") or [], "net"):
        name = str(find(net, "name")[1])
        nodes = {f"{find(n, 'ref')[1]}.{find(n, 'pin')[1]}" for n in find_all(net, "node")}
        nets[name] = nodes
    return comps, nets


def crosscheck(spec: dict, netlist_text: str) -> list[str]:
    """Compare the netlist KiCad extracted from the schematic with the spec."""
    comps, nets = parse_netlist(netlist_text)
    problems = []
    for c in spec["components"]:
        if c["ref"] not in comps:
            problems.append(f"component {c['ref']} missing from schematic netlist")
        elif comps[c["ref"]]["footprint"] != c["footprint"]:
            problems.append(f"{c['ref']}: footprint {comps[c['ref']]['footprint']!r} != spec {c['footprint']!r}")
    by_name = {n.lstrip("/"): pins for n, pins in nets.items()}
    comps_by_ref = {c["ref"]: c for c in spec["components"]}
    pin_cache = {}

    def pins_of(ref):
        if ref not in pin_cache:
            try:
                pin_cache[ref] = {p.number: p for p in kl.symbol_pins(kl.get_symbol(comps_by_ref[ref]["symbol"]))}
            except Exception:
                pin_cache[ref] = {}
        return pin_cache[ref]

    def implicit(extra, listed):
        """KiCad joins hidden pins and same-named/stacked pins (e.g. a module's extra GND pads) by itself."""
        ref, _, num = extra.partition(".")
        sp = pins_of(ref).get(num)
        if sp is None:
            return False
        if sp.hidden:
            return True
        names = {pins_of(r).get(n).name for r, _, n in (x.partition(".") for x in listed)
                 if r == ref and pins_of(r).get(n) is not None}
        return sp.name in names
    for net, pins in spec["nets"].items():
        got = {p for p in by_name.get(net, set()) if not p.startswith("#")}
        want = set(pins)
        if got != want:
            if want - got:
                problems.append(f"net {net}: missing {sorted(want - got, key=_natkey)}")
            extra = [x for x in got - want if not implicit(x, want)]
            if extra:
                problems.append(f"net {net}: extra {sorted(extra, key=_natkey)}")
    # any spec pin landing in an unexpected net
    spec_pins = {p for pins in spec["nets"].values() for p in pins}
    for name, pins in by_name.items():
        if name not in spec["nets"] and not name.startswith("unconnected"):
            stray = (pins & spec_pins)
            if stray:
                problems.append(f"unexpected net {name} contains {sorted(stray, key=_natkey)}")
    return problems


def topology_ok(spec: dict, netlist_text: str) -> tuple[bool, list[str]]:
    """Same connectivity as the spec, whatever KiCad named the nets. Returns (ok, renames) where renames lists
    spec nets that KiCad gave another name. Used so a naming quirk of one KiCad version doesn't throw away a
    correct wired schematic."""
    comps, nets = parse_netlist(netlist_text)
    if any(c["ref"] not in comps for c in spec["components"]):
        return False, []
    where = {}
    for n, pins in nets.items():
        for p in pins:
            if not p.startswith("#"):
                where[p] = n
    renames, used = [], {}
    for net, pins in spec["nets"].items():
        homes = {where.get(p) for p in pins}
        if None in homes or len(homes) != 1:
            return False, []
        home = homes.pop()
        if home in used:                       # two spec nets merged into one
            return False, []
        used[home] = net
        got = {p for p in nets[home] if not p.startswith("#")}
        spec_pins_here = set(pins)
        # anything else in that net must not belong to another spec net (hidden/stacked pins are fine)
        others = {p for q in spec["nets"].values() for p in q} - spec_pins_here
        if got & others:
            return False, []
        if home.lstrip("/") != net:
            renames.append(f"{net} -> {home.lstrip('/')}")
    return True, renames

