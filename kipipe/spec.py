"""Design spec: the JSON contract between the model (or the user) and the pipeline.

The model never writes KiCad files directly. It writes this spec, which is
validated against the installed KiCad libraries; errors go back to the model
for correction. Everything downstream is deterministic.
"""
from __future__ import annotations

import json
import re

from . import kicad_lib as kl

SPEC_DOC = r"""
{
  "name": "short_snake_case_project_name",
  "description": "one paragraph: what the circuit does",
  "board": {
    "width_mm": null,            // null = auto-size from parts
    "height_mm": null,
    "layers": 2,                 // 2 or 4
    "thickness_mm": 1.6,
    "copper_oz": 1,
    "mounting_holes": "M3",      // "M2", "M2.5", "M3" or "none"
    "shape": "rect",             // rect | rounded | circle | plus | hexagon | octagon | {"polygon": [[x, y], ...]} (mm);
                                 // set only when the user asks for a shape; width/height are its bounding box
    "fab": "jlcpcb"              // jlcpcb | oshpark | pcbway | generic
  },
  "components": [
    {
      "ref": "U1",
      "symbol": "Regulator_Linear:AMS1117-3.3",              // exact KiCad library symbol "Lib:Name"
      "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2",   // exact KiCad footprint "Lib:Name"
      "value": "AMS1117-3.3",
      "mpn": "AMS1117-3.3",      // optional but wanted for ICs, modules, connectors: exact manufacturer part number
      "edge": null               // optional: "left"|"right"|"top"|"bottom" for connectors/switches
    }
  ],
  "nets": {
    "VIN":  ["J1.1", "U1.3", "C1.1"],     // "REF.PIN_NUMBER" (pin NUMBER, not name)
    "+3V3": ["U1.2", "C2.1"],
    "GND":  ["J1.2", "U1.1", "C1.2", "C2.2"]
  },
  "no_connect": ["U2.7"],                 // pins intentionally left unconnected
  "netclasses": [
    {"name": "Power", "track_mm": 0.5, "clearance_mm": 0.2, "current_a": 1.0, "nets": ["VIN", "+3V3", "GND"]}
  ],
  "floorplan": {
    // layout intent; the placer enforces these as hard constraints
    "grids": [      // regular arrays (LED matrix, keypad, button row): refs row-major
      {"refs": ["D1", "D2", "D3", "D4"], "cols": 2, "pitch_mm": null},
      {"refs": ["SW1", "SW2", "SW3"], "cols": 3, "pitch_mm": 12}
    ],
    "regions": [    // functional blocks confined to an area of the board
      // area: top|bottom|left|right|center|top-left|top-right|bottom-left|bottom-right or [x0,y0,x1,y1] fractions
      {"refs": ["U1", "C1", "C2", "L1"], "area": "top-left"}
    ],
    "near": [       // hard proximity, pad to pad
      {"ref": "C3", "to": "U2.16", "max_mm": 2.0}
    ],
    "keepouts": [   // no other parts within mm of this part's courtyard
      {"ref": "U4", "mm": 5}
    ]
  },
  "rf": [
    // optional controlled-impedance nets; width is computed from the stackup
    {"net": "RF_OUT", "impedance_ohm": 50, "from": "U2.1", "to": "J2.1"}
  ],
  "derive": {
    // ONLY when the request says to match or stack on another board of the project (listed under PROJECT). Omit otherwise.
    "board": "power_board",      // that board's name
    "outline": true,             // same size, shape and mounting holes: leave width_mm, height_mm and shape out of "board"
    "mates": [                   // connectors of this board that plug onto connectors of that board when stacked
      // side "bottom": this board sits above, so this connector goes on its underside directly over theirs.
      // side "top": same spot on the top side (pass-through stacking headers, the same connector on every board).
      // Give this connector the mating part with the SAME pad pattern (for a PinHeader_1x06 use PinSocket_1x06 of the same pitch).
      // Wire it pin for pin like theirs: pin k here carries what pin k carries there. Mirroring on the underside is handled by code.
      {"ref": "J1", "with": "J2", "side": "bottom"}
    ]
  },
  "rails": {
    // every supply net: its voltage (negative for negative rails) and the current it must carry. The checks compare
    // these with regulator outputs, part ratings and dropout, so include input rails whose names do not state a voltage
    "VBAT": {"volts": 9.0, "amps": 0.3}, "+5V": {"volts": 5.0, "amps": 0.3}
  },
  "assumptions": [
    // every choice you made because the request did not say: supply voltage, load current, connector type, package size.
    // one short sentence each; an empty list if nothing was assumed
    "Assumed a 5 V supply from USB because no power source was given."
  ],
  "requirements": [
    // one entry per thing the request asked for, in the user's words, and which parts or nets deliver it
    {"text": "status LED", "met_by": ["D1", "R3"], "status": "met", "note": ""}     // status: met | partly | not met
  ],
  "rationale": {
    // shown to the user in the Reasoning tab; plain sentences, specific numbers, no filler
    "design": "how the circuit works and the key calculations (e.g. Vout = Vin * R3 / (R1+R2+R3) = 1.51 V)",
    "parts": {"U1": "why this part and value: ratings, margins, package", "C1": "..."},
    "placement": "how the board is laid out and why: what goes where, what is kept apart, which edges"
  }
}
""".strip()

REF_RE = re.compile(r"^[A-Z]+[0-9]+$")
NET_RE = re.compile(r"^[A-Za-z0-9_+\-./~{}]+$")


class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        out = [f"ERROR: {e}" for e in self.errors] + [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(out) or "OK"


def load(text: str) -> dict:
    """Parse spec JSON, tolerating a fenced code block or // comments."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            text = text[s:e + 1]
    text = re.sub(r'(?m)(^|[^:"\\])//[^\n]*', r"\1", text)
    return json.loads(text)


def _relocated(sym_id: str):
    """Libraries get reorganised between KiCad versions (Device:Q_NMOS_GSD became Transistor_FET:Q_NMOS_GSD).
    If exactly one other library has a symbol of the same name, that is the one meant."""
    try:
        from . import preflight
        name = sym_id.split(":", 1)[1]
        hits = [x for x in preflight.symbol_index().get(preflight._n(name), []) if x.split(":", 1)[1] == name and x != sym_id]
        libs_ = {x.split(":")[0] for x in hits}
        if len(libs_) == 1:
            return hits[0]
        pref = [x for x in hits if x.split(":")[0] in ("Device", "Transistor_FET", "Transistor_BJT", "Diode", "Connector", "Connector_Generic")]
        return pref[0] if len(pref) == 1 else None
    except Exception:
        return None


def normalize(spec: dict) -> dict:
    if spec.get("blocks"):                  # verified sub-circuits: expanded by code before anything else sees the spec
        from .addons import blocks as _blocks
        _blocks.expand_spec(spec)
    spec.setdefault("name", "kipipe_board")
    spec["name"] = re.sub(r"[^A-Za-z0-9_\-]", "_", spec["name"]) or "kipipe_board"
    b = spec.setdefault("board", {})
    b.setdefault("width_mm", None)
    b.setdefault("height_mm", None)
    b.setdefault("layers", 2)
    b.setdefault("thickness_mm", 1.6)
    b.setdefault("copper_oz", 1)
    b.setdefault("mounting_holes", "none")
    b.setdefault("fab", "jlcpcb")
    spec.setdefault("components", [])
    spec.setdefault("nets", {})
    spec.setdefault("no_connect", [])
    spec.setdefault("netclasses", [])
    spec.setdefault("rf", [])
    fp = spec.setdefault("floorplan", {})
    for k in ("grids", "regions", "near", "keepouts"):
        fp.setdefault(k, [])
    for g in spec.pop("groups", []) or []:          # older specs
        if g not in fp["grids"]:
            fp["grids"].append({k: v for k, v in g.items() if k != "type"})
    if isinstance(spec["nets"], list):  # accept [{"name":..,"pins":[..]}]
        spec["nets"] = {n["name"]: n["pins"] for n in spec["nets"]}
    r = spec.get("rationale")
    if not isinstance(r, dict):
        spec["rationale"] = {"design": str(r)} if r else {}
    spec["rationale"].setdefault("parts", {})
    if not isinstance(spec["rationale"]["parts"], dict):
        spec["rationale"]["parts"] = {}
    for c in spec["components"]:
        c.setdefault("value", c.get("symbol", "").split(":")[-1])
        c.setdefault("edge", None)
    return spec


def _split(pin: str):
    ref, _, num = pin.partition(".")
    return ref.strip(), num.strip()


def validate(spec: dict) -> Report:
    r = Report()
    spec = normalize(spec)
    for e in spec.pop("_block_errors", None) or []:
        r.errors.append(e)
    if isinstance(spec.get("derive"), dict):        # outline and stacked connectors taken from another board of the project
        from . import derive as _derive
        try:
            _derive.apply(spec, r)
        except Exception as e:
            r.errors.append(f"derive: could not be applied ({type(e).__name__}: {e})")
    if spec.get("calc"):                            # values computed by code from the stated goals
        from . import calc as _calc
        _calc.apply(spec, r)
    comps = {}
    sym_pins: dict[str, dict[str, kl.Pin]] = {}
    fp_pads: dict[str, set[str]] = {}

    if not spec["components"]:
        r.errors.append("no components")
    for c in spec["components"]:
        ref = c.get("ref", "")
        if not REF_RE.match(ref):
            r.errors.append(f"bad reference designator {ref!r} (expected e.g. R1, U3, J2)")
            continue
        if ref in comps:
            r.errors.append(f"duplicate reference {ref}")
            continue
        comps[ref] = c
        sym_id = c.get("symbol", "")
        if sym_id.startswith("power:"):
            r.errors.append(f"{ref}: do not use power symbols as components; use a net name instead")
            continue
        try:
            try:
                sym = kl.get_symbol(sym_id)
            except KeyError:
                moved = _relocated(sym_id)
                if not moved:
                    raise
                r.warnings.append(f"{ref}: symbol {sym_id} is not in this KiCad's libraries; using {moved} (same name, other library)")
                c["symbol"] = sym_id = moved
                sym = kl.get_symbol(sym_id)
            sym_pins[ref] = {p.number: p for p in kl.symbol_pins(sym) if p.style in (0, 1)}
            groups = {}
            for p in sym_pins[ref].values():
                if p.raw:
                    groups.setdefault(p.raw, []).append(p.number)
            for raw, members in groups.items():     # KiCad 10: several pads on one drawn pin are one connection
                on = [n for n, pl in (spec.get("nets") or {}).items() if any(f"{ref}.{m}" in pl for m in members)]
                if len(on) == 1:
                    spec["nets"][on[0]] += [f"{ref}.{m}" for m in members if f"{ref}.{m}" not in spec["nets"][on[0]]]
                elif len(on) > 1:
                    r.errors.append(f"{ref}: pads {raw} are one pin on the symbol and cannot be on different nets ({', '.join(on)})")
        except KeyError:
            sug = kl.suggest_symbols(sym_id)
            r.errors.append(f"{ref}: symbol {sym_id!r} not found in the KiCad libraries"
                            + (f"; close matches: {', '.join(sug)}" if sug else ""))
        fp = c.get("footprint", "")
        if not fp or ":" not in fp:
            r.errors.append(f"{ref}: footprint missing or not in 'Lib:Name' form")
        elif kl.footprint_path(fp) is None:
            sug = kl.suggest_footprints(fp)
            r.errors.append(f"{ref}: footprint {fp!r} not found"
                            + (f"; close matches: {', '.join(sug)}" if sug else ""))
        else:
            fp_pads[ref] = kl.footprint_pads(fp)
        if c.get("edge") not in (None, "left", "right", "top", "bottom"):
            r.errors.append(f"{ref}: edge must be left/right/top/bottom or null")

    # symbol pins vs footprint pads
    for ref, pins in sym_pins.items():
        if ref in fp_pads:
            missing = sorted(set(pins) - fp_pads[ref], key=_natkey)
            if missing:
                r.errors.append(f"{ref}: symbol pins {missing} have no matching pad in footprint "
                                f"{comps[ref]['footprint']} (pads: {sorted(fp_pads[ref], key=_natkey)}); pick a footprint with matching pin numbering")

    used: dict[str, str] = {}
    for net, pins in spec["nets"].items():
        if not NET_RE.match(net):
            r.errors.append(f"net name {net!r} has illegal characters (use A-Z 0-9 _ + - .)")
        if len(pins) < 2:
            r.warnings.append(f"net {net} has only {len(pins)} connection(s)")
        for p in pins:
            ref, num = _split(p)
            if ref not in comps:
                r.errors.append(f"net {net}: unknown component {ref!r} in {p!r}")
                continue
            if ref in sym_pins and num not in sym_pins[ref]:
                names = ", ".join(f"{k}={v.name}" for k, v in sorted(sym_pins[ref].items(), key=lambda kv: _natkey(kv[0])))
                r.errors.append(f"net {net}: {ref} has no pin number {num!r}. Pins are: {names}")
                continue
            if p in used and used[p] != net:
                r.errors.append(f"pin {p} is in two nets: {used[p]} and {net}")
            used[p] = net

    nc = set(spec["no_connect"])
    for p in nc:
        if p in used:
            r.errors.append(f"pin {p} is marked no_connect but is in net {used[p]}")
    for ref, pins in sym_pins.items():
        for num, pin in pins.items():
            key = f"{ref}.{num}"
            if key not in used and key not in nc and not pin.hidden:
                r.warnings.append(f"pin {key} ({pin.name}, {pin.etype}) is unconnected and not marked no_connect")

    for nc_ in spec["netclasses"]:
        for n in nc_.get("nets", []):
            if n not in spec["nets"]:
                r.errors.append(f"netclass {nc_.get('name')}: unknown net {n}")
    for rf in spec["rf"]:
        if rf.get("net") not in spec["nets"]:
            r.errors.append(f"rf: unknown net {rf.get('net')}")
        for k in ("from", "to"):
            if rf.get(k) and rf[k] not in spec["nets"].get(rf.get("net"), []):
                r.errors.append(f"rf {rf.get('net')}: {k} pin {rf[k]} is not on that net")
    _validate_pad_clearance(spec, comps, r)
    _validate_floorplan(spec, comps, sym_pins, r)
    try:
        from . import outline as _ol
        sh = _ol.parse(spec["board"])
        if sh["type"] not in _ol.SHAPES:
            r.errors.append(f"board.shape {sh['type']!r} unknown; use one of {', '.join(_ol.SHAPES)}")
        elif sh["type"] == "polygon" and len(sh.get("points") or []) < 3:
            r.errors.append("board.shape polygon needs at least 3 points")
    except Exception as e:
        r.errors.append(f"board.shape: {e}")
    if spec["board"]["layers"] not in (2, 4):
        r.errors.append("board.layers must be 2 or 4")
    return r


def pad_gaps(spec: dict) -> dict:
    """{net: smallest gap (mm) between one of its pads and a pad of another net on the same footprint}.
    Unconnected pads count as their own net, as in KiCad. No clearance rule can be stricter than this."""
    pin_net = {p: net for net, pins in spec["nets"].items() for p in pins}
    out = {}
    for c in spec.get("components", []):
        rects = kl.footprint_pad_rects(c.get("footprint", ""))
        nets = [pin_net.get(f"{c['ref']}.{x[0]}", f"~{c['ref']}.{x[0]}") for x in rects]
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                if nets[i] == nets[j] or rects[i][0] == rects[j][0]:
                    continue
                g = kl.rect_gap(rects[i][1:], rects[j][1:])
                for n in (nets[i], nets[j]):
                    if not n.startswith("~"):
                        out[n] = min(out.get(n, 9e9), g)
    return out


def _validate_pad_clearance(spec: dict, comps: dict, r: Report):
    """A part whose own pads sit closer than its nets' clearance can never pass DRC, whatever the router does."""
    cl = {}
    for nc_ in spec.get("netclasses") or []:
        try:
            c = float(nc_.get("clearance_mm") or 0)
        except (TypeError, ValueError):
            continue
        for n in nc_.get("nets", []):
            cl[n] = max(cl.get(n, 0.0), c)
    if not any(v >= 0.5 for v in cl.values()):
        return
    pin_net = {p: net for net, pins in spec["nets"].items() for p in pins}
    for ref, c in comps.items():
        # unconnected pads count too: KiCad gives each one its own net
        rects = kl.footprint_pad_rects(c.get("footprint", ""))
        worst = None
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                na = pin_net.get(f"{ref}.{rects[i][0]}", f"(unconnected pad {rects[i][0]})")
                nb = pin_net.get(f"{ref}.{rects[j][0]}", f"(unconnected pad {rects[j][0]})")
                if na == nb or rects[i][0] == rects[j][0]:
                    continue
                need = max(cl.get(na, 0.0), cl.get(nb, 0.0))
                if need < 0.5:
                    continue
                gap = kl.rect_gap(rects[i][1:], rects[j][1:])
                if gap + 0.01 < need and (worst is None or need - gap > worst[0]):
                    worst = (need - gap, gap, need, na, nb)
        if worst:
            _, gap, need, na, nb = worst
            r.errors.append(
                f"{ref} ({c['footprint'].split(':')[-1]}): its own pads on {na} and {nb} are only {gap:.2f} mm apart but "
                f"the net classes need {need:.2f} mm, so DRC fails whatever the routing. Either use a larger package "
                f"whose pads are at least {need:.1f} mm apart (e.g. 2512 resistors, 1812/2220 or through-hole capacitors, "
                f"SMC or DO-41/DO-201 diodes, connectors with a wider pitch; an unused pin in between doesn't help, its pad still counts), or, if only a small voltage appears across this part (one multiplier stage), "
                f"put those nets in a class sized for that voltage instead.")


AREAS = {"top": (0, 0, 1, 0.45), "bottom": (0, 0.55, 1, 1), "left": (0, 0, 0.45, 1), "right": (0.55, 0, 1, 1),
         "center": (0.2, 0.2, 0.8, 0.8), "top-left": (0, 0, 0.55, 0.55), "top-right": (0.45, 0, 1, 0.55),
         "bottom-left": (0, 0.45, 0.55, 1), "bottom-right": (0.45, 0.45, 1, 1)}


def area_rect(area):
    if isinstance(area, str):
        return AREAS.get(area.lower().replace("_", "-").replace(" ", "-"))
    if isinstance(area, (list, tuple)) and len(area) == 4:
        x0, y0, x1, y1 = (float(v) for v in area)
        if 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1:
            return (x0, y0, x1, y1)
    return None


def _validate_floorplan(spec, comps, sym_pins, r):
    fp = spec["floorplan"]
    seen = set()
    for i, g in enumerate(fp["grids"]):
        refs = g.get("refs", [])
        bad = [x for x in refs if x not in comps]
        if bad:
            r.errors.append(f"floorplan.grids[{i}]: unknown refs {bad}")
        if not isinstance(g.get("cols"), int) or g["cols"] < 1 or (refs and len(refs) % g["cols"]):
            r.errors.append(f"floorplan.grids[{i}]: cols must be an integer dividing the number of refs ({len(refs)})")
        dup = seen & set(refs)
        if dup:
            r.errors.append(f"floorplan.grids[{i}]: refs already in another grid: {sorted(dup)}")
        seen |= set(refs)
        fps = {comps[x]["footprint"] for x in refs if x in comps}
        if len(fps) > 1:
            r.warnings.append(f"floorplan.grids[{i}]: mixed footprints; pitch is taken from the first")
        edges = {comps[x].get("edge") for x in refs if x in comps}
        if len(edges) > 1:
            r.warnings.append(f"floorplan.grids[{i}]: members have different 'edge' values; the block is placed freely. "
                              "Give all members the same edge to put the block on that edge")
    in_region = {}
    for i, g in enumerate(fp["regions"]):
        if area_rect(g.get("area")) is None:
            r.errors.append(f"floorplan.regions[{i}]: bad area {g.get('area')!r}; use {', '.join(AREAS)} or [x0,y0,x1,y1] fractions")
        for x in g.get("refs", []):
            if x not in comps:
                r.errors.append(f"floorplan.regions[{i}]: unknown ref {x}")
            elif x in in_region:
                r.errors.append(f"floorplan.regions: {x} is in two regions")
            in_region[x] = i
    for i, n in enumerate(fp["near"]):
        ref, to = n.get("ref"), str(n.get("to", ""))
        tref, _, tpin = to.partition(".")
        if ref not in comps:
            r.errors.append(f"floorplan.near[{i}]: unknown ref {ref}")
        if tref not in comps:
            r.errors.append(f"floorplan.near[{i}]: unknown target {to}")
        elif tpin and tref in sym_pins and tpin not in sym_pins[tref]:
            r.errors.append(f"floorplan.near[{i}]: {tref} has no pin {tpin}")
        try:
            if float(n.get("max_mm", 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            r.errors.append(f"floorplan.near[{i}]: max_mm must be a positive number")
    for i, k in enumerate(fp["keepouts"]):
        if k.get("ref") not in comps:
            r.errors.append(f"floorplan.keepouts[{i}]: unknown ref {k.get('ref')}")


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def connection_list(spec: dict) -> str:
    """Plain-text connection list, one net per line."""
    comps = {c["ref"]: c for c in spec["components"]}
    lines = ["COMPONENTS"]
    for c in sorted(spec["components"], key=lambda c: _natkey(c["ref"])):
        lines.append(f"  {c['ref']:<6} {c['value']:<20} {c['symbol']}  [{c['footprint']}]")
    lines.append("")
    lines.append("NETS")
    for net in sorted(spec["nets"], key=_natkey):
        pins = sorted(spec["nets"][net], key=_natkey)
        lines.append(f"  {net:<14} " + ", ".join(pins))
    if spec.get("no_connect"):
        lines.append("")
        lines.append("NO CONNECT  " + ", ".join(sorted(spec["no_connect"], key=_natkey)))
    del comps
    return "\n".join(lines) + "\n"
