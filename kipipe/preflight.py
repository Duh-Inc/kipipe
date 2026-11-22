"""Before the design: ask what the request leaves open and gather what the design will need.

One small model call reads the request and returns (a) questions whose answer would change the circuit and
(b) the ICs and modules the design will use, named. Code then checks each part against what is on this machine:
a datasheet record in the part database, a symbol (with its footprint) in the KiCad or user libraries, a 3D model.

If nothing is open the run carries on without a word. Otherwise it stops once, shows the questions and the missing
pieces, and continues with the answers. The planned parts are handed to the designer so the design uses the parts
that were just checked. Skipped for spec files and schematics (there is nothing to plan).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import kicad_lib as kl
from .router import DATA

SYSTEM = """A circuit board is about to be designed from the request below by another model. Before that, do two things.

1. QUESTIONS. Ask only what the request does not answer AND whose answer would change the circuit: where power comes
from and at what voltage, how much current a load draws, which connector, how a microcontroller is programmed, a
size it must fit, anything in the request that contradicts itself. Do not ask about things with an obvious default
(resistor sizes, LED colour, layer count) or things the request already states. Zero questions is the right answer
for a complete request. At most 4. Give each 2 to 4 short options; the first option is what you would assume.

2. PARTS. List every IC and module the design will need: the ones the request names, and the ones you would pick for
functions it leaves open (a regulator, a charger, a level shifter). Use the exact name you would search a
distributor for, including the voltage or package suffix where it matters ("AMS1117-3.3", "ESP32-WROOM-32E",
"TPS54202DDCR"). Do not list resistors, capacitors, inductors, LEDs, plain diodes, plain transistors, switches or
connectors.

Reply with one JSON object and nothing else:
{"questions": [{"q": "question", "why": "what depends on it, one short sentence", "options": ["assumed answer", "alternative"]}],
 "parts": [{"name": "exact part name", "role": "what it does on this board", "named": true}]}
"named" is true when the request names the part, false when you chose it."""


# ---------------------------------------------------------------------------
# symbol name index: fast (text scan, cached), so thousands of library symbols can be searched before a design
def _lib_names(path: Path) -> list:
    if path.is_dir():
        return [f.stem for f in path.glob("*.kicad_sym")]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [n for n in re.findall(r'^(?:\t|  )\(symbol "([^"]+)"', text, re.M) if not re.search(r"_\d+_\d+$", n)]


def symbol_index() -> dict:
    """{normalised name: ["Lib:Name", ...]} for every symbol library KiCad and the user have."""
    libs = kl.symbol_libs()
    sig = hashlib.sha256(json.dumps(sorted((k, str(p), p.stat().st_mtime_ns if p.exists() else 0) for k, p in libs.items())).encode()).hexdigest()[:16]
    cache = DATA / ".cache" / "symindex.json"
    try:
        d = json.loads(cache.read_text())
        if d.get("sig") == sig:
            return d["index"]
    except Exception:
        pass
    index = {}
    for lib, path in libs.items():
        for name in _lib_names(Path(path)):
            index.setdefault(_n(name), []).append(f"{lib}:{name}")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"sig": sig, "index": index}))
    except OSError:
        pass
    return index


def _n(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def find_symbols(name: str, index: dict | None = None, limit: int = 4) -> list:
    """Library symbols for a part name: exact first, then symbols that extend the name (a package suffix) or that
    the name extends (an ordering suffix like 'R' or '-TR')."""
    index = index if index is not None else symbol_index()
    key = _n(name)
    if len(key) < 3:
        return []
    out = list(index.get(key, []))
    if len(out) < limit and len(key) >= 5:
        ext = sorted((k for k in index if k != key and k.startswith(key) and len(k) - len(key) <= 8), key=len)
        shorter = sorted((k for k in index if k != key and len(k) >= 5 and key.startswith(k) and len(key) - len(k) <= 4), key=lambda k: -len(k))
        for k in ext + shorter:
            for lid in index[k]:
                if lid not in out:
                    out.append(lid)
    if not out:
        # an ordering code (MCP73831T-2ACI/OT) against library names (MCP73831-2-OT): match on the part family
        m = re.match(r"[A-Z]+\d+[A-Z]?\d*", re.sub(r"[^A-Z0-9]", "", str(name).upper()))
        core = m.group(0) if m else ""
        if len(core) >= 6:
            for k in sorted((k for k in index if k.startswith(core)), key=len):
                out += [lid for lid in index[k] if lid not in out]
    mine = [x for x in out if x.startswith("kipipe_parts:")]
    return (mine + [x for x in out if x not in mine])[:limit]


def _footprint_of(lib_id: str):
    """(default footprint id or None, exists, has 3D model or None if it cannot be told)."""
    try:
        fp = kl.symbol_property(kl.get_symbol(lib_id), "Footprint") or None
    except Exception:
        return None, False, None
    if not fp:
        return None, False, None
    path = kl.footprint_path(fp)
    if not path or not Path(path).exists():
        return fp, False, None
    model = None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        m = re.search(r'\(model\s+"?([^"\s)]+)', text)
        if m:
            ref = m.group(1)
            model = _model_exists(ref)
        else:
            model = False
    except OSError:
        pass
    return fp, True, model


def _model_exists(ref: str):
    import os
    m = re.match(r"\$\{(\w+)\}/?(.*)", ref)
    if not m:
        return Path(ref).exists()
    var, rest = m.groups()
    roots = [os.environ.get(var)] if os.environ.get(var) else []
    if "3DMODEL" in var:
        roots += ["/usr/share/kicad/3dmodels", "/app/share/kicad/3dmodels", str(Path.home() / ".var/app/org.kicad.KiCad/data/kicad/3dmodels"),
                  "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad/3dmodels"]
    roots = [r for r in roots if r and Path(r).is_dir()]
    if not roots:
        return None                     # the model folder cannot be found from here: say nothing rather than guess
    stem = Path(rest).with_suffix("")
    return any((Path(r) / stem).with_suffix(ext).exists() for r in roots for ext in (".step", ".stp", ".wrl", ".STEP"))


# ---------------------------------------------------------------------------
def plan(o, workdir, log) -> dict:
    """The model call. Returns {"questions": [...], "parts": [...], "model": who}; empty lists if the reply is unusable."""
    from . import llm
    from .addons import aids, projects
    be = llm.Pool(o, Path(workdir), log).get("preflight")
    if hasattr(be, "session"):
        be.session = None
    text = "REQUEST\n" + (o.prompt or "").strip()
    ctx = projects.prompt_block(o, workdir)
    if ctx:
        text += "\n\n" + ctx
    hints = aids.brief(o.prompt or "")
    if hints:
        text += "\n\nA keyword scan suggests these may be open (ignore any the request does answer): " + "; ".join(h["question"] for h in hints)
    if o.images:
        text += f"\n\n({len(o.images)} image(s) are attached to the request and will be seen by the designer; do not ask about what a sketch would show.)"
    kw = {"tools": []} if isinstance(be, llm.ClaudeCode) else {}
    out = be.ask(text, SYSTEM, **kw)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    qs = [q for q in d.get("questions") or [] if isinstance(q, dict) and str(q.get("q") or "").strip()][:4]
    for q in qs:
        q["options"] = [str(x) for x in (q.get("options") or []) if str(x).strip()][:4]
    parts = [p for p in d.get("parts") or [] if isinstance(p, dict) and str(p.get("name") or "").strip()
             and not re.search(r"^(logic.level |low.side |high.side |n.channel |p.channel |small.signal |schottky |tvs |zener )*(n-?|p-?)?(mosfet|transistor|bjt|diode|fet)\b",
                               str(p.get("role") or ""), re.I)][:16]
    return {"questions": qs, "parts": parts, "model": llm._who(be)}


def assess(pf: dict, log=None) -> dict:
    """Check each planned part against this machine: datasheet record, symbol, footprint, 3D model."""
    from . import partdb
    index = symbol_index()
    for p in pf.get("parts", []):
        name = str(p["name"]).strip()
        rec = partdb.find(name)
        p["record"] = (rec.get("identity") or {}).get("mpn") if rec else None
        syms = find_symbols(name, index)
        p["symbols"] = syms
        p["symbol_exact"] = bool(syms) and (_n(syms[0].split(":")[1]) == _n(name) or _n(syms[0].split(":")[1]).startswith(_n(name)) or _n(name).startswith(_n(syms[0].split(":")[1])))
        p["footprint"], p["footprint_ok"], p["model3d"] = _footprint_of(syms[0]) if syms else (None, False, None)
        need = []
        if not p["record"]:
            need.append("datasheet")
        if not syms:
            need.append("symbol")
        elif not p["footprint_ok"]:
            need.append("footprint")
        p["needs"] = need
    pf["open"] = bool(pf.get("questions")) or any(p["needs"] for p in pf.get("parts", []))
    return pf


def designer_block(o) -> str:
    """What the preflight settled, for the designer's prompt."""
    out = []
    ans = (getattr(o, "answers", "") or "").strip()
    if ans:
        out.append("ANSWERS FROM THE USER to questions asked before the design (these are requirements):\n" + ans)
    plan_ = getattr(o, "plan", None) or []
    if plan_:
        lines = ["PARTS PLANNED BEFORE THE DESIGN. Their datasheets and library parts were checked or supplied for these exact parts, "
                 "so use them unless one cannot do the job (say so in the assumptions if you swap one):"]
        for p in plan_:
            sym = f" Symbol: {p['symbols'][0]}." if p.get("symbols") else " No library symbol was found: pick the closest generic symbol and say so."
            fp = f" Footprint: {p['footprint']}." if p.get("footprint") and p.get("footprint_ok") else ""
            lines.append(f"- {p['name']}: {p.get('role', '')}.{sym}{fp}" + (" Datasheet record available." if p.get("record") else ""))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def answers_text(pf: dict, answers: dict) -> str:
    """{question index: answer} -> the text given to the designer. Unanswered questions take their first option."""
    lines = []
    for i, q in enumerate(pf.get("questions", [])):
        a = str(answers.get(str(i), answers.get(i, "")) or "").strip()
        assumed = not a
        if not a:
            a = (q.get("options") or ["no preference: use your judgement"])[0]
        lines.append(f"- {q['q']} -> {a}" + (" (not answered; assumed)" if assumed else ""))
    return "\n".join(lines)


def ask_terminal(pf: dict) -> tuple:
    """Interactive version for the command line. Returns (answers, datasheets, libraries)."""
    answers, sheets, libs_ = {}, [], []
    if pf.get("questions"):
        print("\nBefore the design, a few questions (Enter takes the first option):")
    for i, q in enumerate(pf.get("questions", [])):
        print(f"\n  {q['q']}\n    {q.get('why', '')}")
        for n, opt in enumerate(q.get("options", []), 1):
            print(f"    {n}. {opt}")
        a = input("  > ").strip()
        if a.isdigit() and 1 <= int(a) <= len(q.get("options", [])):
            a = q["options"][int(a) - 1]
        answers[str(i)] = a
    need = [p for p in pf.get("parts", []) if p["needs"]]
    if need:
        print("\nParts the design will use that are missing something here (Enter skips):")
    for p in need:
        print(f"\n  {p['name']} ({p.get('role', '')}): no {', no '.join(p['needs'])}")
        if "datasheet" in p["needs"]:
            a = input("    datasheet PDF, file or link > ").strip()
            if a:
                sheets.append(a)
        if "symbol" in p["needs"] or "footprint" in p["needs"]:
            a = input("    library zip from SnapEDA, Ultra Librarian or the maker (symbol, footprint, 3D model) > ").strip()
            if a:
                libs_.append(a)
    return answers, sheets, libs_
