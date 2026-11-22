"""Generators for building and using the board: interactive assembly guide, firmware pin map, test points,
SPICE netlist, power tree."""
from __future__ import annotations

import copy
import json
import re

from .core import Job, addon, fmt_si
from .draw import bom, board_svg, esc, md_table, natkey, page

# ---------------------------------------------------------------------------
_ASM_ORDER = [("Small passives", lambda g: g["kind"] in ("R", "C", "FB", "L") and not g["tht"]),
              ("Diodes and LEDs", lambda g: g["kind"] in ("D", "LED") and not g["tht"]),
              ("ICs and transistors", lambda g: g["kind"] in ("U", "Q", "Y", "F") and not g["tht"]),
              ("Other surface-mount parts", lambda g: not g["tht"]),
              ("Through-hole parts, lowest first", lambda g: g["kind"] not in ("J",)),
              ("Connectors", lambda g: True)]

_ASM_CSS = """
.wrap{display:grid;grid-template-columns:minmax(300px,440px) 1fr;gap:16px;align-items:start}@media(max-width:800px){.wrap{grid-template-columns:1fr}}
.board{position:sticky;top:10px}.brd{width:100%;height:auto;border-radius:6px}
.part{cursor:pointer}.part.dim .body{fill-opacity:.25}.part.hi .body{fill:#ffd23f;fill-opacity:1;stroke:#000;stroke-width:.25}.part.done .body{fill:#58c27d}
tr.g{cursor:pointer}tr.g:hover td{background:rgba(127,127,127,.12)}tr.g.sel td{background:rgba(255,210,63,.28)}tr.g.done td{color:var(--dim);text-decoration:line-through}
h3{margin:18px 0 4px;font-size:14px;color:var(--dim);font-weight:600}.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0 2px}
.bar i{display:block;height:100%;background:var(--acc);width:0}button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
"""

_ASM_JS = """
const KEY='kipipe-asm-'+document.title;let done={};try{done=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
const rows=[...document.querySelectorAll('tr.g')],parts=[...document.querySelectorAll('.part')];
function paint(){let n=0,t=0;rows.forEach(r=>{const d=!!done[r.dataset.k];r.classList.toggle('done',d);r.querySelector('input').checked=d;
 const q=+r.dataset.qty;t+=q;if(d)n+=q;r.dataset.refs.split(' ').forEach(x=>parts.filter(p=>p.dataset.ref===x).forEach(p=>p.classList.toggle('done',d)))});
 document.querySelector('.bar i').style.width=(t?100*n/t:0)+'%';document.getElementById('prog').textContent=n+' of '+t+' parts placed';
 try{localStorage.setItem(KEY,JSON.stringify(done))}catch(e){}}
function sel(refs){rows.forEach(r=>r.classList.toggle('sel',refs&&r.dataset.refs.split(' ').some(x=>refs.includes(x))));
 parts.forEach(p=>{p.classList.toggle('hi',!!refs&&refs.includes(p.dataset.ref));p.classList.toggle('dim',!!refs&&!refs.includes(p.dataset.ref))})}
rows.forEach(r=>{r.onclick=e=>{if(e.target.tagName==='INPUT'){done[r.dataset.k]=e.target.checked;paint();return}sel(r.dataset.refs.split(' '))}});
parts.forEach(p=>p.onclick=()=>{const r=rows.find(r=>r.dataset.refs.split(' ').includes(p.dataset.ref));if(r){sel(r.dataset.refs.split(' '));r.scrollIntoView({block:'center',behavior:'smooth'})}});
document.getElementById('clr').onclick=()=>sel(null);document.getElementById('rst').onclick=()=>{done={};paint()};
document.querySelectorAll('[data-side]').forEach(b=>b.onclick=()=>document.querySelectorAll('.sidev').forEach(s=>s.hidden=s.dataset.s!==b.dataset.side));paint();
"""


@addon(id="gen.assembly", title="Assembly guide", kind="generate", group="Build and order", needs=("spec", "layout"),
       summary="An interactive page for building the board by hand: click a BOM line to light up where those parts go, tick them off as you solder.")
def gen_assembly(r):
    j = r.job
    rows = bom(j)
    left = list(rows)
    steps = []
    for title, pick in _ASM_ORDER:
        got = [g for g in left if pick(g)]
        left = [g for g in left if g not in got]
        if got:
            steps.append((title, got))
    has_back = any(p.get("back") for p in j.layout.get("parts", []))
    tbl = []
    for title, got in steps:
        tbl.append(f"<tr><td colspan='5'><h3>{esc(title)}</h3></td></tr>")
        for g in got:
            k = "_".join(g["refs"])
            tbl.append(f"<tr class='g' data-k='{esc(k)}' data-refs='{esc(' '.join(g['refs']))}' data-qty='{g['qty']}'><td><input type='checkbox' aria-label='placed'></td>"
                       f"<td>{g['qty']}×</td><td><b>{esc(g['value'] or g['mpn'])}</b><div class='dim'>{esc(g['footprint'])}</div></td>"
                       f"<td class='mono'>{esc(' '.join(g['refs']))}</td><td class='dim'>{esc(g['mpn'])}</td></tr>")
    sides = f"<div class='sidev' data-s='top'>{board_svg(j, 'top')}</div>" + (f"<div class='sidev' data-s='bottom' hidden>{board_svg(j, 'bottom')}</div>" if has_back else "")
    body = (f"<h1>Assembly: {esc(j.name.replace('_', ' '))}</h1><p class='lead'>Click a line to see where its parts go. Tick it when soldered; progress is kept in this browser.</p>"
            f"<div class='bar'><i></i></div><p class='dim' id='prog'></p><p class='noprint'><button id='clr'>Show all</button> <button id='rst'>Reset ticks</button>"
            + (" <button data-side='top'>Top side</button> <button data-side='bottom'>Bottom side</button>" if has_back else "")
            + f"</p><div class='wrap'><div><table>{''.join(tbl)}</table></div><div class='board card'>{sides}"
              "<p class='dim'>Bright pad = pin 1. Order: lowest parts first, connectors last, so the board lies flat while you work.</p></div></div>")
    r.write(f"{j.name}-assembly.html", page(f"{j.name} assembly", body, _ASM_CSS, _ASM_JS), "Assembly guide (open in a browser)")
    r.summary = f"{len(rows)} line(s) in {len(steps)} step(s)" + (", two sides" if has_back else "")


# ---------------------------------------------------------------------------
_MCU_LIBS = re.compile(r"^(MCU_|RF_Module|CPU|FPGA|DSP)", re.I)
_BOARDS = [(r"ESP32-S3", "espressif32", "esp32-s3-devkitc-1", "arduino"), (r"ESP32-C3", "espressif32", "esp32-c3-devkitm-1", "arduino"),
           (r"ESP32", "espressif32", "esp32dev", "arduino"), (r"ESP-12|ESP8266", "espressif8266", "esp12e", "arduino"),
           (r"RP2040", "raspberrypi", "pico", "arduino"), (r"ATmega328", "atmelavr", "uno", "arduino"), (r"ATmega32U4", "atmelavr", "leonardo", "arduino"),
           (r"ATtiny85", "atmelavr", "attiny85", "arduino"), (r"STM32F103C8", "ststm32", "bluepill_f103c8", "arduino")]


def _gpio(name: str):
    """'IO25' -> 25, 'GPIO5' -> 5, 'RXD0/IO3' -> 3, 'SENSOR_VP' -> 36, 'PA5' -> 'PA5', 'PB3/..' -> 'PB3'. None if not an I/O pin."""
    n = name.upper()
    fixed = {"SENSOR_VP": 36, "SENSOR_VN": 39}
    if n in fixed:
        return fixed[n]
    m = re.search(r"(?:^|[/_ ])(?:GP)?IO(\d+)", n) or re.search(r"(?:^|[/_ ])GPIO(\d+)", n) or re.search(r"(?:^|/)GP(\d+)", n)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:^|[/(~{} ])(P[A-K]\d{1,2})(?:$|[/)} ])", n)
    if m:
        return m.group(1)
    return None


def find_mcus(j: Job):
    out = []
    for ref in j.ics():
        c = j.comps[ref]
        lib = c.get("symbol", "").split(":")[0]
        ios = [p for p in j.pins(ref) if _gpio(p.name) is not None]
        if _MCU_LIBS.match(lib) and len(ios) >= 4 or len(ios) >= 8:
            out.append(ref)
    return out


def _ident(net: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", net.lstrip("/").replace("+", "P").replace("~", "N")).strip("_").upper()
    return ("PIN_" + s) if s else "PIN_UNNAMED"


@addon(id="gen.firmware", title="Firmware pin map", kind="generate", group="Test and firmware", needs=("spec", "libs"),
       summary="pins.h, pins.py and a pin table generated from the netlist, so the firmware uses the pins the board actually wired.")
def gen_firmware(r):
    j = r.job
    mcus = find_mcus(j)
    if not mcus:
        r.summary = "no microcontroller found on this board"
        return
    for ref in mcus:
        c = j.comps[ref]
        ns = j.nets_of(ref)
        rows, seen = [], {}
        for p in sorted(j.pins(ref), key=lambda p: natkey(p.number)):
            g = _gpio(p.name)
            net = ns.get(p.number)
            if g is None or not net or j.voltage(net) is not None:
                continue
            ident = _ident(net)
            if ident in seen and seen[ident] != g:
                ident = f"{ident}_{g}"
            seen[ident] = g
            to = ", ".join(f"{o}.{n}" + (f" ({j.comps[o].get('value', '')})" if j.kind(o) in ("U", "J", "SW", "LED") else "") for o, n in j.others(net, ref)[:5])
            rows.append((ident, g, p.number, p.name, net, to))
        if not rows:
            continue
        tag = f"{ref}_" if len(mcus) > 1 else ""
        w = max(len(x[0]) for x in rows) + len(tag)
        head = f"Pin map for {j.name}, {ref} ({c.get('value', '')}). Generated by kipipe from the netlist: regenerate after changing the board."
        hl = [f"// {head}", "#pragma once", ""]
        py = [f"# {head}", ""]
        for ident, g, num, name, net, to in rows:
            val = g if isinstance(g, int) else g
            hl.append(f"#define {(tag + ident).ljust(w)}  {val}   // pin {num} {name} -> {to}")
            py.append(f"{(tag + ident).ljust(w)} = {val if isinstance(g, int) else repr(g)}   # pin {num} {name} -> {to}")
        sfx = f"-{ref}" if len(mcus) > 1 else ""
        r.write(f"pins{sfx}.h", "\n".join(hl) + "\n", f"pins.h ({ref})")
        r.write(f"pins{sfx}.py", "\n".join(py) + "\n", f"pins.py ({ref})")
        md = [f"# Pin map: {ref} {c.get('value', '')}", "", md_table(["Name", "GPIO", "Pin", "Pin name", "Net", "Goes to"], rows), ""]
        r.write(f"pinmap{sfx}.md", "\n".join(md), f"Pin table ({ref})")
        text = f"{c.get('symbol', '')} {c.get('value', '')}"
        b = next((b for b in _BOARDS if re.search(b[0], text, re.I)), None)
        if b:
            r.write(f"platformio{sfx}.ini", f"; Starting point only: the board id is the closest dev board, not this PCB.\n[env:{j.name}]\nplatform = {b[1]}\n"
                    f"board = {b[2]}\nframework = {b[3]}\nmonitor_speed = 115200\n", f"platformio.ini ({ref})")
        strap = [x for x in rows if isinstance(x[1], int) and re.search(r"ESP32", text, re.I) and x[1] in (0, 2, 12, 15)]
        for x in strap:
            r.finding("note", f"{x[0]} is on boot strap GPIO{x[1]}", "Do not drive it from the other side during reset.", refs=[ref], nets=[x[4]])
        r.data[ref] = [{"name": x[0], "gpio": x[1], "pin": x[2], "net": x[4]} for x in rows]
    r.summary = ", ".join(f"{ref}: {len(v)} pin(s)" for ref, v in r.data.items()) or "no I/O pins in use"


# ---------------------------------------------------------------------------
_KEY_NETS = re.compile(r"(^|[^A-Z])(RST|RESET|NRST|EN|BOOT\d?|TXD?\d*|RXD?\d*|SDA|SCL|SWDIO|SWCLK|SWD|MISO|MOSI|SCK|CLK|CS|INT|IRQ)($|[^A-Z])", re.I)


@addon(id="gen.testpoints", title="Test point suggestions", kind="generate", group="Test and firmware", status="beta",
       options=[{"key": "footprint", "label": "Test point footprint", "type": "text", "default": "TestPoint:TestPoint_Pad_D1.5mm"},
                {"key": "max", "label": "At most", "type": "number", "default": 12}],
       summary="Which nets deserve a test point (rails, reset, buses, signals with nothing to clip onto), and a spec with them added, ready to run.")
def gen_testpoints(r):
    j = r.job
    picks = []
    have = {n for ref in j.of_kind("TP") for n in j.nets_of(ref).values()}
    for n, pins in j.nets.items():
        if n in have:
            continue
        refs = [p.split(".")[0] for p in pins]
        reach = any(j.kind(x) == "J" or re.search(r"THT|PinHeader|TerminalBlock", j.comps.get(x, {}).get("footprint", "")) for x in refs)
        v = j.voltage(n)
        if j.is_gnd(n):
            picks.append((0, n, "ground reference for the probe"))
        elif j.is_rail(n):
            if not reach:
                picks.append((1, n, "supply rail" + (f" ({v:g} V)" if v is not None else "") + " with no connector or through-hole pin to probe"))
        elif _KEY_NETS.search(n):
            picks.append((2 if not reach else 4, n, "debug signal" + ("" if not reach else "; already on a connector")))
        elif not reach and sum(1 for x in refs if j.kind(x) == "U") >= 2:
            picks.append((3, n, "runs between ICs with nothing to clip a probe onto"))
    picks.sort()
    picks = picks[:int(r.num("max", 12))]
    if not picks:
        r.summary = "no nets need a test point"
        return
    spec = copy.deepcopy(j.spec)
    nums = [int(re.sub(r"\D", "", x) or 0) for x in j.comps if re.match(r"TP\d+$", x)]
    k = max(nums or [0])
    rows = []
    for _, n, why in picks:
        k += 1
        ref = f"TP{k}"
        spec["components"].append({"ref": ref, "symbol": "Connector:TestPoint", "footprint": r.opts["footprint"], "value": n[:12], "edge": None})
        spec["nets"][n].append(f"{ref}.1")
        rows.append((ref, n, why))
    try:
        from .. import spec as specmod
        rep = specmod.validate(specmod.normalize(copy.deepcopy(spec)))
        if not rep.ok:
            r.finding("warning", "The spec with test points does not validate here", "; ".join(rep.errors[:3]),
                      fix="Change the footprint option to one your KiCad libraries have.")
    except Exception as e:
        r.finding("note", "Could not validate the new spec here", f"{type(e).__name__}: {e}")
    r.write("testpoints.md", f"# Test points for {j.name}\n\n" + md_table(["Ref", "Net", "Why"], rows) + "\n", "Suggestions")
    r.write("spec_with_testpoints.json", json.dumps(spec, indent=1), "Spec with test points (drop it into Files and run)")
    r.data["testpoints"] = [{"ref": a, "net": b, "why": c} for a, b, c in rows]
    r.summary = f"{len(rows)} test point(s) suggested"


# ---------------------------------------------------------------------------
def _node(j: Job, net):
    if net is None:
        return "NC"
    if j.is_gnd(net):
        return "0"
    return re.sub(r"[^A-Za-z0-9_]", "_", net.lstrip("/").replace("+", "P").replace("-", "N"))


@addon(id="gen.spice", title="SPICE netlist", kind="generate", group="Test and firmware", status="preview",
       summary="A starting .cir file: passives, diodes and supplies are real; every IC is a labelled placeholder for you to replace with the maker's model.")
def gen_spice(r):
    j = r.job
    out = [f"* {j.name}: SPICE netlist written by kipipe from the spec", "* Passives, diodes and sources are complete. ICs are stubs: see the end of the file.", ""]
    stubs, n_real = [], 0
    for ref in sorted(j.comps, key=natkey):
        c, k, ns = j.comps[ref], j.kind(ref), j.nets_of(ref)
        v = j.value(ref)
        nodes = [_node(j, ns.get(p)) for p in sorted(ns, key=natkey)]
        if k in ("R", "C", "L", "FB") and len(nodes) == 2:
            val = f"{v:g}" if v else {"FB": "1"}.get(k, "1")
            name = ref if ref[0] in "RCL" else ("L" if k == "FB" else k) + ref
            out.append(f"{name} {nodes[0]} {nodes[1]} {val}" + ("" if v else "   ; value not parsed from " + repr(c.get("value"))))
            n_real += 1
        elif k in ("D", "LED") and len(nodes) == 2:
            from .checks import _led_pins
            a, kk = _led_pins(j, ref)
            out.append(f"D{ref} {_node(j, a)} {_node(j, kk)} {'DLED' if k == 'LED' else 'DSCH' if 'Schottky' in c.get('symbol', '') else 'DGEN'}")
            n_real += 1
        elif k in ("J", "H", "TP", "SW", "BT"):
            out.append(f"* {ref} ({c.get('value', '')}): " + ", ".join(f"{p}={_node(j, ns[p])}" for p in sorted(ns, key=natkey)))
        else:
            pins = sorted(ns, key=natkey)
            stubs.append((ref, c, pins))
            out.append(f"* X{ref} {' '.join(_node(j, ns[p]) for p in pins)} {re.sub(r'[^A-Za-z0-9_]', '_', str(c.get('value') or ref))}   ; uncomment once the model is included")
    out.append("")
    for n in power_tree(j)["roots"]:
        v = j.voltage(n)
        out.append(f"V{_node(j, n)} {_node(j, n)} 0 DC {v:g}" if v else f"* V{_node(j, n)} {_node(j, n)} 0 DC ?   ; input: set its voltage and uncomment")
    out += ["", ".model DGEN D(Is=2.5n Rs=0.5 N=1.75)", ".model DSCH D(Is=1u Rs=0.05 N=1.05)", ".model DLED D(Is=1e-18 Rs=3 N=2)", "",
            "* ---- ICs without a model ----"]
    for ref, c, pins in stubs:
        out.append(f"* {ref} {c.get('value', '')} ({c.get('mpn') or 'no MPN'}): pins in X-line order: "
                   + ", ".join(f"{p}:{j.pin_name(ref, p) or '?'}" for p in pins))
    out += ["", ".op", ".end", ""]
    r.write(f"{j.name}.cir", "\n".join(out), "SPICE netlist")
    if stubs:
        r.finding("note", f"{len(stubs)} IC(s) need a vendor model: {', '.join(s[0] for s in stubs)}",
                  "Download the .lib or .subckt from the manufacturer, .include it, and uncomment the matching X line. Pin order is listed at the end of the file.",
                  refs=[s[0] for s in stubs])
    r.summary = f"{n_real} device(s) written, {len(stubs)} IC stub(s)"


# ---------------------------------------------------------------------------
def power_tree(j: Job) -> dict:
    """Supplies and what sits between them: {rails: [...], links: [{from, to, via, kind}], loads: {rail: [refs]}}."""
    rails = {n: j.voltage(n) for n in j.nets if j.is_rail(n) and not j.is_gnd(n)}
    links, loads = [], {}
    for ref in j.comps:
        k, ns = j.kind(ref), j.nets_of(ref)
        on = sorted({n for n in ns.values() if n in rails})
        if k == "U":
            ins = sorted({n for p, n in ns.items() if n in rails and (j.pin_type(ref, p) == "power_in" or re.search(r"^(VI|VIN|IN|VCC|VDD)\b", j.pin_name(ref, p), re.I))})
            outs = sorted({n for p, n in ns.items() if n in rails and (j.pin_type(ref, p) == "power_out" or re.search(r"^(VO|VOUT|OUT)\b", j.pin_name(ref, p), re.I))})
            outs = [n for n in outs if n not in ins or len(on) == 1 and False]
            if ins and outs:
                links += [{"from": a, "to": b, "via": ref, "kind": "regulator"} for a in ins for b in outs if a != b]
            else:
                for n in on:
                    loads.setdefault(n, []).append(ref)
        elif k in ("D", "FB", "F", "L", "Q", "SW") or (k == "R" and (j.value(ref) or 1e9) <= 10):
            if len(on) == 2:
                a, b = on
                if k == "D":
                    from .checks import _led_pins
                    an, ka = _led_pins(j, ref)
                    a, b = (an, ka) if an in rails and ka in rails else (a, b)
                links.append({"from": a, "to": b, "via": ref, "kind": {"D": "diode", "FB": "ferrite", "F": "fuse", "L": "inductor", "Q": "switch", "SW": "switch", "R": "resistor"}[k]})
        elif k == "LED":
            for n in on:
                loads.setdefault(n, []).append(ref)
    conn = {n for n, pins in j.nets.items() if n in rails and any(j.kind(p.split(".")[0]) in ("J", "BT") for p in pins)}
    reg_out = {l["to"] for l in links if l["kind"] == "regulator"}
    # passive links (diode, fuse, ferrite) have no direction of their own: point them away from the inputs
    roots = (conn - reg_out) or (set(rails) - reg_out)
    seen, todo = set(roots), list(roots)
    while todo:
        n = todo.pop(0)
        for l in links:
            if l["kind"] != "regulator" and n in (l["from"], l["to"]):
                o = l["to"] if l["from"] == n else l["from"]
                if o not in seen:
                    l["from"], l["to"] = n, o
                    seen.add(o)
                    todo.append(o)
            elif l["kind"] == "regulator" and l["from"] == n and l["to"] not in seen:
                seen.add(l["to"])
                todo.append(l["to"])
    fed = {l["to"] for l in links}
    roots = sorted(roots - fed or roots, key=lambda n: -(abs(rails[n] or 0)))
    return {"rails": rails, "links": links, "loads": loads, "roots": roots, "sources": roots}


@addon(id="gen.powertree", title="Power tree", kind="generate", group="Share", status="beta", needs=("spec", "libs"),
       summary="A diagram of where power comes in, which regulators and protection parts it passes through, and what hangs on each rail.")
def gen_powertree(r):
    j = r.job
    t = power_tree(j)
    if not t["rails"]:
        r.summary = "no supply rails recognised from the net names"
        return
    depth = {n: 0 for n in t["roots"]}
    for _ in range(len(t["rails"]) + 1):
        for l in t["links"]:
            if l["from"] in depth and depth.get(l["to"], -1) < depth[l["from"]] + 1 and l["to"] not in t["roots"]:
                depth[l["to"]] = depth[l["from"]] + 1
    for n in t["rails"]:
        depth.setdefault(n, 0)
    cols, row = {}, {}
    for d in sorted(set(depth.values())):
        ns = [n for n in depth if depth[n] == d]
        parent = lambda n: min([row.get(l["from"], 99) for l in t["links"] if l["to"] == n] or [99])     # noqa: E731
        ns.sort(key=lambda n: (parent(n), n) if d else (-(t["rails"][n] or 0), n))
        cols[d] = ns
        row.update({n: i for i, n in enumerate(ns)})
    cw, rh, bw = 250, 96, 170
    pos = {n: (30 + d * cw, 30 + i * rh) for d, ns in cols.items() for i, n in enumerate(ns)}
    amps = {n: nc.get("current_a") for nc in j.spec.get("netclasses") or [] for n in nc.get("nets") or [] if nc.get("current_a")}
    W = 60 + (max(cols) + 1) * cw
    H = 60 + max(len(v) for v in cols.values()) * rh
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="DejaVu Sans, Arial, sans-serif" font-size="12">'
         f'<rect width="{W}" height="{H}" fill="#f6f5f0"/>'
         '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="#555"/></marker></defs>']
    for l in t["links"]:
        if l["from"] in pos and l["to"] in pos:
            (x0, y0), (x1, y1) = pos[l["from"]], pos[l["to"]]
            xa, ya, xb, yb = x0 + bw, y0 + 28, x1, y1 + 28
            if x1 <= x0:
                xa, xb = x0 + bw / 2, x1 + bw / 2
                ya, yb = (y0 + 56, y1) if y1 > y0 else (y0, y1 + 56)
            o.append(f'<path d="M{xa} {ya} C{(xa + xb) / 2} {ya} {(xa + xb) / 2} {yb} {xb} {yb}" fill="none" stroke="#555" stroke-width="1.5" marker-end="url(#a)"/>'
                     f'<text x="{(xa + xb) / 2}" y="{yb - 6}" text-anchor="middle" fill="#333"><tspan font-weight="700">{esc(l["via"])}</tspan> {esc(l["kind"])}</text>')
    for n, (x, y) in pos.items():
        v = t["rails"][n]
        col = "#b3261e" if (v or 0) > 0 else "#1d4ed8" if (v or 0) < 0 else "#555"
        ld = sorted(t["loads"].get(n, []), key=natkey)
        o.append(f'<rect x="{x}" y="{y}" width="{bw}" height="56" rx="7" fill="#fff" stroke="{col}" stroke-width="{2.5 if n in t["sources"] else 1.3}"/>'
                 f'<text x="{x + 10}" y="{y + 20}" font-weight="700" font-size="14" fill="{col}">{esc(n)}</text>'
                 f'<text x="{x + bw - 10}" y="{y + 20}" text-anchor="end" fill="#555">{", ".join(x for x in ("" if v is None else f"{v:g} V", f"{amps[n]:g} A" if n in amps else "") if x)}</text>'
                 f'<text x="{x + 10}" y="{y + 42}" fill="#333">{esc(("feeds " + ", ".join(ld[:6]) + (" ..." if len(ld) > 6 else "")) if ld else ("input" if n in t["sources"] else ""))[:34]}</text>')
    o.append("</svg>")
    r.write(f"{j.name}-power-tree.svg", "".join(o), "Power tree")
    r.write("power-tree.json", json.dumps(t, indent=1), "Power tree (data)")
    lone = [n for n in t["rails"] if n not in t["sources"] and not any(l["to"] == n for l in t["links"])]
    for n in lone:
        r.finding("note", f"{n} has no visible source in the tree", "Nothing recognised as a regulator, diode or switch feeds it, and it is not on a connector.", nets=[n])
    r.summary = f"{len(t['rails'])} rail(s), {len(t['links'])} link(s), input: {', '.join(t['sources']) or 'not recognised'}"
    r.data = {"rails": {k: v for k, v in t["rails"].items()}, "links": t["links"]}
