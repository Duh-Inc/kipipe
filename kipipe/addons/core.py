"""Registry, the Job view of a design, and the runner. See __init__ for the overview."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .. import kicad_lib as kl

HOME = Path(os.environ.get("KIPIPE_HOME", Path.home() / ".local/share/kipipe"))
PLUGIN_DIR = HOME / "addons"
SEVERITIES = ("error", "warning", "note")
STATUSES = ("ready", "beta", "preview")
KINDS = ("check", "generate", "aid")

REG: dict[str, "Addon"] = {}
PLUGIN_ERRORS: list[str] = []
EVENTS: dict[str, list] = {}


@dataclass
class Addon:
    id: str
    title: str
    kind: str
    summary: str
    fn: object
    status: str = "ready"
    needs: tuple = ("spec",)        # spec | layout | pcb | sch | kicad | libs | model
    auto: bool = False               # checks only: run in the pipeline's Design checks stage
    options: list = field(default_factory=list)   # [{key, label, type: text|number|select|bool, default, choices}]
    group: str = ""
    source: str = "built in"

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "summary": self.summary, "status": self.status,
                "needs": list(self.needs), "auto": self.auto, "options": self.options, "group": self.group,
                "source": self.source}


def addon(id, title, kind, summary, status="ready", needs=("spec",), auto=False, options=None, group="", source=None):
    """Decorator: register a function taking one argument, the Run."""
    if kind not in KINDS:
        raise ValueError(f"add-on {id}: kind must be one of {KINDS}")
    if status not in STATUSES:
        raise ValueError(f"add-on {id}: status must be one of {STATUSES}")

    def deco(fn):
        REG[id] = Addon(id, title, kind, summary, fn, status, tuple(needs), auto, options or [], group,
                        source or _SOURCE[0])
        return fn
    return deco


_SOURCE = ["built in"]


def on(event: str, fn):
    """Subscribe to an event: spec_ready(job), job_done(job, result)."""
    EVENTS.setdefault(event, []).append(fn)


def emit(event: str, log=None, **kw):
    for fn in EVENTS.get(event, []):
        try:
            fn(**kw)
        except Exception as e:      # an add-on must never break a run
            if log:
                log(f"  add-on hook for {event} failed: {type(e).__name__}: {e}")


def load_plugins():
    PLUGIN_ERRORS.clear()
    if not PLUGIN_DIR.is_dir():
        return
    from .. import addons as api
    for f in sorted(PLUGIN_DIR.glob("*.py")):
        _SOURCE[0] = f"plugin {f.name}"
        try:
            spec = importlib.util.spec_from_file_location(f"kipipe_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "register"):
                raise RuntimeError("no register(api) function")
            mod.register(api)
        except Exception as e:
            PLUGIN_ERRORS.append(f"{f.name}: {type(e).__name__}: {e}")
        finally:
            _SOURCE[0] = "built in"


# ---------------------------------------------------------------------------
# Job: one design, read-only, with the questions every add-on asks already answered
# ---------------------------------------------------------------------------
GND_RE = re.compile(r"^/?(A|D|P|S)?(GND|VSS|0V|EARTH)[A-Z0-9_]*$", re.I)
_VOLT = re.compile(r"(?<![A-Za-z0-9.])([+-])?(\d+(?:\.\d+)?)V(\d+)?(?![A-Za-z]*\d)", re.I)
_KNOWN_RAILS = {"VBUS": 5.0, "VUSB": 5.0, "USB_5V": 5.0}
_PREFIX_KIND = (("LED", "LED"), ("SW", "SW"), ("BT", "BT"), ("FB", "FB"), ("TP", "TP"), ("R", "R"), ("C", "C"),
                ("L", "L"), ("D", "D"), ("Q", "Q"), ("J", "J"), ("P", "J"), ("Y", "Y"), ("X", "Y"), ("F", "F"),
                ("H", "H"), ("U", "U"), ("K", "K"))


def net_voltage(name: str):
    """Voltage a net name states: '+3V3' -> 3.3, '-5V' -> -5, 'GND' -> 0, 'VBUS' -> 5. None when the name says nothing."""
    n = str(name).lstrip("/")
    if GND_RE.match(n):
        return 0.0
    if n.upper() in _KNOWN_RAILS:
        return _KNOWN_RAILS[n.upper()]
    m = _VOLT.search(n.replace("_", " "))
    if not m:
        return None
    v = float(m.group(2) + ("." + m.group(3) if m.group(3) and "." not in m.group(2) else ""))
    if m.group(1) == "-" or re.search(r"(^|[_ ])(NEG|N)($|[_ ])", n, re.I) and m.group(1) != "+":
        v = -v
    return v


def comp_kind(c: dict) -> str:
    """R, C, L, D, LED, Q, U, J, SW, Y, F, FB, BT, TP, H, K. Symbol first, reference prefix as the fallback."""
    lib, _, name = str(c.get("symbol", "")).partition(":")
    n = name.upper()
    if lib == "Device":
        for pre, k in (("LED", "LED"), ("R_", "R"), ("C_", "C"), ("L_", "L"), ("D_", "D"), ("Q_", "Q"),
                       ("CRYSTAL", "Y"), ("RESONATOR", "Y"), ("FUSE", "F"), ("POLYFUSE", "F"), ("FERRITEBEAD", "FB"),
                       ("BATTERY", "BT"), ("THERMISTOR", "R"), ("VARISTOR", "R"), ("ANTENNA", "ANT")):
            if n.startswith(pre):
                return k
        if n in ("R", "C", "L", "D"):
            return n
    if lib.startswith("LED"):
        return "LED"
    if lib.startswith("Diode"):
        return "D"
    if lib.startswith("Transistor"):
        return "Q"
    if lib.startswith("Connector"):
        return "J"
    if lib.startswith("Switch"):
        return "SW"
    if lib.startswith("MountingHole"):
        return "H"
    if lib.startswith("Relay"):
        return "K"
    ref = str(c.get("ref", "")).upper()
    for pre, k in _PREFIX_KIND:
        if re.match(pre + r"\d", ref):
            return k
    return "U"


def parse_value(text):
    """Leading number with an SI prefix: '10u 25V X7R' -> 1e-5, '4k7' -> 4700, '1.5k' -> 1500. None when there is none."""
    from ..partdb import parse_value as pv
    try:
        v = pv(str(text or "").strip().split()[0]) if str(text or "").strip() else None
    except Exception:
        v = None
    return v


def fmt_si(v, unit=""):
    if v is None:
        return "?"
    if v == 0:
        return f"0 {unit}".strip()
    for p, s in ((1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= p:
            x = v / p
            return f"{x:.3g} {s}{unit}".strip()
    return f"{v:.3g} {unit}".strip()


class Job:
    """A design as add-ons see it. Works on a finished job folder, on a job mid-run, or on a bare spec file."""

    def __init__(self, path, spec: dict | None = None):
        p = Path(path)
        self.dir = p if p.is_dir() else p.parent
        spec_file = p if p.is_file() else self.dir / "spec.json"
        self.spec = spec if spec is not None else (json.loads(spec_file.read_text()) if spec_file.exists() else {})
        self.bare = p.is_file()
        self.name = self.spec.get("name") or self.dir.name
        self.comps = {c["ref"]: c for c in self.spec.get("components", []) if c.get("ref")}
        self.nets = {n: list(p) for n, p in (self.spec.get("nets") or {}).items()}
        self.pin_net = {p: n for n, pins in self.nets.items() for p in pins}
        self.board = self.spec.get("board") or {}
        self._layout = None
        self._pins = {}
        self._result = None

    # ---- files ----
    def file(self, rel) -> Path | None:
        f = self.dir / rel
        return f if f.exists() else None

    def first(self, pattern) -> Path | None:
        return next(iter(sorted(self.dir.glob(pattern))), None)

    @property
    def result(self) -> dict:
        if self._result is None:
            f = self.dir / "result.json"
            try:
                self._result = json.loads(f.read_text()) if f.exists() and not self.bare else {}
            except Exception:
                self._result = {}
        return self._result

    @property
    def pcb(self):
        return None if self.bare else self.first("*.kicad_pcb")

    @property
    def sch(self):
        return None if self.bare else self.first("*.kicad_sch")

    # ---- circuit ----
    def kind(self, ref) -> str:
        return comp_kind(self.comps.get(ref, {"ref": ref}))

    def of_kind(self, *kinds):
        return [r for r in self.comps if self.kind(r) in kinds]

    def value(self, ref):
        return parse_value(self.comps.get(ref, {}).get("value"))

    def pins(self, ref) -> list:
        """Library pins of a part (number, name, etype). Empty when the symbol can't be read here."""
        if ref not in self._pins:
            try:
                self._pins[ref] = kl.symbol_pins(kl.get_symbol(self.comps[ref]["symbol"]))
            except Exception:
                self._pins[ref] = []
        return self._pins[ref]

    def pin_name(self, ref, num) -> str:
        return next((p.name for p in self.pins(ref) if p.number == str(num)), "")

    def pin_type(self, ref, num) -> str:
        return next((p.etype for p in self.pins(ref) if p.number == str(num)), "")

    def nets_of(self, ref) -> dict:
        """pin number -> net for one part."""
        pre = ref + "."
        return {p[len(pre):]: n for p, n in self.pin_net.items() if p.startswith(pre)}

    def two_pin(self, ref):
        """(net_a, net_b) for a two-terminal part, or None."""
        ns = self.nets_of(ref)
        if len(ns) == 2:
            a, b = sorted(ns)
            return ns[a], ns[b]
        return None

    def others(self, net, ref=None):
        """(ref, pin) on a net, leaving out one part."""
        out = []
        for p in self.nets.get(net, []):
            r, _, n = p.partition(".")
            if r != ref:
                out.append((r, n))
        return out

    def is_gnd(self, net) -> bool:
        return bool(net) and bool(GND_RE.match(str(net).lstrip("/")))

    def voltage(self, net):
        """Volts on a net: the spec's "rails" entry if it has one, else what the net name states. None if unknown."""
        if not net:
            return None
        rail = (self.spec.get("rails") or {}).get(net)
        if isinstance(rail, dict) and isinstance(rail.get("volts"), (int, float)):
            return float(rail["volts"])
        if isinstance(rail, (int, float)):
            return float(rail)
        return net_voltage(net)

    def current(self, net):
        """Amps a net is meant to carry: the spec's rails entry, else its net class. None if not stated."""
        rail = (self.spec.get("rails") or {}).get(net)
        if isinstance(rail, dict) and isinstance(rail.get("amps"), (int, float)):
            return float(rail["amps"])
        for nc in self.spec.get("netclasses") or []:
            if net in (nc.get("nets") or []) and nc.get("current_a"):
                return float(nc["current_a"])
        return None

    def is_rail(self, net) -> bool:
        """A supply or ground by name or by net class."""
        if net is None:
            return False
        if net in (self.spec.get("rails") or {}) or self.voltage(net) is not None or re.match(r"^/?(VCC|VDD|VIN|VBAT|VSYS|VBUS|VPP|AVDD|DVDD|V\+|V-)", str(net), re.I):
            return True
        return any(net in (nc.get("nets") or []) and re.search(r"power|supply|gnd|ground", str(nc.get("name", "")), re.I)
                   for nc in self.spec.get("netclasses") or [])

    def bridges(self, net, kinds, to=None):
        """Two-terminal parts of the given kinds with one end on `net`. `to(other_net)` filters the far end.
        Returns [(ref, other_net)]."""
        out = []
        for r, _ in self.others(net):
            if self.kind(r) in kinds:
                tp = self.two_pin(r)
                if tp and net in tp:
                    o = tp[1] if tp[0] == net else tp[0]
                    if o != net and (to is None or to(o)):
                        out.append((r, o))
        return sorted(set(out))

    def pullups(self, net):
        return self.bridges(net, ("R",), lambda o: (self.voltage(o) or 0) > 0 or (self.is_rail(o) and not self.is_gnd(o)))

    def pulldowns(self, net):
        return self.bridges(net, ("R",), self.is_gnd)

    def caps_to_gnd(self, net):
        return self.bridges(net, ("C",), self.is_gnd)

    def ics(self):
        return [r for r in self.comps if self.kind(r) == "U"]

    # ---- layout ----
    @property
    def layout(self) -> dict:
        """out/layout.json: {board: [w, h], parts: [...], outline}. Empty before placement."""
        if self._layout is None:
            f = self.dir / "out/layout.json"
            try:
                self._layout = json.loads(f.read_text()) if f.exists() and not self.bare else {}
            except Exception:
                self._layout = {}
            self._parts = {p["ref"]: p for p in self._layout.get("parts", [])}
        return self._layout

    def placed(self, ref):
        self.layout
        return self._parts.get(ref)

    def size(self):
        b = self.layout.get("board")
        if b:
            return float(b[0]), float(b[1])
        m = (self.result.get("metrics") or {}).get("board") or {}
        if m.get("width"):
            return float(m["width"]), float(m["height"])
        if self.board.get("width_mm") and self.board.get("height_mm"):
            return float(self.board["width_mm"]), float(self.board["height_mm"])
        return None

    def box(self, ref):
        """Courtyard box of a placed part in board mm: (x0, y0, x1, y1)."""
        p = self.placed(ref)
        if not p:
            return None
        g = (p.get("geo") or {}).get(str(p.get("rot", "0")))
        if not g:
            return None
        b = g["box"]
        return (p["x"] + b[0], p["y"] + b[1], p["x"] + b[2], p["y"] + b[3])

    def pads(self, ref):
        """[(pad number, net, cx, cy, w, h)] in board mm."""
        p = self.placed(ref)
        if not p:
            return []
        g = (p.get("geo") or {}).get(str(p.get("rot", "0"))) or {}
        return [(str(n), net, p["x"] + (x0 + x1) / 2, p["y"] + (y0 + y1) / 2, x1 - x0, y1 - y0)
                for n, net, x0, y0, x1, y1 in g.get("pads", [])]

    def pad_distance(self, ref_a, net_a, ref_b, net_b):
        """Shortest centre-to-centre distance between a's pads on net_a and b's pads on net_b. None if not placed."""
        def on(ref, net):
            return [(x, y) for _, n, x, y, _, _ in self.pads(ref) if str(n).lstrip("/") == str(net).lstrip("/")]
        a, b = on(ref_a, net_a), on(ref_b, net_b)
        if not a or not b:
            return None
        return min(math.hypot(ax - bx, ay - by) for ax, ay in a for bx, by in b)

    def holes(self):
        self.layout
        return [p for p in self._parts.values() if str(p.get("lib", "")).startswith("MountingHole")]

    def has(self, need) -> tuple:
        if need == "spec":
            return bool(self.comps), "no spec"
        if need == "layout":
            return bool(self.layout.get("parts")), "needs a placed board (run the pipeline first)"
        if need == "pcb":
            return self.pcb is not None, "needs the .kicad_pcb"
        if need == "sch":
            return self.sch is not None, "needs the .kicad_sch"
        if need == "kicad":
            try:
                return bool(kl.detect().cli_cmd), "needs kicad-cli"
            except Exception:
                return False, "needs kicad-cli"
        if need == "libs":
            ic = next(iter(self.comps), None)
            return (not ic) or bool(self.pins(ic)) or bool(kl.symbol_libs()), "needs the KiCad symbol libraries"
        return True, ""


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
@dataclass
class Finding:
    severity: str
    title: str
    detail: str = ""
    refs: list = field(default_factory=list)
    nets: list = field(default_factory=list)
    fix: str = ""


class Run:
    """What an add-on function receives."""

    def __init__(self, addon: Addon, job: Job, opts: dict, log):
        self.addon, self.job, self.log = addon, job, log
        self.opts = {o["key"]: o.get("default") for o in addon.options}
        self.opts.update({k: v for k, v in (opts or {}).items() if v not in (None, "")})
        self.findings: list[Finding] = []
        self.files: list[dict] = []
        self.summary = ""
        self.data: dict = {}
        self.out = job.dir / "out" / "addons" / addon.id.replace("/", "_")

    def finding(self, severity, title, detail="", refs=(), nets=(), fix=""):
        if severity not in SEVERITIES:
            severity = "note"
        self.findings.append(Finding(severity, title, detail, sorted(set(refs)), sorted(set(nets)), fix))

    def write(self, name, content, label=None) -> Path:
        """Save a file with the job and list it as an output."""
        self.out.mkdir(parents=True, exist_ok=True)
        f = self.out / name
        f.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            f.write_bytes(content)
        else:
            f.write_text(content, encoding="utf-8")
        self.attach(f, label)
        return f

    def attach(self, path, label=None):
        rel = str(Path(path).resolve().relative_to(self.job.dir.resolve()))
        if not any(x["path"] == rel for x in self.files):
            self.files.append({"label": label or Path(path).name, "path": rel})

    def num(self, key, default=None):
        try:
            return float(self.opts.get(key))
        except (TypeError, ValueError):
            return default


def available(a: Addon, job: Job) -> tuple:
    for need in a.needs:
        ok, why = job.has(need)
        if not ok:
            return False, why
    return True, ""


def run(job_or_dir, addon_id: str, opts: dict | None = None, log=None, save=True) -> dict:
    """Run one add-on. Always returns a record; an exception inside the add-on becomes status 'error'."""
    from . import load
    load()
    log = log or (lambda m: None)
    job = job_or_dir if isinstance(job_or_dir, Job) else Job(job_or_dir)
    a = REG.get(addon_id)
    if a is None:
        return {"id": addon_id, "status": "error", "summary": f"no add-on called {addon_id}", "findings": [], "files": []}
    rec = {"id": a.id, "title": a.title, "kind": a.kind, "maturity": a.status, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "findings": [], "files": [], "summary": "", "data": {}}
    ok, why = available(a, job)
    if not ok:
        rec.update(status="unavailable", summary=why)
        return rec
    r = Run(a, job, opts or {}, log)
    t0 = time.time()
    try:
        a.fn(r)
        counts = {s: sum(f.severity == s for f in r.findings) for s in SEVERITIES}
        rec["status"] = "fail" if counts["error"] else "warn" if counts["warning"] else "ok"
        rec["counts"] = counts
        if not r.summary:
            bits = [f"{n} {s}{'s' if n != 1 else ''}" for s, n in counts.items() if n]
            r.summary = ", ".join(bits) if bits else ("nothing found" if a.kind == "check" else f"{len(r.files)} file(s)")
    except Exception as e:
        rec["status"] = "error"
        r.summary = f"{type(e).__name__}: {e}"
        tb = traceback.extract_tb(e.__traceback__)[-1]
        log(f"  add-on {a.id} failed: {r.summary} ({Path(tb.filename).name}:{tb.lineno})")
    rec.update(summary=r.summary, findings=[f.__dict__ for f in r.findings], files=r.files, data=r.data,
               seconds=round(time.time() - t0, 2))
    if save and not job.bare:
        try:
            r.out.mkdir(parents=True, exist_ok=True)
            (r.out / "result.json").write_text(json.dumps(rec, indent=1, default=str))
        except OSError:
            pass
    return rec


def run_checks(job_or_dir, log=None, only_auto=True) -> dict:
    """Every (automatic) check that can run on this job. Returns {status, counts, checks: [records]}."""
    from . import load
    load()
    job = job_or_dir if isinstance(job_or_dir, Job) else Job(job_or_dir)
    recs = []
    for a in REG.values():
        if a.kind != "check" or (only_auto and not a.auto):
            continue
        if not available(a, job)[0]:
            continue
        recs.append(run(job, a.id, log=log))
    counts = {s: sum((r.get("counts") or {}).get(s, 0) for r in recs) for s in SEVERITIES}
    broken = [r["id"] for r in recs if r["status"] == "error"]
    out = {"status": "fail" if counts["error"] else "warn" if counts["warning"] or broken else "ok", "counts": counts,
           "checks": recs, "broken": broken}
    if not job.bare:
        (job.dir / "out").mkdir(exist_ok=True)
        (job.dir / "out/checks.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def job_state(job_dir) -> list:
    """Catalog for one job: every add-on, whether it can run here, and its last result."""
    from . import load
    load()
    job = Job(job_dir)
    out = []
    for a in REG.values():
        d = a.public()
        ok, why = available(a, job)
        d["available"], d["why"] = ok, why
        f = job.dir / "out" / "addons" / a.id.replace("/", "_") / "result.json"
        if f.exists():
            try:
                d["last"] = json.loads(f.read_text())
            except Exception:
                pass
        out.append(d)
    return out


def catalog() -> dict:
    from . import load
    load()
    return {"addons": [a.public() for a in REG.values()], "plugin_dir": str(PLUGIN_DIR), "plugin_errors": PLUGIN_ERRORS}
