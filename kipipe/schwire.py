"""Spec -> readable .kicad_sch with real wires.

Layout:
  * big parts (ICs, connectors, modules) first, then each small part next to the
    pins it connects to, rotated so its pins face them; ground pins prefer to
    face down and supply pins up (decoupling caps stand upright)
  * matrices (spec floorplan grids, or auto-detected R x C 2-pin arrays) are
    laid out as a real grid
Wiring:
  * signal nets are routed as orthogonal wires on a 1.27 mm grid (A* with bend
    and crossing costs); a wire may cross another net only going straight
    through, never bending, ending or joining on it, so a crossing can't be
    read as a connection; T-joins get a junction dot
  * power nets get standard power symbols (GND, +5V, ...) at the pins; adjacent
    pins on the same power net share one symbol
  * every signal net gets one name label on its wire (net classes match by name)
If a connection can't be routed, that pin gets a name label instead. The caller
still cross-checks KiCad's netlist against the spec and falls back to the
label-only generator (schgen) if anything is off.
"""
from __future__ import annotations

import heapq
import math
import re
import uuid as _uuid
from dataclasses import dataclass, field

from . import kicad_lib as kl
from . import schgen, sexpr
from .schgen import FONT, PAPERS, U, _graphics_bbox, _hide, _order, _outward
from .sexpr import Q, fmt
from .spec import _natkey

G = 1.27            # routing grid
P = 2.54            # placement grid
DX = (1, 0, -1, 0)  # 0 right, 1 down, 2 left, 3 up (schematic coords, y down)
DY = (0, 1, 0, -1)
BEND, CROSS, NEAR, SOFT_TEXT = 4.0, 3.0, 0.6, 25.0

POWER_RE = re.compile(r"^(GND|AGND|DGND|GNDA|GNDD|GNDPWR|VSS|VCC|VDD|VEE|VBUS|VBAT|VIN|VSYS|\+BATT|"
                      r"[+-]?\d+V\d*|[+-]?\d+\.\d+V)$", re.I)
GND_RE = re.compile(r"^(GND|AGND|DGND|GNDA|GNDD|GNDPWR|VSS|0V)$", re.I)


class Unroutable(Exception):
    pass


def rotv(x: float, y: float, r: int):
    """Rotate a schematic-coordinate vector the way KiCad rotates a symbol by r."""
    for _ in range((int(r) // 90) % 4):
        x, y = y, -x
    return x, y


def dir_of(dx, dy) -> int:
    return {(1, 0): 0, (0, 1): 1, (-1, 0): 2, (0, -1): 3}[(int(dx), int(dy))]


def text_w(s: str, size=1.27) -> float:
    return len(s) * size * 0.86 + 0.6


# ---------------------------------------------------------------------------
# Units and placement items
# ---------------------------------------------------------------------------

@dataclass
class Unit:
    ref: str
    lib_id: str
    unit: int
    comp: dict
    pins: list               # kl.Pin
    gbox: tuple              # graphics box, rot 0, schematic coords
    rot: int = 0
    x: float = 0.0
    y: float = 0.0
    item: "Item" = None

    def end(self, p, rot=None, x=None, y=None):
        r = self.rot if rot is None else rot
        rx, ry = rotv(p.x, -p.y, r)
        return (self.x if x is None else x) + rx, (self.y if y is None else y) + ry

    def out(self, p, rot=None) -> int:
        dx, dy = _outward(p.angle)
        return dir_of(*rotv(dx, dy, self.rot if rot is None else rot))

    def box(self, rot=None):
        """Body + pin ends, relative to origin, for rotation rot."""
        r = self.rot if rot is None else rot
        pts = [rotv(*c, r) for c in ((self.gbox[0], self.gbox[1]), (self.gbox[2], self.gbox[3]),
                                     (self.gbox[0], self.gbox[3]), (self.gbox[2], self.gbox[1]))]
        pts += [rotv(p.x, -p.y, r) for p in self.pins if not p.hidden]
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def gfx(self, rot=None):
        r = self.rot if rot is None else rot
        pts = [rotv(*c, r) for c in ((self.gbox[0], self.gbox[1]), (self.gbox[2], self.gbox[3]))]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def text_layout(self, rot=None):
        """Reference/Value positions (relative, centred) and their rect."""
        r = self.rot if rot is None else rot
        val = self.comp.get("value", "")
        w = max(text_w(self.ref), text_w(val))
        sides = {self.out(p, r) for p in self.pins if not p.hidden}
        bx = self.box(r)
        gb = self.gfx(r)
        cx, cy = (gb[0] + gb[2]) / 2, (gb[1] + gb[3]) / 2
        if 0 not in sides:                        # right of the body
            x = max(gb[2], max([bx[2]] if 0 in sides else [gb[2]])) + 1.0 + w / 2
            pos = [(x, cy - 1.27), (x, cy + 1.27)]
        elif 2 not in sides:
            x = gb[0] - 1.0 - w / 2
            pos = [(x, cy - 1.27), (x, cy + 1.27)]
        elif 3 not in sides:
            pos = [(cx, bx[1] - 3.81), (cx, bx[1] - 1.6)]
        else:
            # pins on every side: stack above a top corner of the body, away from the top pins
            tops = [rotv(p.x, -p.y, r)[0] for p in self.pins if not p.hidden and self.out(p, r) == 3]
            lx = gb[0] + w / 2 + 0.3
            rx = gb[2] - w / 2 - 0.3
            clear_l = all(abs(t - lx) > w / 2 + 1.0 for t in tops)
            clear_r = all(abs(t - rx) > w / 2 + 1.0 for t in tops)
            if clear_l or clear_r:
                x = lx if clear_l else rx
                pos = [(x, gb[1] - 3.6), (x, gb[1] - 1.4)]
            else:          # top is crowded too: put it left of the left pins
                x = bx[0] - 1.5 - w / 2
                pos = [(x, bx[1] - 3.6), (x, bx[1] - 1.4)]
        rect = (min(p[0] for p in pos) - w / 2, min(p[1] for p in pos) - 0.9,
                max(p[0] for p in pos) + w / 2, max(p[1] for p in pos) + 0.9)
        return pos, rect


@dataclass
class Item:
    members: list                         # [(Unit, dx, dy)]
    rots: list = field(default_factory=lambda: [0])
    placed: bool = False
    x: float = 0.0
    y: float = 0.0
    rot: int = 0


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

class WiredSchematic:
    def __init__(self, spec: dict, project: str):
        self.spec, self.project = spec, project
        self.env = kl.detect()
        self.comps = {c["ref"]: c for c in spec["components"]}
        self.pin_net = {p: net for net, pins in spec["nets"].items() for p in pins}
        self.nc = set(spec.get("no_connect", []))
        self.sym_cache = {}
        self.power_sym = {}       # net -> lib id
        self.notes = []

    # -- library helpers ---------------------------------------------------
    def sym(self, lib_id):
        if lib_id not in self.sym_cache:
            self.sym_cache[lib_id] = kl.get_symbol(lib_id)
        return self.sym_cache[lib_id]

    def power_symbol_for(self, net: str):
        """Library power symbol for this net. KiCad 7 names the net after the symbol's pin, so the pin name must
        match; KiCad 8+ names it after the Value field (which we set to the net name), so any single-pin power
        symbol of that name works."""
        if net in self.power_sym:
            return self.power_sym[net]
        found = None
        if POWER_RE.match(net) or GND_RE.match(net):
            try:
                s = self.sym(f"power:{net}")
                pins = kl.symbol_pins(s)
                if len(pins) == 1 and (pins[0].name == net or self.env.major >= 8):
                    found = f"power:{net}"
            except Exception:
                found = None
        self.power_sym[net] = found
        return found

    def is_power(self, net):
        return net is not None and self.power_symbol_for(net) is not None

    # -- build units -------------------------------------------------------
    def build_units(self):
        self.units: list[Unit] = []
        for c in sorted(self.spec["components"], key=lambda c: (_order(c["ref"]), _natkey(c["ref"]))):
            s = self.sym(c["symbol"])
            allpins = [p for p in kl.symbol_pins(s) if p.style in (0, 1)]
            for unit in kl.symbol_units(s):
                pins = [p for p in allpins if p.unit in (0, unit)]
                gx, gy = _graphics_bbox(s, unit)
                if not gx:
                    gx, gy = [p.x for p in pins] or [0], [-p.y for p in pins] or [0]
                # graphics box without pin ends (pins are added by Unit.box)
                body_pts = [(x, y) for x, y in zip(gx, gy)]
                gb = (min(q[0] for q in body_pts), min(q[1] for q in body_pts),
                      max(q[0] for q in body_pts), max(q[1] for q in body_pts))
                self.units.append(Unit(c["ref"], c["symbol"], unit, c, pins, gb))
        self.unit_pins = {}       # "REF.PIN" -> (unit, pin)
        for u in self.units:
            for p in u.pins:
                key = f"{u.ref}.{p.number}"
                if key not in self.unit_pins or not p.hidden:
                    self.unit_pins[key] = (u, p)

    def net_of(self, u, p):
        return self.pin_net.get(f"{u.ref}.{p.number}")

    # -- items: singles + matrices ------------------------------------------
    def build_items(self):
        grids = list((self.spec.get("floorplan") or {}).get("grids") or [])
        if not grids:
            grids = self.find_matrices()
        by_ref = {}
        for u in self.units:
            by_ref.setdefault(u.ref, []).append(u)
        used = set()
        self.items: list[Item] = []
        self.matrices: list[Item] = []
        for g in grids:
            refs = [r for r in g.get("refs", []) if r in by_ref and len(by_ref[r]) == 1 and r not in used]
            if len(refs) < 4:
                continue
            cols = max(1, int(g.get("cols") or round(math.sqrt(len(refs)))))
            u0 = by_ref[refs[0]][0]
            # stand members up so their pins face up/down
            rot = 0
            for r in (0, 90):
                if all(u0.out(p, r) in (1, 3) for p in u0.pins if not p.hidden):
                    rot = r
                    break
            # a true matrix: one pin's net is shared along each row, the other's along each column.
            # Then the row pin faces up (row lines above), the column pin down (column lines beside).
            mx = None
            vis = [p for p in u0.pins if not p.hidden]
            if len(vis) == 2 and len(refs) % cols == 0 and len(refs) // cols >= 2 and cols >= 2:
                nrows = len(refs) // cols
                grid = [[by_ref[refs[j * cols + i]][0] for i in range(cols)] for j in range(nrows)]
                def const(num, along_row):
                    lines = grid if along_row else [list(c) for c in zip(*grid)]
                    nets = [{self.pin_net.get(f"{u.ref}.{num}") for u in line} for line in lines]
                    return all(len(n) == 1 and None not in n for n in nets) and len({next(iter(n)) for n in nets}) == len(lines)
                a, b = vis[0].number, vis[1].number
                rowpin = a if const(a, True) and const(b, False) else b if const(b, True) and const(a, False) else None
                if rowpin:
                    colpin = b if rowpin == a else a
                    for r in (0, 90, 180, 270):
                        outs = {p.number: u0.out(p, r) for p in vis}
                        if outs[rowpin] == 3 and outs[colpin] == 1:
                            rot = r
                            mx = {"grid": grid, "rowpin": rowpin, "colpin": colpin}
                            break
            bx = u0.box(rot)
            pitch_x = math.ceil((bx[2] - bx[0] + 8.0) / P) * P
            pitch_y = math.ceil((bx[3] - bx[1] + 8.0) / P) * P
            members = []
            for i, r in enumerate(refs):
                u = by_ref[r][0]
                u.rot = rot
                members.append((u, (i % cols) * pitch_x, (i // cols) * pitch_y))
                used.add(r)
            it = Item(members, [0])
            if mx:
                it.matrix = mx
                self.matrices.append(it)
            for u, _, _ in members:
                u.item = it
            self.items.append(it)
        self.bus = self.find_buses()
        for u in self.units:
            if u.item is None:
                small = len([p for p in u.pins if not p.hidden]) <= 3
                it = Item([(u, 0, 0)], [0, 90, 180, 270] if small else [0])
                u.item = it
                self.items.append(it)

    def find_buses(self, matrix_lines=8, bundle=6):
        """Nets drawn as name tags instead of wires: lines of big matrices, and bundles of two-ended
        nets that all run between the same two parts. Small nets always stay wired."""
        bus = set()
        for it in self.matrices:
            g = it.matrix["grid"]
            if len(g) + len(g[0]) >= matrix_lines:
                for u in (row[0] for row in g):
                    bus.add(self.pin_net[f"{u.ref}.{it.matrix['rowpin']}"])
                for u in g[0]:
                    bus.add(self.pin_net[f"{u.ref}.{it.matrix['colpin']}"])
        pairs = {}
        for net, pins in self.spec["nets"].items():
            if self.is_power(net) or net in bus:
                continue
            refs = sorted({p.partition(".")[0] for p in pins})
            if len(pins) == 2 and len(refs) == 2:
                pairs.setdefault(tuple(refs), []).append(net)
        for nets in pairs.values():
            if len(nets) >= bundle:
                bus.update(nets)
        return bus

    def find_matrices(self, min_n=9):
        groups = {}
        for c in self.spec["components"]:
            pins = sorted([p for p in self.pin_net if p.partition(".")[0] == c["ref"]], key=_natkey)
            if len(pins) != 2:
                continue
            groups.setdefault((c["symbol"], c.get("footprint")), []).append(
                (c["ref"], self.pin_net[pins[0]], self.pin_net[pins[1]]))
        found = []
        for items in groups.values():
            if len(items) < min_n:
                continue
            a = sorted({x[1] for x in items}, key=_natkey)
            b = sorted({x[2] for x in items}, key=_natkey)
            pairs = {(x[1], x[2]) for x in items}
            if len(a) >= 2 and len(b) >= 2 and len(a) * len(b) == len(items) == len(pairs):
                by = {(x[1], x[2]): x[0] for x in items}
                found.append({"refs": [by[(ra, cb)] for ra in a for cb in b], "cols": len(b)})
        return found

    # -- placement ----------------------------------------------------------
    def footprint(self, it: Item, rot: int, x: float, y: float):
        """Exclusive rectangles (abs coords) for item at (x, y) with rot (singles only rotate)."""
        rects = []
        for u, dx, dy in it.members:
            r = rot if len(it.members) == 1 else u.rot
            bx = u.box(r)
            _, tr = u.text_layout(r)
            m = {0: 1.27, 1: 1.27, 2: 1.27, 3: 1.27}
            for p in u.pins:
                if p.hidden:
                    continue
                d = u.out(p, r)
                net = self.net_of(u, p)
                need = 7.62 if self.is_power(net) else (3.81 + text_w(net) if net in self.bus else 3.81 if net else 1.27)
                m[d] = max(m[d], need)
            ox, oy = x + dx, y + dy
            rects.append((ox + min(bx[0], tr[0]) - m[2], oy + min(bx[1], tr[1]) - m[3],
                          ox + max(bx[2], tr[2]) + m[0], oy + max(bx[3], tr[3]) + m[1]))
        if getattr(it, "matrix", None):
            # row lead-outs stick out on the left, column lead-outs at the bottom
            x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
            x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
            rects.append((x0 - 12.7, y0, x0, y1))
            rects.append((x0, y1, x1, y1 + 10.16))
        return rects

    @staticmethod
    def _cells(rects):
        out = set()
        for r in rects:
            for i in range(math.floor(r[0] / P), math.ceil(r[2] / P)):
                for j in range(math.floor(r[1] / P), math.ceil(r[3] / P)):
                    out.add((i, j))
        return out

    def is_decoupler(self, it):
        if len(it.members) != 1:
            return False
        u = it.members[0][0]
        nets = [self.net_of(u, p) for p in u.pins if not p.hidden]
        return len(nets) == 2 and all(self.is_power(n) for n in nets) and u.ref.startswith("C")

    def place(self):
        placed_pins = {}          # net -> [(x, y, unit_pins_count)]
        occ = set()

        def item_pins(it, rot, x, y):
            for u, dx, dy in it.members:
                r = rot if len(it.members) == 1 else u.rot
                for p in u.pins:
                    if p.hidden:
                        continue
                    net = self.net_of(u, p)
                    ex, ey = u.end(p, r, x + dx, y + dy)
                    d = u.out(p, r)
                    yield u, p, net, ex + DX[d] * P, ey + DY[d] * P, d

        def npins(u):
            return len([p for p in u.pins if not p.hidden])

        def conn(it):
            s = 0.0
            dec = self.is_decoupler(it)
            for u, p, net, *_ in item_pins(it, 0, 0, 0):
                if net in placed_pins and net not in self.bus:
                    s += (0.2 if self.is_power(net) and not dec else 1.0)
            return s

        def cost(it, rot, x, y):
            c = 0.0
            dec = self.is_decoupler(it)
            for u, p, net, ax, ay, d in item_pins(it, rot, x, y):
                if net and net in placed_pins and net not in self.bus:
                    pw = self.is_power(net)
                    if pw and not dec:
                        w = 0.0
                    elif pw:
                        w = 0.7
                    else:
                        w = 1.0
                    if w:
                        cand = placed_pins[net]
                        if pw:   # decoupler: nearest IC supply pin
                            cand = [q for q in cand if q[2] >= 6] or cand
                        q = min(cand, key=lambda q: abs(ax - q[0]) + abs(ay - q[1]))
                        c += w * (abs(ax - q[0]) + abs(ay - q[1]))
                        if not pw and abs(ax - q[0]) > 0.1 and abs(ay - q[1]) > 0.1:
                            c += 6.0       # needs a bend: prefer lining up with the pin
                if net and len(it.members) == 1 and npins(u) <= 3:
                    if GND_RE.match(net) and d == 3:
                        c += 12
                    elif self.is_power(net) and not GND_RE.match(net) and d == 1:
                        c += 12
                    elif self.is_power(net) and d in (0, 2):
                        c += 8
            return c

        def put(it, rot, x, y):
            it.placed, it.rot, it.x, it.y = True, rot, x, y
            occ.update(self._cells(self.footprint(it, rot, x, y)))
            for u, dx, dy in it.members:
                if len(it.members) == 1:
                    u.rot = rot
                u.x, u.y = x + dx, y + dy
            for u, p, net, ax, ay, d in item_pins(it, rot, x, y):
                if net:
                    placed_pins.setdefault(net, []).append((ax, ay, npins(u)))

        items = sorted(self.items, key=lambda it: -sum(npins(u) for u, _, _ in it.members))
        first = items[0]
        put(first, first.rots[0], 0.0, 0.0)
        while True:
            todo = [it for it in items if not it.placed]
            if not todo:
                break
            it = max(todo, key=lambda t: (conn(t) + (2.0 if sum(npins(u) for u, _, _ in t.members) >= 8 else 0),
                                          sum(npins(u) for u, _, _ in t.members)))
            # target: centroid of placed pins it connects to (or the middle of what is placed)
            pts = [q for u, p, net, *_ in item_pins(it, 0, 0, 0) if net in placed_pins and net not in self.bus and
                   (not self.is_power(net) or self.is_decoupler(it)) for q in placed_pins[net]]
            if not pts:
                pts = [q for qs in placed_pins.values() for q in qs] or [(0, 0, 0)]
            tx = sum(q[0] for q in pts) / len(pts)
            ty = sum(q[1] for q in pts) / len(pts)
            best = None
            for rot in it.rots:
                rel = list(item_pins(it, rot, 0, 0))
                cx = sum(q[3] for q in rel) / max(1, len(rel))
                cy = sum(q[4] for q in rel) / max(1, len(rel))
                ci, cj = round((tx - cx) / P), round((ty - cy) / P)
                found_ring = None
                for ring in range(0, 400):
                    if found_ring is not None and ring > found_ring + 4:
                        break
                    for (i, j) in self._ring(ci, cj, ring):
                        x, y = i * P, j * P
                        if self._cells(self.footprint(it, rot, x, y)) & occ:
                            continue
                        if found_ring is None:
                            found_ring = ring
                        c = cost(it, rot, x, y) + 0.15 * (abs(x - tx) + abs(y - ty)) * (0 if pts else 1) \
                            + 0.02 * (abs(x) + abs(y))
                        if best is None or c < best[0]:
                            best = (c, rot, x, y)
            if best is None:
                raise Unroutable(f"could not place {it.members[0][0].ref}")
            put(it, best[1], best[2], best[3])

        # normalise onto the sheet
        rects = [r for it in self.items for r in self.footprint(it, it.rot, it.x, it.y)]
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
        margin = 25.4
        extra_flags = 15.0                         # strip at the bottom for PWR_FLAGs
        w, h = x1 - x0 + 2 * margin, y1 - y0 + 2 * margin + extra_flags
        for name, W, H in PAPERS:
            if w <= W and h <= H:
                break
        else:
            name, W, H = "User", math.ceil(w / 10) * 10, math.ceil(h / 10) * 10
        self.paper = (name, W, H)
        sx = math.floor((margin + (W - w) / 2 - x0) / P) * P + P * 0
        sy = math.floor((margin + (H - h) / 2 - y0) / P) * P
        for u in self.units:
            u.x += sx
            u.y += sy
        self.flag_y = y1 + sy + 7.62
        self.flag_x0 = x0 + sx + 5.08

    @staticmethod
    def _ring(ci, cj, r):
        if r == 0:
            yield ci, cj
            return
        for k in range(-r, r + 1):
            yield ci + k, cj - r
            yield ci + k, cj + r
        for k in range(-r + 1, r):
            yield ci - r, cj + k
            yield ci + r, cj + k

    # -- routing grid ---------------------------------------------------------
    def C(self, x, y):
        i, j = x / G, y / G
        ri, rj = round(i), round(j)
        if abs(i - ri) > 0.02 or abs(j - rj) > 0.02:
            raise Unroutable(f"point ({x:.3f}, {y:.3f}) is off the 1.27 mm grid")
        return ri, rj

    def idx(self, i, j):
        return j * self.NX + i

    def inb(self, i, j):
        return 0 <= i < self.NX and 0 <= j < self.NY

    def grid_init(self):
        _, W, H = self.paper
        self.NX, self.NY = int(W / G) + 1, int(H / G) + 1
        n = self.NX * self.NY
        self.block = bytearray(n)
        self.soft = [0.0] * n
        self.node = [-1] * n          # -1 free, -2 dead (unconnected pin), else net id
        self.occH = [-1] * n
        self.occV = [-1] * n
        self.near = bytearray(n)
        self.reserve = [-1] * n       # first cells outside pins, reserved for that pin's net
        self.textmask = bytearray(n)
        self.pwire = bytearray(n)      # power stubs/bars: no crossings
        self.net_ids = {net: k for k, net in enumerate(self.spec["nets"])}
        self.adj = {}                 # net id -> {cell: set(dirs)}
        self.terms = {}               # net id -> [(cell, outdir, key)]
        self.pin_cells = {}           # cell -> net id (pin ends of the net)

    def mark_rect(self, rect, arr, val, skip=(), inside=False):
        """inside=True: cells whose point lies within rect; else every cell the rect touches."""
        if inside:
            i0, j0 = math.ceil(rect[0] / G - 1e-6), math.ceil(rect[1] / G - 1e-6)
            i1, j1 = math.floor(rect[2] / G + 1e-6), math.floor(rect[3] / G + 1e-6)
        else:
            i0, j0 = math.floor(rect[0] / G + 1e-6), math.floor(rect[1] / G + 1e-6)
            i1, j1 = math.ceil(rect[2] / G - 1e-6), math.ceil(rect[3] / G - 1e-6)
        for j in range(max(0, j0), min(self.NY, j1 + 1)):
            for i in range(max(0, i0), min(self.NX, i1 + 1)):
                c = self.idx(i, j)
                if c in skip:
                    continue
                if arr is self.soft:
                    arr[c] = max(arr[c], val)
                else:
                    arr[c] = val

    def setup_obstacles(self):
        for u in self.units:
            ends = set()
            for p in u.pins:
                if p.hidden:
                    continue
                ex, ey = u.end(p)
                i, j = self.C(ex, ey)
                c = self.idx(i, j)
                ends.add(c)
                key = f"{u.ref}.{p.number}"
                net = self.pin_net.get(key)
                d = u.out(p)
                if key in self.nc or not net:
                    self.node[c] = -2
                    continue
                nid = self.net_ids[net]
                self.node[c] = nid
                self.pin_cells[c] = nid
                self.terms.setdefault(nid, []).append((c, d, key, u))
                for k in (1, 2):
                    ii, jj = i + DX[d] * k, j + DY[d] * k
                    if self.inb(ii, jj):
                        self.reserve[self.idx(ii, jj)] = nid
            bx = u.box()
            # inner rect: shrink so pin-end cells stay on the boundary
            self.mark_rect((u.x + bx[0], u.y + bx[1], u.x + bx[2], u.y + bx[3]), self.block, 1, skip=ends, inside=True)
            # cells right next to bodies cost a little
            gb = u.gfx()
            self.mark_rect((u.x + gb[0] - G, u.y + gb[1] - G, u.x + gb[2] + G, u.y + gb[3] + G), self.soft, 0.5)
            _, tr = u.text_layout()
            self.mark_rect((u.x + tr[0], u.y + tr[1], u.x + tr[2], u.y + tr[3]), self.soft, SOFT_TEXT)
            self.mark_rect((u.x + tr[0], u.y + tr[1], u.x + tr[2], u.y + tr[3]), self.textmask, 1)
        for c in self.pin_cells:
            self.block[c] = 0

    # -- low-level commit ------------------------------------------------------
    def commit_path(self, nid, cells):
        a = self.adj.setdefault(nid, {})
        for k in range(len(cells) - 1):
            c0, c1 = cells[k], cells[k + 1]
            d = self._dir(c0, c1)
            a.setdefault(c0, set()).add(d)
            a.setdefault(c1, set()).add((d + 2) % 4)
        for c in cells:
            ds = a.get(c, set())
            if 0 in ds or 2 in ds:
                self.occH[c] = nid
            if 1 in ds or 3 in ds:
                self.occV[c] = nid
            straight = ds in ({0, 2}, {1, 3})
            if not straight:
                self.node[c] = nid
            i, j = c % self.NX, c // self.NX
            for d in range(4):
                ii, jj = i + DX[d], j + DY[d]
                if self.inb(ii, jj):
                    self.near[self.idx(ii, jj)] = 1
        # ends of a path and pin cells are nodes
        self.node[cells[0]] = nid
        self.node[cells[-1]] = nid

    def _dir(self, c0, c1):
        d = c1 - c0
        return {1: 0, self.NX: 1, -1: 2, -self.NX: 3}[d]

    def free_for(self, nid, c, axis_dir=None):
        """Cell usable as a node (end/bend/symbol pin) by nid."""
        if self.block[c] and c not in self.pin_cells:
            return False
        if self.node[c] not in (-1, nid):
            return False
        if self.occH[c] not in (-1, nid) or self.occV[c] not in (-1, nid):
            return False
        if self.reserve[c] not in (-1, nid):
            return False
        return True

    # -- power symbols ------------------------------------------------------------
    def power_geometry(self, lib_id, rot):
        """Graphic rect (relative) and graphic direction for a power symbol at rot."""
        s = self.sym(lib_id)
        gx, gy = _graphics_bbox(s, 1)
        pts = [rotv(x, y, rot) for x, y in zip(gx, gy)] or [(0, 0)]
        rect = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
        p0 = kl.symbol_pins(s)[0]
        a = math.radians(p0.angle)
        gdx, gdy = rotv(round(math.cos(a)), -round(math.sin(a)), rot)
        return rect, dir_of(gdx, gdy)

    def rot_for(self, lib_id, want_dir):
        for r in (0, 90, 180, 270):
            if self.power_geometry(lib_id, r)[1] == want_dir:
                return r
        return 0

    def place_power(self):
        self.power_syms = []          # (lib_id, net, x, y, rot, textpos)
        self.label_pins = []          # (cell, outdir, net) fallback labels
        groups = {}
        for nid, terms in self.terms.items():
            net = self.spec_net(nid)
            if not self.is_power(net):
                continue
            for (c, d, key, u) in terms:
                groups.setdefault((id(u), d, nid), []).append((c, d, key, u))
        for (uid, d, nid), terms in groups.items():
            net = self.spec_net(nid)
            u = terms[0][3]
            # other visible pins on that side split clusters
            side_cells = sorted([self.C(*u.end(p)) for p in u.pins if not p.hidden and u.out(p) == d],
                                key=lambda ij: (ij[1], ij[0]) if d in (0, 2) else (ij[0], ij[1]))
            mine = {c for (c, *_rest) in terms}
            clusters, cur = [], []
            for ij in side_cells:
                c = self.idx(*ij)
                if c in mine:
                    if cur:
                        prev = cur[-1]
                        pi, pj = prev % self.NX, prev // self.NX
                        if abs(pi - ij[0]) + abs(pj - ij[1]) > 4:
                            clusters.append(cur)
                            cur = []
                    cur.append(c)
                else:
                    if cur:
                        clusters.append(cur)
                    cur = []
            if cur:
                clusters.append(cur)
            for cl in clusters:
                # longer stubs before giving up: a text label on a power net clashes with its power symbols
                # (KiCad 8+ ERC: "Local and global labels have same name")
                if not any(self.power_cluster(nid, net, cl, d, k) for k in (2, 3, 4, 6)):
                    for c in cl:
                        self.label_pins.append((c, d, net))

    def spec_net(self, nid):
        if not hasattr(self, "_nid_net"):
            self._nid_net = {v: k for k, v in self.net_ids.items()}
        return self._nid_net[nid]

    def power_cluster(self, nid, net, cl, d, k=2):
        lib = self.power_symbol_for(net)
        gnd = self.power_geometry(lib, 0)[1] == 1     # graphic points down at rot 0
        natural = 1 if gnd else 3
        NX = self.NX

        def cell(c, dd, k=1):
            i, j = c % NX + DX[dd] * k, c // NX + DY[dd] * k
            return self.idx(i, j) if self.inb(i, j) else None

        stubs = [cell(c, d, k) for c in cl]
        if None in stubs:
            return False
        paths = [[c] + [cell(c, d, i) for i in range(1, k)] + [s] for c, s in zip(cl, stubs)]
        if any(x is None for p in paths for x in p):
            return False
        # bar joining stub ends
        order = sorted(range(len(cl)), key=lambda k: (stubs[k] // NX, stubs[k] % NX))
        bar = []
        if len(cl) > 1:
            a, b = stubs[order[0]], stubs[order[-1]]
            step = 1 if d in (1, 3) else NX
            if d in (0, 2):
                step = NX
            else:
                step = 1
            bar = list(range(a, b + step, step))
        options = []          # (symbol pin cell, use stubs+bar, extension, graphic direction)
        if d == natural:
            mid = stubs[order[len(order) // 2]]
            options.append((mid, True, [], natural))
        elif d in (0, 2):
            # side pins: extend from the end of the bar in the natural direction, symbol upright
            endc = stubs[order[-1]] if natural == 1 else stubs[order[0]]
            ext = [endc, cell(endc, natural, 1), cell(endc, natural, 2)]
            if None not in ext:
                options.append((ext[-1], True, ext, natural))
        options.append((stubs[order[0]], True, [], d))
        if len(cl) == 1:
            options.append((cl[0], False, [], d))        # last resort: symbol right on the pin
        for (pc, use, ext, gdir) in options:
            rot = self.rot_for(lib, gdir)
            rect, _ = self.power_geometry(lib, rot)
            wire_cells = ([c for p in paths for c in p[1:]] + bar if use else []) + ext[1:]
            ok = all(self.free_for(nid, c) for c in wire_cells)
            if not ok:
                continue
            px, py = (pc % NX) * G, (pc // NX) * G
            gr = (px + rect[0] + 0.1, py + rect[1] + 0.1, px + rect[2] - 0.1, py + rect[3] - 0.1)
            gcells = self._cover_cells(gr) - {pc}
            if any(self.block[c] or self.occH[c] != -1 or self.occV[c] != -1 or self.node[c] != -1
                   or self.reserve[c] not in (-1, nid) or self.textmask[c] for c in gcells):
                continue
            tw = text_w(net)
            off = 2.54 + 0.9 + (tw / 2 if gdir in (0, 2) else 0.6)
            tx, ty = px + DX[gdir] * off, py + DY[gdir] * off
            trc = self._rect_cells((tx - tw / 2, ty - 0.6, tx + tw / 2, ty + 0.6))
            if (pc, use, ext, gdir) != options[-1] and any(
                    self.textmask[c] or self.block[c] or (self.node[c] not in (-1,) ) for c in trc):
                continue
            # commit
            if use:
                for p in paths:
                    self.commit_path(nid, p)
                if len(bar) > 1:
                    self.commit_path(nid, bar)
            if ext:
                self.commit_path(nid, ext)
            for c in wire_cells:
                self.pwire[c] = 1
            for c in gcells:
                self.block[c] = 1
            self.node[pc] = nid
            self.mark_rect((tx - tw / 2, ty - 0.8, tx + tw / 2, ty + 0.8), self.soft, SOFT_TEXT)
            self.mark_rect((tx - tw / 2, ty - 0.8, tx + tw / 2, ty + 0.8), self.textmask, 1)
            self.power_syms.append((lib, net, px, py, rot, (tx, ty)))
            return True
        return False

    def _cover_cells(self, r):
        """Every grid point a rectangle touches or comes within half a cell of."""
        out = set()
        for j in range(max(0, math.floor(r[1] / G)), min(self.NY, math.ceil(r[3] / G) + 1)):
            for i in range(max(0, math.floor(r[0] / G)), min(self.NX, math.ceil(r[2] / G) + 1)):
                out.add(self.idx(i, j))
        return out

    def _rect_cells(self, r):
        out = set()
        for j in range(max(0, math.ceil(r[1] / G - 1e-6) - 0), min(self.NY, math.floor(r[3] / G + 1e-6) + 1)):
            for i in range(max(0, math.ceil(r[0] / G - 1e-6)), min(self.NX, math.floor(r[2] / G + 1e-6) + 1)):
                out.add(self.idx(i, j))
        # rects narrower than a cell still cover the cell they sit in
        if not out:
            out.add(self.idx(round((r[0] + r[2]) / 2 / G), round((r[1] + r[3]) / 2 / G)))
        return out

    # -- A* -----------------------------------------------------------------------
    def astar(self, nid, src, sdir, targets, pin_entry, pad):
        NX = self.NX
        ti = [t % NX for t in targets]
        tj = [t // NX for t in targets]
        tx0, tx1, ty0, ty1 = min(ti), max(ti), min(tj), max(tj)
        si, sj = src % NX, src // NX
        wx0, wx1 = max(0, min(tx0, si) - pad), min(NX - 1, max(tx1, si) + pad)
        wy0, wy1 = max(0, min(ty0, sj) - pad), min(self.NY - 1, max(ty1, sj) + pad)
        block, node, occH, occV, soft, near, reserve = (self.block, self.node, self.occH, self.occV,
                                                         self.soft, self.near, self.reserve)
        own = self.adj.get(nid, {})
        pwire = self.pwire

        def h(c):
            i, j = c % NX, c // NX
            return (max(tx0 - i, 0, i - tx1) + max(ty0 - j, 0, j - ty1))

        start = src + (1 if sdir == 0 else NX if sdir == 1 else -1 if sdir == 2 else -NX)
        best = {}
        came = {}
        heap = []

        def try_push(c, d, prev_state, g):
            i, j = c % NX, c // NX
            if not (wx0 <= i <= wx1 and wy0 <= j <= wy1):
                return
            if c in targets:
                if c in pin_entry and pin_entry[c] != d:
                    return
                if c in own and c not in pin_entry:
                    pass
                st = (c, d)
                g2 = g + 1
                if g2 < best.get(st, 1e18):
                    best[st] = g2
                    came[st] = prev_state
                    heapq.heappush(heap, (g2, g2, c, d, True))
                return
            if block[c]:
                return
            nd = node[c]
            if nd != -1 and nd != nid:
                return
            if c in own:            # own tree cell that isn't a target (crossed by another net)
                return
            rv = reserve[c]
            if rv != -1 and rv != nid:
                return
            horiz = d in (0, 2)
            oh, ov = occH[c], occV[c]
            cross = False
            if horiz:
                if oh != -1 and oh != nid:
                    return
                if ov != -1 and ov != nid:
                    cross = True
            else:
                if ov != -1 and ov != nid:
                    return
                if oh != -1 and oh != nid:
                    cross = True
            if cross and pwire[c]:
                return
            g2 = g + 1 + soft[c] + (NEAR if near[c] else 0) + (CROSS if cross else 0)
            st = (c, d)
            if g2 < best.get(st, 1e18):
                best[st] = g2
                came[st] = prev_state
                heapq.heappush(heap, (g2 + h(c), g2, c, d, False))

        # the source pin's first step must go outward
        si_ok = True
        if start < 0 or start >= len(block):
            si_ok = False
        if si_ok:
            try_push(start, sdir, None, 0.0)
        steps = 0
        while heap:
            f, g, c, d, done = heapq.heappop(heap)
            if g > best.get((c, d), 1e18) + 1e-9:
                continue
            if done:
                path = [c]
                st = came[(c, d)]
                while st is not None:
                    path.append(st[0])
                    st = came[st]
                path.append(src)
                return path[::-1]
            steps += 1
            if steps > 400000:
                return None
            crossing = (occH[c] not in (-1, nid)) or (occV[c] not in (-1, nid))
            for d2 in range(4):
                if d2 == (d + 2) % 4:
                    continue
                if crossing and d2 != d:
                    continue
                i, j = c % NX + DX[d2], c // NX + DY[d2]
                if not (0 <= i < NX and 0 <= j < self.NY):
                    continue
                n = c + DX[d2] + DY[d2] * NX
                try_push(n, d2, (c, d), g + (BEND if d2 != d else 0))
        return None

    def can_pass(self, nid, c, horiz):
        """Same rule as A*: a straight run may cross another net, never overlap, bend or end on it."""
        if self.block[c] and c not in self.pin_cells:
            return False
        if self.node[c] not in (-1, nid) or self.reserve[c] not in (-1, nid):
            return False
        same, other = (self.occH[c], self.occV[c]) if horiz else (self.occV[c], self.occH[c])
        if same not in (-1, nid):
            return False
        if other not in (-1, nid) and self.pwire[c]:
            return False
        return True

    def path_ok(self, nid, cells):
        for k, c in enumerate(cells):
            if c in self.pin_cells:
                if self.pin_cells[c] != nid:
                    return False
                continue
            if k in (0, len(cells) - 1):
                if not self.free_for(nid, c):
                    return False
                continue
            d0, d1 = self._dir(cells[k - 1], c), self._dir(c, cells[k + 1])
            if d0 != d1:
                if not self.free_for(nid, c):
                    return False
            elif not self.can_pass(nid, c, d0 in (0, 2)):
                return False
        return True

    def route_matrices(self):
        """Draw each matrix the way a person would: a straight line per row and per column, joined by
        short stubs, with one lead-out per line outside the grid for the router to connect to."""
        self.pre = {}
        NX = self.NX
        for it in self.matrices:
            mx = it.matrix
            grid = mx["grid"]
            snap = (list(self.occH), list(self.occV), list(self.node), list(self.near), bytearray(self.pwire),
                    {k: {c: set(v) for c, v in a.items()} for k, a in self.adj.items()})
            plan = []      # (nid, cells)
            ends = {}
            try:
                def pin_cell(u, num):
                    p = next(p for p in u.pins if p.number == num)
                    return self.idx(*self.C(*u.end(p)))
                ij = lambda c: (c % NX, c // NX)
                first_x = min(ij(pin_cell(u, mx["rowpin"]))[0] for u in (row[0] for row in grid))
                for row in grid:
                    nid = self.net_ids[self.pin_net[f"{row[0].ref}.{mx['rowpin']}"]]
                    tops = [pin_cell(u, mx["rowpin"]) for u in row]
                    ly = ij(tops[0])[1] - 2
                    xs = [ij(t)[0] for t in tops]
                    lead = first_x - 6
                    line = [self.idx(x, ly) for x in range(lead, max(xs) + 1)]
                    plan.append((nid, line))
                    for t in tops:
                        plan.append((nid, [t, t - NX, t - 2 * NX]))
                    ends[nid] = line[0]
                last_y = 0
                for ci in range(len(grid[0])):
                    col = [row[ci] for row in grid]
                    nid = self.net_ids[self.pin_net[f"{col[0].ref}.{mx['colpin']}"]]
                    joints = []
                    for u in col:
                        k = pin_cell(u, mx["colpin"])
                        x, y = ij(k)
                        stub = [k, self.idx(x, y + 1), self.idx(x, y + 2), self.idx(x - 1, y + 2), self.idx(x - 2, y + 2)]
                        plan.append((nid, stub))
                        joints.append((x - 2, y + 2))
                    lx = joints[0][0]
                    y0, y1 = joints[0][1], joints[-1][1] + 4
                    last_y = max(last_y, y1)
                    line = [self.idx(lx, y) for y in range(y0, y1 + 1)]
                    plan.append((nid, line))
                    ends[nid] = line[-1]
                for nid, cells in plan:
                    if not self.path_ok(nid, cells):
                        raise Unroutable("matrix line blocked")
                    self.commit_path(nid, cells)
                    # a stub ending on a straight line makes a T there
            except (Unroutable, StopIteration, KeyError):
                self.occH, self.occV, self.node, self.near, self.pwire, self.adj = snap
                self.notes.append(f"matrix {grid[0][0].ref}..{grid[-1][-1].ref}: drawn by the router instead")
                continue
            # T-joins created after the line was committed: mark them as nodes
            for nid in {n for n, _ in plan}:
                for c, ds in self.adj[nid].items():
                    if len(ds) >= 3:
                        self.node[c] = nid
            # keep other nets out of the grid
            us = [u for row in grid for u in row]
            bxs = [(u.x + u.box()[0], u.y + u.box()[1], u.x + u.box()[2], u.y + u.box()[3]) for u in us]
            self.mark_rect((min(b[0] for b in bxs) - 2.5, min(b[1] for b in bxs) - 2.5,
                            max(b[2] for b in bxs) + 2.5, max(b[3] for b in bxs) + 2.5), self.soft, 6.0)
            for nid, end in ends.items():
                pins = {t[0] for t in self.terms[nid] if t[0] in self.adj.get(nid, {})}
                self.pre[nid] = {"pins": pins, "ends": {end}}

    def route_signals(self):
        nets = []
        for nid, terms in self.terms.items():
            net = self.spec_net(nid)
            if self.is_power(net):
                continue
            ii = [t[0] % self.NX for t in terms]
            jj = [t[0] // self.NX for t in terms]
            nets.append(((max(ii) - min(ii)) + (max(jj) - min(jj)), len(terms), nid))
        nets.sort()
        self.failed = []
        self.routed = 0
        self.leadout_labels = []
        self.long_wires = []
        self.max_wire = float((self.spec.get("schematic") or {}).get("max_wire_mm", 50))
        for _, _, nid in nets:
            terms = list(self.terms[nid])
            net = self.spec_net(nid)
            if net in self.bus:
                pre = getattr(self, "pre", {}).get(nid)
                for t in terms:
                    if not (pre and t[0] in pre["pins"]):
                        self.label_pins.append((t[0], t[1], net))
                if pre:
                    end = next(iter(pre["ends"]))
                    nb = [c for c in self.adj[nid].get(end, ())]
                    out = (nb[0] + 2) % 4 if nb else 2
                    self.leadout_labels.append((end, out, net))
                continue
            if len(terms) < 2:
                for (c, d, key, u) in terms:
                    self.label_pins.append((c, d, self.spec_net(nid)))
                continue
            # start the tree at the pin of the biggest part (or with an already drawn matrix line)
            terms.sort(key=lambda t: -len(t[3].pins))
            pre = getattr(self, "pre", {}).get(nid)
            if pre:
                tree_pins = [t for t in terms if t[0] in pre["pins"]]
                rest = [t for t in terms if t[0] not in pre["pins"]]
            else:
                tree_pins = [terms[0]]
                rest = terms[1:]
            while rest:
                tcells = set(self.adj.get(nid, {}).keys()) | {t[0] for t in tree_pins}

                def dist(t):
                    i, j = t[0] % self.NX, t[0] // self.NX
                    return min(abs(i - c % self.NX) + abs(j - c // self.NX) for c in tcells)
                rest.sort(key=dist)
                t = rest.pop(0)
                targets = {c for c in tcells
                           if self.occH[c] in (-1, nid) and self.occV[c] in (-1, nid)}
                pin_entry = {tp[0]: (tp[1] + 2) % 4 for tp in tree_pins}
                # a tree pin that already has its wire can't take another one from outside
                for tp in tree_pins:
                    if tp[0] in self.adj.get(nid, {}):
                        targets.discard(tp[0])
                        pin_entry.pop(tp[0], None)
                if pre:       # join a matrix line only at its lead-out (or wires already hanging off it)
                    outside = {c for c in targets if self.soft[c] < 6.0}
                    targets = (outside - {c for t in tree_pins for c in [t[0]]}) or targets
                path = None
                if targets:
                    for pad in (10, 40, 400):
                        path = self.astar(nid, t[0], t[1], targets, pin_entry, pad)
                        if path:
                            break
                if path and len(path) * G > self.max_wire and len(terms) > 1 and not pre:
                    # a wire this long is spaghetti to a reader: name both ends instead
                    self.long_wires.append((net, round(len(path) * G)))
                    self.label_pins.append((t[0], t[1], net))
                    tree_pins.append(t)          # joined by name; the tree end gets its net label below
                    continue
                if path:
                    self.commit_path(nid, path)
                    tree_pins.append(t)
                    self.routed += 1
                else:
                    self.failed.append(t[2])
                    self.label_pins.append((t[0], t[1], self.spec_net(nid)))
                    if len(tree_pins) == 1 and not self.adj.get(nid):
                        pass
            # if the tree is a lone pin whose partners all failed, it needs a label too
            if any(w[0] == net for w in self.long_wires):
                # pins joined by name: every pin with no wire of its own needs its label
                lab = {c for c, _, n in self.label_pins if n == net}
                wired = self.adj.get(nid, {})
                for tp in tree_pins:
                    if tp[0] not in wired and tp[0] not in lab:
                        self.label_pins.append((tp[0], tp[1], net))
            elif len(tree_pins) == 1 and not self.adj.get(nid):
                tp = tree_pins[0]
                self.label_pins.append((tp[0], tp[1], self.spec_net(nid)))

    def prune_leadouts(self):
        """Remove matrix lead-outs nobody connected to (they would be loose wire ends)."""
        NX = self.NX
        step = (1, NX, -1, -NX)
        for nid in getattr(self, "pre", {}):
            a = self.adj.get(nid, {})
            while True:
                keep = {c for c, _, _ in getattr(self, "leadout_labels", [])}
                tips = [c for c, ds in a.items() if len(ds) == 1 and c not in self.pin_cells and c not in keep]
                if not tips:
                    break
                for c in tips:
                    d = next(iter(a[c]))
                    n = c + step[d]
                    del a[c]
                    self.occH[c] = self.occV[c] = -1
                    self.node[c] = -1
                    ds = a.get(n)
                    if ds is None:
                        continue
                    ds.discard((d + 2) % 4)
                    self.occH[n] = nid if (0 in ds or 2 in ds) else -1
                    self.occV[n] = nid if (1 in ds or 3 in ds) else -1
                    straight = ds in ({0, 2}, {1, 3})
                    self.node[n] = -1 if straight and n not in self.pin_cells else nid

    # -- labels ------------------------------------------------------------------
    def _pieces(self, a):
        """Connected pieces of one net's wiring (adj: cell -> directions)."""
        NX, seen, out = self.NX, set(), []
        for start in a:
            if start in seen:
                continue
            comp, stack = set(), [start]
            while stack:
                c = stack.pop()
                if c in comp:
                    continue
                comp.add(c)
                for d in a.get(c, ()):
                    nb = c + DX[d] + DY[d] * NX
                    if nb in a and nb not in comp:
                        stack.append(nb)
            seen |= comp
            out.append(comp)
        return out

    def place_labels(self):
        self.labels = []              # (net, x, y, angle)
        labelled = set()
        anchors = {}
        NX = self.NX
        for (c, d, net) in self.label_pins:
            nid = self.net_ids[net]
            # a power net is named by its power symbol; a text label of the same name makes KiCad 8+ warn
            if self.is_power(net) and any(self.power_cluster(nid, net, [c], d, k) for k in (2, 3, 4, 6)):
                continue
            s1 = c + DX[d] + DY[d] * NX
            s2 = c + 2 * (DX[d] + DY[d] * NX)
            ok = all(0 <= s < len(self.block) and self.free_for(nid, s) and not self.block[s] for s in (s1, s2))
            ok1 = 0 <= s1 < len(self.block) and self.free_for(nid, s1) and not self.block[s1]
            if ok:
                self.commit_path(nid, [c, s1, s2])
                anchor = s2
            elif ok1:
                self.commit_path(nid, [c, s1])      # a one-cell stub still gives the label a wire to sit on
                anchor = s1
            else:
                anchor = self._any_wire_cell(nid, prefer=c)   # a label must sit on a wire, never on a bare pin
                if anchor is None:
                    raise Unroutable(f"no room for a name label on {net} at {c}")
            self.labels.append((net, (anchor % NX) * G, (anchor // NX) * G, {0: 0, 1: 270, 2: 180, 3: 90}[d]))
            labelled.add(nid)
            anchors.setdefault(nid, set()).add(anchor)
        for (c, d, net) in getattr(self, "leadout_labels", []):
            self.labels.append((net, (c % NX) * G, (c // NX) * G, {0: 0, 1: 270, 2: 180, 3: 90}[d]))
            labelled.add(self.net_ids[net])
            anchors.setdefault(self.net_ids[net], set()).add(c)
        for nid, a in self.adj.items():
            net = self.spec_net(nid)
            if nid not in labelled or self.is_power(net):
                continue
            # a net joined partly by name: each separate piece of wire needs the name too
            for comp in self._pieces(a):
                if comp & anchors.get(nid, set()):
                    continue
                spot = self._label_spot(nid, net)
                cell = None if spot is None else round(spot[1] / G) * NX + round(spot[0] / G)
                if cell in comp:
                    self.labels.append((net, spot[0], spot[1], spot[2]))
                else:
                    c = min(comp)
                    self.labels.append((net, (c % NX) * G, (c // NX) * G, 0))
        for nid, a in self.adj.items():
            net = self.spec_net(nid)
            if nid in labelled or self.is_power(net):
                continue
            spot = self._label_spot(nid, net)
            if spot is None:
                # crowded: put the name on any wire of the net (text may overlap, the connection is what matters)
                c = self._any_wire_cell(nid)
                if c is None:
                    raise Unroutable(f"net {net} has no wire to carry its name")
                spot = ((c % NX) * G, (c // NX) * G, 0)
            self.labels.append((net, spot[0], spot[1], spot[2]))

    def _any_wire_cell(self, nid, prefer=None):
        """A cell on the net's wires (not a pin end), nearest to `prefer` if given."""
        a = self.adj.get(nid, {})
        cells = [c for c in a if c not in self.pin_cells]
        if not cells:
            return None
        if prefer is None:
            return cells[0]
        pi, pj = prefer % self.NX, prefer // self.NX
        return min(cells, key=lambda c: abs(c % self.NX - pi) + abs(c // self.NX - pj))

    def _label_spot(self, nid, net):
        NX = self.NX
        L = math.ceil(text_w(net) / G) + 1
        a = self.adj[nid]
        best = None
        for c, ds in a.items():
            i, j = c % NX, c // NX
            if 0 in ds and ds <= {0, 2} and c not in self.pin_cells:
                # horizontal run: text sits above, extends right along the wire
                run = 0
                k = c
                while k in a and (0 in a[k] or k == c) and run < L:
                    run += 1
                    k += 1
                cells = [self.idx(i + t, j - 1) for t in range(L) if self.inb(i + t, j - 1)]
                if len(cells) < L:
                    continue
                if any(self.occH[x] != -1 or self.occV[x] != -1 or self.block[x] or self.node[x] != -1
                       or self.textmask[x] or self.soft[x] >= 0.5 for x in cells):
                    continue
                score = -min(run, L) * 2 + (0 if run >= L else 6)
                if best is None or score < best[0]:
                    best = (score, i * G, j * G, 0, cells)
        if best is None:
            for c, ds in a.items():
                i, j = c % NX, c // NX
                if 1 in ds and ds <= {1, 3} and c not in self.pin_cells:
                    cells = [self.idx(i - 1, j - t) for t in range(L) if self.inb(i - 1, j - t)]
                    if len(cells) < L:
                        continue
                    if any(self.occH[x] != -1 or self.occV[x] != -1 or self.block[x] or self.node[x] != -1
                           or self.textmask[x] or self.soft[x] >= 0.5 for x in cells):
                        continue
                    best = (0, i * G, j * G, 90, cells)
                    break
        if best is None:
            return None
        for x in best[4]:
            self.textmask[x] = 1
        return best[1], best[2], best[3]

    # -- PWR_FLAGs ------------------------------------------------------------------
    def flag_nets(self):
        if getattr(self, "no_flags", False):      # previews: PWR_FLAGs only matter for ERC
            return []
        out = []
        for net, pins in self.spec["nets"].items():
            types = []
            for p in pins:
                up = self.unit_pins.get(p)
                if up:
                    types.append(up[1].etype)
            # a library power symbol (+5V, +3V3, GND...) adds its own power_in pin wherever
            # it is drawn (place_power()), so a net that gets one needs a flag just as much
            # as a net whose real component pins are power_in with no power_out driver.
            has_power_in = "power_in" in types or self.is_power(net)
            if has_power_in and "power_out" not in types:
                out.append(net)
        return out

    # -- emit ---------------------------------------------------------------------
    def wires_of(self, nid):
        """Maximal straight segments between node points, plus junction cells."""
        a = self.adj.get(nid, {})
        NX = self.NX
        segs, junc = [], []
        seen = set()

        def is_node(c):
            ds = a.get(c, set())
            return not (ds in ({0, 2}, {1, 3})) or c in self.pin_cells or self.node[c] == nid and len(ds) != 2
        for c, ds in a.items():
            if len(ds) >= 3:
                junc.append(c)
            for d in ds:
                if (c, d) in seen:
                    continue
                if not is_node(c):
                    continue
                k = c
                while True:
                    seen.add((k, d))
                    n = k + DX[d] + DY[d] * NX
                    seen.add((n, (d + 2) % 4))
                    k = n
                    if is_node(k):
                        break
                segs.append((c, k))
        return segs, junc

    def emit(self):
        env = self.env
        root = schgen.new_uuid()
        NX = self.NX
        lib_syms = {}

        def need(lib_id):
            if lib_id not in lib_syms:
                s = kl.get_symbol(lib_id)
                s[1] = Q(lib_id)
                lib_syms[lib_id] = s

        body = []

        def prop(key, val, x, y, hidden=False, angle=0):
            eff = ["effects", FONT]
            if hidden:
                eff = _hide(env, eff)
            return ["property", Q(key), Q(val), ["at", fmt(x), fmt(y), fmt(angle)], eff]

        def sym_node(lib_id, ref, value, x, y, rot, unit, props, pins, in_bom=True):
            node = ["symbol", ["lib_id", Q(lib_id)], ["at", fmt(x), fmt(y), str(rot)], ["unit", str(unit)]]
            if env.major >= 8:
                node.append(["exclude_from_sim", "no"])
            node += [["in_bom", "yes" if in_bom else "no"], ["on_board", "yes" if in_bom else "no"], ["dnp", "no"], U()]
            node += props
            for p in pins:
                node.append(["pin", Q(p), U()])
            node.append(["instances", ["project", Q(self.project),
                                       ["path", Q("/" + root), ["reference", Q(ref)], ["unit", str(unit)]]]])
            return node

        for u in self.units:
            need(u.lib_id)
            pos, _ = u.text_layout()
            ta = 90 if u.rot in (90, 270) else 0
            props = [prop("Reference", u.ref, u.x + pos[0][0], u.y + pos[0][1], angle=ta),
                     prop("Value", u.comp.get("value", ""), u.x + pos[1][0], u.y + pos[1][1], angle=ta),
                     prop("Footprint", u.comp.get("footprint", ""), u.x, u.y, hidden=True),
                     prop("Datasheet", u.comp.get("datasheet") or "~", u.x, u.y, hidden=True)]
            mpn = u.comp.get("mpn") or u.comp.get("MPN")
            if mpn:
                props.append(prop("MPN", mpn, u.x, u.y, hidden=True))
            body.append(sym_node(u.lib_id, u.ref, u.comp.get("value", ""), u.x, u.y, u.rot, u.unit, props,
                                 list(dict.fromkeys(p.raw or p.number for p in u.pins))))
            for p in u.pins:
                if f"{u.ref}.{p.number}" in self.nc and not p.hidden:
                    ex, ey = u.end(p)
                    body.append(["no_connect", ["at", fmt(ex), fmt(ey)], U()])

        pwr_n = 0
        for (lib, net, x, y, rot, (tx, ty)) in self.power_syms:
            need(lib)
            pwr_n += 1
            ref = f"#PWR{pwr_n:03d}"
            ta = 90 if rot in (90, 270) else 0
            props = [prop("Reference", ref, x, y, hidden=True), prop("Value", net, tx, ty, angle=ta),
                     prop("Footprint", "", x, y, hidden=True), prop("Datasheet", "", x, y, hidden=True)]
            body.append(sym_node(lib, ref, net, x, y, rot, 1, props, ["1"], in_bom=False))

        # PWR_FLAGs on a strip below the drawing
        flags = self.flag_nets()
        fx = self.flag_x0
        fy = self.flag_y
        for k, net in enumerate(flags, 1):
            need("power:PWR_FLAG")
            fref = f"#FLG{k:02d}"
            x = round(fx / P) * P
            y = round(fy / P) * P
            props = [prop("Reference", fref, x, y - 1.9, hidden=True), prop("Value", "PWR_FLAG", x, y - 3.81),
                     prop("Footprint", "", x, y, hidden=True), prop("Datasheet", "~", x, y, hidden=True)]
            body.append(sym_node("power:PWR_FLAG", fref, "PWR_FLAG", x, y, 0, 1, props, ["1"], in_bom=False))
            lib = self.power_symbol_for(net)
            if lib:
                need(lib)
                pwr_n += 1
                pref = f"#PWR{pwr_n:03d}"
                gdir = self.power_geometry(lib, 0)[1]
                if gdir == 1:       # ground hangs below the flag, same point
                    px = x
                    tpos = (px, y + 4.2)
                else:
                    px = x + 7.62
                    body.append(["wire", ["pts", ["xy", fmt(x), fmt(y)], ["xy", fmt(px), fmt(y)]],
                                 ["stroke", ["width", "0"], ["type", "default"]], U()])
                    tpos = (px, y - 4.2)
                props = [prop("Reference", pref, px, y, hidden=True), prop("Value", net, *tpos),
                         prop("Footprint", "", px, y, hidden=True), prop("Datasheet", "", px, y, hidden=True)]
                body.append(sym_node(lib, pref, net, px, y, 0, 1, props, ["1"], in_bom=False))
                fx += 20.32
            else:
                ex = x + 5.08
                body.append(["wire", ["pts", ["xy", fmt(x), fmt(y)], ["xy", fmt(ex), fmt(y)]],
                             ["stroke", ["width", "0"], ["type", "default"]], U()])
                if self.is_power(net):
                    # the drawing names this net with global labels (no library power symbol carries its name); a local
                    # label here would be a separate net and leave the flag connected to nothing
                    body.append(["global_label", Q(net), ["shape", "passive"], ["at", fmt(ex), fmt(y), "0"],
                                 ["fields_autoplaced"], ["effects", FONT, ["justify", "left"]], U(),
                                 ["property", Q("Intersheetrefs"), Q("${INTERSHEET_REFS}"), ["at", fmt(ex), fmt(y), "0"],
                                  ["effects", FONT, "hide"] if env.major < 9 else ["effects", FONT, ["hide", "yes"]]]])
                else:
                    body.append(["label", Q(net), ["at", fmt(ex), fmt(y), "0"],
                                 ["effects", FONT, ["justify", "left", "bottom"]], U()])
                fx += 10.16 + text_w(net)

        nwires = 0
        njunc = 0
        for nid in self.adj:
            segs, junc = self.wires_of(nid)
            for a, b in segs:
                body.append(["wire", ["pts", ["xy", fmt((a % NX) * G), fmt((a // NX) * G)],
                                      ["xy", fmt((b % NX) * G), fmt((b // NX) * G)]],
                             ["stroke", ["width", "0"], ["type", "default"]], U()])
                nwires += 1
            for c in junc:
                body.append(["junction", ["at", fmt((c % NX) * G), fmt((c // NX) * G)], ["diameter", "0"],
                             ["color", "0", "0", "0", "0"], U()])
                njunc += 1
        for (net, x, y, ang) in self.labels:
            just = "right" if ang in (180, 270) else "left"
            if self.is_power(net):
                # a power net's name is global (its power symbols); naming it with a local label of the same name
                # makes KiCad 8+ warn "Local and global labels have same name". A global label is the right object.
                body.append(["global_label", Q(net), ["shape", "passive"], ["at", fmt(x), fmt(y), str(ang)],
                             ["fields_autoplaced"], ["effects", FONT, ["justify", just]], U(),
                             ["property", Q("Intersheetrefs"), Q("${INTERSHEET_REFS}"), ["at", fmt(x), fmt(y), "0"],
                              ["effects", FONT, "hide"] if env.major < 9 else ["effects", FONT, ["hide", "yes"]]]])
                continue
            body.append(["label", Q(net), ["at", fmt(x), fmt(y), str(ang)],
                         ["effects", FONT, ["justify", just, "bottom"]], U()])

        name, W, H = self.paper
        paper = ["paper", Q(name)] if name != "User" else ["paper", Q("User"), fmt(W), fmt(H)]
        tree = ["kicad_sch", ["version", env.sch_version], ["generator", Q("kipipe")]]
        if env.major >= 8:
            tree.append(["generator_version", Q(f"{env.major}.0")])
        spec = self.spec
        tree += [["uuid", Q(root)], paper,
                 ["title_block", ["title", Q(spec["name"])], ["comment", "1", Q(spec.get("description", "")[:120])],
                  ["comment", "2", Q("Generated by kipipe")]],
                 ["lib_symbols"] + list(lib_syms.values())]
        tree += body
        tree.append(["sheet_instances", ["path", Q("/"), ["page", Q("1")]]])
        info = {"paper": name, "units": len(self.units), "labels": len(self.labels), "wires": nwires,
                "junctions": njunc, "power_symbols": len(self.power_syms), "pwr_flags": flags,
                "routed": self.routed, "label_fallback": sorted(set(self.failed), key=_natkey), "style": "wired",
                "bus_tagged": sorted(self.bus, key=_natkey)}
        return sexpr.dumps(tree) + "\n", info

    def run(self):
        self.build_units()
        self.build_items()
        self.place()
        self.grid_init()
        self.setup_obstacles()
        self.place_power()
        self.route_matrices()
        self.route_signals()
        if self.long_wires:
            self.notes.append(f"{len(self.long_wires)} long connection(s) drawn as net labels instead of wires "
                              f"(over {self.max_wire:g} mm): " + ", ".join(sorted({n for n, _ in self.long_wires}))[:200])
        self.prune_leadouts()
        self.place_labels()
        return self.emit()


def generate(spec: dict, project: str, pwr_flags: bool = True):
    schgen.uid_reset(project)
    ws = WiredSchematic(spec, project)
    ws.no_flags = not pwr_flags
    return ws.run()
