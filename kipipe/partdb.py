"""Part database: datasheet facts read once, stored, reused.

A datasheet PDF is read once (two independent model reads, compared field by field; only the disagreements go to a
stronger model), and the result is stored as one JSON record per part. Every later design reads the record instead
of the PDF. Records are the source of truth; a SQLite index (rebuilt from them at any time) makes them searchable,
and the core can be exported as CSV.

  ~/.local/share/kipipe/partdb/
    parts/<MPN>.json     one record per part (one file per part so the folder can later live in git)
    pdfs/                the datasheets that were read
    index.sqlite         search index, derived from parts/

Record core (what checks can rely on):
  identity       mpn, aliases, manufacturer, description, category, revision, packages
  pins           name, numbers per package, type, functions
  ratings        kind (abs_max / recommended / electrical / thermal / timing), param, symbol, min/typ/max, unit,
                 conditions
  support_parts  what the part needs around it (pull-ups, decoupling, bootstrap caps), between which pins
  application_circuit   the datasheet's typical circuit, in kipipe's spec format (IC pins by name)
  equations      design equations in a form that can be computed, with every variable defined
Every item carries the datasheet page it came from and "chk": agreed | resolved | single.
Everything else the datasheet says that matters goes in "extra" (free-form).

Tiers, for deciding what needs a datasheet at all:
  skip       passives (R, C, ...)
  api        discretes whose few key numbers come from distributor parameter data (phase 2)
  datasheet  ICs and modules: always read
A light model may move a part UP a tier (an unusual manufacturer-specific passive); nothing moves a part down.
"""
from __future__ import annotations

import ast
import concurrent.futures as cf
import csv
import hashlib
import json
import math
import random
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from .router import DATA

DB = DATA / "partdb"
PARTS = DB / "parts"
PDFS = DB / "pdfs"
INDEX = DB / "index.sqlite"
SCHEMA = 1

SKIP, API, DATASHEET = "skip", "api", "datasheet"
TIER_ORDER = {SKIP: 0, API: 1, DATASHEET: 2}


# ---------------------------------------------------------------------------
# units and values
# ---------------------------------------------------------------------------

PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
BASE_UNITS = {"V", "A", "W", "Ω", "Hz", "F", "H", "s", "°C", "%", "dB", "ppm", "bps", "g", "J"}


def norm_unit(u) -> str:
    u = (u or "").strip()
    for a, b in (("ohms", "Ω"), ("Ohms", "Ω"), ("ohm", "Ω"), ("Ohm", "Ω"), ("OHM", "Ω"), ("ºC", "°C"), ("degC", "°C"),
                 ("deg C", "°C"), ("sec", "s")):
        u = u.replace(a, b)
    return u


def to_si(v, unit):
    """(value in base unit, base unit). Unknown or compound units come back unchanged."""
    if v is None:
        return None, norm_unit(unit)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None, norm_unit(unit)
    u = norm_unit(unit)
    if not u or u in BASE_UNITS:
        return v, u
    if len(u) > 1 and u[0] in PREFIX and u[1:] in BASE_UNITS:
        return v * PREFIX[u[0]], u[1:]
    return v, u


_VAL = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([pnuµμmkKMGR]?)\s*([0-9]*)\s*(Ω|ohms?|F|H|V|A|Hz|W)?", re.I)


def parse_value(text):
    """'4.7k' -> 4700, '100nF' -> 1e-7, '4k7' -> 4700, '1R5' -> 1.5, '10 µH' -> 1e-5. None if not a number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _VAL.match(str(text).replace(",", "."))
    if not m:
        return None
    num, pre, frac = m.group(1), m.group(2), m.group(3)
    if frac:
        num = f"{num}.{frac}" if "." not in num else num + frac
    v = float(num)
    if pre and pre.upper() != "R":
        v *= {"p": 1e-12, "n": 1e-9, "u": 1e-6, "U": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
              "M": 1e6, "G": 1e9, "g": 1e9, "N": 1e-9, "P": 1e-12}.get(pre, 1.0)
    return v


def close(a, b, rel=0.03) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a == b:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-30)


# ---------------------------------------------------------------------------
# equations: parsed and evaluated safely (no eval)
# ---------------------------------------------------------------------------

FUNCS = {"sqrt": math.sqrt, "ln": math.log, "log": math.log, "log10": math.log10, "exp": math.exp, "min": min,
         "max": max, "abs": abs, "pi": math.pi, "e": math.e}
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b, ast.Pow: lambda a, b: a ** b}


def split_equation(expr: str):
    """'VOUT = VREF * (1 + R1/R2)' -> ('VOUT', parsed right side). Raises ValueError if it isn't computable."""
    if "=" not in expr:
        raise ValueError("no '=' in equation")
    lhs, rhs = expr.split("=", 1)
    lhs = lhs.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", lhs):
        raise ValueError(f"left side {lhs!r} isn't a single variable")
    rhs = rhs.replace("^", "**").replace("×", "*").replace("·", "*").replace("−", "-").strip()
    try:
        tree = ast.parse(rhs, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"can't parse {rhs!r}: {e.msg}")
    for n in ast.walk(tree):
        if isinstance(n, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub, ast.UAdd, ast.Constant, ast.Name, ast.Load,
                          ast.Call, *_OPS)):
            if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in FUNCS):
                raise ValueError("only sqrt, ln, log10, exp, min, max, abs are allowed")
            continue
        raise ValueError(f"{type(n).__name__} isn't allowed in an equation")
    return lhs, tree


def variables(tree) -> set:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {x for x in names if x not in calls and x not in ("pi", "e")}


def evaluate(tree, values: dict) -> float:
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id in values:
                return float(values[n.id])
            if n.id in FUNCS and not callable(FUNCS[n.id]):
                return FUNCS[n.id]
            raise KeyError(n.id)
        if isinstance(n, ast.BinOp):
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp):
            return -ev(n.operand) if isinstance(n.op, ast.USub) else ev(n.operand)
        if isinstance(n, ast.Call):
            return FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(type(n).__name__)
    return ev(tree)


def check_equation(eq: dict) -> str | None:
    """None if the equation is computable and every variable is defined; otherwise why not."""
    try:
        lhs, tree = split_equation(eq.get("expr", ""))
    except ValueError as e:
        return str(e)
    defined = set((eq.get("vars") or {}).keys())
    missing = (variables(tree) | {lhs}) - defined
    return f"undefined variables: {', '.join(sorted(missing))}" if missing else None


def compute(eq: dict, values: dict) -> float:
    """Evaluate an equation's right side with the given variable values (SI units)."""
    _, tree = split_equation(eq["expr"])
    return evaluate(tree, values)


# ---------------------------------------------------------------------------
# PDF -> pages, and choosing which pages each read needs
# ---------------------------------------------------------------------------

def pdf_pages(path: Path) -> list[str]:
    r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {r.stderr[-300:]}")
    pages = r.stdout.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    if not any(p.strip() for p in pages):
        raise RuntimeError("the PDF has no text layer (a scan?); this needs an OCR'd datasheet")
    return pages


GROUP_KEYS = {
    "pins": [(r"pin (configuration|function|description|assignment|definition|layout)s?", 8), (r"pin ?out", 5),
             (r"terminal functions", 8), (r"pin (no|name|number)\b", 5), (r"\bpin\b", 1), (r"\bI/O\b", 1),
             (r"strapping", 3), (r"\bGPIO\d", 1)],
    "ratings": [(r"absolute maximum", 9), (r"recommended operating", 9), (r"electrical characteristics", 7),
                (r"thermal (information|characteristics|resistance)", 3), (r"(timing|switching) (requirements|characteristics)", 3),
                (r"\bMIN\b.*\bMAX\b", 3), (r"specifications", 2), (r"\bUNIT\b", 1)],
    "application": [(r"typical application", 9), (r"application (information|circuit|schematic|note)", 7),
                    (r"design (requirements|procedure|example)", 7), (r"detailed design", 7),
                    (r"(power supply|supply) recommendations", 4), (r"layout (guidelines|example|considerations)", 3),
                    (r"(decoupling|bypass) capacitor", 3), (r"pull-?up", 2), (r"(selecting|selection of) (the )?(inductor|capacitor|resistor)", 5),
                    (r"(equation|eq\.)\s*\(?\d", 3), (r"\b[A-Z][A-Z0-9_]{1,8}\s*=\s*[A-Z0-9(]", 1)],
}
NOISE = [r"package outline", r"example (board layout|stencil)", r"important notice", r"revision history",
         r"package option addendum", r"package materials information", r"tape and reel", r"mechanical data",
         r"ordering information", r"land pattern", r"soldering (profile|information)", r"marking"]


def select_pages(pages: list[str], group: str, budget: int) -> str:
    """The pages most likely to hold this group's facts, in page order, with '=== page N ===' markers."""
    scored = []
    for i, p in enumerate(pages):
        head = p[:400].lower()
        if any(re.search(n, head) for n in NOISE):
            continue
        s = sum(w * min(3, len(re.findall(k, p, re.I))) for k, w in GROUP_KEYS[group])
        if s > 0:
            scored.append((s, i))
    scored.sort(reverse=True)
    take, used = set(), 0
    first = pages[0][:3000] if pages else ""
    used += len(first)
    for s, i in scored:
        if i == 0:
            continue
        if used + len(pages[i]) > budget and take:
            continue
        take.add(i)
        used += len(pages[i])
        if used >= budget:
            break
    out = [f"=== page 1 (start) ===\n{first}"]
    for i in sorted(take):
        out.append(f"=== page {i + 1} ===\n{_squeeze(pages[i])}")
    return "\n\n".join(out)


FIGURE_KEYS = [(r"typical application", 6), (r"application (schematic|circuit|diagram)", 6), (r"simplified schematic", 4),
               (r"reference (design|schematic)", 4), (r"schematic", 1), (r"figure \d+", 1)]


def figure_pages(pages: list[str], n: int = 2) -> list[int]:
    """0-based indexes of the pages most likely to hold the typical application circuit drawing."""
    scored = []
    for i, p in enumerate(pages):
        if any(re.search(x, p[:400].lower()) for x in NOISE):
            continue
        sc = sum(w * min(2, len(re.findall(k, p, re.I))) for k, w in FIGURE_KEYS)
        if sc >= 6:
            scored.append((sc, i))
    return [i for _, i in sorted(scored, reverse=True)[:n]]


def render_pages(pdf: Path, idx: list[int], out_dir: Path, dpi: int = 110) -> list[Path]:
    """PNG renders of some pages (for figures: application circuits are drawings, not text)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for i in idx:
        base = out_dir / f"page{i + 1}"
        r = subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(i + 1), "-l", str(i + 1), "-singlefile",
                            str(pdf), str(base)], capture_output=True, timeout=120)
        if r.returncode == 0 and base.with_suffix(".png").exists():
            got.append(base.with_suffix(".png"))
    return got


def _squeeze(t: str) -> str:
    """Layout text has long runs of spaces; collapse them (keeps columns readable, saves tokens)."""
    return re.sub(r"[ \t]{3,}", "   ", t)


# ---------------------------------------------------------------------------
# what each read is asked for
# ---------------------------------------------------------------------------

SYSTEM = """You extract facts from an electronic component datasheet into JSON for a parts database that
circuit-design checks rely on. Rules:
- Only what the text states. Never fill in from memory; leave out what the pages don't give.
- Numbers are JSON numbers; the unit goes in its own field exactly as printed (V, mV, mA, uA, kOhm, MHz, °C, %).
- "±25" means min -25 AND max 25. A limit given relative to another quantity (0.7 × VCC) goes in "conditions"
  word for word, and also in "min_expr"/"max_expr" as an expression (e.g. "0.7 * VCC").
- "page" is the number from the nearest preceding '=== page N ===' marker.
- Output ONE JSON object and nothing else."""

PROMPTS = {
    "pins": """From these datasheet pages, give the part's identity and every pin.
{
 "identity": {"mpn": "the base part number as printed, e.g. TCA9548A",
              "aliases": ["orderable part numbers / variants listed, e.g. TCA9548APWR"],
              "manufacturer": "...", "description": "one line", "category": "e.g. I2C multiplexer, buck regulator, MCU module",
              "revision": "datasheet document number and revision/date as printed",
              "packages": ["package names as printed, e.g. TSSOP-24 (PW)"]},
 "pins": [{"name": "SDA", "numbers": {"<package>": "23"}, "type": "input|output|bidirectional|open_drain|power|ground|analog|nc|passive",
           "functions": ["every function/alternate function listed"], "description": "short", "page": 4}]
}
One entry per pin NAME; a name on several pins (GND) gets all its numbers, comma separated ("4, 12").
Use the package names from "packages" as the keys of "numbers".""",

    "ratings": """From these datasheet pages, list the ratings and characteristics.
{
 "ratings": [{"kind": "abs_max|recommended|electrical|thermal|timing", "param": "Supply voltage", "symbol": "VCC",
              "min": -0.5, "typ": null, "max": 6, "unit": "V", "conditions": "as printed, or empty", "page": 5}]
}
Every row of the absolute maximum and recommended operating tables. From the electrical characteristics take the rows
a designer checks: logic thresholds (VIH, VIL, VOL, VOH), supply and quiescent current, reference/feedback voltage,
output current or current limit, on-resistance, switching frequency, dropout, leakage, input capacitance. A row with
values for several conditions becomes one entry per condition. At most 80 entries.""",

    "application": """From these datasheet pages, give what the part needs around it, its typical application
circuit, and its design equations.
{
 "support_parts": [{"what": "pull-up resistor", "kind": "R|C|L|D|Q|crystal|ferrite|other", "value": "10k or a range like 1k-10k",
                    "between": ["SDA", "VCC"], "required": true, "why": "open-drain bus", "page": 19}],
 "application_circuit": {"title": "...", "page": 19, "ic_ref": "U1",
     "components": [{"ref": "R1", "kind": "R", "value": "10k"}],
     "nets": {"VCC": ["U1.VCC", "R1.1"], "SDA": ["U1.SDA", "R1.2"]}},
 "equations": [{"name": "output voltage", "expr": "VOUT = VREF * (1 + R1 / R2)",
                "vars": {"VOUT": {"unit": "V", "from": "result"}, "VREF": {"unit": "V", "from": "rating", "symbol": "VREF"},
                         "R1": {"unit": "ohm", "from": "component", "ref": "R1"}, "R2": {"unit": "ohm", "from": "component", "ref": "R2"}},
                "page": 20, "notes": ""}],
 "layout_notes": ["short, only what the pages say"],
 "extra": {"anything else a designer must know (strapping, power sequencing, start-up conditions), as short key: value"}
}
- support_parts: "between" uses the IC's pin names, or net names like VCC/GND.
- application_circuit: null if the pages show none. IC pins by NAME ("U1.SDA"); two-terminal parts "1"/"2";
  diodes "A"/"K"; transistors "G"/"D"/"S" or "B"/"C"/"E". Use the refs printed in the figure if it has them.
- equations: one variable alone on the left; only + - * / ** ( ) and sqrt, ln, log10, exp, min, max; every variable
  in "vars" with its unit (V, A, ohm, F, H, Hz, s, W) and where it comes from: result, rating (with symbol),
  component (with ref in the application circuit), or input (a design choice like VIN or IOUT). Write the
  equation exactly as the datasheet gives it.""",
}

GROUP_FIELDS = {"pins": ("identity", "pins"), "ratings": ("ratings",),
                "application": ("support_parts", "application_circuit", "equations", "layout_notes", "extra")}


# ---------------------------------------------------------------------------
# document conventions: rules that apply to the whole datasheet, wherever they're printed
# ---------------------------------------------------------------------------

CONVENTION_KEYS = [
    r"note to (the )?reader", r"unless otherwise (noted|specified|stated|indicated)", r"pin numbers? (shown|given|listed|refer|are for)",
    r"(all )?voltages? (are )?(referred|referenced|with respect|relative) to", r"specifications? (which )?apply over",
    r"(denotes|indicates) (the )?specifications?", r"guaranteed by (design|characterization)", r"not (production |100%? )?tested",
    r"(expressed|given|shown|treated|written) as (positive|negative|absolute|magnitudes?)", r"as if it were a (positive|negative)",
    r"(negative|positive) sign", r"typical values? (are|represent|indicate)", r"(min|max)imum and (max|min)imum values",
    r"(currents?|current flow) into (the|a) (pin|device)", r"absolute values?", r"sign convention", r"conventions?",
    r"all (specifications|limits|parameters) (are|apply)", r"T_?A ?= ?25 ?°?C", r"(package|pinout) (shown|drawing) (is|refers)",
]


def scan_conventions(pages: list[str], budget: int = 12000) -> list[dict]:
    """Every passage in the whole document that sets a rule for reading the rest: sign conventions, table-wide
    conditions, which package pin numbers refer to, what typical means. Plain text search; no model."""
    hits = []
    for i, p in enumerate(pages):
        for k in CONVENTION_KEYS:
            for m in re.finditer(k, p, re.I):
                a = max(0, p.rfind("\n\n", 0, m.start()))
                a = max(a, m.start() - 500)
                b = p.find("\n\n", m.end())
                b = min(b if b > 0 else len(p), m.end() + 700)
                hits.append((i, a, b))
    hits.sort()
    merged = []
    for i, a, b in hits:
        if merged and merged[-1][0] == i and a <= merged[-1][2] + 80:
            merged[-1][2] = max(merged[-1][2], b)
        else:
            merged.append([i, a, b])
    out, used = [], 0
    for i, a, b in merged:
        t = _squeeze(pages[i][a:b]).strip()
        if len(t) < 20 or used + len(t) > budget:
            continue
        out.append({"page": i + 1, "text": t})
        used += len(t)
    return out


CONVENTIONS = """These passages come from all over one datasheet. Find the rules they set for reading the REST of the
datasheet: how values are written (a negative regulator written as positive magnitudes, currents into a pin as
positive), conditions that apply to a whole table ("TA = 25°C, VCC = 5V unless otherwise noted"), which package the
pin numbers in figures refer to, what a symbol or footnote marker means (a dot = over the full temperature range,
"guaranteed by design"). Ignore ordinary content that isn't a rule.
Return ONE JSON object:
{
 "rules": [{"applies_to": "all|ratings|pins|equations|application|<table name>", "rule": "short, plain statement",
            "effect": "sign|conditions|package|reference|guarantee|units|other", "page": 9, "quote": "the key words, verbatim"}],
 "sign": "as_printed" or "magnitudes" (magnitudes = negative quantities are written as positive numbers),
 "table_conditions": [{"table": "Electrical Characteristics", "conditions": "TA = 25°C, VIN = 7V, ...", "page": 4}],
 "pin_package": "the package that figure/application pin numbers refer to, or empty"
}
Only what the passages state. {"rules": []} if they set none.

Passages:
"""


SIGN_NOTES = [r"note to (the )?reader", r"as if it were a (positive|negative)", r"(absolute|positive) values?",
              r"(expressed|shown|given|written) as (positive|negative|absolute|magnitudes?)", r"sign convention",
              r"currents? (flowing )?(into|out of) (the|a) (pin|device) (is|are) (positive|negative)", r"magnitudes?"]


def needs_convention_read(pages: list[str]) -> bool:
    text = "\n".join(pages)
    return any(re.search(k, text, re.I) for k in SIGN_NOTES)


def read_conventions(o, pages, log, workdir) -> dict:
    """One cheap read (Igor or Haiku) of the convention passages. Empty when the document has none."""
    snippets = scan_conventions(pages)
    if not snippets:
        return {"rules": [], "snippets": []}
    from .spec import load
    text = "\n\n".join(f"=== page {x['page']} ===\n{x['text']}" for x in snippets)
    try:
        out, who = _ask(o, "conventions", CONVENTIONS + text, log, workdir)
        conv = load(out)
    except Exception as e:
        log(f"  conventions read failed ({e}); continuing without them")
        return {"rules": [], "snippets": snippets, "error": str(e)}
    conv = conv if isinstance(conv, dict) else {}
    conv["snippets"] = snippets
    conv["read_by"] = who
    return conv


def conventions_block(conv: dict) -> str:
    """Conventions as an instruction block for the other reads and for design prompts."""
    if not conv:
        return ""
    lines = []
    if conv.get("sign") == "magnitudes":
        lines.append("- SIGN: this datasheet writes negative quantities as positive magnitudes. Keep the numbers as printed.")
    for r in conv.get("rules") or []:
        if isinstance(r, dict) and r.get("rule"):
            lines.append(f"- ({r.get('applies_to') or 'all'}, p{r.get('page')}) {r['rule']}")
    for t in conv.get("table_conditions") or []:
        if isinstance(t, dict) and t.get("conditions"):
            tail = "" if "otherwise" in t["conditions"].lower() else " unless the row says otherwise"
            lines.append(f"- table '{t.get('table')}' (p{t.get('page')}): every row is at {t['conditions']}{tail}")
    if conv.get("pin_package"):
        lines.append(f"- pin numbers in figures are for the {conv['pin_package']} package")
    return ("Document conventions (they apply to everything below; follow them):\n" + "\n".join(lines)) if lines else ""


# ---------------------------------------------------------------------------
# calling the models
# ---------------------------------------------------------------------------

def _ask(o, role, text, log, workdir, images=None):
    """A fresh backend for each read (no conversation carried over). A local model that fails hands the call
    to the main backend with the cloud model that step would use otherwise."""
    from . import llm
    kind, model = llm.route(o, role)
    workdir.mkdir(parents=True, exist_ok=True)

    def run(kind, model):
        be = llm.BACKENDS[kind](o, workdir, log)
        be.model = model
        if kind == "ollama":
            be.num_ctx = 32768
        who = llm._who(be)
        if isinstance(be, llm.ClaudeCode):
            # everything the model needs is in the prompt; the only tool it may use is Read, for the images
            return be.ask(text, SYSTEM, images=images or None, max_turns=2 + len(images or []),
                          tools=["Read"] if images else []), who
        return be.ask(text, SYSTEM, images=images or None), who
    try:
        return run(kind, model)
    except Exception as e:
        if kind != "ollama" or o.backend == "ollama":
            raise
        log(f"  local model failed ({e}); using the cloud model for this read")
        base = llm.ROLES[role][0]
        m = ("tier:" + llm.TIERS[base]) if o.backend in llm.OPENAI_BACKENDS else llm.normalize_model(base, o.backend)
        return run(o.backend, m)


def _budget(o, role) -> int:
    from . import llm
    kind, _ = llm.route(o, role)
    return 40000 if kind == "ollama" else 70000


def _read(o, pages, group, role, log, workdir, shuffle=False, figures=None, context=""):
    text = select_pages(pages, group, _budget(o, role))
    if shuffle:
        # the second read sees the same pages in another order, so it doesn't just repeat the first read's path
        blocks = text.split("\n\n=== ")
        head, rest = blocks[0], blocks[1:]
        rest = rest[::-1]
        text = "\n\n=== ".join([head] + rest)
    from .spec import load
    prompt = PROMPTS[group]
    if context:
        prompt = context + "\n\n" + prompt
    if figures:
        prompt += ("\n\nThe typical application circuit is a drawing, so its parts aren't in the text. Images of the "
                   "page(s) that hold it are attached (" + ", ".join(f.stem.replace("page", "page ") for f in figures)
                   + "): read the circuit from the image: every resistor, capacitor, inductor, diode and what each "
                     "connects to.")
    out, who = _ask(o, role, prompt + "\n\nDatasheet pages:\n\n" + text, log, workdir,
                    images=[str(f) for f in figures] if figures else None)
    (Path(workdir) / "reply.txt").write_text(out)
    try:
        data = load(out)
    except Exception as e:
        raise RuntimeError(f"{who} didn't return JSON for {group}: {e}; reply started {out[:200]!r}")
    return data, who


# ---------------------------------------------------------------------------
# normalizing and comparing two reads
# ---------------------------------------------------------------------------

def _k(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


PIN_TYPES = {"in": "input", "input": "input", "i": "input", "out": "output", "output": "output", "o": "output",
             "io": "bidirectional", "i/o": "bidirectional", "bidirectional": "bidirectional", "bidir": "bidirectional",
             "od": "open_drain", "open_drain": "open_drain", "opendrain": "open_drain", "open-drain": "open_drain",
             "power": "power", "pwr": "power", "p": "power", "supply": "power", "ground": "ground", "gnd": "ground",
             "g": "ground", "analog": "analog", "a": "analog", "nc": "nc", "no connect": "nc", "passive": "passive"}


def _pin_numbers(p) -> set:
    nums = set()
    for v in (p.get("numbers") or {}).values():
        for x in re.split(r"[,\s/]+", str(v)):
            if x:
                nums.add(x.upper())
    return nums


def norm_pins(pins):
    out = {}
    for p in pins or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        p["type"] = PIN_TYPES.get(str(p.get("type", "")).lower().strip(), str(p.get("type", "")).lower())
        out.setdefault(_k(p["name"]), p)
    return out


def _rating_key(r):
    sym = _k(r.get("symbol"))
    return (r.get("kind"), sym) if sym else (r.get("kind"), _k(r.get("param"))[:24])


def _rating_vals(r):
    return tuple(to_si(r.get(f), r.get("unit"))[0] for f in ("min", "typ", "max"))


def _rating_same(a, b):
    va, vb = _rating_vals(a), _rating_vals(b)
    if to_si(1, a.get("unit"))[1] != to_si(1, b.get("unit"))[1]:
        return False
    return all(close(x, y) for x, y in zip(va, vb))


def _match_ratings(A, B):
    """Pair up rating rows from two reads. Returns (pairs, only_a, only_b)."""
    pairs, used = [], set()
    by_key = {}
    for j, b in enumerate(B):
        by_key.setdefault(_rating_key(b), []).append(j)
    only_a = []
    for a in A:
        cands = [j for j in by_key.get(_rating_key(a), []) if j not in used]
        if not cands:   # fall back: same kind and identical numbers
            cands = [j for j, b in enumerate(B) if j not in used and b.get("kind") == a.get("kind") and _rating_same(a, b)
                     and any(v is not None for v in _rating_vals(a))]
        if not cands:
            only_a.append(a)
            continue
        best = next((j for j in cands if _rating_same(a, B[j])), cands[0])
        used.add(best)
        pairs.append((a, B[best]))
    return pairs, only_a, [b for j, b in enumerate(B) if j not in used]


KINDS = {"resistor": "R", "res": "R", "r": "R", "capacitor": "C", "cap": "C", "c": "C", "inductor": "L", "l": "L",
         "diode": "D", "d": "D", "schottky": "D", "transistor": "Q", "mosfet": "Q", "q": "Q", "crystal": "CRYSTAL",
         "xtal": "CRYSTAL", "ferrite": "FERRITE", "ferritebead": "FERRITE", "bead": "FERRITE"}


def _kind(k) -> str:
    return KINDS.get(_k(k), str(k or "").upper())


def _tokens(between) -> set:
    out = set()
    for x in between or []:
        for t in re.split(r"[\s/(),]+", str(x)):
            if _k(t):
                out.add(_k(t))
    return out


def _match_support(A, B):
    """Pair support parts from two reads by kind and the pins they sit between (wording of supply names varies)."""
    pairs, used, only_a = [], set(), []
    for a in A:
        ta, best, bs = _tokens(a.get("between")), None, 0.0
        for j, b in enumerate(B):
            if j in used or _kind(a.get("kind")) != _kind(b.get("kind")):
                continue
            tb = _tokens(b.get("between"))
            sc = len(ta & tb) / max(1, len(ta | tb))
            if sc == 0 and _k(a.get("what")) == _k(b.get("what")):
                sc = 0.5
            if sc > bs:
                best, bs = j, sc
        if best is not None and bs >= 0.34:
            used.add(best)
            pairs.append((a, B[best]))
        else:
            only_a.append(a)
    return pairs, only_a, [b for j, b in enumerate(B) if j not in used]


_NOVAL = {"", "none", "null", "notshown", "notspecified", "na", "seeequation", "seetext", "varies", "?"}


def _value_same(a, b):
    if _k(a) in _NOVAL and _k(b) in _NOVAL:
        return True
    va, vb = parse_value(a), parse_value(b)
    if va is None and vb is None:
        return True          # neither read gives a number: wording differences aren't a disagreement
    if va is None or vb is None:
        return False
    return close(va, vb, 0.05)


def _eq_lhs(e):
    try:
        return _k(split_equation(e.get("expr", ""))[0])
    except ValueError:
        return _k(e.get("name"))


def _eq_same(a, b):
    try:
        la, ta = split_equation(a["expr"])
        lb, tb = split_equation(b["expr"])
    except (ValueError, KeyError):
        return False
    if _k(la) != _k(lb):
        return False
    va, vb = variables(ta), variables(tb)
    if {_k(x) for x in va} != {_k(x) for x in vb}:
        return False
    rnd = random.Random(7)
    for _ in range(4):
        vals = {_k(x): rnd.uniform(1.0, 9.0) for x in va}
        try:
            ra = evaluate(ta, {x: vals[_k(x)] for x in va})
            rb = evaluate(tb, {x: vals[_k(x)] for x in vb})
        except (ZeroDivisionError, ValueError, OverflowError, KeyError):
            continue
        if not close(ra, rb, 1e-6):
            return False
    return True


def circuit_usable(c) -> bool:
    """A circuit with no parts, or whose nets touch only the IC, wasn't really read (a drawing seen as text)."""
    if not c or not isinstance(c, dict):
        return False
    comps = [x for x in c.get("components") or [] if isinstance(x, dict) and x.get("ref")]
    multi = [n for n in (c.get("nets") or {}).values() if isinstance(n, list) and len(n) >= 2]
    return bool(comps) and bool(multi)


def circuit_signature(c):
    """Ref-independent description of an application circuit: what's attached to each IC pin."""
    if not c or not isinstance(c, dict):
        return None
    ic = c.get("ic_ref") or "U1"

    def mag(v):
        # decade of a value; -99 when the read gave none (sorts fine, never equals a real value)
        return round(math.log10(abs(v)), 1) if v else -99.0
    kinds = {x.get("ref"): (str(x.get("kind", "")).upper(), mag(parse_value(x.get("value"))))
             for x in c.get("components") or [] if isinstance(x, dict)}
    sig = {}
    for name, nodes in (c.get("nets") or {}).items():
        nodes = [n for n in nodes if isinstance(n, str) and "." in n]
        pins = [n.split(".", 1)[1] for n in nodes if n.split(".", 1)[0] == ic]
        others = sorted(kinds.get(n.split(".", 1)[0], ("?", -99.0)) for n in nodes if n.split(".", 1)[0] != ic)
        for p in pins:
            sig[_k(p)] = tuple(others)
    return {"pins": sig, "components": sorted(kinds.values())}


def compare(group, a: dict, b: dict):
    """Field-by-field comparison of two reads of one group. Returns (merged, disputes); merged holds every item the
    reads agree on (chk=agreed); disputes are [{"field", "a", "b"}] for anything that differs or one read lacks."""
    merged, disputes = {}, []
    if group == "pins":
        ia, ib = a.get("identity") or {}, b.get("identity") or {}
        merged["identity"] = dict(ia or ib)
        if ia and ib and _k(ia.get("mpn")) != _k(ib.get("mpn")):
            disputes.append({"field": "identity.mpn", "a": ia.get("mpn"), "b": ib.get("mpn")})
        if ia and ib:
            merged["identity"]["aliases"] = sorted({*(ia.get("aliases") or []), *(ib.get("aliases") or [])})
        pa, pb = norm_pins(a.get("pins")), norm_pins(b.get("pins"))
        merged["pins"] = []
        for key in list(pa) + [k for k in pb if k not in pa]:
            x, y = pa.get(key), pb.get(key)
            if x and y and x.get("type") == y.get("type") and _pin_numbers(x) == _pin_numbers(y):
                funcs = {}
                for f in [*(x.get("functions") or []), *(y.get("functions") or [])]:
                    funcs.setdefault(_k(f), f)
                merged["pins"].append({**x, "functions": list(funcs.values()), "chk": "agreed"})
            else:
                disputes.append({"field": "pins", "a": x, "b": y})
    elif group == "ratings":
        pairs, oa, ob = _match_ratings([r for r in a.get("ratings") or [] if isinstance(r, dict)],
                                       [r for r in b.get("ratings") or [] if isinstance(r, dict)])
        merged["ratings"] = []
        for x, y in pairs:
            if _rating_same(x, y):
                merged["ratings"].append({**x, "chk": "agreed"})
            else:
                disputes.append({"field": "ratings", "a": x, "b": y})
        disputes += [{"field": "ratings", "a": x, "b": None} for x in oa]
        disputes += [{"field": "ratings", "a": None, "b": y} for y in ob]
    else:
        # support parts
        pairs, oa, ob = _match_support([x for x in a.get("support_parts") or [] if isinstance(x, dict)],
                                       [x for x in b.get("support_parts") or [] if isinstance(x, dict)])
        merged["support_parts"] = []
        for x, y in pairs:
            if _value_same(x.get("value"), y.get("value")) and (x.get("required") is False) == (y.get("required") is False):
                merged["support_parts"].append({**x, "chk": "agreed"})
            else:
                disputes.append({"field": "support_parts", "a": x, "b": y})
        disputes += [{"field": "support_parts", "a": x, "b": None} for x in oa]
        disputes += [{"field": "support_parts", "a": None, "b": y} for y in ob]
        # equations
        ea = {_eq_lhs(e): e for e in a.get("equations") or [] if isinstance(e, dict)}
        eb = {_eq_lhs(e): e for e in b.get("equations") or [] if isinstance(e, dict)}
        merged["equations"] = []
        for key in list(ea) + [k for k in eb if k not in ea]:
            x, y = ea.get(key), eb.get(key)
            if x and y and _eq_same(x, y):
                merged["equations"].append({**x, "chk": "agreed"})
            else:
                disputes.append({"field": "equations", "a": x, "b": y})
        # application circuit: compared as a whole, by what hangs on each IC pin
        ca, cb = a.get("application_circuit"), b.get("application_circuit")
        ca = ca if circuit_usable(ca) else None
        cb = cb if circuit_usable(cb) else None
        if not ca and not cb:
            merged["application_circuit"] = None
        elif ca and cb and circuit_signature(ca) == circuit_signature(cb):
            merged["application_circuit"] = {**ca, "chk": "agreed"}
        else:
            merged["application_circuit"] = None
            disputes.append({"field": "application_circuit", "a": ca, "b": cb})
        merged["layout_notes"] = list(dict.fromkeys([*(a.get("layout_notes") or []), *(b.get("layout_notes") or [])]))
        merged["extra"] = {**(b.get("extra") or {}), **(a.get("extra") or {})}
    return merged, disputes


# ---------------------------------------------------------------------------
# settling disagreements
# ---------------------------------------------------------------------------

RESOLVE = """Two independent reads of this datasheet disagree on the items below. For each, decide from the pages
what is correct. Each dispute shows read "a" and read "b" (null = that read didn't list it).
Return ONE JSON object:
{"resolutions": {"D1": {"item": <the correct item, same shape as a/b, with "page">},
                 "D2": {"item": null, "why": "not in the datasheet"}}}
Use null only when the item really shouldn't be in the record. Don't invent values the pages don't give.

Disputes:
"""


def _resolve(o, pages, group, disputes, log, workdir, context=""):
    if not disputes:
        return {}, None
    from .spec import load
    text = select_pages(pages, group, _budget(o, "extract_resolve"))
    out_all, who = {}, None
    for start in range(0, len(disputes), 30):
        chunk = {f"D{start + i + 1}": {"field": d["field"], "a": d["a"], "b": d["b"]}
                 for i, d in enumerate(disputes[start:start + 30])}
        reply, who = _ask(o, "extract_resolve", (context + "\n\n" if context else "") + RESOLVE + json.dumps(chunk, ensure_ascii=False, indent=1)
                          + "\n\nDatasheet pages:\n\n" + text, log, workdir)
        try:
            out_all.update((load(reply).get("resolutions") or {}))
        except Exception as e:
            log(f"  couldn't parse the resolver's answer for {group}: {e}")
    return out_all, who


def _apply(group, merged, disputes, resolutions):
    """Fold settled disputes into the merged group; unsettled ones keep read a's version marked 'single'."""
    for i, d in enumerate(disputes):
        res = resolutions.get(f"D{i + 1}")
        field = d["field"]
        if res is not None:
            item, chk = res.get("item"), "resolved"
        else:
            item, chk = d["a"] if d["a"] is not None else d["b"], "single"
        if item is None:
            continue
        if field == "identity.mpn":
            merged.setdefault("identity", {})["mpn"] = item if isinstance(item, str) else item.get("mpn", d["a"])
            continue
        if not isinstance(item, dict):
            continue
        item = {**item, "chk": chk}
        if field == "application_circuit":
            merged["application_circuit"] = item
        else:
            merged.setdefault(field, []).append(item)


# ---------------------------------------------------------------------------
# reading a datasheet into a record
# ---------------------------------------------------------------------------

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slug(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", str(mpn).strip()).upper()[:80] or "UNKNOWN"


def by_sha(sha: str) -> dict | None:
    for f in PARTS.glob("*.json") if PARTS.exists() else []:
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        if (r.get("source") or {}).get("sha256") == sha:
            return r
    return None


ELECTRICAL = """Below is a datasheet's electrical characteristics table, given two ways:
1. the grid a table parser produced, rows r0..rN. Its TEXT is exact, but where a parameter's label spans several
   rows the label sits on only one of them (often the middle one), and cells of two rows are sometimes merged
   into one ("4 4" = 4 on one row and 4 on the next);
2. an image of the same table, which shows which rows each label really spans.
Return every data row as one rating:
{"ratings": [{"kind": "electrical", "param": "...", "symbol": "...", "min": null, "typ": 1.2, "max": 1.5,
              "unit": "V", "conditions": "test conditions and the supply column for that row", "rows": ["r3"],
              "page": 5}]}
Rules: numbers come ONLY from the grid (never invent or compute one); "rows" lists the grid row(s) each rating's
numbers sit on (required: they are checked cell by cell); split merged cells back into their rows; a unit printed
once for a group of rows applies to the whole group. Output one JSON object."""

APPLICATION_DL = """From the datasheet's application section (text below) and the attached image of its typical
application circuit, give what the part needs around it and the circuit itself.
{
 "support_parts": [{"what": "pull-up resistor", "kind": "R|C|L|D|Q|crystal|ferrite|other", "value": "10k or a range",
                    "between": ["SDA", "VCC"], "required": true, "why": "open-drain bus", "page": 19}],
 "application_circuit": {"title": "...", "page": 20, "ic_ref": "U1",
     "components": [{"ref": "R1", "kind": "R", "value": "10k"}],
     "nets": {"VCC": ["U1.VCC", "R1.1"], "SDA": ["U1.SDA", "R1.2"]}},
 "layout_notes": ["short, only what the text says"],
 "extra": {"anything else a designer must know (strapping, sequencing, start-up), as short key: value"}
}
- "between" uses the IC's pin names or net names (VCC, GND).
- application_circuit: read it from the image. IC pins by NAME ("U1.SDA"); two-terminal parts "1"/"2"; diodes
  "A"/"K"; transistors "G"/"D"/"S" or "B"/"C"/"E". Use the refs printed in the figure if it has them. null if none.
- The design equations have already been extracted; don't repeat them. Output one JSON object."""


def _dedupe(lines: list, key_only=False) -> list:
    """Drop lines that say the same thing as an earlier one (two reads paraphrase each other)."""
    out, seen = [], []
    for ln in lines:
        words = set(re.findall(r"[a-z0-9]+", str(ln).lower()))
        if any(len(words & w) / max(1, len(words | w)) > 0.55 for w in seen):
            continue
        seen.append(words)
        out.append(ln)
    return out


def _ask_json(o, role, prompt, log, workdir, images=None):
    from .spec import load
    out, who = _ask(o, role, prompt, log, workdir, images=images)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    (Path(workdir) / "reply.txt").write_text(out)
    data = load(out)
    return (data if isinstance(data, dict) else {}), who


def _two_reads(o, prompt, log, work, name, images=None):
    """The same small question to the first-read and second-read models, in parallel."""
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(_ask_json, o, "extract", prompt, log, work / f"{name}_a", images)
        fb = ex.submit(_ask_json, o, "extract_check", prompt, log, work / f"{name}_b", images)
        out = []
        for f, side in ((fa, "a"), (fb, "b")):
            try:
                out.append(f.result())
            except Exception as e:
                log(f"  {name} read {side} failed: {e}")
                out.append(({}, None))
    return out


def _docling_reads(pdf, sha, pages, o, log, work, mpn):
    """Tables, pins, conditions and equations by Docling plus code. A model only for what code can't read: an
    irregular table (grid + crop), a drawn pinout or a listed rating section (text + page image), and the application
    circuit drawing (crop). Each of those is an independent task; they all run at the same time."""
    from . import docparse
    import threading
    doc = docparse.convert(pdf, sha, log)
    p = docparse.parse(doc, mpn, pdf.stem)
    for why in p.get("skipped") or []:
        log(f"  docling: skipped {why}")
    need = p.get("need") or {}
    if not p["pins"] and not p["ratings"] and not p["todo"]["electrical"]:
        raise RuntimeError("docling found no tables in this PDF (its tables may be pictures)")
    log(f"  docling: {len(p['pins'])} pins, {len(p['ratings'])} rating rows, {len(p['equations'])} equations read "
        f"without a model; left for the model: {len(p['todo']['electrical'])} irregular table(s)"
        + (", the pinout drawing" if need.get("pins") else "") + (", the absolute maximum list" if need.get("abs_max") else "")
        + (", the application figure" if p["todo"]["figure"] else ""))
    record = {"schema": SCHEMA, "identity": p["identity"], "pins": p["pins"], "ratings": list(p["ratings"]),
              "equations": p["equations"], "_parser": "docling"}
    lock = threading.Lock()
    state = {"whos": set(), "resolver": None}

    # conventions: table conditions come from the parser; a model read only when the document has a note that
    # changes how values are written (sign conventions, magnitudes), which a text search can spot
    if needs_convention_read(pages):
        conv = read_conventions(o, pages, log, work / "conventions")
    else:
        conv = {"rules": [], "snippets": []}
        log("  no sign or value conventions in the text: conventions read skipped")
    if p["table_conditions"]:
        conv["table_conditions"] = p["table_conditions"]          # exact, from the table captions
    ctx = conventions_block(conv)
    if p["notes"]:
        ctx = (ctx + "\n" if ctx else "") + "Notes printed in the datasheet: " + " ".join(p["notes"])
    pre = (ctx + "\n\n") if ctx else ""

    def note(who_pair, resolver=None):
        with lock:
            state["whos"] |= {w for w in who_pair if w}
            if resolver and not state["resolver"]:
                state["resolver"] = resolver

    def settle(group, a, b, name, extra_ctx=""):
        """Compare two reads; disputed items go to the settling model. Returns the merged group."""
        merged, disputes = compare(group, a, b)
        if disputes:
            log(f"  {name}: {len(disputes)} item(s) to settle")
            try:
                res, who = _resolve(o, pages, group, disputes, log, work / f"{name}_r", ctx + extra_ctx)
                note((), who)
            except Exception as e:
                log(f"  settling {name} failed ({e}); disputed items keep the first read, marked unchecked")
                res = {}
            _apply(group, merged, disputes, res)
        return merged

    def single(items):
        return [dict(x, chk="single") for x in items if isinstance(x, dict)]

    def irregular_table(i, t):
        img = docparse.crop(pdf, doc, t["page"], t["bbox"], work / f"table{i}.png")
        kind = t["kind"] if t["kind"] in ("abs_max", "recommended", "timing", "thermal") else "electrical"
        prompt = pre + ELECTRICAL.replace('"kind": "electrical"', f'"kind": "{kind}"') + (
            f"\n\nTable '{t['section']}' on page {t['page']}" + (f", conditions: {t['conditions']}" if t["conditions"] else "")
            + ":\n" + docparse.grid_text(t))
        (a, wa), (b, wb) = _two_reads(o, prompt, log, work, f"table{i}", [str(img)] if img else None)
        note((wa, wb))
        allowed = docparse.numbers_in(t)

        def grounded(rows):
            keep = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                for f in ("min", "typ", "max"):
                    v = r.get(f)
                    try:
                        ok = v is None or any(abs(float(v) - n) <= 1e-9 + 1e-6 * abs(n) for n in allowed)
                    except (TypeError, ValueError):
                        ok = False
                    if not ok:
                        log(f"  dropped {r.get('symbol') or r.get('param')} {f}={v}: not in the table (made up)")
                        r[f] = None
                r["kind"] = kind
                r.setdefault("page", t["page"])
                if t["conditions"]:
                    r["table_conditions"] = t["conditions"]
                keep.append(r)
            return keep
        ra, rb = grounded(a.get("ratings")), grounded(b.get("ratings"))
        ra, rb = docparse.check_cells(t, ra, log, "read a"), docparse.check_cells(t, rb, log, "read b")
        if ra and rb:
            return {"ratings": settle("ratings", {"ratings": ra}, {"ratings": rb}, f"table p{t['page']}",
                                      "\n\nThe table as parsed:\n" + docparse.grid_text(t)).get("ratings", [])}
        return {"ratings": single(ra or rb)}

    def pinout():
        sec = need["pins"]
        imgs = render_pages(pdf, [pg - 1 for pg in sec["pages"]], work / "pin_pages", dpi=130)
        prompt = (pre + PROMPTS["pins"] + "\n\nThe pinout is drawn (package diagrams), so read it from the attached "
                  "page images; the text below is what the parser found there.\n\n" + sec["text"])
        (a, wa), (b, wb) = _two_reads(o, prompt, log, work, "pins", [str(i) for i in imgs] or None)
        note((wa, wb))
        if a.get("pins") and b.get("pins"):
            pins = settle("pins", a, b, "pinout").get("pins") or []
        else:
            pins = single(a.get("pins") or b.get("pins") or [])
        return {"pins": pins, "identity": (a or b).get("identity") or {}}

    def abs_list():
        prompt = (pre + PROMPTS["ratings"] + "\n\nOnly the ABSOLUTE MAXIMUM ratings (kind abs_max). They are printed "
                  "as a list with dotted leaders ('Input Voltage ......... 20V'); a pair like '20V, -0.5V' is max and "
                  "min.\n\n" + need["abs_max"]["text"])
        (a, wa), (b, wb) = _two_reads(o, prompt, log, work, "absmax")
        note((wa, wb))
        ra = [dict(x, kind="abs_max") for x in a.get("ratings") or [] if isinstance(x, dict)]
        rb = [dict(x, kind="abs_max") for x in b.get("ratings") or [] if isinstance(x, dict)]
        if ra and rb:
            return {"ratings": settle("ratings", {"ratings": ra}, {"ratings": rb}, "absolute maximum",
                                      "\n\nThe section:\n" + need["abs_max"]["text"]).get("ratings", [])}
        return {"ratings": single(ra or rb)}

    def application():
        fig = p["todo"]["figure"]
        img = docparse.crop(pdf, doc, fig["page"], fig["bbox"], work / "figure.png") if fig else None
        if fig and img:
            log(f"  application circuit: {fig['caption']} (page {fig['page']}), sent as a crop")
        prompt = pre + APPLICATION_DL + "\n\nApplication section:\n" + (p["application_text"] or "(none found)")
        (a, wa), (b, wb) = _two_reads(o, prompt, log, work, "application", [str(img)] if img else None)
        note((wa, wb))
        if a and b:
            merged = settle("application", {**a, "equations": []}, {**b, "equations": []}, "application")
        else:
            merged, _ = compare("application", a or b, a or b)
            for it in merged.get("support_parts") or []:
                it["chk"] = "single"
            if merged.get("application_circuit"):
                merged["application_circuit"]["chk"] = "single"
        merged.pop("equations", None)
        merged["layout_notes"] = _dedupe(merged.get("layout_notes") or [])
        extra = merged.get("extra") or {}
        keep = _dedupe([f"{k}: {v}" for k, v in extra.items()])
        merged["extra"] = {k: v for k, v in extra.items() if f"{k}: {v}" in keep
                           and not re.search(r"not available|wasn't available|no image|image was not", str(v), re.I)}
        merged["_eq_fallback"] = a.get("equations") or b.get("equations") or []
        return merged

    tasks = [(f"table p{t['page']}", irregular_table, (i, t)) for i, t in enumerate(p["todo"]["electrical"])]
    if need.get("pins"):
        tasks.append(("pinout", pinout, ()))
    if need.get("abs_max"):
        tasks.append(("absolute maximum", abs_list, ()))
    tasks.append(("application", application, ()))
    t0 = time.time()
    results = {}
    with cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {ex.submit(fn, *args): name for name, fn, args in tasks}
        for f in cf.as_completed(futs):
            try:
                results[futs[f]] = f.result()
            except Exception as e:
                log(f"  {futs[f]} failed: {e}")
    log(f"  model reads ({len(tasks)} tasks in parallel): {time.time() - t0:.0f} s")
    for name, _, _ in tasks:
        r = results.get(name) or {}
        record["ratings"] += r.get("ratings") or []
        if r.get("pins"):
            record["pins"] = r["pins"]
        for k, v in (r.get("identity") or {}).items():
            if v and not record["identity"].get(k):
                record["identity"][k] = v
        if name == "application":
            eqf = r.pop("_eq_fallback", [])
            record.update(r)
            if not record.get("equations") and eqf:
                record["equations"] = single(eqf)
    return record, conv, sorted(state["whos"]), state["resolver"]


MAX_PDF = 40 * 1024 * 1024


class FetchError(RuntimeError):
    """A datasheet link could not be turned into a PDF. The message says what to do instead."""


def is_url(x) -> bool:
    return isinstance(x, str) and re.match(r"https?://", x.strip(), re.I) is not None


def by_url(url: str) -> dict | None:
    for r in all_records():
        if (r.get("source") or {}).get("url") == url:
            return r
    return None


def fetch_pdf(url: str, log=print) -> Path:
    """Download a datasheet from a direct link. Follows redirects and, if the link is a web page with exactly one
    PDF link on it, that link once. Anything else (a login page, a viewer, a site that refuses scripted downloads)
    raises FetchError: download it in a browser and add the file instead."""
    import urllib.error
    import urllib.parse
    import urllib.request
    url = url.strip()
    if not is_url(url):
        raise FetchError("not an http(s) link")
    inbox = DB / ".uploads"
    inbox.mkdir(parents=True, exist_ok=True)

    def get(u):
        req = urllib.request.Request(u, headers={"User-Agent": "kipipe datasheet reader", "Accept": "application/pdf,*/*;q=0.5"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                size = int(r.headers.get("Content-Length") or 0)
                if size > MAX_PDF:
                    raise FetchError(f"the file is {size // 1048576} MB; the limit is {MAX_PDF // 1048576} MB")
                data = r.read(MAX_PDF + 1)
                return data, r.geturl(), (r.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as e:
            why = {401: "the site wants a login", 403: "the site refuses scripted downloads", 404: "nothing at that address", 429: "the site is rate limiting"}.get(e.code, "")
            raise FetchError(f"HTTP {e.code}" + (f" ({why})" if why else "")) from None
        except urllib.error.URLError as e:
            raise FetchError(f"could not reach the site ({e.reason})") from None
        except TimeoutError:
            raise FetchError("the site did not answer in time") from None

    data, final, ctype = get(url)
    if not data.startswith(b"%PDF") and b"<html" in data[:4000].lower():
        links = sorted({urllib.parse.urljoin(final, m) for m in re.findall(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', data.decode("utf-8", "replace"), re.I)})
        if len(links) == 1:
            log(f"  that link is a web page; following its one PDF link: {links[0]}")
            data, final, ctype = get(links[0])
        else:
            raise FetchError("the link is a web page, not a PDF" + (f" (it has {len(links)} PDF links; use the one you want)" if links else ""))
    if len(data) > MAX_PDF:
        raise FetchError(f"the file is larger than {MAX_PDF // 1048576} MB")
    if not data.startswith(b"%PDF"):
        raise FetchError(f"what came back is not a PDF ({ctype or 'unknown type'})")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(urllib.parse.urlparse(final).path).name or "datasheet.pdf")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    out = inbox / (hashlib.sha256(data).hexdigest()[:6] + "-" + name)
    out.write_bytes(data)
    log(f"  downloaded {name} ({len(data) // 1024} kB)")
    return out


def read_datasheet(pdf, o, log=print, mpn: str | None = None, url: str | None = None, force=False) -> dict:
    """Read a datasheet PDF into the database (two reads, compare, settle). Returns the stored record.
    `pdf` may be a file or a direct http(s) link to one."""
    fetched = None
    if is_url(pdf):
        url = str(pdf).strip()
        if not force:
            old = by_url(url)
            if old:
                log(f"  {url} is already in the part database as {old['identity'].get('mpn')}")
                return old
        try:
            fetched = pdf = fetch_pdf(url, log)
        except FetchError as e:
            raise FetchError(f"{e}. Download the PDF in your browser and add the file instead.") from None
    try:
        return _read_datasheet(pdf, o, log, mpn, url, force)
    finally:
        if fetched:
            Path(fetched).unlink(missing_ok=True)


def _read_datasheet(pdf, o, log=print, mpn: str | None = None, url: str | None = None, force=False) -> dict:
    pdf = Path(pdf)
    sha = _sha(pdf)
    if not force:
        old = by_sha(sha)
        if old:
            log(f"  {pdf.name} is already in the part database as {old['identity'].get('mpn')}")
            return old
    pages = pdf_pages(pdf)
    PDFS.mkdir(parents=True, exist_ok=True)
    work = DB / ".work" / sha[:12]
    log(f"reading datasheet {pdf.name} ({len(pages)} pages)")
    t0 = time.time()

    from . import docparse
    record = conv = None
    if docparse.available() or (docparse.CACHE / f"{sha}.json").exists():
        try:
            record, conv, whos, resolver = _docling_reads(pdf, sha, pages, o, log, work, mpn)
        except Exception as e:
            import traceback
            tb = traceback.extract_tb(e.__traceback__)[-1]
            log(f"  docling path failed ({type(e).__name__}: {e} at {Path(tb.filename).name}:{tb.lineno}); "
                "reading with the model instead")
            record = None
    else:
        log("  docling isn't installed: tables are read by the model (install it for exact, repeatable tables)")
    if record is None:
        groups = list(PROMPTS)
        conv = read_conventions(o, pages, log, work / "conventions")
        ctx = conventions_block(conv)
        if ctx:
            log(f"  document conventions: {len(conv.get('rules') or [])} rule(s)"
                + (", negative values written as magnitudes" if conv.get("sign") == "magnitudes" else "")
                + (f", {len(conv.get('table_conditions') or [])} table-wide condition(s)" if conv.get("table_conditions") else ""))
        figs = render_pages(pdf, figure_pages(pages), work / "figures")
        if figs:
            log(f"  application circuit drawing on page {', '.join(f.stem[4:] for f in figs)}: sent as image")
        reads = {}
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {}
            for g in groups:
                fg = figs if g == "application" else None
                futs[ex.submit(_read, o, pages, g, "extract", log, work / f"{g}_a", False, fg, ctx)] = (g, "a")
                futs[ex.submit(_read, o, pages, g, "extract_check", log, work / f"{g}_b", True, fg, ctx)] = (g, "b")
            for f in cf.as_completed(futs):
                g, side = futs[f]
                try:
                    reads[(g, side)] = f.result()
                except Exception as e:
                    log(f"  {g} read {side} failed: {e}")
                    reads[(g, side)] = ({}, None)
        whos = sorted({w for _, w in reads.values() if w})
        record = {"schema": SCHEMA}
        resolver = None
        for g in groups:
            a, _ = reads[(g, "a")]
            b, _ = reads[(g, "b")]
            if not a and not b:
                raise RuntimeError(f"both reads of the {g} section failed")
            if not a or not b:
                # one read only: everything is unchecked
                only = a or b
                merged, disputes = compare(g, only, only)
                for fld in GROUP_FIELDS[g]:
                    v = merged.get(fld)
                    if isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                it["chk"] = "single"
                    elif isinstance(v, dict) and "chk" in v:
                        v["chk"] = "single"
                record.update(merged)
                continue
            merged, disputes = compare(g, a, b)
            if disputes:
                log(f"  {g}: reads agree on most items, {len(disputes)} to settle")
            try:
                res, who = _resolve(o, pages, g, disputes, log, work / f"{g}_r", ctx)
                resolver = resolver or who
            except Exception as e:
                log(f"  settling {g} failed ({e}); disputed items keep the first read, marked unchecked")
                res = {}
            _apply(g, merged, disputes, res)
            record.update(merged)

    counts = {"agreed": 0, "resolved": 0, "single": 0, "parsed": 0, "corrected": 0}
    ident = record.setdefault("identity", {})
    if mpn:
        if ident.get("mpn") and _k(ident["mpn"]) != _k(mpn):
            ident.setdefault("aliases", []).append(ident["mpn"])
        ident["mpn"] = mpn
    if not ident.get("mpn"):
        ident["mpn"] = pdf.stem
    for fld in ("pins", "ratings", "support_parts", "equations"):
        for it in record.get(fld) or []:
            counts[it.get("chk", "single")] = counts.get(it.get("chk", "single"), 0) + 1
    if record.get("application_circuit"):
        c = record["application_circuit"].get("chk", "single")
        counts[c] = counts.get(c, 0) + 1
    record["conventions"] = {k: conv.get(k) for k in ("rules", "sign", "table_conditions", "pin_package", "snippets", "read_by")
                             if conv.get(k)}
    tconds = [t for t in conv.get("table_conditions") or [] if isinstance(t, dict) and t.get("conditions")]
    if tconds:
        def _table_for(kind):
            want = {"electrical": "electrical", "abs_max": "absolute", "recommended": "recommended",
                    "thermal": "thermal", "timing": "timing"}.get(kind, kind or "")
            return next((t for t in tconds if want and want in str(t.get("table", "")).lower()), None)
        for x in record.get("ratings") or []:
            t = _table_for(x.get("kind"))
            if t and not x.get("table_conditions"):
                x["table_conditions"] = t["conditions"]
    record["gaps"] = [f for f, label in (("pins", "pins"), ("ratings", "ratings"), ("support_parts", "support parts"),
                                         ("application_circuit", "application circuit"), ("equations", "equations"))
                      if not record.get(f)]
    for eq in record.get("equations") or []:
        bad = check_equation(eq)
        if bad:
            eq["invalid"] = bad
    stored = PDFS / f"{slug(ident['mpn'])}{pdf.suffix.lower() or '.pdf'}"
    if pdf.resolve() != stored.resolve():
        shutil.copy(pdf, stored)
    record["tier"] = DATASHEET
    record["source"] = {"file": stored.name, "sha256": sha, "pages": len(pages), "url": url,
                        "parser": record.pop("_parser", "model"),
                        "revision": ident.get("revision"), "read_by": whos, "settled_by": resolver}
    n_pins, n_rat = len(record.get("pins") or []), len(record.get("ratings") or [])
    if not n_pins and not n_rat:
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"nothing could be read from {pdf.name}: no pin table and no ratings. Nothing was saved. "
                           "It may be a scanned or picture-only PDF, or not a datasheet")
    record["verification"] = {"counts": counts,
                              "status": ("no pin table read" if not n_pins else "verified" if counts.get("single", 0) == 0 else "partly unchecked")}
    record["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save(record)
    shutil.rmtree(work, ignore_errors=True)
    log(f"  {ident['mpn']}: {len(record.get('pins') or [])} pins, {len(record.get('ratings') or [])} ratings, "
        f"{len(record.get('support_parts') or [])} support parts, {len(record.get('equations') or [])} equations, "
        f"application circuit {'yes' if record.get('application_circuit') else 'no'}; "
        f"{counts['agreed']} agreed, {counts['resolved']} settled, {counts['single']} unchecked; {time.time() - t0:.0f} s")
    return record


# ---------------------------------------------------------------------------
# storage and index
# ---------------------------------------------------------------------------

def path_for(mpn: str) -> Path:
    return PARTS / f"{slug(mpn)}.json"


def save(record: dict):
    PARTS.mkdir(parents=True, exist_ok=True)
    p = path_for(record["identity"]["mpn"])
    p.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n")
    _index_one(record)


def load_record(mpn: str) -> dict | None:
    p = path_for(mpn)
    if p.exists():
        return json.loads(p.read_text())
    return find(mpn)


def all_records() -> list:
    out = []
    for f in sorted(PARTS.glob("*.json")) if PARTS.exists() else []:
        try:
            out.append(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def remove(mpn: str) -> bool:
    r = load_record(mpn)
    if not r:
        return False
    path_for(r["identity"]["mpn"]).unlink(missing_ok=True)
    src = (r.get("source") or {}).get("file")
    if src:
        (PDFS / src).unlink(missing_ok=True)
    reindex()
    return True


def find(name: str) -> dict | None:
    """The record for a part name as it appears in a spec (value, mpn, or symbol name). Ordering suffixes are
    tolerated: TCA9548APWR finds TCA9548A."""
    n = _k(name)
    if len(n) < 3:
        return None
    best, blen = None, 0
    for r in all_records():
        ident = r.get("identity") or {}
        for cand in [ident.get("mpn"), *(ident.get("aliases") or [])]:
            c = _k(cand)
            if not c:
                continue
            if n == c:
                return r
            if len(c) >= 4 and n.startswith(c) and len(c) > blen:
                best, blen = r, len(c)
    return best


def _db():
    DB.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(INDEX)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS parts(mpn TEXT PRIMARY KEY, manufacturer TEXT, description TEXT, category TEXT,
        status TEXT, revision TEXT, file TEXT, updated TEXT, aliases TEXT);
    CREATE TABLE IF NOT EXISTS pins(mpn TEXT, name TEXT, type TEXT, numbers TEXT, functions TEXT, page INT, chk TEXT);
    CREATE TABLE IF NOT EXISTS ratings(mpn TEXT, kind TEXT, param TEXT, symbol TEXT, min REAL, typ REAL, max REAL,
        unit TEXT, conditions TEXT, page INT, chk TEXT);
    CREATE INDEX IF NOT EXISTS ratings_mpn ON ratings(mpn);
    CREATE INDEX IF NOT EXISTS pins_mpn ON pins(mpn);""")
    return con


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _index_one(r, con=None):
    own = con is None
    con = con or _db()
    ident = r.get("identity") or {}
    m = ident.get("mpn")
    con.execute("DELETE FROM parts WHERE mpn=?", (m,))
    con.execute("DELETE FROM pins WHERE mpn=?", (m,))
    con.execute("DELETE FROM ratings WHERE mpn=?", (m,))
    con.execute("INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?)",
                (m, ident.get("manufacturer"), ident.get("description"), ident.get("category"),
                 (r.get("verification") or {}).get("status"), ident.get("revision"),
                 (r.get("source") or {}).get("file"), r.get("updated"), " ".join(ident.get("aliases") or [])))
    for p in r.get("pins") or []:
        con.execute("INSERT INTO pins VALUES (?,?,?,?,?,?,?)",
                    (m, p.get("name"), p.get("type"), json.dumps(p.get("numbers") or {}),
                     "; ".join(p.get("functions") or []), p.get("page"), p.get("chk")))
    for x in r.get("ratings") or []:
        con.execute("INSERT INTO ratings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (m, x.get("kind"), x.get("param"), x.get("symbol"), _num(x.get("min")), _num(x.get("typ")),
                     _num(x.get("max")), x.get("unit"), x.get("conditions"), x.get("page"), x.get("chk")))
    if own:
        con.commit()
        con.close()


def reindex():
    """Rebuild the index from the record files (they are the source of truth)."""
    INDEX.unlink(missing_ok=True)
    con = _db()
    for r in all_records():
        _index_one(r, con)
    con.commit()
    con.close()


def search(q: str = "", limit: int = 50) -> list:
    con = _db()
    like = f"%{q}%"
    rows = con.execute("""SELECT mpn, manufacturer, description, category, status, revision FROM parts
        WHERE ?='' OR mpn LIKE ? OR description LIKE ? OR category LIKE ? OR manufacturer LIKE ? OR aliases LIKE ?
        ORDER BY mpn LIMIT ?""", (q, like, like, like, like, like, limit)).fetchall()
    con.close()
    return [dict(zip(("mpn", "manufacturer", "description", "category", "status", "revision"), r)) for r in rows]


def export_csv(out_dir) -> list:
    """The core as spreadsheets: parts, pins, ratings, support_parts, equations."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    recs = all_records()
    files = []

    def write(name, header, rows):
        p = out / name
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        files.append(p)
    write("parts.csv", ["mpn", "manufacturer", "description", "category", "revision", "status", "pages", "file"],
          [[r["identity"].get("mpn"), r["identity"].get("manufacturer"), r["identity"].get("description"),
            r["identity"].get("category"), r["identity"].get("revision"), (r.get("verification") or {}).get("status"),
            (r.get("source") or {}).get("pages"), (r.get("source") or {}).get("file")] for r in recs])
    write("pins.csv", ["mpn", "name", "type", "numbers", "functions", "page", "check"],
          [[r["identity"]["mpn"], p.get("name"), p.get("type"), json.dumps(p.get("numbers") or {}),
            "; ".join(p.get("functions") or []), p.get("page"), p.get("chk")] for r in recs for p in r.get("pins") or []])
    write("ratings.csv", ["mpn", "kind", "param", "symbol", "min", "typ", "max", "unit", "conditions", "page", "check"],
          [[r["identity"]["mpn"], x.get("kind"), x.get("param"), x.get("symbol"), x.get("min"), x.get("typ"), x.get("max"),
            x.get("unit"), x.get("conditions"), x.get("page"), x.get("chk")] for r in recs for x in r.get("ratings") or []])
    write("support_parts.csv", ["mpn", "what", "kind", "value", "between", "required", "why", "page", "check"],
          [[r["identity"]["mpn"], s.get("what"), s.get("kind"), s.get("value"), " - ".join(s.get("between") or []),
            s.get("required"), s.get("why"), s.get("page"), s.get("chk")] for r in recs for s in r.get("support_parts") or []])
    write("equations.csv", ["mpn", "name", "expr", "variables", "page", "check", "problem"],
          [[r["identity"]["mpn"], e.get("name"), e.get("expr"), json.dumps(e.get("vars") or {}), e.get("page"),
            e.get("chk"), e.get("invalid", "")] for r in recs for e in r.get("equations") or []])
    return files


# ---------------------------------------------------------------------------
# tiers
# ---------------------------------------------------------------------------

_SKIP_SYMS = re.compile(r"^Device:(R|C|CP|C_Polarized|R_Small|C_Small|R_Pack\d*|C_Feedthrough|R_US|Jumper|NetTie|"
                        r"FerriteBead|Ferrite)", re.I)
_SKIP_LIBS = ("Mechanical:", "TestPoint:", "Jumper:", "power:", "Graphic:")
_API_SYMS = re.compile(r"^(Device:(L|D|LED|Q_|Crystal|Resonator|Fuse|Polyfuse|Thermistor|Varistor|Battery|Buzzer|Speaker|"
                       r"Antenna|Transformer)|Diode:|Transistor_|LED:|Connector|Switch:|Relay:|Fuse:|Battery:|Inductor|"
                       r"Crystal:|Oscillator:|Motor:|Buzzer)", re.I)


def classify(comp: dict) -> str:
    """Fixed rule: ICs and modules are read; discretes get distributor parameters; passives are skipped."""
    sym = str(comp.get("symbol", ""))
    ref = str(comp.get("ref", ""))
    if sym.startswith(_SKIP_LIBS) or _SKIP_SYMS.match(sym):
        return SKIP
    if _API_SYMS.match(sym):
        return API
    if re.match(r"^(U|IC|M|MOD|A)\d", ref, re.I) or ":" in sym:
        return DATASHEET
    return API


TRIAGE = """These parts are classed as generic (passives) or as discretes whose datasheet won't be read. Some might
still need their datasheet: a manufacturer-specific part with unusual behaviour (a current-sense resistor with
Kelvin terminals, a PTC fuse, a special-purpose capacitor, a part with a full manufacturer part number), or anything
whose value alone doesn't say what it does.
Answer ONE JSON object: {"read": {"REF": "why its datasheet is needed"}}. {"read": {}} when none do. Be sparing.

Parts:
"""


def triage(spec: dict, o, log=print, workdir=None) -> dict:
    """{ref: tier}. The fixed rule first; then a light model may move generic parts up to 'datasheet'."""
    tiers = {c["ref"]: classify(c) for c in spec.get("components", [])}
    cands = [c for c in spec.get("components", []) if tiers[c["ref"]] != DATASHEET]
    if not cands or o is None:
        return tiers
    lines = [f"{c['ref']}: {c.get('symbol')} value={c.get('value')!r}" + (f" mpn={c['mpn']}" if c.get("mpn") else "")
             + f" (now: {tiers[c['ref']]})" for c in cands]
    try:
        from .spec import load
        out, who = _ask(o, "triage", TRIAGE + "\n".join(lines), log, Path(workdir or DB / ".work") / "triage")
        picks = (load(out).get("read") or {})
    except Exception as e:
        log(f"  part triage skipped ({e})")
        return tiers
    by_ref = {c["ref"]: c for c in spec.get("components", [])}
    for ref, why in picks.items():
        c = by_ref.get(ref)
        # only ever up, and only for a generic part with a real part number and a reason that is more than its own
        # fields read back. Diodes, MOSFETs, connectors and the like stay in the parameters-only tier by design.
        if not c or tiers.get(ref) != SKIP or not c.get("mpn") or re.match(r"\s*(mpn|value|symbol)\s*=", str(why), re.I) or len(str(why)) < 12:
            continue
        tiers[ref] = DATASHEET
        log(f"  {ref}: datasheet needed after all ({why})")
    return tiers


def part_name(comp: dict) -> str:
    return comp.get("mpn") or comp.get("value") or str(comp.get("symbol", "")).split(":")[-1]


_PIN_SYN = {"in": "vin", "vi": "vin", "input": "vin", "out": "vout", "vo": "vout", "output": "vout",
            "gnd": "gnd", "vss": "gnd", "ground": "gnd", "adj": "adj", "fb": "fb", "sense": "fb", "en": "en",
            "enable": "en", "shdn": "shdn", "shutdown": "shdn"}


def _pin_key(n) -> str:
    k = _k(n)
    return _PIN_SYN.get(k, k)


def pin_match(record: dict, symbol: str) -> float | None:
    """How well a KiCad symbol's pins fit a record's pins (share of the symbol's pin names the record has).
    None when the symbol can't be read."""
    from . import kicad_lib as kl
    try:
        pins = {_pin_key(p.name) for p in kl.symbol_pins(kl.get_symbol(symbol)) if p.name and p.name != "~"}
    except Exception:
        return None
    if not pins:
        return None
    have = {_pin_key(p.get("name")) for p in record.get("pins") or []}
    if not have:
        return None
    return len(pins & have) / len(pins)


def _exact(r: dict, name: str) -> bool:
    ident = r.get("identity") or {}
    return any(_k(name) == _k(x) for x in [ident.get("mpn"), *(ident.get("aliases") or [])] if x)


def coverage(spec: dict, tiers: dict) -> dict:
    """Which datasheet-tier parts have a record and which don't. A record found only by a part-number prefix
    (LT1175 for LT1175-5) counts only if the part's pins match it: a fixed-output 3-pin version is a different part
    from the adjustable 8-pin one, and checking it against the wrong pinout would be worse than not checking."""
    have, missing, variant = {}, {}, {}
    for c in spec.get("components", []):
        if tiers.get(c["ref"]) != DATASHEET:
            continue
        name = part_name(c)
        r = find(name) or find(str(c.get("symbol", "")).split(":")[-1])
        if not r:
            missing[c["ref"]] = name
            continue
        if not (_exact(r, name) or _exact(r, str(c.get("symbol", "")).split(":")[-1])):
            m = pin_match(r, c.get("symbol", ""))
            if m is not None and m < 0.75:
                variant[c["ref"]] = f"{name} (record {r['identity']['mpn']} is another variant: pins don't match)"
                continue
        have[c["ref"]] = r["identity"]["mpn"]
    return {"have": have, "missing": missing, "variant": variant}


# ---------------------------------------------------------------------------
# what the model sees
# ---------------------------------------------------------------------------

def _rng(x):
    vals = [x.get("min") if x.get("min") is not None else x.get("min_expr"), x.get("typ"),
            x.get("max") if x.get("max") is not None else x.get("max_expr")]
    if all(v is None for v in vals):
        return "?"
    lo, ty, hi = vals
    s = ""
    if lo is not None:
        s += f"{lo}"
    if ty is not None:
        s += f"{' / ' if s else ''}typ {ty}"
    if hi is not None:
        s += f"{' .. ' if lo is not None else 'max '}{hi}"
    return s + f" {x.get('unit') or ''}".rstrip()


def compact(r: dict, limit: int = 3500) -> str:
    """A record as a short text block for a prompt: the facts a designer checks first."""
    ident = r.get("identity") or {}
    out = [f"## {ident.get('mpn')} ({ident.get('manufacturer') or '?'}): {ident.get('description') or ''}"
           + (f" [datasheet {ident.get('revision')}]" if ident.get("revision") else "")]
    cb = conventions_block(r.get("conventions") or {})
    if cb:
        out.append(cb.replace("Document conventions (they apply to everything below; follow them):", "CONVENTIONS (read the numbers below with these):"))
    pins = r.get("pins") or []
    if pins:
        out.append("pins: " + "; ".join(f"{p.get('name')}={p.get('type')}"
                                        + (f" ({', '.join(p.get('functions')[:3])})" if p.get("functions") else "")
                                        for p in pins[:60]))
    for kind, label in (("abs_max", "absolute max"), ("recommended", "recommended")):
        rows = [x for x in r.get("ratings") or [] if x.get("kind") == kind]
        if rows:
            out.append(f"{label}: " + "; ".join(f"{x.get('symbol') or x.get('param')} {_rng(x)}"
                                                + (f" [{x['conditions'][:40]}]" if x.get("conditions") else "")
                                                for x in rows[:25]))
    ec = [x for x in r.get("ratings") or [] if x.get("kind") == "electrical"]
    if ec:
        out.append("key characteristics: " + "; ".join(f"{x.get('symbol') or x.get('param')} {_rng(x)}" for x in ec[:25]))
    sp = r.get("support_parts") or []
    if sp:
        out.append("needs: " + "; ".join(f"{s.get('what')} {s.get('value') or ''} between {'/'.join(s.get('between') or [])}"
                                         + ("" if s.get("required", True) else " (optional)") for s in sp[:20]))
    eqs = [e for e in r.get("equations") or [] if not e.get("invalid")]
    if eqs:
        out.append("equations: " + "; ".join(f"{e.get('expr')}" for e in eqs[:10]))
    if r.get("application_circuit"):
        c = r["application_circuit"]
        out.append(f"typical circuit (p{c.get('page')}): " + ", ".join(f"{x.get('ref')}={x.get('kind')} {x.get('value') or ''}"
                                                                      for x in (c.get("components") or [])[:20]))
    extra = r.get("extra") or {}
    if extra:
        out.append("notes: " + "; ".join(f"{k}: {v}" for k, v in list(extra.items())[:8]))
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit] + " ..."


def prompt_hint(prompt: str = "", spec: dict | None = None, limit: int = 12000) -> str:
    """Part-database context for the design prompt: a catalog of what's stored, plus the facts for any stored
    part the request or the spec names."""
    recs = all_records()
    if not recs:
        return ""
    lines = ["Parts with datasheet records in the part database (verified facts; prefer them when they fit, and put the "
             "part number in the component's \"mpn\" field):"]
    for r in recs[:60]:
        ident = r["identity"]
        lines.append(f"- {ident.get('mpn')}: {ident.get('description') or ident.get('category') or ''}")
    named = []
    low = _k(prompt)
    for r in recs:
        ident = r["identity"]
        names = [ident.get("mpn"), *(ident.get("aliases") or [])]
        if any(_k(n) and len(_k(n)) >= 4 and _k(n) in low for n in names):
            named.append(r)
    if spec:
        for c in spec.get("components", []):
            r = find(part_name(c))
            if r and r not in named:
                named.append(r)
    used = len("\n".join(lines))
    if named:
        lines.append("\nDatasheet facts for the parts this design uses (follow them: pin functions, limits, required support parts):")
        for r in named:
            block = compact(r)
            if used + len(block) > limit:
                break
            lines.append(block)
            used += len(block)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# pictures for the record viewer: the stored circuit drawn as a schematic, and the datasheet's own figure
# ---------------------------------------------------------------------------

PREVIEWS = DB / "previews"


def render_circuit(mpn: str) -> Path | None:
    """SVG of the stored application circuit, drawn the way datasheets draw it. Cached until the record changes."""
    from . import dsdraw
    r = load_record(mpn)
    if not r or not r.get("application_circuit"):
        return None
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    out = PREVIEWS / f"{slug(r['identity']['mpn'])}_circuit.svg"
    src = path_for(r["identity"]["mpn"])
    if out.exists() and src.exists() and out.stat().st_mtime >= src.stat().st_mtime and out.stat().st_mtime >= Path(dsdraw.__file__).stat().st_mtime:
        return out
    out.write_text(dsdraw.draw(r))
    return out


def render_figure(mpn: str) -> Path | None:
    """PNG of the datasheet page that holds the application circuit, from the stored PDF."""
    r = load_record(mpn)
    if not r:
        return None
    c = r.get("application_circuit") or {}
    src = (r.get("source") or {}).get("file")
    try:
        page = int(c.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    if not src or page < 1 or not (PDFS / src).exists():
        return None
    PREVIEWS.mkdir(parents=True, exist_ok=True)
    out = PREVIEWS / f"{slug(r['identity']['mpn'])}_p{page}.png"
    if not out.exists():
        got = render_pages(PDFS / src, [page - 1], PREVIEWS / ".fig", dpi=120)
        if not got:
            return None
        shutil.move(str(got[0]), out)
    return out
