"""Block library: small sub-circuits that are known to be right, placed into a design as a unit.

A block is a JSON file: parts with local names, nets, ports (the nets that leave the block), parameters, layout
hints and notes. Built-in blocks live next to this file in blocks/; your own go in ~/.local/share/kipipe/blocks/.

In a spec:

    "blocks": [{"use": "ldo_ams1117", "name": "REG", "params": {"vout": "3.3"},
                "ports": {"VIN": "+5V", "VOUT": "+3V3", "GND": "GND"}}]

Expansion is plain code: parts get real reference designators, port nets join the spec's nets, internal nets get
the instance name as a prefix. The model never re-derives the circuit, so it cannot get it subtly wrong.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .core import HOME, Job, addon

BUILTIN = Path(__file__).parent / "blocks"
USER = HOME / "blocks"


def _load_dir(d: Path, source: str) -> dict:
    out = {}
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                b = json.loads(f.read_text(encoding="utf-8"))
                b.setdefault("id", f.stem)
                b["_source"] = source
                out[b["id"]] = b
            except Exception as e:
                out[f.stem] = {"id": f.stem, "title": f.stem, "_source": source, "_error": f"{type(e).__name__}: {e}"}
    return out


def library() -> dict:
    lib = _load_dir(BUILTIN, "built in")
    lib.update(_load_dir(USER, "yours"))
    return lib


def public(b: dict) -> dict:
    return {k: v for k, v in b.items() if not k.startswith("_")} | {"source_kind": b.get("_source", ""), "error": b.get("_error")}


def _fill(text, params):
    return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), str(text))


def _prefix(local: str) -> str:
    return re.match(r"[A-Za-z]+?(?=_|\d|$)", local + "_").group(0).upper() if re.match(r"[A-Za-z]", local) else "U"


def expand(block: dict, spec: dict, name: str = "", ports: dict | None = None, params: dict | None = None) -> dict:
    """Add one instance of a block to a spec (in place). Returns {refs: {local: real}, nets: {block net: spec net}}."""
    if block.get("_error"):
        raise ValueError(f"block {block['id']} could not be read: {block['_error']}")
    p = {k: v.get("default") for k, v in (block.get("params") or {}).items()}
    for k, v in (params or {}).items():
        if k not in p:
            raise ValueError(f"block {block['id']} has no parameter {k!r} (it has: {', '.join(p) or 'none'})")
        ch = (block["params"][k] or {}).get("choices")
        if ch and str(v) not in [str(c) for c in ch]:
            raise ValueError(f"block {block['id']}: {k} must be one of {', '.join(map(str, ch))}")
        p[k] = v
    ports = dict(ports or {})
    unknown = [x for x in ports if x not in (block.get("ports") or {})]
    if unknown:
        raise ValueError(f"block {block['id']} has no port {', '.join(unknown)} (ports: {', '.join(block.get('ports') or {})})")
    comps = spec.setdefault("components", [])
    nets = spec.setdefault("nets", {})
    used = {c.get("ref") for c in comps}
    refs, symbols = {}, {}
    for c in block.get("components", []):
        pre = _prefix(c["ref"])
        n = 1
        while f"{pre}{n}" in used:
            n += 1
        real = f"{pre}{n}"
        used.add(real)
        refs[c["ref"]] = real
        new = {k: (_fill(v, p) if isinstance(v, str) else v) for k, v in c.items()}
        if isinstance(c.get("symbol"), list):           # alternatives: library names differ between KiCad versions
            cands = [_fill(x, p) for x in c["symbol"]]
            new["symbol"] = next((x for x in cands if _symbol_ok(x)), None)
            if not new["symbol"]:
                raise ValueError(f"block {block['id']} cannot be used with this KiCad's libraries: none of {', '.join(cands)} exists")
        if isinstance(c.get("footprint"), list):
            from .. import kicad_lib as _kl
            fps = [_fill(x, p) for x in c["footprint"]]
            new["footprint"] = next((x for x in fps if _kl.footprint_path(x)), fps[0])
        symbols[c["ref"]] = new["symbol"]
        new["ref"] = real
        new.setdefault("edge", None)
        new["block"] = name or block["id"]
        comps.append(new)
    inst = re.sub(r"[^A-Za-z0-9]", "_", name or block["id"]).upper()

    def pins_of(x):
        """'U.3' -> ['U1.3'];  'J.@GND' -> every pin of J named GND (pin numbers differ between library versions)."""
        local, _, num = x.partition(".")
        if not num.startswith("@"):
            return [f"{refs[local]}.{num}"]
        from .. import kicad_lib as kl
        want = num[1:].upper()
        got = [q.number for q in kl.symbol_pins(kl.get_symbol(symbols[local])) if q.name.upper() == want]
        if not got:
            raise ValueError(f"block {block['id']}: symbol {symbols[local]} has no pin named {num[1:]} in this KiCad's libraries")
        return [f"{refs[local]}.{n}" for n in got]

    def pin(x):
        return pins_of(x)[0]
    netmap = {}
    for bn, pins in (block.get("nets") or {}).items():
        if bn in (block.get("ports") or {}):
            target = ports.get(bn) or ("GND" if bn.upper() == "GND" else f"{inst}_{bn}")
        else:
            target = f"{inst}_{bn.lstrip('_')}"
        netmap[bn] = target
        lst = nets.setdefault(target, [])
        for x in pins:
            lst += [q for q in pins_of(x) if q not in lst]
    if block.get("no_connect"):
        spec.setdefault("no_connect", [])
        spec["no_connect"] += [q for x in block["no_connect"] for q in pins_of(x)]
    fp = spec.setdefault("floorplan", {})
    for nr in block.get("near") or []:
        fp.setdefault("near", []).append({"ref": refs[nr["ref"]], "to": pin(nr["to"]), "max_mm": nr.get("max_mm", 5)})
    return {"refs": refs, "nets": netmap}


def _symbol_ok(lib_id: str) -> bool:
    from .. import kicad_lib as kl
    try:
        kl.get_symbol(lib_id)
        return True
    except Exception:
        return False


_USABLE = {}


def usable() -> dict:
    """Blocks that expand and validate against the libraries on this machine. Checked once per library set."""
    from .. import preflight
    try:
        sig = str(sorted((k, str(v)) for k, v in preflight.kl.symbol_libs().items()).__hash__())
    except Exception:
        sig = "?"
    lib = library()
    key = (sig, tuple(sorted(lib)))
    if key not in _USABLE:
        _USABLE.clear()
        _USABLE[key] = {k: b for k, b in lib.items() if not check(b)}
    return _USABLE[key]


def expand_spec(spec: dict) -> dict:
    """Expand every entry of spec["blocks"]. Problems are collected in spec["_block_errors"] for the validator."""
    todo = spec.pop("blocks", None)
    if not todo:
        return spec
    lib = library()
    errors, used = [], spec.setdefault("blocks_used", [])
    for i, b in enumerate(todo):
        bid = (b or {}).get("use")
        if bid not in lib:
            near = [k for k in lib if bid and (bid in k or k in bid)]
            errors.append(f"blocks[{i}]: no block called {bid!r}" + (f" (did you mean {', '.join(near)}?)" if near else f" (available: {', '.join(lib)})"))
            continue
        try:
            res = expand(lib[bid], spec, b.get("name") or f"{bid}{i + 1}", b.get("ports"), b.get("params"))
            used.append({"use": bid, "name": b.get("name") or f"{bid}{i + 1}", "refs": res["refs"], "nets": res["nets"], "params": b.get("params") or {}})
        except Exception as e:
            errors.append(f"blocks[{i}] ({bid}): {e}. Remove this blocks entry and write those parts out in components and nets instead.")
    if errors:
        spec["_block_errors"] = errors
    return spec


def check(block: dict) -> list:
    """Problems that would stop this block from working with the KiCad libraries installed here."""
    if block.get("_error"):
        return [block["_error"]]
    from .. import spec as specmod
    probs = []
    try:
        s = {"name": "block_check", "board": {}, "components": [], "nets": {}}
        expand(block, s, "T")
        rep = specmod.validate(specmod.normalize(s))
        probs += [e for e in rep.errors]
    except Exception as e:
        probs.append(f"{type(e).__name__}: {e}")
    for k in ("title", "summary", "ports", "components", "nets"):
        if not block.get(k):
            probs.append(f"missing '{k}'")
    return probs


def prompt_hint() -> str:
    """Text for the design model: which blocks exist and how to use one."""
    lib = usable()
    if not lib:
        return ""
    lines = ["VERIFIED BLOCKS. For these sub-circuits, do not list the parts yourself: add an entry to a top-level \"blocks\" array and "
             "the pipeline places the known-good circuit. Connect to a block only through its ports (map each port to one of your net names). "
             "Example: \"blocks\": [{\"use\": \"ldo_ams1117\", \"name\": \"REG\", \"params\": {\"vout\": \"3.3\"}, \"ports\": {\"VIN\": \"+5V\", \"VOUT\": \"+3V3\", \"GND\": \"GND\"}}]. "
             "Block parts get their own reference designators; do not reference them in nets or the floorplan. Use a block only where it fits the request."]
    for b in lib.values():
        par = "; ".join(f"{k} (default {v.get('default')}" + (f", one of {'/'.join(map(str, v['choices']))}" if v.get("choices") else "") + ")" for k, v in (b.get("params") or {}).items())
        lines.append(f"- {b['id']}: {b.get('summary', '')} Ports: {', '.join(f'{k} ({v})' for k, v in (b.get('ports') or {}).items())}." + (f" Params: {par}." if par else ""))
    return "\n".join(lines)


def from_job(job: Job, refs: list, bid: str, title: str = "", summary: str = "") -> dict:
    """Turn a group of parts from a finished design into a block: nets that leave the group become ports."""
    refs = [r for r in refs if r in job.comps]
    if not refs:
        raise ValueError("none of those references are in this design")
    counts, local = {}, {}
    for r in refs:
        pre = re.match(r"[A-Z]+", r).group(0)
        counts[pre] = counts.get(pre, 0) + 1
        local[r] = pre if sum(1 for x in refs if re.match(r"[A-Z]+", x).group(0) == pre) == 1 else f"{pre}_{counts[pre]}"
    comps = []
    for r in refs:
        c = {k: v for k, v in job.comps[r].items() if k in ("symbol", "footprint", "value", "mpn", "edge") and v}
        comps.append({"ref": local[r], **c})
    nets, ports = {}, {}
    for n, pins in job.nets.items():
        mine = [p for p in pins if p.split(".")[0] in refs]
        if not mine:
            continue
        leaves = len(mine) < len(pins) or job.is_rail(n)
        name = re.sub(r"[^A-Za-z0-9]", "_", n.lstrip("/")).strip("_").upper().replace("__", "_") or "N"
        if job.is_gnd(n):
            name = "GND"
        key = name if leaves else "_" + name
        nets[key] = [f"{local[p.split('.')[0]]}.{p.split('.', 1)[1]}" for p in mine]
        if leaves:
            v = job.voltage(n)
            ports[key] = "ground" if job.is_gnd(n) else (f"was {n}" + (f" ({v:g} V)" if v else ""))
    near = [{"ref": local[x["ref"]], "to": f"{local[x['to'].split('.')[0]]}.{x['to'].split('.', 1)[1]}", "max_mm": x.get("max_mm", 5)}
            for x in (job.spec.get("floorplan") or {}).get("near") or [] if x.get("ref") in refs and str(x.get("to", "")).split(".")[0] in refs]
    nc = [f"{local[p.split('.')[0]]}.{p.split('.', 1)[1]}" for p in job.spec.get("no_connect") or [] if p.split(".")[0] in refs]
    why = (job.spec.get("rationale") or {}).get("parts") or {}
    block = {"id": re.sub(r"[^a-z0-9_]", "_", bid.lower()), "title": title or bid, "summary": summary or f"{len(refs)} parts taken from {job.name}.",
             "tags": ["saved"], "params": {}, "ports": ports, "components": comps, "nets": nets, "near": near,
             "notes": [f"{local[r]}: {why[r]}" for r in refs if why.get(r)], "source": f"saved from {job.name} ({', '.join(refs)})"}
    if nc:
        block["no_connect"] = nc
    return block


def save(block: dict) -> Path:
    USER.mkdir(parents=True, exist_ok=True)
    f = USER / f"{block['id']}.json"
    f.write_text(json.dumps({k: v for k, v in block.items() if not k.startswith("_")}, indent=1, ensure_ascii=False), encoding="utf-8")
    return f


@addon(id="aid.saveblock", title="Save parts as a block", kind="aid", group="Design aids", status="beta",
       options=[{"key": "refs", "label": "Parts (for example U2 C5 C6 D3)", "type": "text", "default": ""},
                {"key": "id", "label": "Block id (short, no spaces)", "type": "text", "default": ""},
                {"key": "title", "label": "Title", "type": "text", "default": ""}],
       summary="Turn a group of parts from this board into a reusable block: nets that leave the group become its ports. Use it once a circuit has proven itself.")
def save_block(r):
    refs = re.findall(r"[A-Z]+\d+", str(r.opts.get("refs") or "").upper())
    bid = str(r.opts.get("id") or "").strip()
    if not refs or not bid:
        r.summary = "give the parts and a block id"
        r.finding("note", "Nothing saved", "Fill in which parts belong to the block and an id for it.")
        return
    b = from_job(r.job, refs, bid, str(r.opts.get("title") or ""))
    probs = check(b)
    f = save(b)
    r.write(f"{b['id']}.json", json.dumps(b, indent=1, ensure_ascii=False), "Block file (copy)")
    for p in probs:
        r.finding("warning", "The block does not validate on its own", p)
    internal = [n for n in b["nets"] if n.startswith("_")]
    r.finding("note", f"Saved to {f}", f"Ports: {', '.join(b['ports']) or 'none'}. Internal nets: {len(internal)}. Edit the file to add parameters, better port descriptions and notes.")
    r.data["block"] = b["id"]
    r.summary = f"block {b['id']}: {len(b['components'])} part(s), {len(b['ports'])} port(s)"
