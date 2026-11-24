"""Generators that document a finished job: BOM and placement files, order sheet, bring-up plan, pinout card,
project page, shareable report, and a diff against another job."""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import shutil
from pathlib import Path

from .core import Job, addon, fmt_si, run_checks
from .draw import bom, board_svg, csv_line, esc, md_table, natkey, page, pcb_stats

GROUP = "Build and order"


# ---------------------------------------------------------------------------
@addon(id="gen.bom", title="BOM and placement files", kind="generate", group=GROUP,
       summary="Grouped bill of materials (CSV), plus BOM and placement files in the column layout assembly houses ask for.")
def gen_bom(r):
    j = r.job
    rows = bom(j)
    lines = [csv_line(["Qty", "References", "Value", "Footprint", "MPN", "Type", "Mount"])]
    lines += [csv_line([g["qty"], " ".join(g["refs"]), g["value"], g["footprint"], g["mpn"], g["kind_name"],
                        "through-hole" if g["tht"] else "SMD"]) for g in rows]
    r.write(f"{j.name}-bom.csv", "\n".join(lines) + "\n", "BOM (grouped)")
    jl = [csv_line(["Comment", "Designator", "Footprint", "LCSC Part #"])]
    jl += [csv_line([g["value"] or g["mpn"], ",".join(g["refs"]), g["footprint"], ""]) for g in rows]
    r.write(f"{j.name}-bom-assembly.csv", "\n".join(jl) + "\n", "BOM (assembly house columns)")
    pos = j.first("fab/*-pos.csv")
    cpl = [csv_line(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])]
    n = 0
    if pos:
        for row in csv.DictReader(io.StringIO(pos.read_text())):
            if row.get("Ref", "").startswith("H") and row["Ref"] not in j.comps:
                continue
            cpl.append(csv_line([row["Ref"], f"{float(row['PosX']):.3f}mm", f"{float(row['PosY']):.3f}mm",
                                 "Top" if row.get("Side", "top") == "top" else "Bottom", f"{float(row['Rot']):g}"]))
            n += 1
        src = "KiCad's position file"
    else:
        h = (j.size() or (0, 0))[1]
        for p in j.layout.get("parts", []):
            if p["ref"] in j.comps:
                cpl.append(csv_line([p["ref"], f"{p['x']:.3f}mm", f"{h - p['y']:.3f}mm", "Bottom" if p.get("back") else "Top", p.get("rot", 0)]))
                n += 1
        src = "the placement (no fab output yet, so the origin is the board's top-left corner)"
    if n:
        r.write(f"{j.name}-cpl.csv", "\n".join(cpl) + "\n", "Placement (CPL)")
    no_mpn = [g for g in rows if not g["mpn"] and g["kind"] in ("U", "Q", "D", "J", "Y", "L")]
    if no_mpn:
        r.finding("note", f"{sum(g['qty'] for g in no_mpn)} part(s) have no manufacturer part number",
                  "An assembly house cannot source these from the value alone: " + ", ".join(g["refs"][0] + (" ..." if g["qty"] > 1 else "") for g in no_mpn[:10]),
                  refs=[x for g in no_mpn for x in g["refs"]])
    if n:
        r.finding("note", "Rotations are KiCad's", f"Placement comes from {src}. Assembly houses define the zero rotation of some packages "
                  "differently (SOT-23, tantalums, some ICs): check their placement preview before paying.")
    r.summary = f"{len(rows)} BOM line(s), {sum(g['qty'] for g in rows)} part(s)" + (f", {n} placements" if n else "")
    r.data["bom"] = rows


# ---------------------------------------------------------------------------
@addon(id="gen.fabsheet", title="Order sheet", kind="generate", group=GROUP,
       summary="The numbers a board order form asks for (size, layers, smallest track and hole, pad counts), read from the board itself.")
def gen_fabsheet(r):
    j = r.job
    st = pcb_stats(j)
    size = j.size()
    b = j.board
    rows = bom(j)
    smd = sum(g["qty"] for g in rows if not g["tht"])
    tht = sum(g["qty"] for g in rows if g["tht"])
    mm = lambda v: "n/a" if v is None else f"{v:g} mm"      # noqa: E731
    facts = [("Board size", f"{size[0]:.1f} × {size[1]:.1f} mm" if size else "not placed yet"),
             ("Layers", b.get("layers", 2)), ("Thickness", f"{b.get('thickness_mm', 1.6)} mm"), ("Copper", f"{b.get('copper_oz', 1)} oz"),
             ("Design rules used", b.get("fab", "generic")),
             ("Smallest track", mm(st.get("min_track_mm"))), ("Smallest via (pad / hole)", f"{mm(st.get('min_via_size_mm'))} / {mm(st.get('min_via_drill_mm'))}"),
             ("Smallest hole", mm(st.get("min_drill_mm"))), ("Largest hole", mm(st.get("max_drill_mm"))),
             ("Vias", st.get("vias", "n/a")), ("SMD pads", st.get("smd_pads", "n/a")), ("Plated through-hole pads", st.get("tht_pads", "n/a")),
             ("Unplated holes", st.get("npth", "n/a")),
             ("Parts", f"{smd + tht} ({smd} SMD, {tht} through-hole), {len(rows)} unique"),
             ("Assembly sides", "both" if st.get("parts_on_back") else "top only")]
    files = j.result.get("files") or {}
    md = [f"# Order sheet: {j.name}", "", md_table(["Item", "Value"], facts), "",
          "## Files to upload", "",
          f"- Gerbers and drill: `{files.get('gerbers_zip', 'not exported (the run stopped before fab output)')}`",
          "- For assembly: run the *BOM and placement files* add-on.", "",
          "## Before ordering", "",
          "- Open the gerber zip in the fab's viewer and look at every layer.",
          "- Prices and lead times are not shown here: they change daily and differ per fab.", ""]
    drc = (j.result.get("metrics") or {}).get("drc")
    if drc:
        md.insert(4, f"DRC at export: {drc.get('errors', '?')} errors, {drc.get('warnings', '?')} warnings, {drc.get('unconnected', '?')} unconnected.\n")
        if drc.get("errors") or drc.get("unconnected"):
            r.finding("warning", "The board has DRC errors or unconnected nets", "Fix them before ordering.")
    if size and (size[0] > 100 or size[1] > 100):
        r.finding("note", "Larger than 100 × 100 mm", "Most prototype price tiers end at 100 × 100 mm.")
    r.write(f"{j.name}-order-sheet.md", "\n".join(md), "Order sheet")
    r.data["facts"] = dict((k, str(v)) for k, v in facts)
    r.summary = f"{facts[0][1]}, {b.get('layers', 2)} layers, smallest track {mm(st.get('min_track_mm'))}, smallest hole {mm(st.get('min_drill_mm'))}"


# ---------------------------------------------------------------------------
def _rails(j: Job):
    """[(net, volts, source refs)] for supply nets, inputs first."""
    out = []
    for n, pins in j.nets.items():
        v = j.voltage(n)
        if v is None or j.is_gnd(n):
            continue
        src = sorted({p.split(".")[0] for p in pins if j.kind(p.split(".")[0]) in ("J", "BT")}, key=natkey)
        out.append((n, v, src))
    out.sort(key=lambda x: (not x[2], -abs(x[1])))
    return out


def _probe(j: Job, net):
    """A good place to put a probe on a net: a connector or through-hole pin first, then the largest pad."""
    best = None
    for ref, num in j.others(net):
        k = j.kind(ref)
        score = {"TP": 0, "J": 1, "C": 3, "R": 4}.get(k, 5)
        if best is None or score < best[0]:
            best = (score, f"{ref} pin {num}")
    return best[1] if best else "?"


@addon(id="gen.bringup", title="Bring-up plan", kind="generate", group="Test and firmware", needs=("spec",),
       summary="A step-by-step first power-on checklist for this board: what to measure, where, and what to expect.")
def gen_bringup(r):
    j = r.job
    rails = _rails(j)
    gnd = next((n for n in j.nets if j.is_gnd(n)), "GND")
    md = [f"# Bring-up plan: {j.name}", "", "Work top to bottom. Stop at the first step that fails; do not go on with a wrong rail.", ""]
    md += ["## 1. Before power", "", "- [ ] Look over every joint under magnification: bridges on ICs, tombstoned passives, missing parts."]
    pol = []
    for ref in j.comps:
        c = j.comps[ref]
        k = j.kind(ref)
        if k in ("D", "LED") or re.search(r"Polari", c.get("symbol", "")) or k == "U":
            pol.append(ref)
    if pol:
        md.append(f"- [ ] Orientation of polarised parts and ICs (pin 1 marks): {', '.join(sorted(pol, key=natkey))}.")
    for n, v, _ in rails:
        md.append(f"- [ ] Resistance from **{n}** to {gnd} at {_probe(j, n)}: not a short (more than about 50 Ω; it may climb as capacitors charge).")
    md += ["", "## 2. First power", ""]
    from .gen_build import power_tree
    tree = power_tree(j)
    inputs = [n for n in tree["roots"] if any(j.kind(ref) in ("J", "BT") for ref, _ in j.others(n))]
    amps = max([float(nc.get("current_a") or 0) for nc in j.spec.get("netclasses") or []] or [0])
    limit = f"{min(max(amps * 1.2, 0.05), 3):.2g} A" if amps else "100 mA to start"
    if inputs:
        for n in inputs:
            v = j.voltage(n)
            pins = ", ".join(f"{ref} pin {num}" for ref, num in j.others(n) if j.kind(ref) in ("J", "BT"))
            volts = (f"**{abs(v):g} V**" + (" (negative rail: supply + to ground)" if v < 0 else "")) if v else \
                "the designed input voltage (the net name does not state it; see the description)"
            md.append(f"- [ ] Bench supply at {volts}, current limit {limit}, on {pins} ({n}).")
        gp = ", ".join(f"{ref} pin {num}" for ref, num in j.others(gnd) if j.kind(ref) in ("J", "BT") and any(ref == r2 for n in inputs for r2, _ in j.others(n)))
        md.append(f"- [ ] Supply ground on {gp or gnd}.")
    else:
        md.append(f"- [ ] Connect the supply with a current limit of {limit}. (No input rail could be recognised.)")
    md += ["- [ ] Current settles within a second and stays well under the limit. If the supply goes into limiting, switch off and find the short.",
           "- [ ] Nothing gets hot to the touch after 30 seconds.", "", "## 3. Rails", "",
           md_table(["Rail", "Expect", "Measure at", "OK"], [(n, f"{v:+g} V" if v < 0 else f"{v:g} V", _probe(j, n), "[ ]") for n, v, _ in rails]) if rails
           else "No net name states a voltage, so there is no table. Measure each IC's supply pins against its datasheet.", ""]
    ics = j.ics()
    if ics:
        md += ["## 4. ICs", ""]
        for ref in sorted(ics, key=natkey):
            c = j.comps[ref]
            sup = sorted({(n, j.voltage(n)) for num, n in j.nets_of(ref).items() if j.voltage(n) not in (None, 0.0)})
            md.append(f"- [ ] **{ref}** ({c.get('value', '')}): " + (", ".join(f"{n} = {v:g} V" for n, v in sup) + " at its own pins" if sup else "supply pins at the expected voltage")
                      + ".")
        md.append("")
    md += ["## 5. Function", ""]
    for ref in sorted(j.of_kind("LED"), key=natkey):
        ctl = set()
        for n in j.nets_of(ref).values():
            if j.is_rail(n):
                continue
            for o, _ in j.others(n, ref):
                if j.kind(o) in ("U", "Q"):
                    ctl.add(o)
                elif j.kind(o) == "R":
                    far = [x for x in (j.two_pin(o) or ()) if x != n and not j.is_rail(x)]
                    ctl |= {q for x in far for q, _ in j.others(x, o) if j.kind(q) in ("U", "Q")}
        md.append(f"- [ ] {ref} ({j.comps[ref].get('value', 'LED')}): " + (f"lights when {', '.join(sorted(ctl))} drives it." if ctl else "lit as soon as its rail is up."))
    for ref in sorted(j.of_kind("SW"), key=natkey):
        ns = [n for n in j.nets_of(ref).values() if j.voltage(n) is None]
        md.append(f"- [ ] {ref} ({j.comps[ref].get('value', 'switch')}): pressing it changes {', '.join(sorted(set(ns))) or 'its net'}.")
    uart = [n for n in j.nets if re.search(r"(^|_)(TX|RX)D?\d*($|_)", n, re.I)]
    if uart:
        md.append(f"- [ ] Serial: adapter RX to the board's TX and TX to RX ({', '.join(uart)}), grounds joined, 3.3 V logic unless the supply says otherwise.")
    md += ["", "## Connectors", ""]
    for ref in sorted(j.of_kind("J"), key=natkey):
        ns = j.nets_of(ref)
        md += [f"**{ref}** {j.comps[ref].get('value', '')}", "", md_table(["Pin", "Net"], [(n, ns[n]) for n in sorted(ns, key=natkey)]), ""]
    r.write(f"{j.name}-bringup.md", "\n".join(md), "Bring-up plan")
    r.summary = f"{len(rails)} rail(s), {len(ics)} IC(s), {len(j.of_kind('J'))} connector(s)"


# ---------------------------------------------------------------------------
@addon(id="gen.pinout", title="Pinout card", kind="generate", group="Test and firmware",
       summary="A printable card of every connector with its pins and signal names.")
def gen_pinout(r):
    j = r.job
    conns = sorted(j.of_kind("J"), key=natkey)
    if not conns:
        r.summary = "no connectors"
        return
    col_w, row_h, pad = 250, 22, 18
    x = y = pad
    colh = 0
    blocks, max_h = [], 0
    for ref in conns:
        ns = j.nets_of(ref)
        nums = sorted(ns, key=natkey)
        hgt = 34 + row_h * len(nums)
        if y + hgt > 900 and y > pad:
            x += col_w + pad
            y = pad
        g = [f'<g transform="translate({x},{y})"><rect width="{col_w}" height="{hgt}" rx="6" fill="#fff" stroke="#222"/>',
             f'<text x="10" y="21" font-weight="700" font-size="14">{esc(ref)}</text>',
             f'<text x="{col_w - 10}" y="21" text-anchor="end" font-size="12" fill="#555">{esc(str(j.comps[ref].get("value", "")))[:26]}</text>']
        for i, n in enumerate(nums):
            yy = 34 + i * row_h
            v = j.voltage(ns[n])
            col = "#1c1e21" if v is None else "#000" if v == 0 else "#b3261e" if v > 0 else "#1d4ed8"
            fill = "#fff" if v is None else "#e9e9e9" if v == 0 else "#fde8e6" if v > 0 else "#e3ecfd"
            g.append(f'<rect x="6" y="{yy}" width="{col_w - 12}" height="{row_h - 3}" rx="3" fill="{fill}"/>'
                     f'<text x="14" y="{yy + 14}" font-family="monospace" font-size="12">{esc(n)}</text>'
                     f'<text x="52" y="{yy + 14}" font-size="13" fill="{col}">{esc(ns[n])}</text>')
        g.append("</g>")
        blocks.append("".join(g))
        y += hgt + pad
        max_h = max(max_h, y)
    w = x + col_w + pad
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {max_h}" width="{w}" height="{max_h}" font-family="DejaVu Sans, Arial, sans-serif">'
           f'<rect width="{w}" height="{max_h}" fill="#f4f3ee"/>{"".join(blocks)}</svg>')
    r.write(f"{j.name}-pinout.svg", svg, "Pinout card")
    r.summary = f"{len(conns)} connector(s)"


# ---------------------------------------------------------------------------
def _img(j: Job, key):
    rel = (j.result.get("files") or {}).get(key)
    return j.file(rel) if rel else None


@addon(id="gen.readme", title="Project page (README)", kind="generate", group="Share",
       summary="A README.md with pictures, BOM and pinout, ready to drop into a GitHub repository next to the KiCad files.")
def gen_readme(r):
    j = r.job
    rows = bom(j)
    size = j.size()
    imgs = []
    for key, name in (("pcb_top", "board-top"), ("schematic_png", "schematic"), ("render3d", "render")):
        f = _img(j, key)
        if f:
            dst = r.out / "images" / (name + f.suffix)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
            r.attach(dst, f"image: {name}")
            imgs.append((name, f"images/{dst.name}"))
    md = [f"# {j.name.replace('_', ' ')}", "", j.spec.get("description", ""), ""]
    for name, rel in imgs[:2]:
        md += [f"![{name}]({rel})", ""]
    md += ["## Board", "", md_table(["", ""], [("Size", f"{size[0]:.0f} × {size[1]:.0f} mm" if size else "n/a"), ("Layers", j.board.get("layers", 2)),
                                              ("Parts", sum(g["qty"] for g in rows)), ("Design rules", j.board.get("fab", "generic"))]), ""]
    how = (j.spec.get("rationale") or {}).get("design")
    if how:
        md += ["## How it works", "", how, ""]
    md += ["## Bill of materials", "", md_table(["Qty", "Refs", "Value", "Footprint", "MPN"],
                                                 [(g["qty"], " ".join(g["refs"]), g["value"], g["footprint"], g["mpn"]) for g in rows]), ""]
    conns = sorted(j.of_kind("J"), key=natkey)
    if conns:
        md += ["## Connectors", ""]
        for ref in conns:
            ns = j.nets_of(ref)
            md += [f"**{ref}** {j.comps[ref].get('value', '')}: " + ", ".join(f"{n} = {ns[n]}" for n in sorted(ns, key=natkey)), ""]
    md += ["## Files", "", "- `*.kicad_pro`, `*.kicad_sch`, `*.kicad_pcb`: the KiCad project", "- `fab/`: gerbers, drill and position files", "",
           "## Status", "", "Generated with kipipe. Not built or tested unless stated here: ", "",
           "- [ ] ordered", "- [ ] assembled", "- [ ] powered up and tested", ""]
    r.write("README.md", "\n".join(md), "README.md")
    r.summary = f"README with {len(imgs)} image(s)"


# ---------------------------------------------------------------------------
def _inline_svg(f: Path) -> str:
    t = f.read_text(encoding="utf-8", errors="replace")
    t = re.sub(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", "", t)
    return f'<img alt="" style="width:100%;max-width:820px" src="data:image/svg+xml;base64,{base64.b64encode(t.encode()).decode()}">'


@addon(id="gen.report", title="Shareable report", kind="generate", group="Share",
       summary="One self-contained HTML file with the schematic, board views, BOM, check results and reasoning. Opens anywhere, prints cleanly.")
def gen_report(r):
    j = r.job
    res = j.result
    rows = bom(j)
    size = j.size()
    m = res.get("metrics") or {}
    facts = [("Size", f"{size[0]:.0f} × {size[1]:.0f} mm" if size else "n/a"), ("Layers", j.board.get("layers", 2)),
             ("Parts", sum(g["qty"] for g in rows)), ("Nets", len(j.nets)),
             ("Vias", (m.get("routing") or {}).get("vias", "n/a")), ("Unrouted", (m.get("routing") or {}).get("unrouted", "n/a")),
             ("DRC errors", (m.get("drc") or {}).get("errors", "n/a"))]
    body = [f"<h1>{esc(j.name.replace('_', ' '))}</h1><p class='lead'>{esc(j.spec.get('description', ''))}</p>",
            "<div class='facts'>" + "".join(f"<div><b>{esc(str(v))}</b><span>{esc(k)}</span></div>" for k, v in facts) + "</div>"]
    for key, title in (("schematic_svg", "Schematic"), ("pcb_top", "Board, top"), ("pcb_bottom", "Board, bottom")):
        f = _img(j, key)
        if f and f.suffix == ".svg":
            body.append(f"<h2>{title}</h2><div class='card'>{_inline_svg(f)}</div>")
    if not _img(j, "pcb_top") and j.layout.get("parts"):
        body.append(f"<h2>Placement</h2><div class='card'>{board_svg(j)}</div>")
    body.append("<h2>Bill of materials</h2><table><tr><th>Qty</th><th>Refs</th><th>Value</th><th>Footprint</th><th>MPN</th></tr>"
                + "".join(f"<tr><td>{g['qty']}</td><td class='mono'>{esc(' '.join(g['refs']))}</td><td>{esc(g['value'])}</td><td class='dim'>{esc(g['footprint'])}</td>"
                          f"<td>{esc(g['mpn'])}</td></tr>" for g in rows) + "</table>")
    chk = run_checks(j, log=r.log)
    fs = [(c["title"], f) for c in chk["checks"] for f in c["findings"]]
    fs.sort(key=lambda x: ("error", "warning", "note").index(x[1]["severity"]))
    body.append(f"<h2>Design checks</h2><p class='dim'>{len(chk['checks'])} rule checks ran: {chk['counts']['error']} errors, {chk['counts']['warning']} warnings, "
                f"{chk['counts']['note']} notes. They are fixed rules about common mistakes, not a proof that the circuit works.</p>"
                + ("<table>" + "".join(f"<tr><td><span class='sev {f['severity']}'>{f['severity']}</span></td><td><b>{esc(f['title'])}</b>"
                                       f"<div class='dim'>{esc(f['detail'])}</div></td><td class='dim'>{esc(t)}</td></tr>" for t, f in fs) + "</table>" if fs else ""))
    rat = j.spec.get("rationale") or {}
    if rat.get("design"):
        body.append(f"<h2>How it works</h2><p>{esc(rat['design'])}</p>")
    if rat.get("parts"):
        body.append("<h2>Part choices</h2><table>" + "".join(f"<tr><td class='mono'>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rat["parts"].items()) + "</table>")
    if j.spec.get("assumptions"):
        body.append("<h2>Assumptions</h2><ul>" + "".join(f"<li>{esc(str(a))}</li>" for a in j.spec["assumptions"]) + "</ul>")
    if res.get("stages"):
        body.append("<h2>Pipeline</h2><table>" + "".join(f"<tr><td>{esc(s['name'])}</td><td class='sev {'note' if s['status'] in ('ok', 'skip') else 'warning' if s['status'] == 'warn' else 'error'}'>"
                                                         f"{esc(s['status'])}</td><td class='dim'>{esc(s.get('detail', ''))}</td></tr>" for s in res["stages"]) + "</table>")
    body.append("<p class='dim' style='margin-top:40px'>Generated by kipipe. Machine-made design: review it before you build it.</p>")
    f = r.write(f"{j.name}-report.html", page(j.name, "".join(body)), "Report (single file)")
    r.summary = f"{f.stat().st_size // 1024} kB, {len(fs)} check finding(s) included"


# ---------------------------------------------------------------------------
def _previous(j: Job):
    """The job this one was revised from, or the newest earlier job with the same project name."""
    for tag in ("-r", "-e"):
        base, _, n = j.dir.name.rpartition(tag)
        if base and n.isdigit():
            prev = j.dir.parent / (f"{base}{tag}{int(n) - 1}" if int(n) > 1 else base)
            if (prev / "spec.json").exists():
                return prev
    for d in sorted(j.dir.parent.glob("*"), reverse=True):
        if d.name < j.dir.name and (d / "spec.json").exists():
            try:
                if json.loads((d / "spec.json").read_text()).get("name") == j.name:
                    return d
            except Exception:
                pass
    return None


@addon(id="gen.diff", title="Compare with another run", kind="generate", group="Share",
       options=[{"key": "other", "label": "Job id or folder to compare with (empty: the run this one came from)", "type": "text", "default": ""}],
       summary="What changed between two runs: parts added, removed or changed, nets rewired, parts that moved.")
def gen_diff(r):
    j = r.job
    other = str(r.opts.get("other") or "").strip()
    od = (Path(other) if "/" in other else j.dir.parent / other) if other else _previous(j)
    if not od or not (Path(od) / "spec.json").exists():
        r.summary = "nothing to compare with: give a job id" if not other else f"no spec.json in {od}"
        r.finding("note", r.summary, "Revisions (job-e1, job-e2) are compared with their parent automatically.")
        return
    o = Job(od)
    md = [f"# {o.dir.name} → {j.dir.name}", ""]
    added = sorted(set(j.comps) - set(o.comps), key=natkey)
    removed = sorted(set(o.comps) - set(j.comps), key=natkey)
    changed = []
    for ref in sorted(set(j.comps) & set(o.comps), key=natkey):
        for k in ("value", "symbol", "footprint", "mpn", "edge"):
            a, b = o.comps[ref].get(k), j.comps[ref].get(k)
            if (a or None) != (b or None):
                changed.append((ref, k, a, b))
    rewired = []
    for ref in sorted(set(j.comps) & set(o.comps), key=natkey):
        a, b = o.nets_of(ref), j.nets_of(ref)
        for pin in sorted(set(a) | set(b), key=natkey):
            if a.get(pin) != b.get(pin):
                rewired.append((f"{ref}.{pin}", a.get(pin, "unconnected"), b.get(pin, "unconnected")))
    moved = []
    for ref in sorted(set(j.comps) & set(o.comps), key=natkey):
        a, b = o.placed(ref), j.placed(ref)
        if a and b:
            d = ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5
            if d > 0.3 or str(a.get("rot")) != str(b.get("rot")):
                moved.append((ref, f"{d:.1f} mm", f"{a.get('rot')}° → {b.get('rot')}°" if str(a.get("rot")) != str(b.get("rot")) else ""))
    sa, sb = o.size(), j.size()
    if sa and sb and (abs(sa[0] - sb[0]) > 0.1 or abs(sa[1] - sb[1]) > 0.1):
        md += [f"Board size: {sa[0]:.0f} × {sa[1]:.0f} mm → {sb[0]:.0f} × {sb[1]:.0f} mm", ""]
    if added:
        md += ["## Added", "", md_table(["Ref", "Value", "Footprint"], [(x, j.comps[x].get("value", ""), j.comps[x].get("footprint", "").split(":")[-1]) for x in added]), ""]
    if removed:
        md += ["## Removed", "", md_table(["Ref", "Value"], [(x, o.comps[x].get("value", "")) for x in removed]), ""]
    if changed:
        md += ["## Changed", "", md_table(["Ref", "Field", "Before", "After"], changed), ""]
    if rewired:
        md += ["## Rewired pins", "", md_table(["Pin", "Before", "After"], rewired), ""]
    if moved:
        md += ["## Moved", "", md_table(["Ref", "Distance", "Rotation"], moved), ""]
    if len(md) == 2:
        md.append("No differences in parts, connections or placement.")
    r.write(f"diff-{o.dir.name}.md", "\n".join(md), "Differences")
    r.data.update(added=[[x, j.comps[x].get("value", "")] for x in added], removed=[[x, o.comps[x].get("value", "")] for x in removed],
                  changed=[list(map(lambda v: "" if v is None else str(v), c)) for c in changed], rewired=[list(x) for x in rewired], moved=[list(x) for x in moved],
                  other=str(od), other_id=o.dir.name, other_name=o.name,
                  size=[list(sa) if sa else None, list(sb) if sb else None],
                  images=[(o.result.get("files") or {}).get("pcb_top"), (j.result.get("files") or {}).get("pcb_top")])
    r.summary = f"vs {o.dir.name}: {len(added)} added, {len(removed)} removed, {len(changed)} changed, {len(rewired)} pin(s) rewired, {len(moved)} moved"
