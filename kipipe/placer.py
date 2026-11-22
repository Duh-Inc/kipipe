"""Pure-Python footprint placer (no pcbnew dependency, unit-testable).

Units: mm, KiCad orientation (y down). Each part carries geometry for the 4
rotations, measured by pcbnew, so no rotation math is done here.

Algorithm
  1. Locked parts are fixed obstacles.
  2. Non-edge parts: seed the most connected part at the center, then
     greedily place the part most strongly connected to what is already
     placed, at the grid position/rotation that minimizes weighted distance of
     each pad to the nearest already-placed pad on the same net.
  3. Refinement: randomized moves, rotations and same-size swaps, accepted
     only when weighted half-perimeter wirelength (HPWL) drops.
  4. Edge parts (connectors) go along the requested board edge.
  5. Auto-size: outline shrinks to the placed parts plus margin, grown until
     the mounting-hole corners are clear.
"""
from __future__ import annotations

import math

from . import outline
import random
import re
from dataclasses import dataclass, field

GROUND_NAMES = {"GND", "GNDA", "GNDD", "AGND", "DGND", "VSS", "0V", "GNDPWR"}
ROTS = (0, 90, 180, 270)


@dataclass
class Geo:
    xmin: float     # courtyard bbox relative to footprint origin
    ymin: float
    xmax: float
    ymax: float
    pads: list      # [(net, dx, dy)]
    padbox: tuple | None = None   # bbox of pad copper (xmin, ymin, xmax, ymax)


@dataclass
class Part:
    ref: str
    geo: dict                       # rot -> Geo
    edge: str | None = None
    locked: bool = False
    x: float = 0.0
    y: float = 0.0
    rot: int = 0
    placed: bool = False
    members: list | None = None     # grid macro: [(child Part, {rot: (dx, dy)})]
    region: tuple | None = None     # (x0, y0, x1, y1) as fractions of the board
    near: list = field(default_factory=list)   # [(target_ref, target_pad_index, max_mm, my_pad_index|None)]
    keepout: float = 0.0            # no other parts within this many mm of the courtyard
    fp: str = ""                    # footprint lib id (some families have a known mating side)
    antenna: bool = False           # antenna module: edge, overhang, clear band beside it
    band: tuple | None = None       # board-edge band to keep copper pour out of (set for antennas)
    halo: float = 0.0               # extra clearance (routing escape room for many-pin parts)
    pad_edge: float = 0.0           # min pad-to-board-edge distance (high-voltage parts need room to route out)

    @property
    def rots(self):
        return tuple(r for r in ROTS if r in self.geo)

    def rect(self, clr=0.0, x=None, y=None, rot=None):
        g = self.geo[self.rot if rot is None else rot]
        x = self.x if x is None else x
        y = self.y if y is None else y
        return (x + g.xmin - clr, y + g.ymin - clr, x + g.xmax + clr, y + g.ymax + clr)

    def nets(self):
        return {n for g in self.geo.values() for (n, _, _) in g.pads if n}


# ---------------------------------------------------------------------------
# Grids (LED matrices, keypads): placed as one rigid block
# ---------------------------------------------------------------------------

def _natkey(s):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or "")]


def _rotation_fn(child):
    """Work out pcbnew's 90-degree rotation convention from measured pads, so
    no sign convention is assumed."""
    a = next(((dx, dy) for (_, dx, dy) in child.geo[0].pads if abs(dx) + abs(dy) > 1e-3), None)
    if a is None or 90 not in child.geo:
        return lambda x, y: (y, -x)
    b = child.geo[90].pads[[i for i, (_, dx, dy) in enumerate(child.geo[0].pads) if (dx, dy) == a][0]]
    for f in (lambda x, y: (y, -x), lambda x, y: (-y, x)):
        fx, fy = f(*a)
        if abs(fx - b[1]) < 1e-3 and abs(fy - b[2]) < 1e-3:
            return f
    return lambda x, y: (y, -x)


def make_grid(ref, children, cols, pitch=None, clearance=0.5):
    """Composite Part for children laid out row-major in `cols` columns."""
    g0 = children[0].geo[0]
    w, h = g0.xmax - g0.xmin, g0.ymax - g0.ymin
    if pitch:
        px = py = float(pitch)
    else:
        # square pitch with a routing channel between parts (0805 LEDs -> 5 mm, the usual matrix pitch)
        px = py = round((max(w, h) + 1.6) * 2) / 2
    rows = math.ceil(len(children) / cols)
    base = []
    for i, c in enumerate(children):
        r, k = divmod(i, cols)
        base.append(((k - (cols - 1) / 2) * px, (r - (rows - 1) / 2) * py))
    rot90 = _rotation_fn(children[0])
    offs = {0: base}
    for rot in (90, 180, 270):
        offs[rot] = [rot90(*o) for o in offs[rot - 90]]
    geo = {}
    for rot in ROTS:
        if not all(rot in c.geo for c in children):
            continue
        xs0, ys0, xs1, ys1, pads = [], [], [], [], []
        for c, (dx, dy) in zip(children, offs[rot]):
            cg = c.geo[rot]
            xs0.append(dx + cg.xmin); ys0.append(dy + cg.ymin)
            xs1.append(dx + cg.xmax); ys1.append(dy + cg.ymax)
            pads += [(n, dx + px_, dy + py_) for (n, px_, py_) in cg.pads]
        geo[rot] = Geo(min(xs0), min(ys0), max(xs1), max(ys1), pads)
    macro = Part(ref, geo)
    macro.members = [(c, {rot: offs[rot][i] for rot in geo}) for i, c in enumerate(children)]
    return macro


def expand(macro):
    for c, offs in macro.members:
        dx, dy = offs[macro.rot]
        c.x, c.y, c.rot, c.placed = macro.x + dx, macro.y + dy, macro.rot, True


def find_matrices(parts, footprints, min_n=9):
    """Detect R x C matrices: N identical 2-pin parts, pin 1 on R distinct nets,
    pin 2 on C distinct nets, every (row, col) pair used once."""
    groups = {}
    for p in parts:
        if p.locked or p.edge:
            continue
        nets = [n for (n, _, _) in p.geo[0].pads]
        if len(nets) == 2 and all(nets):
            groups.setdefault(footprints.get(p.ref), []).append((p, nets[0], nets[1]))
    found = []
    for fp, items in groups.items():
        if len(items) < min_n:
            continue
        a = sorted({x[1] for x in items}, key=_natkey)
        b = sorted({x[2] for x in items}, key=_natkey)
        pairs = {(x[1], x[2]) for x in items}
        if len(a) >= 2 and len(b) >= 2 and len(a) * len(b) == len(items) == len(pairs):
            by = {(x[1], x[2]): x[0] for x in items}
            order = [by[(ra, cb)] for ra in a for cb in b]
            found.append({"refs": [p.ref for p in order], "cols": len(b), "auto": f"{len(a)}x{len(b)} {fp}"})
    return found


def narrow_to_body(part, margin=1.5):
    """Antenna modules' courtyards include the antenna clearance zone (e.g. 48 mm wide
    on the ESP32-WROOM). For packing, use the module body: pad extents plus a margin
    across the antenna axis, full length along it."""
    for rot, g in part.geo.items():
        pb = g.padbox
        if not pb:
            continue
        pts = [(dx, dy) for (_, dx, dy) in g.pads] or [((g.xmin + g.xmax) / 2, (g.ymin + g.ymax) / 2)]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        vert = max(cy - g.ymin, g.ymax - cy) - min(cy - g.ymin, g.ymax - cy) >= \
            max(cx - g.xmin, g.xmax - cx) - min(cx - g.xmin, g.xmax - cx)
        if vert:     # antenna points up/down: narrow x
            g.xmin, g.xmax = max(g.xmin, pb[0] - margin), min(g.xmax, pb[2] + margin)
        else:
            g.ymin, g.ymax = max(g.ymin, pb[1] - margin), min(g.ymax, pb[3] + margin)


OPP = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
ANTENNA_SIDE = 6.0      # mm kept clear beside an antenna module along its edge
ANTENNA_BAND = 2.5      # mm deep strip from the edge beside it (antenna itself overhangs)


# Footprint families whose mating side can't be read from the courtyard. KiCad's library puts the wire entry of
# horizontal screw terminals on the footprint's +Y side; rotating by 90/180/270 (KiCad, counter-clockwise on screen)
# turns +Y to the right / top / left.
FACING_PLUS_Y = re.compile(r"TerminalBlock(?!.*Vertical)", re.I)
ROT_FOR_SIDE = {"bottom": 0, "right": 90, "top": 180, "left": 270}


def known_facing(part, side):
    if part.fp and FACING_PLUS_Y.search(part.fp):
        r = ROT_FOR_SIDE[side]
        for k in part.rots:
            if int(round(float(k))) % 360 == r:
                return k
    return None


def edge_orientation(part, side):
    """Pick the rotation whose body overhang points out of `side`.

    Connectors put their body/opening on one side of the pads (SMA, USB-C,
    barrel jacks). That side should face out of the board. Returns
    (rot, overhang) where overhang means the body is clearly asymmetric and
    the part should sit with its pads at the edge and its body hanging over."""
    rot = known_facing(part, side)
    if rot is not None:
        return rot, False
    best = None
    for rot in part.rots:
        g = part.geo[rot]
        pts = [(dx, dy) for (_, dx, dy) in g.pads] or [((g.xmin + g.xmax) / 2, (g.ymin + g.ymax) / 2)]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        ext = {"left": cx - g.xmin, "right": g.xmax - cx, "top": cy - g.ymin, "bottom": g.ymax - cy}
        score = ext[side] - ext[OPP[side]]
        depth = (g.xmax - g.xmin) if side in ("left", "right") else (g.ymax - g.ymin)
        # sub-0.5 mm asymmetry is footprint noise (e.g. pin 1 pad), not a body overhang
        key = (round(score * 2) / 2 if score > 0.5 else 0.0, -depth, -rot)
        if best is None or key > best[0]:
            best = (key, rot, score)
    return best[1], best[2] > 1.5


def inner_depth(part, side, rot, overhang, edge_clr):
    """How far the part reaches into the board from the edge."""
    g = part.geo[rot]
    pb = g.padbox or (g.xmin, g.ymin, g.xmax, g.ymax)
    if not overhang:
        return (g.xmax - g.xmin) if side in ("left", "right") else (g.ymax - g.ymin)
    if side == "left":
        return g.xmax - pb[0] + edge_clr
    if side == "right":
        return pb[2] - g.xmin + edge_clr
    if side == "top":
        return g.ymax - pb[1] + edge_clr
    return pb[3] - g.ymin + edge_clr


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


@dataclass
class Placer:
    parts: list
    width: float
    height: float
    clearance: float = 0.5          # courtyard-to-courtyard
    edge_margin: float = 1.0        # courtyard-to-board-edge
    edge_clr: float = 0.5           # copper-to-board-edge (for overhanging connectors)
    corner: float = 0.0             # mounting-hole keepout square at each corner
    obstacles: list = field(default_factory=list)   # fixed rects
    grid: float = 0.5
    seed: int = 1
    log: callable = print
    recorder: list | None = None    # frames for the placement video
    shape: list | None = None       # board outline polygon (board mm); None = the width x height rectangle

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.offset = [0.0, 0.0]
        self.relaxed: set = set()        # "REF (what)" for reporting
        self.relax_lvl: dict = {}        # ref -> 1 region dropped, 2 + keepout, 3 + near
        self.dependents: dict = {}       # target ref -> parts that must stay near it
        for p in self.parts:
            for t in p.near:
                self.dependents.setdefault(t[0], []).append(p)
        self.by_ref = {p.ref: p for p in self.parts}
        self.net_parts: dict[str, list[Part]] = {}
        for p in self.parts:
            for n in p.nets():
                self.net_parts.setdefault(n, []).append(p)
        self.weight = {}
        for n, ps in self.net_parts.items():
            base = n.lstrip("/").upper()
            k = len(ps)
            w = 1.0 / max(1, k - 1) ** 0.5
            if base in GROUND_NAMES:
                w *= 0.15     # ground is poured; don't let it dominate
            self.weight[n] = w

    def record(self, label, active=None, hpwl=None):
        if self.recorder is None:
            return
        self.recorder.append({
            "label": label, "active": active, "board": [self.width, self.height], "off": list(self.offset),
            "hpwl": round(self.hpwl() if hpwl is None else hpwl, 1),
            "parts": {p.ref: [round(p.x, 3), round(p.y, 3), p.rot, p.placed] for p in self.parts}})

    # -- geometry ---------------------------------------------------------
    def _inside(self, r):
        m = self.edge_margin
        if self.shape:
            return outline.rect_inside(self.shape, r, m)
        return r[0] >= m and r[1] >= m and r[2] <= self.width - m and r[3] <= self.height - m

    def _related(self, a, b):
        return any(t[0] == b.ref for t in a.near) or any(t[0] == a.ref for t in b.near)

    def _free(self, part, x, y, rot):
        c2 = self.clearance / 2
        r = part.rect(c2 + part.halo, x, y, rot)
        r0 = part.rect(0, x, y, rot)
        if not self._inside(r0):
            return False
        pb = part.geo[rot].padbox
        if pb and not part.edge:
            ec = max(self.edge_clr, part.pad_edge)
            if self.shape:
                if not outline.rect_inside(self.shape, (x + pb[0], y + pb[1], x + pb[2], y + pb[3]), ec):
                    return False
            elif x + pb[0] < ec or y + pb[1] < ec or x + pb[2] > self.width - ec or y + pb[3] > self.height - ec:
                return False
        for o in self.obstacles:
            if _overlap(r, o):
                return False
        lvl = self.relax_lvl.get(part.ref, 0)
        # a pinned part follows its target; its region doesn't apply
        if part.region and not part.near and lvl < 1:
            x0, y0, x1, y1 = part.region
            cx, cy = (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2
            if not (x0 * self.width <= cx <= x1 * self.width and y0 * self.height <= cy <= y1 * self.height):
                return False
        for q in self.parts:
            if q is part or not q.placed:
                continue
            if _overlap(r, q.rect(c2 + q.halo)):
                return False
            if lvl < 2 and (q.keepout or part.keepout) and not self._related(part, q):
                if q.keepout and _overlap(r, q.rect(c2 + q.keepout)):
                    return False
                if part.keepout and _overlap(part.rect(c2 + part.keepout, x, y, rot), q.rect(c2)):
                    return False
        # moving a target must not break the parts pinned to it
        for dep in self.dependents.get(part.ref, ()):
            if not dep.placed or self.relax_lvl.get(dep.ref, 0) >= 3 or dep is part:
                continue
            dpads = dep.geo[dep.rot].pads
            tp = part.geo[rot].pads
            for (tref, tidx, maxd, myidx) in dep.near:
                if tref != part.ref or tidx >= len(tp):
                    continue
                tx, ty = x + tp[tidx][1], y + tp[tidx][2]
                cand = [dpads[myidx]] if myidx is not None and myidx < len(dpads) else dpads
                if min(math.hypot(dep.x + p[1] - tx, dep.y + p[2] - ty) for p in cand) > maxd:
                    return False
        if part.near and lvl < 3:
            pads = part.geo[rot].pads
            for (tref, tidx, maxd, myidx) in part.near:
                t = self.by_ref.get(tref)
                if t is None or not t.placed:
                    continue
                tp = t.geo[t.rot].pads
                if tidx >= len(tp):
                    continue
                tx, ty = t.x + tp[tidx][1], t.y + tp[tidx][2]
                cand = [pads[myidx]] if myidx is not None and myidx < len(pads) else pads
                if min(math.hypot(x + p[1] - tx, y + p[2] - ty) for p in cand) > maxd:
                    return False
        return True

    def pad_positions(self, part, x=None, y=None, rot=None):
        g = part.geo[part.rot if rot is None else rot]
        x = part.x if x is None else x
        y = part.y if y is None else y
        return [(n, x + dx, y + dy) for (n, dx, dy) in g.pads if n]

    # -- cost ---------------------------------------------------------------
    def _attach_cost(self, part, x, y, rot):
        """Distance of each pad to the nearest placed pad on its net."""
        cost = 0.0
        for n, px, py in self.pad_positions(part, x, y, rot):
            best = None
            for q in self.net_parts.get(n, ()):
                if q is part or not q.placed:
                    continue
                for (m, qx, qy) in self.pad_positions(q):
                    if m == n:
                        d = abs(px - qx) + abs(py - qy)
                        if best is None or d < best:
                            best = d
            if best is not None:
                cost += self.weight[n] * best
        return cost

    def hpwl(self, nets=None):
        total = 0.0
        for n in (nets if nets is not None else self.net_parts):
            xs, ys = [], []
            for q in self.net_parts[n]:
                if not q.placed:
                    continue
                for (m, px, py) in self.pad_positions(q):
                    if m == n:
                        xs.append(px)
                        ys.append(py)
            if len(xs) > 1:
                total += self.weight[n] * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
        return total

    def _connectivity(self, part):
        s = 0.0
        for n in part.nets():
            if any(q.placed for q in self.net_parts[n] if q is not part):
                s += self.weight[n]
        return s

    # -- phases -------------------------------------------------------------
    def _best_spot(self, part, cx, cy, radius):
        best = None
        g = self.grid
        steps = int(radius / g)
        cands = []
        for i in range(-steps, steps + 1):
            for j in range(-steps, steps + 1):
                cands.append((cx + i * g, cy + j * g))
        cands.sort(key=lambda c: abs(c[0] - cx) + abs(c[1] - cy))
        for (x, y) in cands:
            x, y = round(x / g) * g, round(y / g) * g
            for rot in part.rots:
                if not self._free(part, x, y, rot):
                    continue
                c = self._attach_cost(part, x, y, rot) + 0.02 * (abs(x - cx) + abs(y - cy))
                if best is None or c < best[0]:
                    best = (c, x, y, rot)
            if best is not None and abs(x - cx) + abs(y - cy) > best[0] + 15:
                break
        return best

    def place_greedy(self):
        todo = [p for p in self.parts if not p.locked and not p.edge and not p.placed]
        if not todo:
            return
        todo.sort(key=lambda p: -sum(len(g.pads) for g in p.geo.values()))
        cx, cy = self.width / 2, self.height / 2
        radius = max(self.width, self.height)
        def ready(p):
            return all((self.by_ref.get(t[0]) is None) or self.by_ref[t[0]].placed for t in p.near)
        while todo:
            if any(q.placed for q in self.parts):
                # pinned parts go right after their target, before neighbours take the space
                todo.sort(key=lambda p: (not ready(p), not (p.near and ready(p)), -self._connectivity(p), -len(p.geo[0].pads)))
            part = todo.pop(0)
            # aim: the near-target pad, else the centroid of connected placed pads, clamped into the region
            pts = []
            for n in part.nets():
                for q in self.net_parts[n]:
                    if q.placed and q is not part:
                        pts += [(px, py) for (m, px, py) in self.pad_positions(q) if m == n]
            ax, ay = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else (cx, cy)
            for (tref, tidx, _, _) in part.near:
                t = self.by_ref.get(tref)
                if t is not None and t.placed and tidx < len(t.geo[t.rot].pads):
                    ax, ay = t.x + t.geo[t.rot].pads[tidx][1], t.y + t.geo[t.rot].pads[tidx][2]
                    break
            if part.region:
                x0, y0, x1, y1 = part.region
                if not pts:
                    ax, ay = (x0 + x1) / 2 * self.width, (y0 + y1) / 2 * self.height
                ax = min(max(ax, x0 * self.width), x1 * self.width)
                ay = min(max(ay, y0 * self.height), y1 * self.height)
            spot = self._best_spot(part, ax, ay, radius)
            if spot is None and (part.near or part.region or part.keepout):
                # drop constraints one at a time: region, then keepout, then near
                for lvl, what, has in ((1, "region", part.region and not part.near), (2, "keepout", part.keepout),
                                       (3, "near", part.near)):
                    self.relax_lvl[part.ref] = lvl
                    if not has:
                        continue
                    spot = self._best_spot(part, ax, ay, radius)
                    if spot is not None:
                        self.relaxed.add(f"{part.ref} ({what})")
                        self.log(f"warning: {part.ref}: could not keep its {what} constraint; placed without it")
                        break
                if spot is None:
                    spot = self._best_spot(part, ax, ay, radius)
            if spot is None:
                raise RuntimeError(f"no room on the board for {part.ref}; enlarge the board")
            _, part.x, part.y, part.rot = spot
            part.placed = True
            self.record(f"place {part.ref}", part.ref)

    def refine(self, iterations=3000, temp0=0.6):
        movable = [p for p in self.parts if p.placed and not p.locked and not p.edge]
        if len(movable) < 2:
            return
        cur = self.hpwl()
        start = cur
        best = (cur, [(p.x, p.y, p.rot) for p in movable])
        every = max(1, iterations // 60)
        for it in range(iterations):
            if it % every == 0:
                self.record("refine", hpwl=cur)
            if cur < best[0] - 1e-9:
                best = (cur, [(p.x, p.y, p.rot) for p in movable])
            t = temp0 * (1 - it / iterations) + 1e-3
            p = self.rng.choice(movable)
            old = (p.x, p.y, p.rot)
            nets = p.nets()
            r = self.rng.random()
            if r < 0.6:
                step = max(self.grid, 3.0 * (1 - it / iterations) + self.grid)
                nx = round((p.x + self.rng.uniform(-step, step)) / self.grid) * self.grid
                ny = round((p.y + self.rng.uniform(-step, step)) / self.grid) * self.grid
                nr = p.rot
            elif r < 0.85:
                nx, ny, nr = p.x, p.y, self.rng.choice(p.rots)
            else:
                q = self.rng.choice(movable)
                if q is p:
                    continue
                qold = (q.x, q.y, q.rot)
                nets2 = nets | q.nets()
                before = self.hpwl(nets2)
                p.x, p.y, q.x, q.y = q.x, q.y, p.x, p.y
                p.placed = q.placed = False
                ok = self._free(p, p.x, p.y, p.rot)
                p.placed = True
                ok = ok and self._free(q, q.x, q.y, q.rot)
                q.placed = True
                after = self.hpwl(nets2) if ok else math.inf
                if after < before or (ok and self.rng.random() < math.exp(-(after - before) / t)):
                    cur += after - before
                else:
                    p.x, p.y, p.rot = old
                    q.x, q.y, q.rot = qold
                continue
            before = self.hpwl(nets)
            p.placed = False
            ok = self._free(p, nx, ny, nr)
            p.placed = True
            if not ok:
                continue
            p.x, p.y, p.rot = nx, ny, nr
            after = self.hpwl(nets)
            if after < before or self.rng.random() < math.exp(-(after - before) / t):
                cur += after - before
            else:
                p.x, p.y, p.rot = old
        if self.hpwl() > best[0]:
            for p, st in zip(movable, best[1]):
                p.x, p.y, p.rot = st
        self.record("refine done")
        self.log(f"placement refine: weighted HPWL {start:.1f} -> {self.hpwl():.1f} mm")

    def place_edges_shaped(self):
        """Edge parts on a non-rectangular board: body facing out, as close to that side of the outline as fits
        (no overhang)."""
        cx, cy = self.width / 2, self.height / 2
        for side in ("left", "right", "top", "bottom"):
            for p in [q for q in self.parts if q.edge == side and not q.locked]:
                rot, over = edge_orientation(p, side)
                saved = p.rots if hasattr(p, "rots") else None
                tx, ty = {"left": (0, cy), "right": (self.width, cy), "top": (cx, 0), "bottom": (cx, self.height)}[side]
                best = None
                g = self.grid
                for k in range(0, int(max(self.width, self.height) / g)):
                    # walk in from the side along the axis, trying a strip of positions across it
                    x = tx + (k * g if side == "left" else -k * g if side == "right" else 0)
                    y = ty + (k * g if side == "top" else -k * g if side == "bottom" else 0)
                    for off in [i * g * s_ for i in range(0, int(max(self.width, self.height) / (2 * g))) for s_ in (1, -1)]:
                        px, py = (x, y + off) if side in ("left", "right") else (x + off, y)
                        gx = px - (p.geo[rot].xmin + p.geo[rot].xmax) / 2
                        gy = py - (p.geo[rot].ymin + p.geo[rot].ymax) / 2
                        gx, gy = round(gx / g) * g, round(gy / g) * g
                        if self._free_edge_shaped(p, gx, gy, rot, over):
                            best = (gx, gy)
                            break
                    if best:
                        break
                if best is None:
                    self.log(f"warning: {p.ref} doesn't fit against the {side} edge of this outline; placing it inside instead")
                    self.relaxed.add(f"{p.ref} (edge)")
                    p.edge = None
                    continue
                p.x, p.y, p.rot = best[0], best[1], rot
                p.placed = True
                p.overhang = over
                del saved
                self.record(f"edge {p.ref} ({side})", p.ref)

    def _free_edge_shaped(self, p, x, y, rot, over=False):
        """Overhanging connectors (edge-mount SMA, USB) only need their pads inside, like on rectangular boards."""
        pb = p.geo[rot].padbox
        if over and pb:
            if not outline.rect_inside(self.shape, (x + pb[0], y + pb[1], x + pb[2], y + pb[3]), self.edge_clr):
                return False
        elif not outline.rect_inside(self.shape, p.rect(0, x, y, rot), self.edge_margin):
            return False
        r = p.rect(self.clearance / 2, x, y, rot)
        if any(_overlap(r, o) for o in self.obstacles):
            return False
        return not any(q is not p and q.placed and _overlap(r, q.rect(self.clearance / 2)) for q in self.parts)

    def place_edges(self, region=None):
        """Place edge parts along the board edges, body facing out."""
        if self.shape:
            return self.place_edges_shaped()
        x0, y0, x1, y1 = region or (0, 0, self.width, self.height)
        m, ec = self.edge_margin, self.edge_clr
        for side in ("left", "right", "top", "bottom"):
            parts = [p for p in self.parts if p.edge == side and not p.locked]
            if not parts:
                continue
            info = {}
            for p in parts:
                p.rot, over = edge_orientation(p, side)
                info[p.ref] = over
            # order along the edge by where their connections are
            def pull(p):
                pts = []
                for n in p.nets():
                    for q in self.net_parts[n]:
                        if q is not p and q.placed and not q.edge:
                            pts += [(px, py) for (mm, px, py) in self.pad_positions(q) if mm == n]
                if not pts:
                    return None
                return sum(t[1] if side in ("left", "right") else t[0] for t in pts) / len(pts)
            vertical = side in ("left", "right")
            lo, hi = (y0, y1) if vertical else (x0, x1)
            wants = [(pull(p), p) for p in parts]
            wants.sort(key=lambda t: (t[0] is None, t[0] if t[0] is not None else 0))
            sizes = [((p.geo[p.rot].ymax - p.geo[p.rot].ymin) if vertical else (p.geo[p.rot].xmax - p.geo[p.rot].xmin))
                     + (2 * ANTENNA_SIDE if p.antenna else 0) for _, p in wants]
            total = sum(sizes) + 2.0 * (len(parts) - 1)
            # start from desired centers, then resolve overlaps along the edge
            centers = []
            for (w, p), sz in zip(wants, sizes):
                c = w if w is not None else (lo + hi) / 2
                centers.append(c)
            if len(parts) > 1 and all(w is None for w, _ in wants):
                step = (hi - lo) / (len(parts) + 1)
                centers = [lo + step * (i + 1) for i in range(len(parts))]
            for i in range(1, len(centers)):
                need = centers[i - 1] + (sizes[i - 1] + sizes[i]) / 2 + 2.0
                centers[i] = max(centers[i], need)
            lim = max(m, self.corner + 0.5)
            over_hi = centers[-1] + sizes[-1] / 2 - (hi - lim) if centers else 0
            if over_hi > 0:
                centers = [c - over_hi for c in centers]
            under = (lo + lim) - (centers[0] - sizes[0] / 2) if centers else 0
            if under > 0:
                centers = [c + under for c in centers]
            del total
            for (w, p), c in zip(wants, centers):
                g = p.geo[p.rot]
                pb = g.padbox or (g.xmin, g.ymin, g.xmax, g.ymax)
                over = info[p.ref]
                if side == "left":
                    p.x = (x0 + ec - pb[0]) if over else max(x0 + m - g.xmin, x0 + ec - pb[0])
                    p.y = c - (g.ymin + g.ymax) / 2
                elif side == "right":
                    p.x = (x1 - ec - pb[2]) if over else min(x1 - m - g.xmax, x1 - ec - pb[2])
                    p.y = c - (g.ymin + g.ymax) / 2
                elif side == "top":
                    p.x = c - (g.xmin + g.xmax) / 2
                    p.y = (y0 + ec - pb[1]) if over else max(y0 + m - g.ymin, y0 + ec - pb[1])
                else:
                    p.x = c - (g.xmin + g.xmax) / 2
                    p.y = (y1 - ec - pb[3]) if over else min(y1 - m - g.ymax, y1 - ec - pb[3])
                ex, ey = p.x, p.y
                p.x, p.y = round(p.x / self.grid) * self.grid, round(p.y / self.grid) * self.grid
                # snapping to the grid must never move pads outward past the copper-to-edge clearance
                if side == "left" and p.x < ex - 1e-9:
                    p.x = math.ceil(ex / self.grid - 1e-9) * self.grid
                elif side == "right" and p.x > ex + 1e-9:
                    p.x = math.floor(ex / self.grid + 1e-9) * self.grid
                elif side == "top" and p.y < ey - 1e-9:
                    p.y = math.ceil(ey / self.grid - 1e-9) * self.grid
                elif side == "bottom" and p.y > ey + 1e-9:
                    p.y = math.floor(ey / self.grid + 1e-9) * self.grid
                # slide along the edge if the spot collides with locked parts, holes or other edge parts
                if not self._edge_free(p):
                    base = (p.x, p.y)
                    for k in range(1, int(max(self.width, self.height) / self.grid)):
                        hit = False
                        for sgn in (1, -1):
                            d = sgn * k * self.grid
                            p.x, p.y = (base[0], base[1] + d) if vertical else (base[0] + d, base[1])
                            if self._edge_free(p):
                                hit = True
                                break
                        if hit:
                            break
                    else:
                        p.x, p.y = base
                p.placed = True
                p.overhang = over
                if p.antenna:
                    r = p.rect()
                    S, D = ANTENNA_SIDE, ANTENNA_BAND
                    p.band = {"top": (r[0] - S, y0, r[2] + S, y0 + D), "bottom": (r[0] - S, y1 - D, r[2] + S, y1),
                              "left": (x0, r[1] - S, x0 + D, r[3] + S), "right": (x1 - D, r[1] - S, x1, r[3] + S)}[side]
                    # nothing else in the strip beside the antenna along this edge
                    if side in ("top", "bottom"):
                        self.obstacles += [(r[0] - S, p.band[1], r[0] - 0.01, p.band[3]), (r[2] + 0.01, p.band[1], r[2] + S, p.band[3])]
                    else:
                        self.obstacles += [(p.band[0], r[1] - S, p.band[2], r[1] - 0.01), (p.band[0], r[3] + 0.01, p.band[2], r[3] + S)]
                self.record(f"edge {p.ref} ({side})", p.ref)

    def _edge_free(self, p):
        r = p.rect(self.clearance / 2)
        if any(_overlap(r, o) for o in self.obstacles):
            return False
        for q in self.parts:
            if q is not p and q.placed and (q.locked or q.edge) and _overlap(r, q.rect(self.clearance / 2)):
                return False
        return True

    def bbox(self, parts=None):
        rs = [p.rect() for p in (parts or self.parts) if p.placed]
        return (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))

    def shift(self, dx, dy):
        self.offset = [self.offset[0] + dx, self.offset[1] + dy]
        for p in self.parts:
            p.x += dx
            p.y += dy
        self.obstacles = [(a + dx, b + dy, c + dx, d + dy) for (a, b, c, d) in self.obstacles]
        for p in self.parts:
            if p.band:
                a, b, c, d = p.band
                p.band = (a + dx, b + dy, c + dx, d + dy)


def auto_size(parts, fill=0.35, aspect=1.3):
    # count the extra spacing a part keeps (high-voltage clearance, escape room) as part of its footprint
    area = sum((p.geo[0].xmax - p.geo[0].xmin + 1 + 2 * p.halo) * (p.geo[0].ymax - p.geo[0].ymin + 1 + 2 * p.halo) for p in parts)
    a = max(area / fill, 400)
    w = math.sqrt(a * aspect)
    return math.ceil(w), math.ceil(a / w)


def run(parts, width=None, height=None, hole_keepout=0.0, clearance=0.5, edge_margin=1.0,
        iterations=3000, edge_clr=0.5, obstacles=None, recorder=None, log=print, shape=None, holes=None):
    """Place everything. Returns (width, height). Coordinates are relative to
    the board's top-left corner. If width/height are None the outline is sized
    to fit the placement. shape: outline dict (see outline.py) for non-rectangular boards;
    holes: mounting-hole centres (board mm) to keep clear, for shaped boards."""
    if shape and not outline.is_rect(shape):
        return _run_shaped(parts, width, height, shape, hole_keepout, clearance, edge_margin, iterations, edge_clr,
                           obstacles, recorder, log, holes)
    if width is not None and height is not None:
        return _run(parts, width, height, False, hole_keepout, clearance, edge_margin, iterations, edge_clr,
                    obstacles, recorder, log)
    # Auto-size: regions are fractions of the canvas, so with regions the canvas must be close to the
    # final board (shrinking afterwards keeps the arrangement). Grow and retry if it doesn't fit.
    w0, h0 = auto_size(parts)
    factors = (1.3, 1.7, 2.3) if any(p.region for p in parts) else (2.0, 2.8)
    snapshot = [(p, p.x, p.y, p.rot, p.placed, p.edge) for p in parts]

    def restore(state):
        for p, x, y, r, placed, edge in state:
            p.x, p.y, p.rot, p.placed, p.edge = x, y, r, placed, edge
            p.band = None
    result = None
    for i, f in enumerate(factors):
        try:
            if recorder is not None:
                del recorder[:]
            result = _run(parts, w0 * f, h0 * f, True, hole_keepout, clearance, edge_margin, iterations, edge_clr,
                          obstacles, recorder, log)
            break
        except RuntimeError as e:
            if "no room" not in str(e) or i == len(factors) - 1:
                raise
            log(f"placement: {e}; retrying on a larger canvas")
            restore(snapshot)
    return _compact(parts, result, f, w0, h0, snapshot, restore, hole_keepout, clearance, edge_margin, iterations,
                    edge_clr, obstacles, recorder, log)


COMPACT_FLOOR = 0.6   # smallest canvas tried, as a multiple of the area estimate (fill 0.35)


def _compact(parts, result, f, w0, h0, snapshot, restore, hole_keepout, clearance, edge_margin, iterations, edge_clr,
             obstacles, recorder, log):
    """The outline is cut to the placed parts, but the parts spread to fill the canvas they were placed on (regions
    are fractions of it, edge parts sit on its sides), so a generous canvas means a generous board. Re-place on
    smaller canvases and keep the smallest that still fits everything with no extra dropped constraints. Stops at
    about the area estimate, which leaves room for routing."""
    base_relaxed = set(run.relaxed)
    best = result
    best_state = [(p, p.x, p.y, p.rot, p.placed, p.edge) for p in parts]
    best_rec = list(recorder) if recorder is not None else None
    scale = 0.85
    quiet = lambda *_: None
    why = "reached the smallest canvas tried"
    while f * scale >= COMPACT_FLOOR:
        restore(snapshot)
        if recorder is not None:
            del recorder[:]
        try:
            r = _run(parts, w0 * f * scale, h0 * f * scale, True, hole_keepout, clearance, edge_margin, iterations,
                     edge_clr, obstacles, recorder, quiet)
        except RuntimeError as e:
            why = f"no room on a {w0 * f * scale:.0f} x {h0 * f * scale:.0f} canvas ({e})"
            break
        overlap = [f"{p.ref}/{q.ref}" for i, p in enumerate(parts) for q in parts[i + 1:]
                   if p.placed and q.placed and _overlap(p.rect(), q.rect())]
        extra = sorted(set(run.relaxed) - base_relaxed)
        if overlap or extra:
            why = (f"parts would overlap ({', '.join(overlap[:4])})" if overlap else
                   f"it would drop {', '.join(extra[:4])}")
            break
        # a step can come out no smaller (placement is not perfectly monotonic); keep trying smaller canvases
        if r[0] * r[1] < best[0] * best[1] * 0.98:
            best = r
            best_state = [(p, p.x, p.y, p.rot, p.placed, p.edge) for p in parts]
            best_rec = list(recorder) if recorder is not None else None
        scale *= 0.88
    restore(best_state)
    if recorder is not None and best_rec is not None:
        recorder[:] = best_rec
    run.relaxed = sorted(base_relaxed)
    if best != result:
        log(f"compacted the board from {result[0]:.0f} x {result[1]:.0f} to {best[0]:.0f} x {best[1]:.0f} mm "
            f"(stopped: {why})")
    else:
        log(f"board size {best[0]:.0f} x {best[1]:.0f} mm kept (couldn't shrink: {why})")
    return best


def _run_shaped(parts, width, height, shape, keepout, clearance, edge_margin, iterations, edge_clr,
                obstacles, recorder, log, holes=None):
    """Non-rectangular outline: the shape is the canvas. If no size is given, start from the parts' area and
    grow the shape until everything fits."""
    fixed = width is not None and height is not None
    if fixed:
        sizes = [(width, height)]
    else:
        frac = outline.area_fraction(shape)
        aspect = outline.default_aspect(shape)
        area = sum((p.geo[0].xmax - p.geo[0].xmin + 1 + 2 * p.halo) * (p.geo[0].ymax - p.geo[0].ymin + 1 + 2 * p.halo)
                   for p in parts if not p.locked)
        a = max(area / (0.3 * frac), 500)
        w0 = math.sqrt(a * aspect)
        sizes = [(math.ceil(w0 * f), math.ceil(w0 * f / aspect)) for f in (1.0, 1.15, 1.3, 1.5, 1.75, 2.0)]
    snapshot = [(p, p.x, p.y, p.rot, p.placed, p.edge) for p in parts]
    for i, (w, h) in enumerate(sizes):
        poly = outline.polygon(shape, w, h)
        hs = holes if holes is not None else (outline.hole_positions(shape, w, h, keepout) if keepout > 0 else [])
        pl = Placer(parts, w, h, clearance=clearance, edge_margin=edge_margin, edge_clr=edge_clr, log=log,
                    recorder=recorder, shape=poly)
        pl.obstacles += list(obstacles or [])
        pl.obstacles += [(x - keepout / 2, y - keepout / 2, x + keepout / 2, y + keepout / 2) for x, y in hs]
        try:
            if recorder is not None:
                del recorder[:]
            pl.record("start")
            pl.place_edges()
            pl.place_greedy()
            pl.refine(iterations)
        except RuntimeError as e:
            if "no room" not in str(e) or i == len(sizes) - 1:
                raise
            log(f"placement: {e}; trying a larger {shape.get('type')} ({sizes[i + 1][0]} x {sizes[i + 1][1]} mm)")
            for p, x, y, r, placed, edge in snapshot:
                p.x, p.y, p.rot, p.placed, p.edge = x, y, r, placed, edge
            continue
        pl.record("placed")
        run.relaxed = sorted(pl.relaxed)
        run.holes = hs
        return w, h


def _run(parts, width, height, auto, hole_keepout, clearance, edge_margin, iterations, edge_clr,
         obstacles, recorder, log):
    pl = Placer(parts, width, height, clearance=clearance, edge_margin=edge_margin, edge_clr=edge_clr, log=log,
                recorder=recorder, corner=hole_keepout)
    pl.record("start")
    if not auto and obstacles:
        pl.obstacles += list(obstacles)
    if not auto and hole_keepout > 0:
        k = hole_keepout
        pl.obstacles += [(0, 0, k, k), (width - k, 0, width, k), (0, height - k, k, height), (width - k, height - k, width, height)]
    if not auto:
        pl.place_edges()          # fixed outline: connectors first, others avoid them
    pl.place_greedy()
    pl.refine(iterations)
    if auto:
        core = [p for p in parts if p.placed]
        if core:
            x0, y0, x1, y1 = pl.bbox(core)
        else:
            x0, y0, x1, y1 = width / 2 - 5, height / 2 - 5, width / 2 + 5, height / 2 + 5
        # leave room for edge parts on their sides
        gap = 1.5
        pad = {s: 0.0 for s in ("left", "right", "top", "bottom")}
        for p in parts:
            if p.edge and not p.locked:
                rot, over = edge_orientation(p, p.edge)
                d = inner_depth(p, p.edge, rot, over, edge_clr)
                pad[p.edge] = max(pad[p.edge], d + gap - (edge_margin if not over else 0),
                                  (ANTENNA_BAND + gap) if p.antenna else 0)
        m = edge_margin
        region = [x0 - pad["left"] - m, y0 - pad["top"] - m, x1 + pad["right"] + m, y1 + pad["bottom"] + m]
        # parts that need their pads well clear of the edge (high voltage) push the outline out
        for p in core:
            pb = p.geo[p.rot].padbox if p.rot in p.geo else None
            if not pb or not p.pad_edge or p.edge:
                continue
            e = p.pad_edge
            region[0] = min(region[0], p.x + pb[0] - e)
            region[1] = min(region[1], p.y + pb[1] - e)
            region[2] = max(region[2], p.x + pb[2] + e)
            region[3] = max(region[3], p.y + pb[3] + e)
        # make sure edge parts fit along their edges
        for side in ("left", "right", "top", "bottom"):
            ps = [p for p in parts if p.edge == side]
            if ps:
                rots = [edge_orientation(p, side)[0] for p in ps]
                need = sum(((p.geo[r].ymax - p.geo[r].ymin) if side in ("left", "right") else (p.geo[r].xmax - p.geo[r].xmin)) + 2
                           + (2 * ANTENNA_SIDE if p.antenna else 0) for p, r in zip(ps, rots)) \
                    + 2 * max(edge_margin, hole_keepout + 0.5)
                if side in ("left", "right") and region[3] - region[1] < need:
                    extra = (need - (region[3] - region[1])) / 2
                    region[1] -= extra
                    region[3] += extra
                if side in ("top", "bottom") and region[2] - region[0] < need:
                    extra = (need - (region[2] - region[0])) / 2
                    region[0] -= extra
                    region[2] += extra
        # clear the mounting-hole corners: for each blocked corner, push out only the cheaper of its two sides
        k = hole_keepout
        for _ in range(80):
            if k <= 0:
                break
            corners = {("L", "T"): (region[0], region[1], region[0] + k, region[1] + k),
                       ("R", "T"): (region[2] - k, region[1], region[2], region[1] + k),
                       ("L", "B"): (region[0], region[3] - k, region[0] + k, region[3]),
                       ("R", "B"): (region[2] - k, region[3] - k, region[2], region[3])}
            blocked = None
            for (sx, sy), c in corners.items():
                hit = [p.rect() for p in parts if p.placed and _overlap(c, p.rect())]
                if hit:
                    blocked = (sx, sy, hit)
                    break
            if not blocked:
                break
            sx, sy, hit = blocked
            # how far each side must move for the corner square to clear every part it touches
            dx = max((region[0] + k - r[0]) if sx == "L" else (r[2] - (region[2] - k)) for r in hit)
            dy = max((region[1] + k - r[1]) if sy == "T" else (r[3] - (region[3] - k)) for r in hit)
            dx, dy = max(dx, 0) + 0.2, max(dy, 0) + 0.2
            if dx <= dy:
                if sx == "L":
                    region[0] -= dx
                else:
                    region[2] += dx
            else:
                if sy == "T":
                    region[1] -= dy
                else:
                    region[3] += dy
        # round outline to whole mm
        region[2] = region[0] + math.ceil(region[2] - region[0])
        region[3] = region[1] + math.ceil(region[3] - region[1])
        pl.shift(-region[0], -region[1])
        pl.width, pl.height = region[2] - region[0], region[3] - region[1]
        pl.record("outline")
        if k > 0:
            pl.obstacles += [(0, 0, k, k), (pl.width - k, 0, pl.width, k), (0, pl.height - k, k, pl.height), (pl.width - k, pl.height - k, pl.width, pl.height)]
        pl.place_edges()
        # parts pinned to an edge part were placed before it existed: place them again now
        deps = [p for p in parts if not p.locked and not p.edge
                and any(pl.by_ref.get(t[0]) is not None and pl.by_ref[t[0]].edge for t in p.near)]
        if deps:
            for p in deps:
                p.placed = False
            pl.place_greedy()
        pl.refine(iterations // 2)    # re-settle the core now that connectors are fixed (holes are obstacles now)
    # verify
    bad = []
    for i, p in enumerate(parts):
        for q in parts[i + 1:]:
            if p.placed and q.placed and _overlap(p.rect(), q.rect()):
                bad.append(f"{p.ref}/{q.ref}")
    if bad:
        log(f"warning: courtyard overlaps after placement: {', '.join(bad[:10])}")
    pl.record("placed")
    run.relaxed = sorted(pl.relaxed)
    return pl.width, pl.height
