"""Draw a datasheet application circuit the way datasheets do.

The general schematic generator (schwire) places parts on a grid and routes between them, which turns a part
with 19 pull-ups into a tangle. Datasheet figures follow conventions instead, and this follows them too:

  - the IC in the middle; supply pins on top, ground pins at the bottom, signal pins on the sides
  - pins that talk to another chip (a host, a master) on the left, lined up with that chip's box
  - numbered pin groups (SD0/SC0, SD1/SC1 ...) on the right, kept in pairs
  - every part drawn at the pin it serves: pull-ups hang from the signal line up to a labelled rail (one rail per
    group), parts to ground drop from the line to a ground symbol, supply caps hang beside the supply stub
  - pins tied straight to ground share one short bus and one ground symbol
Output is a standalone SVG (black on white).
"""
from __future__ import annotations

import html
import re

PITCH = 22          # base spacing between pins on a side
COL = 42            # spacing between part columns (room for the ref/value text beside each part)
UP = 50             # room above a pin group for a rail and the part bodies
DOWN = 62           # room below a pin for a part to ground
TWO = {"R", "C", "L", "FERRITE", "CRYSTAL", "D", "LED"}


def _k(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def is_gnd(net: str) -> bool:
    k = _k(net)
    return k in ("gnd", "ground", "vss", "agnd", "dgnd", "pgnd", "sgnd", "0", "gnd0", "earth") or k.startswith("gnd")


def _kind(k) -> str:
    m = {"resistor": "R", "res": "R", "r": "R", "capacitor": "C", "cap": "C", "c": "C", "inductor": "L", "l": "L",
         "diode": "D", "d": "D", "schottky": "D", "led": "LED", "crystal": "CRYSTAL", "xtal": "CRYSTAL",
         "ferrite": "FERRITE", "ferritebead": "FERRITE", "bead": "FERRITE"}
    return m.get(_k(k), str(k or "").upper())


class SVG:
    def __init__(self):
        self.items = []
        self.xs, self.ys = [], []

    def _ext(self, *pts):
        for x, y in pts:
            self.xs.append(x)
            self.ys.append(y)

    def line(self, x1, y1, x2, y2, w=1.3):
        self.items.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke-width="{w}"/>')
        self._ext((x1, y1), (x2, y2))

    def poly(self, pts, fill="none", w=1.3):
        p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        self.items.append(f'<polyline points="{p}" fill="{fill}" stroke-width="{w}"/>')
        self._ext(*pts)

    def rect(self, x, y, w, h, fill="none", sw=1.4):
        self.items.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{fill}" stroke-width="{sw}"/>')
        self._ext((x, y), (x + w, y + h))

    def dot(self, x, y, r=2.4):
        self.items.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#111" stroke="none"/>')

    def ring(self, x, y, r=2.6):
        self.items.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="#fff" stroke-width="1.2"/>')
        self._ext((x - r, y - r), (x + r, y + r))

    def text(self, x, y, s, anchor="start", size=10, weight="normal", rot=0, fill="#111"):
        t = html.escape(str(s))
        tr = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot else ""
        self.items.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" '
                          f'font-weight="{weight}" fill="{fill}" stroke="none"{tr}>{t}</text>')
        w = len(str(s)) * size * 0.58
        if rot:
            self._ext((x - size, y - w), (x + size, y + w))
        else:
            x0 = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
            self._ext((x0, y - size), (x0 + w, y + 3))

    # -- symbols (vertical, between y1 (top) and y2 (bottom) at x) -------------------------------------------
    def part_v(self, kind, x, y1, y2, ref="", value="", flip_label=False):
        mid = (y1 + y2) / 2
        if kind == "R":
            h = 24
            self.line(x, y1, x, mid - h / 2)
            pts = [(x, mid - h / 2)]
            for i in range(6):
                pts.append((x + (5 if i % 2 == 0 else -5), mid - h / 2 + (i + 0.5) * h / 6))
            pts.append((x, mid + h / 2))
            self.poly(pts)
            self.line(x, mid + h / 2, x, y2)
        elif kind == "C":
            self.line(x, y1, x, mid - 3)
            self.line(x - 9, mid - 3, x + 9, mid - 3, 2)
            self.line(x - 9, mid + 3, x + 9, mid + 3, 2)
            self.line(x, mid + 3, x, y2)
        elif kind == "L":
            h = 24
            self.line(x, y1, x, mid - h / 2)
            for i in range(4):
                cy = mid - h / 2 + 3 + i * 6
                self.items.append(f'<path d="M {x:.1f} {cy - 3:.1f} A 3.4 3 0 0 1 {x:.1f} {cy + 3:.1f}" fill="none" stroke-width="1.3"/>')
            self._ext((x, mid - h / 2), (x + 4, mid + h / 2))
            self.line(x, mid + h / 2, x, y2)
        elif kind in ("D", "LED"):
            # anode on top (current flows down), as most datasheet figures draw a diode to ground
            self.line(x, y1, x, mid - 6)
            self.poly([(x - 7, mid - 6), (x + 7, mid - 6), (x, mid + 5), (x - 7, mid - 6)])
            self.line(x - 7, mid + 5, x + 7, mid + 5, 1.6)
            self.line(x, mid + 5, x, y2)
            if kind == "LED":
                for d in (0, 5):
                    self.line(x + 9, mid - 4 + d, x + 15, mid - 9 + d, 1)
        elif kind == "FERRITE":
            self.line(x, y1, x, mid - 9)
            self.rect(x - 4, mid - 9, 8, 18, fill="#111", sw=1)
            self.line(x, mid + 9, x, y2)
        elif kind == "CRYSTAL":
            self.line(x, y1, x, mid - 8)
            self.line(x - 8, mid - 8, x + 8, mid - 8, 1.6)
            self.rect(x - 6, mid - 5, 12, 10)
            self.line(x - 8, mid + 8, x + 8, mid + 8, 1.6)
            self.line(x, mid + 8, x, y2)
        else:
            self.line(x, y1, x, mid - 10)
            self.rect(x - 7, mid - 10, 14, 20)
            self.line(x, mid + 10, x, y2)
        lx = x - 10 if flip_label else x + 10
        anchor = "end" if flip_label else "start"
        if ref:
            self.text(lx, mid - 1, ref, anchor, 9, fill="#333")
        if value:
            self.text(lx, mid + 10, value, anchor, 9, fill="#333")

    def ground(self, x, y):
        """Ground symbol hanging from (x, y)."""
        self.line(x, y, x, y + 6)
        for i, w in enumerate((9, 6, 3)):
            self.line(x - w, y + 6 + i * 3.5, x + w, y + 6 + i * 3.5, 1.4)

    def rail_up(self, x, y, name):
        """Supply arrow at the top of a stub ending at (x, y)."""
        self.line(x - 7, y, x + 7, y, 1.6)
        self.text(x, y - 5, name, "middle", 10, "bold")

    def render(self, pad=14) -> str:
        if not self.xs:
            return '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
        x0, x1 = min(self.xs) - pad, max(self.xs) + pad
        y0, y1 = min(self.ys) - pad, max(self.ys) + pad
        w, h = x1 - x0, y1 - y0
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.1f} {y0:.1f} {w:.1f} {h:.1f}" '
                f'width="{w:.0f}" height="{h:.0f}" font-family="Helvetica, Arial, sans-serif">'
                f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" fill="#fff" stroke="none"/>'
                f'<g stroke="#111" stroke-linecap="round" stroke-linejoin="round">' + "".join(self.items) + "</g></svg>")


def _fmt_value(v):
    s = str(v or "").strip()
    return "" if _k(s) in ("", "none", "null", "notshown", "notspecified", "na", "seeequation", "seetext") else s


def draw(record: dict) -> str:
    c = record.get("application_circuit") or {}
    # parts documented in magnitudes (a negative regulator written as positive numbers): mark the rails, so the
    # picture doesn't read as a positive circuit
    negative = (record.get("conventions") or {}).get("sign") == "magnitudes"

    def sgn(net):
        return f"{net} (−)" if negative and net and not is_gnd(net) else net
    ic = c.get("ic_ref") or "U1"
    comps = {x["ref"]: x for x in c.get("components") or [] if isinstance(x, dict) and x.get("ref")}
    nets = {n: [x for x in nodes if isinstance(x, str) and "." in x] for n, nodes in (c.get("nets") or {}).items()}

    # pins of the IC: the record's pin list, plus any pin name the circuit uses that the list lacks
    rec_pins = []
    for p in record.get("pins") or []:
        num = next(iter((p.get("numbers") or {}).values()), "")
        rec_pins.append({"name": p["name"], "type": p.get("type") or "passive",
                         "num": re.split(r"[,\s/]+", str(num).strip())[0] if num else ""})
    known = {_k(p["name"]) for p in rec_pins}
    pin_net = {}
    for n, nodes in nets.items():
        for x in nodes:
            ref, pin = x.split(".", 1)
            if ref == ic:
                if _k(pin) not in known:
                    rec_pins.append({"name": pin, "type": "passive", "num": ""})
                    known.add(_k(pin))
                pin_net.setdefault(_k(pin), n)
    if not rec_pins:
        return SVG().render()

    # every other part: its nets per pin
    part_nets: dict = {}
    for n, nodes in nets.items():
        for x in nodes:
            ref, pin = x.split(".", 1)
            if ref != ic:
                part_nets.setdefault(ref, []).append((pin, n))
    two, aux = {}, {}
    for ref, pn in part_nets.items():
        kind = _kind((comps.get(ref) or {}).get("kind"))
        if kind in TWO and len({n for _, n in pn}) == 2:
            two[ref] = (kind, pn)
        elif len({n for _, n in pn}) >= 1 and kind not in TWO:
            aux[ref] = pn

    ic_nets = {n for n in pin_net.values()}
    first_pin_of_net = {}
    for p in rec_pins:
        n = pin_net.get(_k(p["name"]))
        if n and n not in first_pin_of_net:
            first_pin_of_net[n] = _k(p["name"])

    # attach each two-terminal part to a pin: "up" to a rail label, or "down" to ground
    attach: dict = {}          # pin key -> [(ref, kind, dir, other net, pin order)]
    orphans = []
    bridges = []               # parts between two IC pins (crystals, a resistor across two pins)
    for ref, (kind, pn) in two.items():
        n1, n2 = pn[0][1], pn[1][1]
        cands = [n for n in (n1, n2) if n in ic_nets and not is_gnd(n)]
        if len(cands) == 2:
            bridges.append((ref, kind, n1, n2))
            continue
        if not cands:
            orphans.append((ref, kind, pn))
            continue
        on = cands[0]
        other = n2 if on == n1 else n1
        d = "down" if is_gnd(other) else "up"
        # diodes: keep the anode on top
        attach.setdefault(first_pin_of_net[on], []).append((ref, kind, d, other))

    # sides
    def ptype(p):
        return str(p["type"]).lower()

    aux_nets = {n for pn in aux.values() for _, n in pn}
    top, bottom, rest = [], [], []
    for p in rec_pins:
        k = _k(p["name"])
        net = pin_net.get(k)
        if ptype(p) == "ground" or is_gnd(p["name"]):
            bottom.append(p)
        elif ptype(p) == "power" and not (net and is_gnd(net)):
            top.append(p)
        else:
            rest.append(p)
    stems = {}
    for p in rest:
        m = re.match(r"^(.*?)(\d+)$", p["name"])
        if m:
            stems.setdefault(_k(m.group(1)), []).append(p)
    grouped = {id(p) for ps in stems.values() if len(ps) >= 2 for p in ps}
    left = [p for p in rest if id(p) not in grouped and (pin_net.get(_k(p["name"])) in aux_nets or True)]
    right = [p for p in rest if id(p) in grouped]
    # pins that talk to another chip stay left; balance the rest if one side is empty
    if not right:
        movable = [p for p in left if pin_net.get(_k(p["name"])) not in aux_nets]
        right = movable[len(movable) // 2:]
        left = [p for p in left if p not in right]
    if not left and right:
        left, right = right[:len(right) // 2], right[len(right) // 2:]
    # pins tied straight to ground (address straps) go to the less crowded side, as datasheets draw them
    def _tied(p):
        n = pin_net.get(_k(p["name"]))
        return bool(n and is_gnd(n))
    straps = [p for p in left + right if _tied(p)]
    left = [p for p in left if p not in straps]
    right = [p for p in right if p not in straps]
    (left if len(left) <= len(right) else right).extend(straps)

    def numkey(p):
        try:
            return int(p["num"])
        except (TypeError, ValueError):
            return 999

    def gnd_tied(p):
        n = pin_net.get(_k(p["name"]))
        return bool(n and is_gnd(n))
    left.sort(key=lambda p: (0 if pin_net.get(_k(p["name"])) in aux_nets else 2 if gnd_tied(p) else 1, numkey(p)))

    def group_key(p):
        m = re.match(r"^(.*?)(\d+)$", p["name"])
        return (int(m.group(2)), numkey(p)) if m else (999, numkey(p))
    right.sort(key=lambda p: (2 if gnd_tied(p) else 1, group_key(p)))

    # a part between two neighbouring pins on one side is drawn between their lines; otherwise it hangs from the
    # first pin with the other net's name on its far end
    bridge_at = {}              # key of the lower pin -> (ref, kind)
    for ref, kind, n1, n2 in bridges:
        k1, k2 = first_pin_of_net[n1], first_pin_of_net[n2]
        placed = False
        for side in (left, right):
            ks = [_k(p["name"]) for p in side]
            if k1 in ks and k2 in ks and abs(ks.index(k1) - ks.index(k2)) == 1:
                lower = k1 if ks.index(k1) > ks.index(k2) else k2
                bridge_at[lower] = (ref, kind)
                placed = True
        if not placed:
            attach.setdefault(k1, []).append((ref, kind, "up", n2))

    # ---- vertical layout of each side ------------------------------------------------------------------
    def layout_side(pins):
        """y for each pin, rail groups, and part columns. Returns (rows, height)."""
        rows, y, prev_rail = [], 0.0, None
        for p in pins:
            k = _k(p["name"])
            parts = attach.get(k, [])
            ups = [x for x in parts if x[2] == "up"]
            downs = [x for x in parts if x[2] == "down"]
            rail = ups[0][3] if ups else None
            if rail and rail != prev_rail:
                y += UP
            br = bridge_at.get(k)
            if br:
                y += 34                 # room for the part between this pin's line and the one above
            rows.append({"pin": p, "y": y, "ups": ups, "downs": downs, "rail": rail, "bridge": br})
            y += PITCH
            if downs:
                y += DOWN
            prev_rail = rail
        return rows, y

    L, hl = layout_side(left)
    R, hr = layout_side(right)
    longest_l = max([len(p["name"]) for p in left] + [2])
    longest_r = max([len(p["name"]) for p in right] + [2])
    caps_top = {}
    for p in top:
        caps_top[_k(p["name"])] = [x for x in attach.get(_k(p["name"]), [])]
    top_w = sum(40 + COL * max(1, len(v)) for v in caps_top.values()) + 20
    mpn_w = len(record["identity"]["mpn"]) * 8 + 16
    box_w = max(longest_l * 6.4 + longest_r * 6.4 + mpn_w + 40, top_w, 120)
    inner = max(hl, hr, PITCH)
    box_x0, box_x1 = 0.0, box_w
    box_y0 = 0.0
    side_top = box_y0 + (34 if top else 18)
    box_y1 = side_top + inner + 6
    s = SVG()
    s.rect(box_x0, box_y0, box_w, box_y1 - box_y0, fill="#fff", sw=1.6)
    name_x = box_x0 + longest_l * 6.4 + 20 + mpn_w / 2 if left else (box_x0 + box_x1) / 2
    s.text(name_x, (box_y0 + box_y1) / 2 + 4, record["identity"]["mpn"], "middle", 12, "bold")

    def pin_label(x, y, p, side):
        inside = x + 5 if side == "L" else x - 5
        s.text(inside, y + 3.5, p["name"], "start" if side == "L" else "end", 9.5)
        if p["num"]:
            s.text(x - 6 if side == "L" else x + 6, y - 3, p["num"], "end" if side == "L" else "start", 8.5, fill="#555")

    # ---- sides -------------------------------------------------------------------------------------------
    def draw_side(rows, side):
        sign = -1 if side == "L" else 1
        edge = box_x0 if side == "L" else box_x1
        # columns, restarting for each rail group
        groups, cur = [], []
        for r in rows:
            if cur and (r["rail"] != cur[-1]["rail"] or r["rail"] is None):
                groups.append(cur)
                cur = []
            cur.append(r)
        if cur:
            groups.append(cur)
        max_cols = 0
        for g in groups:
            n = sum(len(r["ups"]) + len(r["downs"]) + (1 if r.get("bridge") else 0) for r in g)
            max_cols = max(max_cols, n)
        for a, b in zip(rows, rows[1:]):
            if b.get("bridge"):
                max_cols = max(max_cols, len(a["ups"]) + len(a["downs"]) + 1 + len(b["ups"]) + len(b["downs"]))
        line_end = edge + sign * (36 + COL * max_cols + 16)
        gnd_bus = []
        prev_y, prev_cols = None, 0
        for g in groups:
            col = 0
            rail_y = side_top + g[0]["y"] - UP + 14
            rail_cols = []
            for r in g:
                y = side_top + r["y"]
                p = r["pin"]
                net = pin_net.get(_k(p["name"]))
                pin_label(edge, y, p, side)
                if net and is_gnd(net) and not r["ups"] and not r["downs"]:
                    s.line(edge, y, edge + sign * 18, y)
                    gnd_bus.append((edge + sign * 18, y))
                    continue
                aux_here = net in aux_nets
                end = line_end if (net and (r["ups"] or r["downs"] or aux_here or not is_gnd(net))) else edge + sign * 18
                if not net:
                    s.line(edge, y, edge + sign * 14, y)
                    continue
                s.line(edge, y, end, y)
                if r.get("bridge") and prev_y is not None:
                    ref, kind = r["bridge"]
                    col = max(col, prev_cols)       # past the columns the pin above already uses
                    x = edge + sign * (36 + COL * col)
                    col += 1
                    s.dot(x, prev_y)
                    s.dot(x, y)
                    s.part_v(kind, x, prev_y, y, ref, _fmt_value((comps.get(ref) or {}).get("value")))
                for ref, kind, d, other in r["ups"]:
                    x = edge + sign * (36 + COL * col)
                    col += 1
                    rail_cols.append(x)
                    body_bot = side_top + g[0]["y"] - 10
                    s.part_v(kind, x, rail_y, body_bot, ref, _fmt_value((comps.get(ref) or {}).get("value")),
                             flip_label=False)
                    s.line(x, body_bot, x, y)
                    s.dot(x, y)
                for ref, kind, d, other in r["downs"]:
                    x = edge + sign * (36 + COL * col)
                    col += 1
                    s.dot(x, y)
                    s.part_v(kind, x, y, y + DOWN - 18, ref, _fmt_value((comps.get(ref) or {}).get("value")))
                    s.ground(x, y + DOWN - 18)
                prev_y = y
                prev_cols = col
                if not aux_here and not is_gnd(net):
                    s.ring(end + sign * 2.6, y)
                    if _k(net) != _k(p["name"]):
                        s.text(end + sign * 9, y + 3.5, sgn(net), "start" if side == "R" else "end", 9.5)
            if rail_cols:
                xa, xb = min(rail_cols), max(rail_cols)
                s.line(xa - 4 if sign > 0 else xa, rail_y, xb + (0 if sign > 0 else 4), rail_y)
                for x in rail_cols:
                    s.dot(x, rail_y, 1.8) if len(rail_cols) > 1 else None
                # rail name above the rail, centred on it, clear of the part labels below
                s.text((xa + xb) / 2, rail_y - 5, sgn(g[0]["rail"]), "middle", 10, "bold")
        if gnd_bus:
            bx = gnd_bus[0][0]
            ys = [y for _, y in gnd_bus]
            s.line(bx, min(ys), bx, max(ys))
            for _, y in gnd_bus:
                if len(gnd_bus) > 1:
                    s.dot(bx, y)
            s.ground(bx, max(ys))
        return line_end

    l_end = draw_side(L, "L")
    draw_side(R, "R")

    # ---- top: supplies, with their caps hanging beside the stub ---------------------------------------------
    x = box_x0 + 30
    for p in top:
        k = _k(p["name"])
        net = pin_net.get(k) or p["name"]
        s.text(x, box_y0 + 12, p["name"], "middle", 9.5)
        if p["num"]:
            s.text(x + 4, box_y0 - 4, p["num"], "start", 8.5, fill="#555")
        caps = caps_top.get(k, [])
        stub_top = box_y0 - (96 if caps else 30)
        s.line(x, box_y0, x, stub_top)
        s.rail_up(x, stub_top, sgn(net))
        if caps:
            bx = x + COL * len(caps)
            s.line(x, stub_top + 14, bx, stub_top + 14)
            s.dot(x, stub_top + 14)
            for i, (ref, kind, d, other) in enumerate(caps):
                cx = x + COL * (i + 1)
                bot = stub_top + 14 + 46
                if d == "down":
                    s.part_v(kind, cx, stub_top + 14, bot, ref, _fmt_value((comps.get(ref) or {}).get("value")))
                    s.ground(cx, bot)
                else:
                    s.part_v(kind, cx, stub_top + 14, bot, ref, _fmt_value((comps.get(ref) or {}).get("value")))
                    s.text(cx, bot + 12, sgn(other), "middle", 9.5)
        x += 40 + COL * max(1, len(caps))

    # ---- bottom: ground pins --------------------------------------------------------------------------------
    n = len(bottom)
    for i, p in enumerate(bottom):
        bx = box_x0 + box_w * (i + 1) / (n + 1)
        s.text(bx, box_y1 - 5, p["name"], "middle", 9.5)
        if p["num"]:
            s.text(bx + 4, box_y1 + 11, p["num"], "start", 8.5, fill="#555")
        s.line(bx, box_y1, bx, box_y1 + 16)
        s.ground(bx, box_y1 + 16)

    # ---- other chips (a host, a master): a box left of the left side, pins lined up with the nets -------------
    ax = l_end - 30
    for ref, pn in aux.items():
        rows = []
        for pin, net in pn:
            r = next((r for r in L if pin_net.get(_k(r["pin"]["name"])) == net), None)
            if r:
                rows.append((pin, side_top + r["y"]))
        if not rows:
            continue
        ys = [y for _, y in rows]
        width = max(len(pn_) for pn_, _ in rows) * 6.4 + 24
        val = _fmt_value((comps.get(ref) or {}).get("value")) or ref
        bx1 = ax
        bx0 = bx1 - max(width, len(val) * 6.4 + 16)
        top_y, bot_y = min(ys) - 26, max(ys) + 14
        s.rect(bx0, top_y, bx1 - bx0, bot_y - top_y)
        s.text((bx0 + bx1) / 2, top_y + 13, val, "middle", 10, "bold")
        for pin, y in rows:
            s.text(bx1 - 5, y + 3.5, pin, "end", 9.5)
            s.line(bx1, y, l_end, y)
        ax = bx0 - 40

    # ---- anything not attached to a pin: drawn as labelled sticks underneath ---------------------------------
    ox = box_x0
    oy = box_y1 + 70
    for ref, kind, pn in orphans:
        (_, n1), (_, n2) = pn[0], pn[1]
        if is_gnd(n1):
            n1, n2 = n2, n1
        s.text(ox, oy - 4, n1, "middle", 9.5)
        s.part_v(kind, ox, oy, oy + 48, ref, _fmt_value((comps.get(ref) or {}).get("value")))
        if is_gnd(n2):
            s.ground(ox, oy + 48)
        else:
            s.text(ox, oy + 60, n2, "middle", 9.5)
        ox += 70
    if negative:
        y = max(s.ys) + 22 if s.ys else 0
        s.text(min(s.xs) if s.xs else 0, y, "(−) negative rail: this datasheet writes negative voltages as positive "
               "numbers (see Conventions)", "start", 10, "bold", fill="#a33")
    return s.render()
