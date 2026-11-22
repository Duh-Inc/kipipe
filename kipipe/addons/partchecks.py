"""Datasheet checks (phase 3): the design against what the part database says about its ICs.

Everything here is arithmetic and lookup on records that were read once from the datasheet; no model is called.
Five checks: pin numbers for the package actually used, voltages and currents against the ratings, the support
parts the datasheet asks for, the datasheet's own design equations evaluated with the fitted values, and
regulators against the rails they are supposed to make.

What limits them: a net's voltage is known only from its name ("+3V3") or the spec's "rails" entry; a part with
no record, or with a record for another variant, is skipped and said so. Findings carry the datasheet page.
"""
from __future__ import annotations

import re

from .core import Job, addon, fmt_si
from .draw import natkey

GROUP = "Datasheet checks"

_FAMILY = [  # (footprint name, package label in a datasheet)
    (r"TSSOP", r"TSSOP|\bPW\b"), (r"MSOP", r"MSOP|DGK|\bMS8?\b"), (r"SSOP", r"SSOP|\bDB\b"),
    (r"SOIC|SO-\d|SOP-\d|_SO_", r"SOIC|\bSO\b|\bS8\b|\bS\d\d\b|\bD\b|\(D\)|SO-?\d"), (r"DIP", r"DIP|\bN8\b|\bN\d\d\b|\(N\)|\(P\)|\bP\b"),
    (r"QFN|DFN|VQFN|WQFN|UQFN|MLF", r"QFN|DFN|RGE|RHB|RTE|RGT|MLF|LFCSP"), (r"SOT-223", r"SOT-?223|\bST\b|DCY"),
    (r"SOT-23|TSOT", r"SOT-?23|DBV|TSOT"), (r"SOT-89", r"SOT-?89"), (r"SC-70|SOT-323|SOT-353|SOT-363", r"SC-?70|DCK"),
    (r"TO-220", r"TO-?220|\bT\b|\bKC\b"), (r"TO-252|DPAK", r"TO-?252|D-?PAK|\bKVU\b"), (r"TO-263|D2PAK", r"TO-?263|DD-?PAK|D2-?PAK|\bKTT\b"),
    (r"TO-92", r"TO-?92|\bLP\b"), (r"LGA", r"LGA|\bCC\b"), (r"BGA|WLCSP", r"BGA|CSP"), (r"QFP", r"QFP|\bPT\b|\bPM\b"),
]


def _k(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


_SYN = {"in": "vin", "vi": "vin", "input": "vin", "out": "vout", "vo": "vout", "output": "vout", "vss": "gnd", "ground": "gnd",
        "sense": "fb", "enable": "en", "shutdown": "shdn", "vdd": "vcc", "vs": "vcc", "tab": "", "ep": "pad", "epad": "pad", "thermalpad": "pad"}


def _tokens(name) -> set:
    raw = re.sub(r"~\{([^}]*)\}", r"\1", str(name or "")).replace("~", "")
    parts = [raw] + re.split(r"[/,\s()]+", raw)
    out = set()
    for p in parts:
        k = _k(p)
        if k:
            out.add(_SYN.get(k, k))
    out.discard("")
    return out


def same_name(a, b) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if ta & tb or (_k(a) and _k(a) == _k(b)):
        return True
    # RES / RESERVED, VDD_IO / VDDIO: one name is the start of the other
    return any(len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x)) for x in ta for y in tb)


def _split_nums(v) -> list:
    return [x.upper() for x in re.split(r"[,\s/;]+", str(v or "")) if x and x not in ("-", "—")]


class PartView:
    """One IC of the design next to its datasheet record."""

    def __init__(self, job: Job, ref: str, rec: dict):
        self.j, self.ref, self.rec = job, ref, rec
        self.comp = job.comps[ref]
        self.nets = job.nets_of(ref)
        self.sym = {p.number.upper(): p for p in job.pins(ref)}
        self.rpins = [p for p in rec.get("pins") or [] if isinstance(p, dict) and p.get("name")]
        self.mpn = (rec.get("identity") or {}).get("mpn", "")
        self.sign_mag = (rec.get("conventions") or {}).get("sign") == "magnitudes"
        self.package, self.pkg_why = self._pick_package()
        self.by_num = {}
        for p in self.rpins:
            for n in self.numbers(p):
                self.by_num.setdefault(n, p)
        # if the symbol and the record disagree about which pin is which, numbers cannot be trusted for the other
        # checks: they fall back to matching by pin name, and the pinout check reports the disagreement
        self.mismatch = sum(1 for n, sp in self.sym.items() if n in self.by_num and not same_name(sp.name, self.by_num[n]["name"]))

    def _pick_package(self):
        pkgs = []
        for p in self.rpins:
            for k in (p.get("numbers") or {}):
                if k not in pkgs:
                    pkgs.append(k)
        if not pkgs:
            return None, "the record has no pin numbers"
        fp = self.comp.get("footprint", "").split(":")[-1]
        n_sym = len(self.sym)
        scored = []
        for k in pkgs:
            nums = {n for p in self.rpins for n in _split_nums((p.get("numbers") or {}).get(k))}
            fam = any(re.search(a, fp, re.I) and re.search(b, k, re.I) for a, b in _FAMILY)
            cnt = bool(nums) and (len(nums) == n_sym or len(nums) + 1 == n_sym)
            scored.append((2 * fam + cnt, k, len(nums)))
        scored.sort(key=lambda x: -x[0])
        same = len({tuple(sorted((p["name"], tuple(_split_nums((p.get("numbers") or {}).get(k)))) for p in self.rpins)) for k in pkgs}) == 1
        if same:
            return pkgs[0], "every package in the record has the same pin numbers"
        if scored[0][0] >= 2 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1], f"matched to footprint {fp}"
        if len(pkgs) == 1 and scored[0][0] >= 1:
            return pkgs[0], "the only package in the record, pin count fits"
        return None, f"could not tell which of the record's packages ({', '.join(pkgs)}) is footprint {fp}"

    def numbers(self, rpin) -> list:
        nums = rpin.get("numbers") or {}
        if self.package and self.package in nums:
            return _split_nums(nums[self.package])
        return []

    def named(self, name):
        """Record pin for a name used elsewhere in the record (support parts, ratings, equations)."""
        for p in self.rpins:
            if _k(p["name"]) == _k(name):
                return p
        for p in self.rpins:
            if same_name(p["name"], name):
                return p
        return None

    def nets_of_pin(self, rpin) -> list:
        """Design nets on a record pin. With no package match, fall back to the symbol's pin names."""
        nums = [] if self.mismatch else self.numbers(rpin)
        if not nums:
            nums = [n for n, sp in self.sym.items() if same_name(sp.name, rpin["name"])]
        return sorted({self.nets[n] for n in nums if n in self.nets} | {self.nets[n.lower()] for n in nums if n.lower() in self.nets})

    def resolve(self, name):
        """A name from the record -> (design nets, how). how: pin | ground | unused | unknown."""
        p = self.named(name)
        if p:
            nets = self.nets_of_pin(p)
            return (nets, "pin") if nets else ([], "unused")
        if re.fullmatch(r"(A|D|P)?(GND|VSS|GROUND|0V)", str(name).strip(), re.I):
            g = sorted({n for n in self.nets.values() if self.j.is_gnd(n)}) or sorted(n for n in self.j.nets if self.j.is_gnd(n))
            return g, "ground"
        return [], "unknown"

    def volts(self, net):
        v = self.j.voltage(net)
        return None if v is None else (abs(v) if self.sign_mag else v)

    def tag(self, item) -> str:
        bits = []
        if item.get("page"):
            bits.append(f"datasheet page {item['page']}")
        if item.get("chk") in ("single",):
            bits.append("read once, not cross-checked")
        return (" (" + ", ".join(bits) + ")") if bits else ""


def views(job: Job) -> tuple:
    """({ref: PartView}, {ref: why not}) for the ICs and modules of a design."""
    from .. import partdb
    tiers = {c["ref"]: partdb.classify(c) for c in job.spec.get("components", []) if c.get("ref")}
    cov = partdb.coverage(job.spec, tiers)
    out, skipped = {}, {}
    for ref, name in cov["have"].items():
        rec = partdb.find(name)
        if rec:
            out[ref] = PartView(job, ref, rec)
    for ref, name in cov["missing"].items():
        skipped[ref] = f"no datasheet record for {name}"
    for ref, why in cov["variant"].items():
        skipped[ref] = why
    return out, skipped


def _skips(r, skipped, n_checked):
    if skipped:
        r.finding("note", f"Not checked: {', '.join(sorted(skipped, key=natkey))}",
                  "; ".join(f"{k}: {v}" for k, v in sorted(skipped.items(), key=lambda x: natkey(x[0]))) + ". Add the datasheet under Datasheets to include them.",
                  refs=list(skipped))
    r.data["checked"], r.data["skipped"] = n_checked, skipped


# ---------------------------------------------------------------------------
@addon(id="part.pinout", title="Pin numbers for the package", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="Each IC's pin numbers against the datasheet's pin table for the package on the board, plus supply and ground pins on the wrong net, unconnected supplies and floating inputs.")
def pinout(r):
    j = r.job
    vs, skipped = views(j)
    nc = {x.upper() for x in j.spec.get("no_connect") or []}
    for ref, v in sorted(vs.items(), key=lambda x: natkey(x[0])):
        who = f"{ref} ({v.mpn})"
        if not v.package:
            r.finding("warning", f"{who}: pin numbers not checked", v.pkg_why[0].upper() + v.pkg_why[1:] + ". Compare the symbol with the datasheet's pin table by hand.", refs=[ref])
            continue
        extra, wrong, clash_n, floating = [], [], 0, []
        for num, sp in sorted(v.sym.items(), key=lambda x: natkey(x[0])):
            rp = v.by_num.get(num)
            net = v.nets.get(num) or v.nets.get(num.lower())
            if rp is None:
                if net:
                    extra.append(f"{num} ({sp.name})")
                continue
            t = str(rp.get("type", ""))
            desc = str(rp.get("description") or "")
            if not same_name(sp.name, rp["name"]):
                clash = (t == "ground" and net and not j.is_gnd(net)) or (t == "power" and net and j.is_gnd(net)) \
                    or (sp.etype == "power_in" and t in ("input", "output", "io", "nc")) or (t in ("power", "ground") and sp.etype not in ("power_in", "power_out", "passive", ""))
                clash_n += bool(clash)
                wrong.append((num, sp.name, rp["name"], net, rp))
                continue
            if t == "ground" and net and not j.is_gnd(net):
                r.finding("error", f"{who} pin {num} ({rp['name']}) is a ground pin wired to {net}", desc + v.tag(rp), refs=[ref], nets=[net])
            elif t == "power" and net and j.is_gnd(net):
                r.finding("error", f"{who} pin {num} ({rp['name']}) is a supply pin wired to ground", desc + v.tag(rp), refs=[ref], nets=[net])
            elif not net and f"{ref}.{num}".upper() not in nc:
                if t in ("power", "ground"):
                    r.finding("error", f"{who} pin {num} ({rp['name']}) is not connected", "Supply and ground pins must all be connected. " + desc + v.tag(rp), refs=[ref])
                elif re.search(r"or left (open|floating|unconnected)|or leave|(may|can) be left|leave (open|floating|unconnected)", desc, re.I):
                    pass
                elif t == "input" or re.search(r"must be (connected|tied)|do not (leave )?float|connect directly to|tie to", desc, re.I):
                    floating.append((num, rp["name"], desc, rp))
            elif net and t == "nc" and not re.search(r"may be|can be|or left|optional|or to", desc, re.I):
                r.finding("note", f"{who} pin {num} ({rp['name']}) is marked no-connect or reserved but is wired to {net}", desc + v.tag(rp), refs=[ref], nets=[net])
        if wrong:
            lst = "; ".join(f"pin {n}: symbol {a}, datasheet {b}" + (f" (wired to {net})" if net else "") for n, a, b, net, _ in wrong[:12])
            if len(wrong) > max(2, len(v.sym) // 2):
                r.finding("error", f"{who}: the symbol and the datasheet's {v.package} pin table disagree on {len(wrong)} of {len(v.sym)} pins",
                          lst + ". Either the symbol is for another package or variant, or the record was read from another variant's datasheet. "
                          "Settle which before trusting anything else about this part." + v.tag(wrong[0][4]), refs=[ref],
                          fix="Compare both with the datasheet's pin drawing for the package you will buy.")
            else:
                r.finding("error" if clash_n else "warning", f"{who}: {len(wrong)} pin(s) differ from the datasheet's {v.package} pin table",
                          lst + ". A symbol drawn for another package or variant puts signals on the wrong pads." + v.tag(wrong[0][4]), refs=[ref],
                          nets=[w[3] for w in wrong if w[3]], fix="Use the symbol and footprint for this exact part and package, or renumber the pins.")
        if floating:
            r.finding("warning", f"{who}: input pin(s) left floating: {', '.join(f'{n} ({nm})' for n, nm, _, _ in floating[:10])}",
                      (floating[0][2] or "An unconnected input picks up noise and can sit at any level.") + v.tag(floating[0][3]), refs=[ref],
                      fix="Tie each to the level the datasheet asks for, or list it under no_connect if the datasheet allows it open.")
        missing = [f"{n} ({p['name']})" for n, p in sorted(v.by_num.items(), key=lambda x: natkey(x[0])) if n not in v.sym]
        if missing:
            r.finding("warning", f"{who}: the symbol lacks datasheet pin(s) {', '.join(missing[:8])}", f"Package {v.package}. A missing pin cannot be connected or checked.", refs=[ref])
        if extra:
            r.finding("note", f"{who}: pin(s) {', '.join(extra[:6])} are not in the datasheet's pin table", "Often an exposed pad the table lists separately; check where it should go.", refs=[ref])
        r.finding("note", f"{who}: checked against package {v.package}", v.pkg_why[0].upper() + v.pkg_why[1:] + ".", refs=[ref])
    _skips(r, skipped, len(vs))
    r.summary = f"{len(vs)} part(s) compared with their datasheet pin table" + (f", {len(skipped)} without a usable record" if skipped else "")


# ---------------------------------------------------------------------------
def _si(val, unit):
    from ..partdb import to_si
    v, u = to_si(val, unit)
    return v, u


def _limits(v: PartView, rt: dict):
    """(lo, hi, unit) of a rating in SI, evaluating 'VCC + 0.3' style limits with the design's own voltages."""
    lo, unit = _si(rt.get("min"), rt.get("unit"))
    hi, _ = _si(rt.get("max"), rt.get("unit"))
    for key, cur in (("min_expr", "lo"), ("max_expr", "hi")):
        ex = rt.get(key)
        if not ex:
            continue
        val = _eval_expr(v, ex)
        if val is not None:
            if cur == "lo":
                lo = val
            else:
                hi = val
    return lo, hi, unit


def _eval_expr(v: PartView, expr: str):
    from ..partdb import evaluate
    import ast
    text = str(expr)
    for p in sorted(v.rpins, key=lambda p: -len(p["name"])):
        if p["name"] in text:
            nets = v.nets_of_pin(p)
            volts = v.volts(nets[0]) if nets else None
            if volts is None:
                return None
            text = text.replace(p["name"], f"({volts})")
    try:
        return evaluate(ast.parse(text.replace("^", "**").replace("−", "-"), mode="eval"), {})
    except Exception:
        return None


def _targets(v: PartView, rt: dict):
    """Record pins a voltage rating applies to: [(label, [pins]) or ('diff', a, b)]."""
    param, sym = str(rt.get("param") or ""), str(rt.get("symbol") or "")
    m = re.search(r"([A-Za-z][\w/ ]*?)[- ]to[- ]([A-Za-z][\w/ ]*?)\s+(differential|voltage)", param, re.I)
    if m:
        a, b = v.named(m.group(1).strip()), v.named(m.group(2).strip())
        if a and b:
            return ("diff", a, b)
    if sym:
        p = v.named(sym)
        if p and _k(sym) not in ("vi", "vo", "vin", "vout", "in", "out") or (p and _k(p["name"]) == _k(sym)):
            return ("pins", [p])
    for p in sorted(v.rpins, key=lambda p: -len(p["name"])):
        if len(_k(p["name"])) >= 2 and re.search(r"(^|[^A-Za-z0-9_])" + re.escape(p["name"]) + r"($|[^A-Za-z0-9_])", param, re.I):
            return ("pins", [p])
    low = param.lower()
    if re.search(r"supply voltage|^vcc|^vdd|power supply", low) and not re.search(r"current", low):
        return ("pins", [p for p in v.rpins if p.get("type") == "power"])
    if re.search(r"input voltage", low):
        vin = v.named("VIN")
        if vin and vin.get("type") in ("power", "input", ""):
            if re.search(r"regulator|converter|ldo|dropout", str((v.rec.get("identity") or {}).get("category", "")) + str((v.rec.get("identity") or {}).get("description", "")), re.I):
                return ("pins", [vin])
        return ("pins", [p for p in v.rpins if p.get("type") in ("input", "io")])
    if re.search(r"digital pins|all (other )?pins|any pin|i/o voltage|logic", low):
        return ("pins", [p for p in v.rpins if p.get("type") in ("input", "io", "output")])
    return None


@addon(id="part.ratings", title="Voltages and currents against ratings", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="The voltage on each IC pin against the datasheet's absolute maximum and recommended ranges, and regulator output current against the current the rail is meant to carry.")
def ratings(r):
    j = r.job
    vs, skipped = views(j)
    checked = unknown = 0
    for ref, v in sorted(vs.items(), key=lambda x: natkey(x[0])):
        who = f"{ref} ({v.mpn})"
        seen, unknown_nets, said = set(), set(), set()
        for rt in sorted(v.rec.get("ratings") or [], key=lambda x: x.get("kind") != "abs_max"):
            kind = rt.get("kind")
            lo, hi, unit = _limits(v, rt)
            key = (kind, rt.get("param"), rt.get("symbol"), lo, hi)
            if key in seen or kind not in ("abs_max", "recommended") or (lo is None and hi is None) or (lo is not None and lo == hi):
                continue
            seen.add(key)
            label = f"{'absolute maximum' if kind == 'abs_max' else 'recommended range'} for {rt.get('param')}"
            rng = " to ".join(fmt_si(x, unit) for x in (lo, hi) if x is not None) if lo is not None and hi is not None else \
                (f"max {fmt_si(hi, unit)}" if hi is not None else f"min {fmt_si(lo, unit)}")
            if unit == "V":
                tg = _targets(v, rt)
                if not tg:
                    continue
                pairs = []
                if tg[0] == "diff":
                    na, nb = v.nets_of_pin(tg[1]), v.nets_of_pin(tg[2])
                    if na and nb:
                        va, vb = v.volts(na[0]), v.volts(nb[0])
                        if va is None or vb is None:
                            unknown_nets |= {n for n, x in ((na[0], va), (nb[0], vb)) if x is None}
                        else:
                            pairs.append((f"{tg[1]['name']} to {tg[2]['name']}", abs(va - vb), [na[0], nb[0]]))
                else:
                    for p in tg[1]:
                        for net in v.nets_of_pin(p):
                            if j.is_gnd(net):
                                continue
                            val = v.volts(net)
                            if val is None:
                                unknown_nets.add(net)
                            else:
                                pairs.append((f"pin {p['name']} ({net})", val, [net]))
                for what, val, nets_ in pairs:
                    checked += 1
                    over = (hi is not None and val > hi + 1e-9) or (lo is not None and val < lo - 1e-9)
                    if over and (what, kind) in said:
                        continue
                    if over:
                        said.add((what, kind))
                        r.finding("error" if kind == "abs_max" else "warning", f"{who}: {what} is at {val:g} V, outside the {label} ({rng})",
                                  ("Beyond an absolute maximum the part can be damaged at once." if kind == "abs_max" else "Outside the recommended range the datasheet's figures no longer apply.")
                                  + (" The datasheet states magnitudes for this negative-rail part." if v.sign_mag else "") + v.tag(rt), refs=[ref], nets=nets_)
                    elif kind == "abs_max" and hi and hi > 0 and val > 0.9 * hi and not rt.get("max_expr") and (what, "margin") not in said:
                        said.add((what, "margin"))
                        r.finding("warning", f"{who}: {what} is at {val:g} V, within 10% of the {label} ({fmt_si(hi, 'V')})",
                                  "No margin for supply tolerance or transients." + v.tag(rt), refs=[ref], nets=nets_)
            elif unit == "A" and re.search(r"output current|load current", str(rt.get("param") or ""), re.I) and hi:
                out = v.named("VOUT")
                for net in (v.nets_of_pin(out) if out else []):
                    amps = j.current(net)
                    if amps is None:
                        continue
                    checked += 1
                    if amps > abs(hi) + 1e-12:
                        r.finding("error" if kind == "abs_max" else "warning", f"{who}: {net} is meant to carry {amps:g} A, above the {label} ({fmt_si(abs(hi), 'A')})",
                                  "The rail's current comes from the spec's net class or rails entry." + v.tag(rt), refs=[ref], nets=[net])
        unknown_nets = {n for n in unknown_nets if j.is_rail(n)}      # a signal's level is not a rail to look up
        if unknown_nets:
            unknown += len(unknown_nets)
            r.finding("note", f"{who}: voltage unknown on {', '.join(sorted(unknown_nets))}",
                      "These net names do not state a voltage and the spec has no rails entry for them, so ratings on those pins were not checked.", refs=[ref], nets=sorted(unknown_nets))
    _skips(r, skipped, len(vs))
    r.summary = f"{checked} limit(s) checked on {len(vs)} part(s)" + (f", {unknown} net(s) of unknown voltage" if unknown else "")


# ---------------------------------------------------------------------------
_KIND = {"r": "R", "resistor": "R", "c": "C", "capacitor": "C", "cap": "C", "l": "L", "inductor": "L", "d": "D", "diode": "D",
         "fb": "FB", "ferrite": "FB", "y": "Y", "crystal": "Y", "led": "LED", "q": "Q", "f": "F", "fuse": "F"}


def _want_value(text):
    """('min'|'max'|'range'|'nom', value[, value2]) from '1uF minimum', '2.2 to 10 µF', '10k'. None if no number."""
    from ..partdb import parse_value
    t = str(text or "").replace("µ", "u").replace("μ", "u").replace("Ω", "").strip()
    if not t or re.search(r"not stated|see |equation|varies|none", t, re.I):
        return None
    m = re.match(r"\s*([0-9.]+\s*[pnumkMGR]?[A-Za-z]*)\s*(?:to|-|–)\s*([0-9.]+\s*[pnumkMGR]?[A-Za-z]*)", t)
    if m and parse_value(m.group(1)) and parse_value(m.group(2)):
        a, b = parse_value(m.group(1)), parse_value(m.group(2))
        if b > a:
            return ("range", a, b)
    v = parse_value(re.sub(r"^[^0-9.]*", "", t))
    if not v:
        return None
    if re.search(r"min|at least|≥|>=|or (larger|greater|more|higher)", t, re.I):
        return ("min", v)
    if re.search(r"max|at most|≤|<=|or (less|smaller|lower)", t, re.I):
        return ("max", v)
    return ("nom", v)


def between(v: PartView, kind: str, a_name, b_name):
    """Design parts of a kind wired between two names from the record. Returns (refs, status).
    status: ok | unused (a named pin is not used on this board) | none."""
    j = v.j
    na, ha = v.resolve(a_name)
    nb, hb = v.resolve(b_name)
    if "unused" in (ha, hb):
        return [], "unused"
    if ha == "unknown" and hb == "unknown":
        return [], "unknown"
    kinds = (kind,) if kind != "C" else ("C",)
    found = []
    for ref in j.comps:
        if j.kind(ref) not in kinds or ref == v.ref:
            continue
        tp = j.two_pin(ref)
        if not tp:
            continue
        for x, y in (tp, tp[::-1]):
            okx = x in na if ha != "unknown" else (j.is_rail(x) and not j.is_gnd(x) and x not in nb)
            oky = y in nb if hb != "unknown" else (j.is_rail(y) and not j.is_gnd(y) and y not in na)
            if okx and oky:
                found.append(ref)
                break
    return sorted(set(found), key=natkey), "ok" if found else "none"


@addon(id="part.support", title="Support parts the datasheet asks for", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="Every capacitor, resistor or other part the datasheet says an IC needs: present between the right pins, and large enough.")
def support(r):
    j = r.job
    vs, skipped = views(j)
    n = 0
    for ref, v in sorted(vs.items(), key=lambda x: natkey(x[0])):
        who = f"{ref} ({v.mpn})"
        seen = set()
        for sp in v.rec.get("support_parts") or []:
            bt = sp.get("between") or []
            kind = _KIND.get(_k(sp.get("kind")), None)
            if len(bt) != 2 or not kind:
                continue
            key = (kind, _k(bt[0]), _k(bt[1]))
            if key in seen:
                continue
            seen.add(key)
            refs, status = between(v, kind, bt[0], bt[1])
            what = f"{sp.get('what') or kind} between {bt[0]} and {bt[1]}"
            if status in ("unused", "unknown"):
                continue
            n += 1
            if status == "none" and sp.get("required") is False:
                continue
            if status == "none":
                req = True
                sure = req and sp.get("chk") in ("agreed", "resolved", "parsed", "corrected")
                r.finding("error" if sure else "warning" if req else "note", f"{who}: no {what}",
                          (str(sp.get("why") or "") + (f" Datasheet value: {sp['value']}." if sp.get("value") else "")).strip() + v.tag(sp), refs=[ref],
                          fix=f"Add it{' (' + str(sp['value']) + ')' if sp.get('value') else ''}." if req else "")
                continue
            want = _want_value(sp.get("value"))
            if not want:
                continue
            vals = [j.value(x) for x in refs if j.value(x)]
            if not vals:
                continue
            have = sum(vals) if kind == "C" else min(vals, key=lambda x: abs(x - want[1]))
            unit = {"C": "F", "R": "Ω", "L": "H"}.get(kind, "")
            got = f"{', '.join(refs)} = {fmt_si(have, unit)}" + (" in total" if kind == "C" and len(vals) > 1 else "")
            if want[0] == "min" and have < want[1] * 0.999:
                r.finding("error", f"{who}: {what} is too small ({got}; datasheet minimum {fmt_si(want[1], unit)})", str(sp.get("why") or "") + v.tag(sp), refs=[ref] + refs)
            elif want[0] == "max" and have > want[1] * 1.001:
                r.finding("error", f"{who}: {what} is too large ({got}; datasheet maximum {fmt_si(want[1], unit)})", str(sp.get("why") or "") + v.tag(sp), refs=[ref] + refs)
            elif want[0] == "range" and not want[1] * 0.999 <= have <= want[2] * 1.001:
                r.finding("warning", f"{who}: {what} is outside the datasheet's range ({got}; {fmt_si(want[1], unit)} to {fmt_si(want[2], unit)})", str(sp.get("why") or "") + v.tag(sp), refs=[ref] + refs)
            elif want[0] == "nom" and (have < want[1] / 2 or (kind != "C" and have > want[1] * 2)):
                r.finding("note", f"{who}: {what} differs from the datasheet's value ({got}; datasheet {fmt_si(want[1], unit)})",
                          "A different value can be a deliberate choice. " + str(sp.get("why") or "") + v.tag(sp), refs=[ref] + refs)
    _skips(r, skipped, len(vs))
    r.summary = f"{n} support part requirement(s) checked on {len(vs)} part(s)"


# ---------------------------------------------------------------------------
def _map_app_refs(v: PartView) -> dict:
    """Application-circuit component names (R1, C2, ...) -> design refs, by where they sit around the IC."""
    ac = v.rec.get("application_circuit") or {}
    comps = {c.get("ref"): _KIND.get(_k(c.get("kind")), None) for c in ac.get("components") or [] if c.get("ref")}
    ends = {}
    for net, pins in (ac.get("nets") or {}).items():
        ic_pins = [p.split(".", 1)[1] for p in pins if "." in p and p.split(".", 1)[0] not in comps]
        label = ic_pins[0] if ic_pins else net
        for p in pins:
            cref = p.split(".", 1)[0]
            if cref in comps:
                ends.setdefault(cref, []).append(label)
    out = {}
    for cref, kind in comps.items():
        e = ends.get(cref, [])
        if kind and len(e) == 2:
            refs, status = between(v, kind, e[0], e[1])
            if status == "ok" and len(refs) == 1:
                out[cref] = refs[0]
    return out


def _rating_value(v: PartView, symbol):
    """(typ, lo, hi) in SI for a rating symbol, electrical characteristics first."""
    best = None
    for rt in v.rec.get("ratings") or []:
        if not rt.get("symbol") or _k(rt["symbol"]) != _k(symbol):
            continue
        lo, _ = _si(rt.get("min"), rt.get("unit"))
        typ, _ = _si(rt.get("typ"), rt.get("unit"))
        hi, _ = _si(rt.get("max"), rt.get("unit"))
        if typ is None and lo is not None and hi is not None:
            typ = (lo + hi) / 2
        if typ is None:
            typ = hi if hi is not None else lo
        if typ is None:
            continue
        score = {"electrical": 0, "recommended": 1}.get(rt.get("kind"), 2)
        if best is None or score < best[0]:
            best = (score, typ, lo, hi)
    return best[1:] if best else None


@addon(id="part.equations", title="Datasheet equations with your values", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="The datasheet's own design equations (output voltage from a divider, resistor limits) evaluated with the parts fitted, and compared with what the board is meant to do.")
def equations(r):
    from ..partdb import check_equation, compute, split_equation, variables
    j = r.job
    vs, skipped = views(j)
    n_ok = n = 0
    for ref, v in sorted(vs.items(), key=lambda x: natkey(x[0])):
        who = f"{ref} ({v.mpn})"
        eqs = [e for e in v.rec.get("equations") or [] if e.get("expr") and not e.get("invalid")]
        if not eqs:
            continue
        amap = _map_app_refs(v)
        for e in eqs:
            if check_equation(e):
                continue
            lhs, tree = split_equation(e["expr"])
            vals, src, missing = {}, [], []
            for name in sorted(variables(tree)):
                d = (e.get("vars") or {}).get(name) or {}
                how = d.get("from")
                val = None
                if how == "component":
                    dref = amap.get(d.get("ref") or name)
                    if dref and j.value(dref):
                        val = j.value(dref)
                        src.append(f"{name} = {dref} {fmt_si(val)}")
                elif how == "rating":
                    rv = _rating_value(v, d.get("symbol") or name)
                    if rv:
                        val = rv[0]
                        src.append(f"{name} = {fmt_si(val)} (datasheet)")
                if val is None:
                    p = v.named(name)
                    nets = v.nets_of_pin(p) if p else []
                    if nets and v.volts(nets[0]) is not None:
                        val = v.volts(nets[0])
                        src.append(f"{name} = {val:g} V ({nets[0]})")
                if val is None:
                    m = re.search(rf"\b{re.escape(name)}\s*=\s*([0-9.]+\s*[pnuµmkMG]?)\s*(V|A|Ω|ohm|F|Hz|s)?\b", str(e.get("notes") or ""))
                    if m:
                        from ..partdb import parse_value
                        val = parse_value(m.group(1))
                        if val is not None:
                            src.append(f"{name} = {fmt_si(val)} (datasheet note)")
                if val is None:
                    missing.append(name)
                else:
                    vals[name] = val
            if missing:
                r.data.setdefault("not_evaluated", []).append({"ref": ref, "equation": e["expr"], "needs": missing})
                continue
            try:
                got = compute(e, vals)
            except Exception:
                continue
            n += 1
            how = f"{e['expr']} with {', '.join(src)}" + v.tag(e)
            target = None
            p = v.named(lhs)
            if p:
                nets = v.nets_of_pin(p)
                if nets and v.volts(nets[0]) is not None:
                    target = (abs(v.volts(nets[0])) if v.sign_mag else v.volts(nets[0]), f"net {nets[0]}", "V")
            dref = amap.get(lhs)
            if target is None and dref and j.value(dref):
                target = (j.value(dref), dref, "")
            unit_r = _si(1, ((e.get("vars") or {}).get(lhs) or {}).get("unit"))[1]
            if target is None and unit_r not in ("V", "A", "Ω", "F", "H", "W", "Hz"):
                r.data.setdefault("computed", []).append({"ref": ref, "equation": e["expr"], "value": got})
                n -= 1
                continue
            if target is None:
                bound = "min" if re.search(r"min$", lhs, re.I) else "max" if re.search(r"max$", lhs, re.I) else None
                r.finding("note", f"{who}: {e.get('name') or lhs} works out to {fmt_si(got, unit_r)}" + (f" (a {bound}imum)" if bound else ""), how, refs=[ref])
                continue
            want, what, unit = target
            cmp_got = abs(got) if v.sign_mag else got
            err = abs(cmp_got - want) / max(abs(want), 1e-12)
            if err > 0.05:
                r.finding("error", f"{who}: {e.get('name') or lhs} works out to {fmt_si(cmp_got, unit)}, but {what} is {fmt_si(want, unit)}",
                          how + f". That is {err * 100:.0f}% off.", refs=[ref] + [x for x in amap.values()], fix="Change the resistor values, or rename the net if the computed value is the intended one.")
            else:
                n_ok += 1
                r.finding("note", f"{who}: {e.get('name') or lhs} = {fmt_si(cmp_got, unit)}, matches {what}", how, refs=[ref])
    ne = r.data.get("not_evaluated") or []
    if ne:
        r.finding("note", f"{len(ne)} equation(s) could not be evaluated", "; ".join(f"{x['ref']}: {x['equation']} needs {', '.join(x['needs'])}" for x in ne[:6])
                  + ". They depend on quantities the design does not state (bus capacitance, rise time, load).")
    _skips(r, skipped, len(vs))
    r.summary = f"{n} equation(s) evaluated, {n_ok} match the design" + (f", {len(ne)} need values the design does not state" if ne else "")


# ---------------------------------------------------------------------------
def _fixed_output(text: str):
    """Output voltage a part number states: 'AMS1117-3.3' -> 3.3, 'LT1129-5' -> 5, 'LM7805' -> 5, 'XC6206P332MR' -> None."""
    t = str(text or "").upper()
    m = re.search(r"-(\d)V(\d)(?:$|[^0-9])", t)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"-(\d{1,2}(?:\.\d+)?)V?(?:$|[^0-9.])", t)
    if m:
        v = float(m.group(1))
        if "." in m.group(1) or v <= 24:
            return v
        if v in (33.0, 50.0):
            return v / 10
    m = re.search(r"(?:78|79)[LM]?(\d\d)(?:$|[^0-9])", t)
    if m:
        return float(m.group(1))
    return None


@addon(id="part.rails", title="Regulators against their rails", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="Each regulator's output voltage against the rail it feeds, its input headroom against the dropout voltage, and the heat it has to shed.")
def rails(r):
    j = r.job
    vs, _ = views(j)
    n = 0
    for ref in sorted(j.ics(), key=natkey):
        c = j.comps[ref]
        v = vs.get(ref)
        ident = (v.rec.get("identity") or {}) if v else {}
        is_reg = c.get("symbol", "").startswith("Regulator") or re.search(r"regulator|ldo|dropout", str(ident.get("category", "")) + str(ident.get("description", "")), re.I)
        if not is_reg:
            continue
        ns = j.nets_of(ref)
        vin_net = next((net for num, net in ns.items() if re.fullmatch(r"V?IN?|VI|INPUT", j.pin_name(ref, num), re.I)), None)
        out_net = next((net for num, net in ns.items() if re.fullmatch(r"V?OUT?|VO|OUTPUT", j.pin_name(ref, num), re.I)), None)
        if not out_net:
            continue
        n += 1
        who = f"{ref} ({c.get('value', '')})"
        vout, vin = j.voltage(out_net), j.voltage(vin_net) if vin_net else None
        fixed = _fixed_output(c.get("mpn") or c.get("value"))
        if fixed is not None and vout is not None:
            if abs(abs(vout) - fixed) > 0.05 * fixed:
                r.finding("error", f"{who} makes {fixed:g} V, but its output net {out_net} says {abs(vout):g} V", "The part number states the output voltage.", refs=[ref], nets=[out_net],
                          fix="Use the right voltage version, or rename the net.")
            else:
                r.finding("note", f"{who}: {fixed:g} V output matches net {out_net}", "", refs=[ref])
        if vin is None or vout is None:
            if vin_net and vin is None:
                r.finding("note", f"{who}: input voltage unknown ({vin_net})", "Headroom and heat were not checked: the net name does not state a voltage and the spec has no rails entry for it.", refs=[ref], nets=[vin_net])
            continue
        linear = c.get("symbol", "").startswith("Regulator_Linear") or re.search(r"linear|ldo|dropout", str(ident.get("category", "")) + str(ident.get("description", "")), re.I)
        if not linear:
            continue
        head = abs(vin) - abs(vout)
        drop = None
        if v:
            for rt in v.rec.get("ratings") or []:
                if re.search(r"dropout", str(rt.get("param") or ""), re.I) and _si(1, rt.get("unit"))[1] == "V":
                    cand = _si(rt.get("max") if rt.get("max") is not None else rt.get("typ"), rt.get("unit"))[0]
                    if cand is not None and (drop is None or cand > drop[0]):
                        drop = (cand, rt)
        if head <= 0:
            r.finding("error", f"{who}: input {vin_net} ({vin:g} V) is not above output {out_net} ({vout:g} V)", "A linear regulator can only step down.", refs=[ref], nets=[vin_net, out_net])
            continue
        if drop and head < drop[0]:
            r.finding("error", f"{who}: {head:.2g} V of headroom, dropout is up to {fmt_si(drop[0], 'V')}", f"{vin_net} − {out_net}. The output falls out of regulation." + v.tag(drop[1]), refs=[ref], nets=[vin_net, out_net])
        elif drop and head < 1.25 * drop[0]:
            r.finding("warning", f"{who}: {head:.2g} V of headroom against a dropout of up to {fmt_si(drop[0], 'V')}", "Little margin for input tolerance and anything in series with the input." + v.tag(drop[1]), refs=[ref])
        elif not drop:
            r.finding("note", f"{who}: {head:.2g} V of headroom", "No dropout figure in the part database to compare it with.", refs=[ref])
        amps = j.current(out_net)
        if amps:
            p = head * amps
            theta = None
            if v:
                for rt in v.rec.get("ratings") or []:
                    if rt.get("kind") == "thermal" and re.search(r"junction.to.ambient|θ\s*ja|theta.?ja|r.?θja", str(rt.get("param") or "") + str(rt.get("symbol") or ""), re.I):
                        val = rt.get("typ") if rt.get("typ") is not None else rt.get("max")
                        if val:
                            theta = (float(val), rt)
                            break
            if theta:
                rise = p * theta[0]
                sev = "error" if 25 + rise > 150 else "warning" if 25 + rise > 110 else "note"
                r.finding(sev, f"{who} sheds {p:.2g} W: about {25 + rise:.0f} °C at the junction in 25 °C air",
                          f"({head:.2g} V × {amps:g} A) × {theta[0]:g} °C/W junction to ambient" + v.tag(theta[1]) + ". Copper area and airflow change this a lot.", refs=[ref])
            elif p > 0.5:
                r.finding("warning", f"{who} sheds {p:.2g} W", f"{head:.2g} V × {amps:g} A. No thermal resistance in the part database; small packages overheat above roughly half a watt without copper area.", refs=[ref])
            else:
                r.finding("note", f"{who} sheds {p:.2g} W", f"{head:.2g} V × {amps:g} A.", refs=[ref])
    r.summary = f"{n} regulator(s) checked" if n else "no regulators on this board"
