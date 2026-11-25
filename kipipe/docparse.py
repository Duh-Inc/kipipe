"""Deterministic datasheet parsing with Docling.

Docling (IBM, open source, runs locally) turns a PDF into a structured document: section headers, tables with
real rows and columns, captions, formulas (decoded to LaTeX), pictures, each with its page and bounding box.
From that, plain code extracts most of a part record with no model at all:

  pins            the pin table, with a number column per package
  ratings         absolute maximum, recommended operating, thermal, timing tables
  table conditions the "over ... (unless otherwise noted)" line that belongs to each table
  equations       formula blocks, LaTeX -> computable expressions, every symbol checked against the tables
  figure          which picture is the typical application circuit (by its caption), cropped for a model
  identity        part number, maker, revision, packages, orderable variants

What still needs a model (see partdb): the electrical characteristics table (row spans that a grid can't express)
and reading the application circuit drawing. Both get a small, already-located piece: a grid plus a crop.

Docling is optional and heavy (PyTorch). kipipe runs its command-line tool, from PATH, $KIPIPE_DOCLING, or the
venv at ~/.local/share/kipipe/docling; without it, partdb falls back to pdftotext + model reads.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .router import DATA

VENV = DATA / "docling"
CACHE = DATA / "partdb" / ".docling"


# ---------------------------------------------------------------------------
# running docling
# ---------------------------------------------------------------------------

def find_cli() -> str | None:
    for cand in (os.environ.get("KIPIPE_DOCLING"), str(VENV / "bin" / "docling"), shutil.which("docling")):
        if cand and Path(cand).exists() and os.access(cand, os.X_OK):
            return cand
    return None


def available() -> bool:
    return find_cli() is not None


_ACCEL = None


def accel() -> dict:
    """Can docling use the GPU? It runs on PyTorch inside its own environment, so check (1) is there a GPU, (2) is
    that environment's PyTorch a GPU build that sees it. ROCm builds report AMD cards through torch.cuda too.
    {"vendor": "amd"|"nvidia"|None, "torch_gpu": bool, "build": "rocm 6.4"|"cuda 12.4"|"cpu"|None,
     "device": name, "hint": what to install when the GPU goes unused}"""
    global _ACCEL
    if _ACCEL is not None and _ACCEL.get("torch_gpu"):
        return _ACCEL                   # a "no GPU" answer is not remembered: PyTorch may have been installed since
    vendor = None
    if Path("/dev/kfd").exists() or shutil.which("rocminfo"):
        vendor = "amd"
    elif shutil.which("nvidia-smi"):
        vendor = "nvidia"
    out = {"vendor": vendor, "torch_gpu": False, "build": None, "device": "", "hint": ""}
    cli = find_cli()
    if cli:
        py = Path(cli).resolve().parent / "python"
        if py.exists():
            probe = ("import json, torch; ok = torch.cuda.is_available(); print(json.dumps({'ok': ok, 'hip': torch.version.hip, "
                     "'cuda': torch.version.cuda, 'name': torch.cuda.get_device_name(0) if ok else ''}))")
            try:
                r = subprocess.run([str(py), "-c", probe], capture_output=True, text=True, timeout=90)
                info = json.loads(r.stdout.strip().splitlines()[-1])
                out["torch_gpu"] = bool(info["ok"])
                out["build"] = (f"rocm {info['hip']}" if info.get("hip") else f"cuda {info['cuda']}" if info.get("cuda") else "cpu")
                out["device"] = info.get("name") or ""
            except Exception:
                out["build"] = None
        pip = Path(cli).resolve().parent / "pip"
        if vendor and not out["torch_gpu"]:
            if vendor == "amd":
                out["hint"] = (f"{pip} install --force-reinstall torch torchvision --index-url "
                               "https://download.pytorch.org/whl/rocm6.4  (RX 9000 cards need ROCm 6.4 or newer; "
                               "check pytorch.org for the current ROCm wheel)")
            else:
                out["hint"] = (f"{pip} install --force-reinstall torch torchvision --index-url "
                               "https://download.pytorch.org/whl/cu124  (check pytorch.org for the current CUDA wheel)")
    _ACCEL = out
    return out


def accel_text() -> str:
    a = accel()
    if a["torch_gpu"]:
        return f"GPU ({a['device'] or a['vendor']}, PyTorch {a['build']})"
    if a["vendor"]:
        return f"CPU (an {a['vendor'].upper()} GPU is present but docling's PyTorch is {a['build'] or 'unknown'}; to use it: {a['hint']})"
    return "CPU (no GPU found)"


def convert(pdf: Path, sha: str, log=print) -> dict:
    """Docling's JSON for a PDF, cached by the file's hash (a 40-page datasheet takes about a minute on CPU)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{sha}.json"
    if cached.exists():
        return json.loads(cached.read_text())
    cli = find_cli()
    if not cli:
        raise RuntimeError("docling is not installed")
    with tempfile.TemporaryDirectory(dir=CACHE) as out:
        a = accel()
        log(f"  parsing {pdf.name} with docling on " + (f"the GPU ({a['device'] or a['vendor']})" if a["torch_gpu"]
            else "the CPU (about 1.5 s per page)" + ("; your GPU would be faster, see kipipe doctor" if a["vendor"] else "")))
        r = subprocess.run([cli, str(pdf), "--to", "json", "--enrich-formula", "--image-export-mode", "placeholder",
                            "--output", out], capture_output=True, text=True, timeout=1800)
        js = list(Path(out).glob("*.json"))
        if r.returncode != 0 or not js:
            raise RuntimeError(f"docling failed: {(r.stderr or r.stdout)[-400:]}")
        shutil.move(str(js[0]), cached)
    return json.loads(cached.read_text())


# ---------------------------------------------------------------------------
# the document in reading order
# ---------------------------------------------------------------------------

def _resolve(doc, ref):
    node = doc
    for p in ref.lstrip("#/").split("/"):
        node = node[int(p)] if p.isdigit() else node[p]
    return node


def items(doc) -> list[dict]:
    """Body items in reading order: {"kind": table|text|picture, "label", "text", "page", "bbox", "node", "index"}."""
    out = []

    def walk(ref):
        path = ref.lstrip("#/").split("/")
        node = _resolve(doc, ref)
        kind = path[0]
        if kind in ("texts", "tables", "pictures"):
            prov = (node.get("prov") or [{}])[0]
            out.append({"kind": kind[:-1], "label": node.get("label"), "text": node.get("text") or "",
                        "page": prov.get("page_no"), "bbox": prov.get("bbox"), "node": node, "index": int(path[1])})
        if kind in ("body", "groups"):
            for c in node.get("children", []):
                walk(c["$ref"])
    walk("#/body")
    return out


def grid(table: dict) -> list[list[str]]:
    data = table["data"]
    g = [[""] * data["num_cols"] for _ in range(data["num_rows"])]
    for c in data["table_cells"]:
        for r in range(c["start_row_offset_idx"], c["end_row_offset_idx"]):
            for k in range(c["start_col_offset_idx"], c["end_col_offset_idx"]):
                g[r][k] = (c.get("text") or "").strip()
    return g


def _clean(s: str) -> str:
    """Strip footnote markers like '(2)' and normalise minus signs and spaces."""
    s = re.sub(r"\s*\(\d{1,2}\)", "", s or "")
    return s.replace("−", "-").replace("–", "-").replace("\u2009", " ").strip()


# ---------------------------------------------------------------------------
# classifying tables by the section they sit in and their header row
# ---------------------------------------------------------------------------

SECTION_KIND = [
    (r"absolute maximum", "abs_max"), (r"recommended operating", "recommended"),
    (r"electrical characteristics|^specifications$|dc characteristics|static characteristics", "electrical"),
    (r"thermal", "thermal"), (r"timing|switching characteristics|ac characteristics", "timing"),
    (r"pin (configuration|functions|description|assignment)|terminal functions|pin function", "pins"),
    (r"esd ratings", "esd"),
]


def _headers(g, n=3) -> list[str]:
    """Combined header text per column over the first rows (stops at the first row that holds a number)."""
    cols = len(g[0]) if g else 0
    heads = [""] * cols
    for row in g[:n]:
        if any(re.fullmatch(r"-?\d+(\.\d+)?", _clean(x)) for x in row):
            break
        for i, x in enumerate(row):
            if x and x not in heads[i]:
                heads[i] = (heads[i] + " " + x).strip()
    return heads


def _header_rows(g) -> int:
    n = 0
    for row in g[:3]:
        if any(re.fullmatch(r"[-+]?\d+(\.\d+)?", _clean(x)) for x in row):
            break
        if any(re.search(r"\b(MIN|MAX|TYP|UNIT|PIN|NAME|TYPE|PARAMETER|DESCRIPTION|VALUE|NO\.?)\b", x, re.I) for x in row):
            n += 1
        else:
            break
    return max(n, 1)


def classify(section: str, caption: str, g) -> str | None:
    # the section name decides; a caption only when the section says nothing (captions often mention
    # "recommended operating" conditions on electrical tables)
    for text in (section.lower(), caption.lower()):
        for pat, kind in SECTION_KIND:
            if re.search(pat, text):
                return kind
    if re.search(r"packag|ordering|orderable|tape|reel|dimension|quadrant", section, re.I):
        return None
    heads = " ".join(_headers(g)).upper()
    if "PIN" in heads and ("TYPE" in heads or "DESCRIPTION" in heads or "I/O" in heads or "MNEMONIC" in heads):
        return "pins"
    if "MIN" in heads and "MAX" in heads:
        return "electrical" if ("TEST" in heads or "CONDITION" in heads) else "recommended"
    return None


def tables(doc) -> list[dict]:
    """Every table with its section, caption, nearby condition text, kind and grid."""
    its = items(doc)
    out, section, pending = [], "", []
    for it in its:
        if it["label"] == "section_header" and not re.fullmatch(r"www\.\S+", it["text"].strip()):
            section = re.sub(r"^[\d.]+\s*", "", it["text"]).strip()
            pending = []
        elif it["kind"] == "text":
            pending.append(it)
        elif it["kind"] == "table":
            node = it["node"]
            caps = [_resolve(doc, c["$ref"]).get("text", "") for c in node.get("captions") or []]
            near = [p["text"] for p in pending[-4:] if p["page"] == it["page"]]
            cond = next((c for c in caps + near if re.search(r"unless otherwise|over (the )?(recommended|operating)|T_?A\s*=", c, re.I)), "")
            g = grid(node)
            out.append({"index": it["index"], "page": it["page"], "bbox": it["bbox"], "section": section,
                        "caption": " ".join(caps), "conditions": _clean(cond), "grid": g,
                        "kind": classify(section, " ".join(caps), g)})
            pending = []
    # a table continued on the next page ("Electrical Characteristics (continued)") keeps its kind
    for i, t in enumerate(out):
        if t["kind"] is None and i and "continued" in t["section"].lower():
            t["kind"] = out[i - 1]["kind"]
    return out


# ---------------------------------------------------------------------------
# simple rating tables -> rating rows
# ---------------------------------------------------------------------------

NUM = re.compile(r"^[-+]?\d+(\.\d+)?$")


def _value(cell: str):
    """('number', v) | ('expr', python expression) | ('range', lo, hi) for ±x | None."""
    c = _clean(cell)
    if not c or c in ("-", "—"):
        return None
    m = re.fullmatch(r"±\s*(\d+(\.\d+)?)", c)
    if m:
        return ("range", -float(m.group(1)), float(m.group(1)))
    if NUM.match(c):
        return ("number", float(c))
    # an expression needs a number or an operator next to a symbol: "0.7 × VCC", "VCC + 0.5", "20 + 0.1Cb"
    if (re.search(r"[A-Za-z]", c) and re.fullmatch(r"[\w\s.+\-×*/()]+", c)
            and re.search(r"\d|[+×*/]", c) and len(c.split()) <= 6):
        expr = c.replace("×", "*")
        expr = re.sub(r"\s+", " ", expr).strip()
        expr = re.sub(r"(?<=\d) (?=[A-Za-z])", " * ", expr)
        return ("expr", expr)
    return None


def _symbolish(s: str) -> bool:
    """VCC, VIH, Tstg, RθJA, ψJT, fSCL, tr, Cio(off): short, no spaces, no hyphenated words."""
    s = _clean(s)
    if not s or len(s) > 14 or " " in s or not re.match(r"^[A-Za-zΔθψ]", s):
        return False
    if "-" in re.sub(r"\([^)]*\)", "", s):
        return False
    return bool(re.search(r"\d|[A-Z].*[A-Z]|^[a-zΔθψ][A-Za-z]{0,5}$|^[A-Z][a-z]{1,4}$|[θψ]", s))


def rating_rows(t: dict) -> list[dict]:
    """A min/typ/max table as rating rows. Blank leading cells inherit from the row above (a merged label)."""
    g = t["grid"]
    hr = _header_rows(g)
    heads = [h.upper() for h in _headers(g, hr)]
    col = {}
    for i, h in enumerate(heads):
        for key in ("MIN", "TYP", "MAX", "UNIT"):
            if re.search(rf"\b{key}\b", h) and key not in col:
                col[key] = i
        if re.search(r"\bVALUE\b", h) and "TYP" not in col and "MAX" not in col:
            col["VALUE"] = i
    if not col:
        return []
    first_val = min(col.values())
    label_cols = list(range(0, first_val))
    out, prev, group = [], [""] * len(label_cols), ""
    for row in g[hr:]:
        labels = [_clean(row[i]) for i in label_cols]
        if not any(_clean(row[i]) for i in col.values()):
            texts = list(dict.fromkeys(x for x in labels if x))
            if len(texts) == 1 and not _symbolish(texts[0]) and (texts[0].isupper() or "mode" in texts[0].lower()):
                group = texts[0]              # a heading row ("STANDARD MODE") applies to the rows below it
                prev = [""] * len(label_cols)
            else:
                prev = [l or p for l, p in zip(labels, prev)]
            continue
        labels = [l or p for l, p in zip(labels, prev)]
        prev = labels
        uniq = list(dict.fromkeys(x for x in labels if x))
        sym = next((x for x in uniq if _symbolish(x)), "")
        if not sym and uniq:
            head, _, rest = uniq[0].partition(" ")
            if rest and _symbolish(head) and re.search(r"[A-Z].*[A-Z]", head):
                sym, uniq[0] = head, rest
        param = next((x for x in uniq if x != sym and not _symbolish(x)), sym)
        cond = ", ".join(x for x in uniq if x not in (sym, param))
        if group:
            cond = ", ".join(x for x in (group.capitalize(), cond) if x)
        if "=" in param:          # a test condition landed in the parameter column
            cond = ", ".join(x for x in (param, cond) if x)
            param = sym
        r = {"kind": t["kind"], "param": param, "symbol": sym, "min": None, "typ": None, "max": None,
             "unit": _clean(row[col["UNIT"]]) if "UNIT" in col else "", "conditions": cond, "page": t["page"],
             "chk": "parsed"}
        for key, field in (("MIN", "min"), ("TYP", "typ"), ("MAX", "max"), ("VALUE", "typ")):
            if key in col:
                v = _value(row[col[key]])
                if not v:
                    continue
                if v[0] == "number":
                    r[field] = v[1]
                elif v[0] == "range":
                    r["min"], r["max"] = v[1], v[2]
                else:
                    r[f"{field}_expr"] = v[1]
        if t.get("conditions"):
            r["table_conditions"] = t["conditions"]
        out.append(r)
    return out


def thermal_rows(t: dict) -> list[dict]:
    """Thermal tables have one value column per package: one rating per (row, package)."""
    g = t["grid"]
    hr = _header_rows(g) if _header_rows(g) else 1
    heads = _headers(g, max(hr, 2))
    out = []
    for row in g[hr:]:
        cells = [_clean(x) for x in row]
        nums = [(i, c) for i, c in enumerate(cells) if NUM.match(c)]
        if not nums:
            continue
        labels = [c for i, c in enumerate(cells[:nums[0][0]]) if c]
        sym = next((x for x in labels if _symbolish(x)), "")
        param = next((x for x in labels if x != sym), sym)
        unit = cells[-1] if not NUM.match(cells[-1]) else ""
        for i, c in nums:
            out.append({"kind": "thermal", "param": param, "symbol": sym, "min": None, "typ": float(c), "max": None,
                        "unit": unit, "conditions": heads[i], "page": t["page"], "chk": "parsed"})
    return out


def table_is_regular(t: dict) -> bool:
    """True when every data row carries its own label, so it can be read without a model."""
    g = t["grid"]
    hr = _header_rows(g)
    rows = [r for r in g[hr:] if any(NUM.match(_clean(x)) for x in r)]
    if not rows:
        return False
    blank = sum(1 for r in rows if not _clean(r[0]))
    merged = sum(1 for r in rows for x in r if re.fullmatch(r"[-+]?\d+(\.\d+)?(\s+[-+]?\d+(\.\d+)?)+", _clean(x)))
    if blank / len(rows) >= 0.1 or merged:
        return False
    return rows_complete(t, rating_rows(t))


def rows_complete(t: dict, parsed: list) -> bool:
    """Trust code's reading of a table only if nothing was lost: every number in the value columns ended up in a
    row, and every row that has a value also has a unit (when the table has a unit column)."""
    g = t["grid"]
    hr = _header_rows(g)
    heads = [h.upper() for h in _headers(g, hr)]
    vcols = [i for i, h in enumerate(heads) if re.search(r"\b(MIN|TYP|MAX|VALUE)\b", h)]
    has_unit = any(re.search(r"\bUNITS?\b", h) for h in heads)
    if not vcols:
        return False
    in_grid = sum(len(re.findall(r"[-+]?\d+(?:\.\d+)?", _clean(row[i]))) for row in g[hr:] for i in vcols)
    def nums(r, f):
        if r.get(f) is not None:
            return 1
        e = r.get(f + "_expr")
        return len(re.findall(r"\d+(?:\.\d+)?", e)) if e else 0     # "20 + 0.1Cb" holds two of the grid's numbers
    got = sum(nums(r, "min") + nums(r, "typ") + nums(r, "max") for r in parsed)
    if got < in_grid:
        return False
    if has_unit and any(not r.get("unit") for r in parsed if any(r.get(f) is not None for f in ("min", "typ", "max"))):
        return False
    return True


# ---------------------------------------------------------------------------
# pins
# ---------------------------------------------------------------------------

PIN_TYPE = {"i": "input", "o": "output", "i/o": "bidirectional", "io": "bidirectional", "b": "bidirectional",
            "p": "power", "pwr": "power", "power": "power", "g": "ground", "gnd": "ground", "-": "ground",
            "—": "ground", "a": "analog", "ai": "analog", "ao": "analog", "nc": "nc", "od": "open_drain",
            "i/od": "open_drain", "o/od": "open_drain", "di": "input", "do": "output", "dio": "bidirectional",
            "supply": "power", "ground": "ground"}


def pins_from(t: dict) -> list[dict]:
    g = t["grid"]
    hr = _header_rows(g)
    heads = _headers(g, max(hr, 1))
    body = g[hr:]

    def is_num_col(i):
        vals = [_clean(r[i]) for r in body if _clean(r[i])]
        return vals and sum(bool(re.fullmatch(r"([A-Z]?\d+)([,\s/]+[A-Z]?\d+)*|-|—|EP|PAD|Thermal Pad", v, re.I))
                            for v in vals) / len(vals) > 0.8
    up = [h.upper() for h in heads]
    name_col = next((i for i, h in enumerate(up) if re.search(r"\b(NAME|MNEMONIC|SIGNAL|SYMBOL)\b", h)), None)
    type_col = next((i for i, h in enumerate(up) if re.search(r"\b(TYPE|I/O)\b", h)), None)
    desc_col = next((i for i, h in enumerate(up) if re.search(r"DESCRIPTION|FUNCTION", h)), None)
    num_cols = [i for i in range(len(heads)) if i not in (name_col, type_col, desc_col) and is_num_col(i)]
    if name_col is None:
        name_col = next((i for i in range(len(heads)) if i not in num_cols + [type_col, desc_col]), None)
    if name_col is None or not num_cols:
        return []
    pkgs = {}
    for i in num_cols:
        h = re.sub(r"^(PIN|NO\.?|NUMBER)\s*", "", heads[i], flags=re.I).strip() or "default"
        pkgs[i] = h
    out = {}
    for row in body:
        name = _clean(row[name_col])
        if not name or re.fullmatch(r"(NAME|PIN)", name, re.I):
            continue
        p = out.setdefault(name, {"name": name, "numbers": {}, "type": "", "functions": [], "description": "",
                                  "page": t["page"], "chk": "parsed"})
        for i, pk in pkgs.items():
            v = _clean(row[i])
            if v and v not in ("-", "—"):
                p["numbers"][pk] = (p["numbers"][pk] + ", " + v) if pk in p["numbers"] else v
        if type_col is not None and not p["type"]:
            ty = _clean(row[type_col]).lower()
            p["type"] = PIN_TYPE.get(ty, ty)
        if desc_col is not None and _clean(row[desc_col]):
            p["description"] = (p["description"] + " " + _clean(row[desc_col])).strip()[:300]
        if not p["type"] and re.fullmatch(r"(A|D|P)?GND\d*|VSS|EP|PAD", name, re.I):
            p["type"] = "ground"
        if not p["type"] and re.fullmatch(r"V(CC|DD|IN|S|BAT)\w*", name, re.I):
            p["type"] = "power"
    return list(out.values())


# ---------------------------------------------------------------------------
# equations: LaTeX -> python expression
# ---------------------------------------------------------------------------

GREEK = {"alpha": "alpha", "beta": "beta", "gamma": "gamma", "delta": "delta", "Delta": "Delta", "eta": "eta",
         "theta": "theta", "lambda": "lam", "mu": "mu", "pi": "pi", "rho": "rho", "sigma": "sigma", "tau": "tau",
         "phi": "phi", "omega": "omega", "Omega": "Omega", "upsilon": "v"}


class _Tex:
    """A small recursive-descent reader for the LaTeX subset datasheet formulas use."""

    def __init__(self, s: str):
        # escaped characters first: \{ \} are brackets, \\ and "\ " are spacing, \% is a percent sign
        s = (s.replace("\\{", "(").replace("\\}", ")").replace("\\\\", " ").replace("\\ ", " ")
             .replace("\\%", "%").replace("\\Omega", "Ω").replace("\\mu ", "µ").replace("\\mu", "µ"))
        # a unit note at the end, "(\Omega)" or "(V)", isn't part of the maths
        s = re.sub(r"\(\s*(\\mathrm\s*)?\{?\s*(k|M|m|µ|u|n|p)?(Ω|V|A|W|Hz|s|F|H)\s*\}?\s*\)\s*$", "", s)
        s = s.replace("\\left", "").replace("\\right", "").replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
        s = s.replace("\\times", "*").replace("\\cdot", "*").replace("−", "-").replace("\\ast", "*")
        self.s, self.i = s, 0

    def peek(self):
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1
        return self.s[self.i] if self.i < len(self.s) else ""

    def group(self) -> str:
        """Raw text of a {...} group, or a single token."""
        if self.peek() == "{":
            depth, start = 0, self.i
            while self.i < len(self.s):
                ch = self.s[self.i]
                depth += ch == "{"
                depth -= ch == "}"
                self.i += 1
                if depth == 0:
                    return self.s[start + 1:self.i - 1]
            return self.s[start + 1:]
        if self.peek() == "\\":
            m = re.match(r"\\[A-Za-z]+", self.s[self.i:])
            if not m:                      # \{ \} \% \\ "\ ": an escaped character
                self.i += 2
                return self.s[self.i - 1:self.i].replace("{", "(").replace("}", ")")
            self.i += len(m.group(0))
            return m.group(0)
        ch = self.s[self.i] if self.i < len(self.s) else ""
        self.i += 1
        return ch

    def flat(self, raw: str) -> str:
        """Subscript text flattened into an identifier part: '\\text {OL} ( \\max )' -> 'OLmax'."""
        raw = re.sub(r"\\(text|mathrm|mathit|operatorname|rm|it)\s*", "", raw)
        raw = raw.replace("\\max", "max").replace("\\min", "min")
        return re.sub(r"[^A-Za-z0-9]", "", raw)

    def atom(self) -> str | None:
        ch = self.peek()
        if not ch:
            return None
        if ch == "\\":
            m = re.match(r"\\([A-Za-z]+)", self.s[self.i:])
            if not m:                      # escaped character: \{ \} act as brackets, the rest is spacing/text
                nxt = self.s[self.i + 1:self.i + 2]
                self.i += 2
                return {"{": "(", "}": ")"}.get(nxt, "")
            cmd = m.group(1)
            self.i += len(m.group(0))
            if cmd in ("frac", "dfrac", "tfrac"):
                a = _Tex(self.group()).expr()
                b = _Tex(self.group()).expr()
                return self.suffix(f"(({a})/({b}))")
            if cmd == "sqrt":
                return self.suffix(f"sqrt({_Tex(self.group()).expr()})")
            if cmd in ("text", "mathrm", "operatorname", "mathit", "rm"):
                return self.ident(self.flat(self.group()))
            if cmd in ("ln", "log", "exp", "sin", "cos"):
                return f"{'ln' if cmd == 'ln' else 'log10' if cmd == 'log' else cmd}"
            if cmd in GREEK:
                return self.ident(GREEK[cmd])
            return ""
        if ch == "(":
            self.i += 1
            depth, start = 1, self.i
            while self.i < len(self.s) and depth:
                depth += self.s[self.i] == "("
                depth -= self.s[self.i] == ")"
                self.i += 1
            return self.suffix(f"({_Tex(self.s[start:self.i - 1]).expr()})")
        if ch == "{":
            return self.suffix(f"({_Tex(self.group()).expr()})")
        if ch.isdigit() or ch == ".":
            m = re.match(r"[\d. ]+", self.s[self.i:])
            self.i += len(m.group(0))
            num = m.group(0).replace(" ", "").rstrip(".")
            # a unit printed after a number ("3.8 V", "10\,mA", "0.2%") scales it to SI and is not a variable
            u = re.match(r"\s*(\\mathrm\s*\{\s*|\\text\s*\{\s*)?(k|M|m|µ|u|n|p)?(Ω|V|A|W|Hz|s|F|H)\s*\}?(?![A-Za-z_{])",
                         self.s[self.i:])
            if u and (u.group(2) or u.group(3)):
                self.i += len(u.group(0))
                scale = {"k": 1e3, "M": 1e6, "m": 1e-3, "µ": 1e-6, "u": 1e-6, "n": 1e-9, "p": 1e-12}.get(u.group(2) or "", 1)
                if scale != 1:
                    num = f"({num} * {scale:g})"
            elif self.s[self.i:self.i + 1] == "%":
                self.i += 1
                num = f"({num} / 100)"
            return self.suffix(num)
        if ch.isalpha() or ch == "|":
            self.i += 1
            return self.ident(ch)
        return None

    def ident(self, base: str) -> str:
        sub = ""
        while self.peek() == "_":
            self.i += 1
            sub += self.flat(self.group())
        name = base + sub
        return self.suffix(name)

    def suffix(self, a: str) -> str:
        if self.peek() == "^":
            self.i += 1
            return f"({a})**({_Tex(self.group()).expr()})"
        return a

    def expr(self) -> str:
        out, prev_atom = [], False
        while self.i < len(self.s):
            ch = self.peek()
            if not ch:
                break
            if ch in "+-*/=":
                self.i += 1
                out.append(f" {ch} ")
                prev_atom = False
                continue
            if ch in "})":
                self.i += 1
                continue
            a = self.atom()
            if a is None:
                self.i += 1
                continue
            if not a:
                continue
            if prev_atom:
                out.append(" * ")          # implicit multiplication
            out.append(a)
            prev_atom = a not in ("ln", "log10", "exp", "sin", "cos")
        return re.sub(r"\s+", " ", "".join(out)).strip()


OCR_FIXES = [("|", "I"), ("l", "I"), ("0", "O"), ("1", "I"), ("O", "0")]


def latex_to_equation(latex: str, symbols: set) -> dict | None:
    """'R_{p(\\min)} = \\frac{V_{DPUX}-V_{OL(\\max)}}{|_{OL}}' -> an equation record. Every variable is checked
    against the symbols in the datasheet's tables; single-character OCR slips (| for I) are corrected when that
    turns an unknown name into a known one, and the equation says so."""
    if "=" not in latex:
        return None
    parts = latex.split("=")
    if len(parts) > 2:
        # "L = \frac{1.2}{6.3 ...} = 34 \mu H": a worked example with numbers, not a design equation
        return None
    lhs_raw, rhs_raw = parts
    if not re.search(r"[A-Za-z]", re.sub(r"\\[A-Za-z]+", "", rhs_raw)):
        return None
    lhs = _Tex(lhs_raw).expr().replace(" ", "")
    rhs = _Tex(rhs_raw).expr()
    if not re.fullmatch(r"[A-Za-z_|][\w|]*", lhs) or not rhs:
        return None
    known = {re.sub(r"[^a-z0-9]", "", s.lower()) for s in symbols if s}
    fixed, notes = {}, []

    def norm(v):
        return re.sub(r"[^a-z0-9]", "", v.lower())

    def check(v):
        base = norm(v)
        if base in known or re.sub(r"(max|min|typ)$", "", base) in known:
            return v
        for a, b in OCR_FIXES:
            if a in v:
                w = v.replace(a, b)
                bw = norm(w)
                if bw in known or re.sub(r"(max|min|typ)$", "", bw) in known:
                    notes.append(f"read '{v}' as '{w}' (OCR slip; {w} is in the datasheet's tables)")
                    return w
        return v
    names = set(re.findall(r"[A-Za-z_|][\w|]*", rhs)) - {"sqrt", "ln", "log10", "exp", "sin", "cos", "pi"}
    for n in names:
        w = check(n)
        if w != n:
            fixed[n] = w
    for a, b in fixed.items():
        rhs = re.sub(rf"(?<![\w|]){re.escape(a)}(?![\w|])", b, rhs)
    bad = [n for n in re.findall(r"[A-Za-z_|][\w|]*", rhs) if "|" in n]
    # LaTeX writes a multi-letter symbol (ESR, PSRR) as separate letters, which read as a product E * S * R.
    # Join a run of single capitals when the joined name is a known symbol or a common acronym; otherwise say
    # the equation is ambiguous rather than store a product nobody meant.
    ACRO = {"ESR", "ESL", "PSRR", "CMRR", "SNR", "THD", "PWM", "VDD", "VCC", "VSS", "GND", "ADC", "DAC", "LDO", "RMS"}

    def join(m):
        letters = m.group(0).replace(" ", "").replace("*", "")
        if letters in ACRO or norm(letters) in known:
            notes.append(f"joined {' '.join(letters)} into {letters}")
            return letters
        return m.group(0)
    rhs = re.sub(r"\b[A-Z](?: \* [A-Z]\b)+", join, rhs)

    def same(m):                     # "r r" (a symbol like rr), never how a square is written
        notes.append(f"joined {m.group(1)} {m.group(1)} into {m.group(1) * 2}")
        return m.group(1) * 2
    rhs = re.sub(r"\b([a-z]) \* \1\b", same, rhs)
    if re.search(r"\b[A-Za-z] \* [A-Za-z] \* [A-Za-z]\b", rhs):
        bad.append("three single letters multiplied (likely one symbol split up)")
    if "|" in lhs:
        bad.append(lhs)
    variables = {lhs: {"from": "result", "unit": ""}}
    for n in sorted(set(re.findall(r"[A-Za-z_][\w]*", rhs)) - {"sqrt", "ln", "log10", "exp", "sin", "cos", "pi"}):
        variables[n] = ({"from": "rating", "symbol": n, "unit": ""} if norm(n) in known or
                        re.sub(r"(max|min|typ)$", "", norm(n)) in known else {"from": "input", "unit": ""})
    eq = {"name": lhs, "expr": f"{lhs} = {rhs}", "vars": variables, "latex": latex, "chk": "parsed"}
    if notes:
        eq["notes"] = "; ".join(notes)
        eq["chk"] = "corrected"
    if bad:
        eq["invalid"] = f"unreadable symbol(s): {', '.join(bad)}"
    return eq


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

MAKERS = ["Texas Instruments", "Analog Devices", "Linear Technology", "Maxim Integrated", "Microchip", "STMicroelectronics",
          "NXP", "Infineon", "onsemi", "ON Semiconductor", "Espressif", "Nordic Semiconductor", "Renesas", "Rohm",
          "Diodes Incorporated", "Vishay", "Silicon Labs", "Monolithic Power", "Richtek", "Toshiba", "Nexperia",
          "Bosch", "TDK", "InvenSense", "Allegro", "Semtech", "Skyworks", "Qorvo", "Cypress", "Lattice", "Melexis"]
SHORT = {"www.ti.com": "Texas Instruments", "analog.com": "Analog Devices", "microchip.com": "Microchip",
         "st.com": "STMicroelectronics", "nxp.com": "NXP", "espressif.com": "Espressif", "onsemi.com": "onsemi"}


def identity(doc, its, tabs, mpn_hint: str | None, stem: str) -> dict:
    first = " ".join(i["text"] for i in its if i["page"] in (1, 2))[:6000]
    heads = [i["text"] for i in its if i["page"] == 1 and i["label"] in ("section_header", "title")]
    cands = re.findall(r"\b[A-Z]{2,}[0-9][A-Z0-9]*(?:-[A-Z0-9.]+)?\b", " ".join(heads) + " " + first[:1500])
    mpn = mpn_hint or (max(set(cands), key=lambda c: (cands.count(c), -len(c))) if cands else stem.upper())
    maker = next((m for m in MAKERS if m.lower() in first.lower()), "")
    if not maker:
        maker = next((v for k, v in SHORT.items() if k in first.lower()), "")
    rev = ""
    m = re.search(r"\b(SC[A-Z]{2}\d{3}[A-Z]?|SL[A-Z]{2}\d{3}[A-Z]?|DS\d{5,}[A-Z]?|Rev\.?\s*[A-Z0-9]+)\b", first)
    if m:
        rev = m.group(1)
    generic = r"^(typical (application|connection)s?|features|applications|description|block diagram|pin configuration)\b"
    desc = next((re.sub(re.escape(mpn), "", h).strip(" -–:") for h in heads
                 if mpn in h and len(h) > len(mpn) + 4 and not re.match(generic, re.sub(re.escape(mpn), "", h).strip(), re.I)), "")
    if not desc:
        desc = next((h for h in heads if len(h) > 12 and not re.match(generic, h, re.I) and not re.fullmatch(r"www\.\S+", h)), "")
    packages, aliases = [], []
    for t in tabs:
        heads_t = " ".join(_headers(t["grid"])).upper()
        if "PACKAGE" in heads_t and ("PART NUMBER" in heads_t or "DEVICE" in heads_t):
            pc = next((i for i, h in enumerate(_headers(t["grid"])) if "PACKAGE" in h.upper()), None)
            dc = 0
            for row in t["grid"][_header_rows(t["grid"]):]:
                if pc is not None and _clean(row[pc]):
                    packages.append(_clean(row[pc]))
                if re.match(r"^[A-Z0-9]{4,}", _clean(row[dc])) and _clean(row[dc]) != mpn:
                    aliases.append(_clean(row[dc]).split()[0])
    return {"mpn": mpn, "aliases": sorted(set(aliases))[:20], "manufacturer": maker, "description": desc,
            "category": "", "revision": rev, "packages": list(dict.fromkeys(packages))}


# ---------------------------------------------------------------------------
# figures and crops
# ---------------------------------------------------------------------------

def figure(doc, its) -> dict | None:
    """The typical application circuit: the picture whose caption (or the caption right after it) says so."""
    pics = [i for i in its if i["kind"] == "picture"]
    for i, it in enumerate(its):
        if it["label"] == "caption" and re.search(r"typical application|application (schematic|circuit|diagram)|"
                                                  r"simplified schematic|reference (design|schematic)", it["text"], re.I):
            # the largest picture on the same page, before the caption
            same = [p for p in pics if p["page"] == it["page"] and p["bbox"]]
            if not same:
                continue
            big = max(same, key=lambda p: abs((p["bbox"]["r"] - p["bbox"]["l"]) * (p["bbox"]["t"] - p["bbox"]["b"])))
            return {"page": it["page"], "bbox": _union([p["bbox"] for p in same if _area(p["bbox"]) > 0.15 * _area(big["bbox"])]),
                    "caption": it["text"]}
    return None


def _area(b):
    return abs((b["r"] - b["l"]) * (b["t"] - b["b"]))


def _union(bs):
    return {"l": min(b["l"] for b in bs), "r": max(b["r"] for b in bs), "t": max(b["t"] for b in bs),
            "b": min(b["b"] for b in bs), "coord_origin": "BOTTOMLEFT"}


def crop(pdf: Path, doc, page: int, bbox: dict, out: Path, dpi: int = 160, pad: float = 8.0) -> Path | None:
    """PNG of one region of one page (a table or a figure), from the PDF itself."""
    size = (doc.get("pages", {}).get(str(page)) or {}).get("size") or {"width": 612, "height": 792}
    out.parent.mkdir(parents=True, exist_ok=True)
    base = out.with_suffix("")
    r = subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile",
                        str(pdf), str(base) + "_page"], capture_output=True, timeout=120)
    full = Path(str(base) + "_page.png")
    if r.returncode != 0 or not full.exists():
        return None
    try:
        from PIL import Image
    except ImportError:
        full.rename(out)
        return out
    s = dpi / 72.0
    H = size["height"]
    if bbox.get("coord_origin", "BOTTOMLEFT") == "BOTTOMLEFT":
        top, bot = H - bbox["t"], H - bbox["b"]
    else:
        top, bot = bbox["t"], bbox["b"]
    box = (max(0, int((bbox["l"] - pad) * s)), max(0, int((top - pad) * s)),
           int((bbox["r"] + pad) * s), int((bot + pad) * s))
    with Image.open(full) as im:
        im.crop((box[0], box[1], min(box[2], im.width), min(box[3], im.height))).save(out)
    full.unlink(missing_ok=True)
    return out


def section_text(its, pattern: str, limit: int = 12000) -> str:
    """Text of the sections whose header matches (e.g. application information), with page markers."""
    out, on, used, page = [], False, 0, None
    for it in its:
        if it["label"] == "section_header":
            on = bool(re.search(pattern, it["text"], re.I))
        if on and it["kind"] == "text" and it["label"] not in ("page_header", "page_footer"):
            if it["page"] != page:
                out.append(f"=== page {it['page']} ===")
                page = it["page"]
            t = it["text"].strip()
            if t:
                out.append(t)
                used += len(t)
            if used > limit:
                break
    return "\n".join(out)


# ---------------------------------------------------------------------------
# everything code can get
# ---------------------------------------------------------------------------

def parse(doc, mpn_hint: str | None = None, stem: str = "") -> dict:
    """The deterministic part of a record, plus the pieces that still need a model."""
    its = items(doc)
    tabs = tables(doc)
    rec = {"pins": [], "ratings": [], "equations": [], "todo": {"electrical": [], "figure": None}}
    bad_tables = []
    for t in tabs:
        k = t["kind"]
        try:
            if k == "pins" and not rec["pins"]:
                rec["pins"] = pins_from(t)
            elif k in ("abs_max", "recommended", "timing"):
                rows = rating_rows(t)
                if rows and rows_complete(t, rows):
                    rec["ratings"] += rows
                else:
                    rec["todo"]["electrical"].append(t)      # irregular: the model sorts it out from grid + crop
            elif k == "thermal":
                rec["ratings"] += thermal_rows(t)
            elif k == "electrical":
                if table_is_regular(t):
                    rec["ratings"] += rating_rows(t)
                else:
                    rec["todo"]["electrical"].append(t)
        except Exception as e:
            # a table code can't read goes to the model with the irregular ones
            bad_tables.append(f"{k} table on page {t['page']} ({type(e).__name__}: {e})")
            if k in ("abs_max", "recommended", "timing", "thermal", "electrical"):
                rec["todo"]["electrical"].append(t)
    rec["table_conditions"] = [{"table": t["section"], "conditions": t["conditions"], "page": t["page"]}
                               for t in tabs if t["conditions"] and t["kind"] in ("abs_max", "recommended", "electrical",
                                                                                  "thermal", "timing")]
    symbols = {r.get("symbol") for r in rec["ratings"]} | {p["name"] for p in rec["pins"]}
    for t in rec["todo"]["electrical"]:
        for row in t["grid"]:
            for x in row[:3]:
                if _symbolish(x):
                    symbols.add(_clean(x))
    rec["skipped"] = list(bad_tables)
    for it in its:
        if it["label"] == "formula" and it["text"]:
            try:
                eq = latex_to_equation(it["text"], symbols)
            except Exception as e:
                rec["skipped"].append(f"formula on page {it['page']} ({type(e).__name__}: {e}): {it['text'][:80]}")
                continue
            if eq:
                eq["page"] = it["page"]
                rec["equations"].append(eq)
    rec["identity"] = identity(doc, its, tabs, mpn_hint, stem)
    if not rec["identity"]["packages"]:
        pk = sorted({k for p in rec["pins"] for k in p["numbers"]})
        rec["identity"]["packages"] = [k for k in pk if k != "default"]
    rec["todo"]["figure"] = figure(doc, its)
    rec["notes"] = [it["text"] for it in its if re.search(r"pin numbers? (shown|given|are for)", it["text"], re.I)]
    # sections code couldn't turn into rows (pinout drawings, dotted-leader rating lists): text + pages for a model
    PIN_SEC = r"pin (configuration|function|description|assignment|out)|pinout|terminal (configuration|functions)|package/order"
    rec["need"] = {}
    if not rec["pins"]:
        rec["need"]["pins"] = {"text": section_text(its, PIN_SEC, 8000), "pages": section_pages(its, PIN_SEC)[:2]}
    if not any(r["kind"] == "abs_max" for r in rec["ratings"]):
        sec = section_text(its, r"absolute maximum", 5000)
        if sec:
            rec["need"]["abs_max"] = {"text": sec, "pages": section_pages(its, r"absolute maximum")[:1]}
    rec["application_text"] = section_text(its, r"application|typical|design (requirements|procedure)|power supply "
                                                r"recommendations|layout")
    return rec


def section_pages(its, pattern: str) -> list[int]:
    """Pages of the sections whose header matches, in order."""
    pages, on = [], False
    for it in its:
        if it["label"] == "section_header":
            on = bool(re.search(pattern, it["text"], re.I))
        if on and it["page"] and it["page"] not in pages:
            pages.append(it["page"])
    return pages


def value_columns(t: dict) -> dict:
    """{"min": col, "typ": col, "max": col, "unit": col} from the header row(s)."""
    heads = [h.upper() for h in _headers(t["grid"], _header_rows(t["grid"]))]
    out = {}
    for i, h in enumerate(heads):
        for key in ("MIN", "TYP", "MAX", "UNIT"):
            if re.search(rf"\b{key}S?\b", h) and key.lower() not in out:
                out[key.lower()] = i
    return out


def check_cells(t: dict, rows: list, log, who: str) -> list:
    """Hold a model's reading of a table to the grid, cell by cell. Each rating names the grid rows it came from
    ("rows": ["r12"]); every min/typ/max must sit in that row's cell of that column. A value that doesn't is taken
    out (it was moved or swapped). Afterwards: a cell used more often than it holds numbers, or numbers no rating
    used, are reported. What this can't check is the parameter label on a row: that is what the second read is for."""
    cols = value_columns(t)
    g = t["grid"]
    if not cols or not rows:
        return rows
    used: dict = {}
    for r in rows:
        refs = [int(x[1:]) for x in r.get("rows") or [] if isinstance(x, str) and re.fullmatch(r"r\d+", x)]
        refs = [i for i in refs if 0 <= i < len(g)]
        for f in ("min", "typ", "max"):
            v = r.get(f)
            if v is None or f not in cols:
                continue
            hit = None
            for i in refs:
                nums = [float(n) for n in re.findall(r"[-+]?\d+(?:\.\d+)?", _clean(g[i][cols[f]]))]
                if any(abs(float(v) - n) <= 1e-9 + 1e-6 * abs(n) for n in nums):
                    hit = (i, cols[f])
                    break
            if hit is None:
                log(f"  {who}: {r.get('symbol') or r.get('param')} {f}={v} isn't in the {f.upper()} column of the "
                    f"row(s) it names ({', '.join(r.get('rows') or []) or 'none'}): removed")
                r[f] = None
                r["chk_note"] = "value removed: not where the read said it was"
            else:
                used[hit] = used.get(hit, 0) + 1
    over = [f"r{i}" for (i, c), n in used.items() if n > len(re.findall(r"[-+]?\d+(?:\.\d+)?", _clean(g[i][c])))]
    if over:
        log(f"  {who}: cells used more than once ({', '.join(over[:6])}): a value was copied to two rows")
    hr = _header_rows(g)
    missing = sum(max(0, len(re.findall(r"[-+]?\d+(?:\.\d+)?", _clean(g[i][c]))) - used.get((i, c), 0))
                  for i in range(hr, len(g)) for k, c in cols.items() if k != "unit")
    if missing:
        log(f"  {who}: {missing} number(s) in the table weren't assigned to any rating")
    return rows


def grid_text(t: dict) -> str:
    """A table grid as numbered rows for a prompt."""
    return "\n".join(f"r{i}: " + " | ".join(_clean(x) for x in row) for i, row in enumerate(t["grid"]))


def numbers_in(t: dict) -> set:
    out = set()
    for row in t["grid"]:
        for x in row:
            for n in re.findall(r"[-+]?\d+(?:\.\d+)?", _clean(x)):
                out.add(float(n))
    return out
