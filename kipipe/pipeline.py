"""The pipeline: input -> spec -> schematic -> board -> route -> checks -> fab outputs."""
from __future__ import annotations

import json
import math
import re
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import kicad_lib as kl
from . import outline, physics, router, schgen, schwire, spec as specmod
from .schgen import parse_netlist

PKG_ROOT = Path(__file__).resolve().parent.parent

FABS = {
    #            default track/clear  via dia/drill   minimum track/clear/via/drill/annular/edge/hole-hole
    "jlcpcb":  dict(track=0.25, clear=0.2, via=0.6, drill=0.3, min_track=0.127, min_clear=0.127, min_via=0.5, min_drill=0.3, annular=0.1, edge=0.3, h2h=0.5),
    "pcbway":  dict(track=0.25, clear=0.2, via=0.6, drill=0.3, min_track=0.127, min_clear=0.127, min_via=0.6, min_drill=0.3, annular=0.15, edge=0.3, h2h=0.5),
    "oshpark": dict(track=0.25, clear=0.2, via=0.6, drill=0.3, min_track=0.1524, min_clear=0.1524, min_via=0.508, min_drill=0.254, annular=0.127, edge=0.381, h2h=0.5),
    "generic": dict(track=0.25, clear=0.2, via=0.8, drill=0.4, min_track=0.2, min_clear=0.2, min_via=0.8, min_drill=0.4, annular=0.2, edge=0.5, h2h=0.5),
}

ALL_FLAGS = {
    "ipc":       "IPC-2221 trace current check (and auto-widen power nets before routing)",
    "rf":        "Controlled-impedance widths for RF nets (microstrip, from stackup)",
    "pour":      "Ground pour on all copper layers",
    "openems":   "openEMS S-parameter simulation of RF nets via gerber2ems (slow, optional)",
    "render3d":  "3D render of the board (KiCad 9+)",
    "video":     "MP4 of the build: placement as it happens, routing replay, finished board",
    "inspect":   "Model inspects the routed board in passes: small tiles, larger tiles, whole board (3 model calls)",
    "review":    "Model looks at the placement and revises the floorplan once if something is wrong (1 model call)",
    "ask":       "Ask before designing: questions the request leaves open, and datasheets or library parts that are missing. One small model call; silent when nothing is open",
    "checks":    "Design checks: fixed rules for common wiring mistakes (pull-ups, decoupling, polarity, boot pins). No model, a second or two",
    "blocks":    "Offer the model verified building blocks (LDO, USB-C input, I2C pull-ups, ...) that are placed by code instead of re-derived (beta)",
    "force":     "Export fab files even if ERC/DRC report errors",
}
DEFAULT_FLAGS = {"ipc", "rf", "pour", "review", "inspect", "video", "render3d", "checks", "ask"}

# ideal-diode / power-path "OR"-ing controllers: parts meant to have their power_out pin tied to other
# instances of the same kind (each blocks reverse current independently, combining several sources onto one
# net). KiCad's ERC otherwise reports any two power_out pins on a net as a driver conflict. Match by symbol,
# mpn or value; extend this list rather than widening erc()'s check to "any power_out pin".
POWER_OR_PARTS = re.compile(r"LM66100|LM74610|LM74700|TPS2121|TPS2113|TPS2115|TPS2116|TPS2663|AP22814|SGM2576", re.I)


@dataclass
class Options:
    prompt: str = ""
    images: list = field(default_factory=list)
    datasheets: list = field(default_factory=list)   # PDFs to read into the part database before designing
    spec_file: str | None = None
    sch_file: str | None = None
    pcb_file: str | None = None           # existing board: locked parts/tracks are kept
    backend: str = "claude-code"           # claude-code | anthropic-api | ollama
    model: str | None = None              # forces one model for every step
    models: str = "mixed"                 # per-step plan, see llm.ROLES / llm.PLANS
    local_model: str = "qwen3.5:9b"       # Ollama model for steps the plan marks "local" (the igor plan)
    sch_style: str = "wired"              # wired (real wires, power symbols) | labels (every pin tagged)
    api_key: str | None = None
    ollama_host: str = field(default_factory=lambda: os.environ.get("KIPIPE_OLLAMA_HOST", "http://localhost:11434"))
    layers: int | None = None
    fab: str | None = None
    width: float | None = None
    height: float | None = None
    holes: str | None = None
    shape: str | None = None              # board outline: rect, rounded, circle, plus, hexagon, octagon
    flags: set = field(default_factory=lambda: set(DEFAULT_FLAGS))
    passes: int = 30
    router_timeout: int = 300
    placement_iterations: int = 3000
    temp_rise_c: float = 10.0
    video_size: str = "1280x720"
    video_style: str = "3d"               # 3d (orbiting camera, parts as boxes) | flat (top-down)
    video_turntable: int = 36             # KiCad 9+: real 3D renders for the closing turntable (0 = off)
    video_models: bool = True             # 3d style: use the footprints' STEP models (needs pyrender + cascadio)
    fresh: bool = False                   # ignore the design cache and ask the model again
    max_rf_width: float = 1.2      # mm; wider impedance widths are reported, not applied
    level: str = "standard"               # guided | standard | expert: how much is explained and how much is asked. Never changes the design
    project: str | None = None            # project slug: shared context, other boards, interfaces (addons/projects.py)
    datasheet_names: dict = field(default_factory=dict)   # {file or link: part name} when the user gave a datasheet for a named part
    answers: str = ""                     # the user's answers to the questions asked before the design (preflight.py)
    plan: list = field(default_factory=list)   # ICs and modules planned before the design, with what was found for each
    context: str = ""                     # background for this board that is not a request ("goes in the payload bay next to the radio")
    review_rounds: int = 1
    max_fix_rounds: int = 4


class StageError(Exception):
    pass


class Paused(Exception):
    """The run stopped on purpose and can be continued (expert level, after the circuit design)."""


def data_source_model(o) -> bool:
    return not (o.spec_file or o.sch_file)


class Pipeline:
    def __init__(self, opts: Options, workdir: Path, log=print):
        self.o = opts
        self.dir = Path(workdir).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "out").mkdir(exist_ok=True)
        self._log = log
        self.stages: list[dict] = []
        self.files: dict[str, str] = {}
        self.metrics: dict = {}
        self.env = kl.detect()
        self.spec: dict = {}
        self.route_failed = False
        self._logf = open(self.dir / "log.txt", "a")

    # -- infrastructure -----------------------------------------------------
    def log(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self._logf.write(line + "\n")
        self._logf.flush()
        self._log(msg)

    def stage(self, name, status, detail=""):
        self.stages.append({"name": name, "status": status, "detail": detail})
        self.log(f"== {name}: {status.upper()}" + (f" ({detail})" if detail else ""))

    def run_cli(self, *args, check=True):
        cmd = [*self.env.cli_cmd, *map(str, args)]
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=self.dir, env=kl.clean_env())
        if check and p.returncode not in (0, 5):     # 5 = violations found (erc/drc)
            raise StageError(f"kicad-cli {' '.join(map(str, args[:3]))} failed: {(p.stderr or p.stdout).strip()[-400:]}")
        return p

    def pcbnew(self, phase, job: dict):
        jp = self.dir / f"job_{phase}.json"
        jp.write_text(json.dumps(job, indent=1))
        argv, extra = self.env.python_cmd(str(PKG_ROOT))
        env = kl.clean_env(extra)
        p = subprocess.Popen([*argv, "-m", "kipipe.pcbgen", phase, str(jp)], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, cwd=self.dir, env=env)
        tail = []
        for line in p.stdout:
            line = line.rstrip()
            if line and "swig/python detected a memory leak" not in line:
                tail.append(line)
                if not line.startswith(("Traceback", "  File")):
                    self.log("  " + line)
        p.wait()
        if p.returncode != 0:
            raise StageError(f"pcbnew {phase} failed:\n" + "\n".join(tail[-12:]))

    def path(self, ext):
        return self.dir / f"{self.name}{ext}"

    @property
    def name(self):
        return self.spec.get("name", "board")

    # -- stages -------------------------------------------------------------
    def get_spec(self):
        from . import derive
        derive.set_context(self.o.project, self.dir.parent, self.dir.name)
        o = self.o
        if o.sch_file:
            src = Path(o.sch_file)
            self.spec = {"name": src.stem}
            dst = self.path(".kicad_sch")
            if src.resolve() != dst.resolve():
                shutil.copy(src, dst)
            p = self.run_cli("sch", "export", "netlist", "-o", self.dir / "out/netlist.net", dst)
            comps, nets = parse_netlist((self.dir / "out/netlist.net").read_text())
            self.spec = specmod.normalize({
                "name": src.stem,
                "components": [{"ref": r, "symbol": "", "footprint": c["footprint"], "value": c["value"]}
                               for r, c in comps.items() if not r.startswith("#")],
                "nets": {n.lstrip("/"): sorted(p_ for p_ in pins) for n, pins in nets.items() if not n.startswith("unconnected")},
            })
            missing = [c["ref"] for c in self.spec["components"] if not c["footprint"]]
            if missing:
                raise StageError(f"symbols without footprints: {', '.join(missing)}")
            self._apply_overrides()
            (self.dir / "spec.json").write_text(json.dumps(self.spec, indent=2))
            self.stage("Input", "ok", f"existing schematic {src.name}")
            return
        if getattr(self, "_revised", None):
            self.spec = specmod.normalize(self._revised)
        elif o.spec_file:
            self.spec = specmod.normalize(specmod.load(Path(o.spec_file).read_text()))
            self.stage("Input", "ok", f"spec file {Path(o.spec_file).name}")
        else:
            from . import llm
            self.preflight()
            self.read_datasheets()
            self.spec = llm.generate_spec(o, self.dir, self.log)
            self.stage("Model", "ok", f"{o.backend} produced a valid spec")
        self._apply_overrides()
        rep = specmod.validate(self.spec)
        (self.dir / "spec.json").write_text(json.dumps(self.spec, indent=2))
        self.files["spec"] = "spec.json"
        if not rep.ok:
            raise StageError("spec invalid:\n" + rep.text())
        self.stage("Spec validation", "warn" if rep.warnings else "ok",
                   f"{len(self.spec['components'])} parts, {len(self.spec['nets'])} nets"
                   + (f", {len(rep.warnings)} warnings" if rep.warnings else ""))
        for w in rep.warnings:
            self.log("  warning: " + w)
        self.part_data()

    def read_datasheets(self):
        """Datasheet PDFs given with the request go into the part database first, so the design can use them."""
        if not self.o.datasheets:
            return
        from . import partdb
        got, bad = [], []
        for pdf in self.o.datasheets:
            try:
                r = partdb.read_datasheet(pdf, self.o, self.log, mpn=(self.o.datasheet_names or {}).get(str(pdf)))
                got.append(f"{r['identity']['mpn']} ({r['verification']['status']})")
                if r["verification"]["status"] == "no pin table read":
                    bad.append(f"{r['identity']['mpn']}: no pin table could be read, so pin checks will skip it")
            except Exception as e:
                bad.append(str(pdf) if str(pdf).startswith("http") else Path(pdf).name)
                self.log(f"  couldn't read {bad[-1]}: {e}")
        self.stage("Datasheets", "warn" if bad else "ok",
                   ("read " + ", ".join(got) if got else "") + (("; failed: " + ", ".join(bad)) if bad else ""))

    def part_data(self):
        """Which parts need datasheet facts, and which of those the part database has. Informational: it says what
        couldn't be checked against a datasheet rather than blocking the run."""
        from . import partdb
        try:
            o = self.o if data_source_model(self.o) else None     # no model call when the design came from a file
            tiers = partdb.triage(self.spec, o, self.log, self.dir / "input")
            cov = partdb.coverage(self.spec, tiers)
        except Exception as e:
            self.log(f"  part data check skipped: {e}")
            return
        (self.dir / "out").mkdir(exist_ok=True)
        (self.dir / "out" / "parts.json").write_text(json.dumps({"tiers": tiers, **cov}, indent=1))
        n_ds = sum(1 for t in tiers.values() if t == partdb.DATASHEET)
        n_api = sum(1 for t in tiers.values() if t == partdb.API)
        self.metrics["part_records"] = len(cov["have"])
        self.metrics["part_records_missing"] = len(cov["missing"])
        if not n_ds:
            self.stage("Part data", "ok", f"no ICs or modules; {n_api} discretes (distributor data: not yet)")
            return
        detail = f"{len(cov['have'])} of {n_ds} ICs/modules have datasheet records"
        if cov["missing"]:
            detail += "; no datasheet for " + ", ".join(f"{r} ({v})" for r, v in list(cov["missing"].items())[:6])
        if cov.get("variant"):
            detail += "; wrong variant for " + ", ".join(f"{r} {v}" for r, v in list(cov["variant"].items())[:6])
        if cov["missing"] or cov.get("variant"):
            detail += " (add the right datasheet under Datasheets; until then those parts are designed from model memory)"
        self.stage("Part data", "warn" if cov["missing"] or cov.get("variant") else "ok", detail)

    def _apply_overrides(self):
        o, b = self.o, self.spec["board"]
        for k, v in (("layers", o.layers), ("fab", o.fab), ("width_mm", o.width), ("height_mm", o.height),
                     ("mounting_holes", o.holes), ("shape", o.shape)):
            if v not in (None, ""):
                b[k] = v
        if str(b.get("shape") or "").lower() == "circle":
            if b.get("width_mm") and not b.get("height_mm"):
                b["height_mm"] = b["width_mm"]
            if b.get("height_mm") and not b.get("width_mm"):
                b["width_mm"] = b["height_mm"]

    def schematic(self):
        (self.dir / "connections.txt").write_text(specmod.connection_list(self.spec))
        self.files["connections"] = "connections.txt"
        sch = self.path(".kicad_sch")
        net = self.dir / "out/netlist.net"
        self.files["schematic"] = sch.name
        if self.o.sch_file:
            self.run_cli("sch", "export", "netlist", "-o", net, sch)
            self.stage("Schematic", "ok", "using provided schematic")
        else:
            style = "labels"
            self.sch_note = ""
            renames = []
            if self.o.sch_style != "labels":
                try:
                    text, info = schwire.generate(self.spec, self.name)
                    sch.write_text(text)
                    self.run_cli("sch", "export", "netlist", "-o", net, sch)
                    problems = schgen.crosscheck(self.spec, net.read_text())
                    if problems:
                        ok, renames = schgen.topology_ok(self.spec, net.read_text())
                        if ok:
                            # wiring is right, KiCad just picked other names for some nets
                            self.log("  wired schematic: connections match the spec; KiCad named some nets differently: "
                                     + ", ".join(renames[:8]))
                            problems = []
                except Exception as e:
                    problems = [f"{type(e).__name__}: {e}"]
                if problems:
                    if sch.exists():
                        shutil.copy(sch, self.dir / "out/schematic_wired_failed.kicad_sch")
                    self.sch_note = "wired failed: " + problems[0][:120]
                    self.log("  WIRED SCHEMATIC FAILED, using name tags instead"
                             + (" (kept as out/schematic_wired_failed.kicad_sch)" if sch.exists() else "") + ":\n    "
                             + "\n    ".join(p[:300] for p in problems[:8]))
                else:
                    style = "wired"
                    wired_erc = self._erc_errors(sch)
                    if wired_erc:
                        # reported, not a reason to throw the wiring away: the netlist check above proves the connections
                        self.log(f"  wired schematic ERC: {len(wired_erc)} error(s): " + "; ".join(wired_erc[:6]))
                    self.log(f"  schematic: wired, {info['wires']} wires, {info['junctions']} junctions, "
                             f"{info['power_symbols']} power symbols, {info['labels']} net names, paper {info['paper']}"
                             + (f"; big buses as name tags: {len(info['bus_tagged'])} nets" if info.get("bus_tagged") else "")
                             + (f"; tagged because no wire path fit: {', '.join(info['label_fallback'][:8])}" if info["label_fallback"] else ""))
            if style == "labels":
                text, info = schgen.generate(self.spec, self.name)
                sch.write_text(text)
                self.log(f"  schematic: label layout, {info['units']} symbol units, {info['labels']} labels, paper {info['paper']}"
                         + (f", PWR_FLAG on {', '.join(info['pwr_flags'])}" if info["pwr_flags"] else ""))
            self.run_cli("sch", "export", "netlist", "-o", net, sch)
            problems = schgen.crosscheck(self.spec, net.read_text())
            if problems and style == "wired" and schgen.topology_ok(self.spec, net.read_text())[0]:
                problems = []
            if problems:
                raise StageError("schematic netlist does not match spec:\n" + "\n".join(problems))
            self.metrics["schematic_style"] = style
            if style == "wired":
                self.stage("Schematic", "ok", "wired; KiCad's netlist matches the spec" + (" (some nets renamed by KiCad)" if renames else ""))
            else:
                self.stage("Schematic", "warn" if self.sch_note else "ok",
                           ("name tags (" + self.sch_note + "; see log)") if self.sch_note else "name-tag layout (as chosen); netlist matches the spec")
        svgdir = self.dir / "out/sch_svg"
        self.run_cli("sch", "export", "svg", "-o", svgdir, sch)
        svgs = sorted(svgdir.glob("*.svg"))
        if svgs:
            shutil.copy(svgs[0], self.dir / "out/schematic.svg")
            self.files["schematic_svg"] = "out/schematic.svg"
            if self._svg2png(self.dir / "out/schematic.svg", self.dir / "out/schematic.png", 2400):
                self.files["schematic_png"] = "out/schematic.png"

    def _erc_errors(self, sch):
        """ERC error descriptions for a schematic, or None if this KiCad has no ERC."""
        if not self.env.has_erc:
            return None
        out = self.dir / "out/erc_probe.json"
        self.run_cli("sch", "erc", "--format", "json", "--severity-error", "-o", out, sch, check=False)
        if not out.exists():
            return None
        try:
            data = json.loads(out.read_text())
        except ValueError:
            return None
        finally:
            out.unlink(missing_ok=True)
        return [v.get("description", "") for s in data.get("sheets", []) for v in s.get("violations", [])
                if v.get("severity") == "error"]

    def erc(self):
        if not self.env.has_erc:
            self.stage("ERC", "skip", f"kicad-cli {self.env.major} has no ERC command (needs 8+)")
            return
        out = self.dir / "out/erc.json"
        self.run_cli("sch", "erc", "--format", "json", "--severity-all", "-o", out, self.path(".kicad_sch"))
        data = json.loads(out.read_text())
        viol = [v for s in data.get("sheets", []) for v in s.get("violations", [])]
        wired = {p for pins in (self.spec.get("nets") or {}).values() for p in pins}
        by_ref = {c["ref"]: c for c in self.spec.get("components", [])}

        def bookkeeping(v):
            """An undriven power pin on a net that IS wired is a missing PWR_FLAG in the drawing, not a circuit fault.
            The same message on a pin with no net at all is a supply pin left unconnected, which is a real error."""
            if not (v.get("type") == "power_pin_not_driven" or "not driven by any Output Power" in str(v.get("description"))):
                return False
            for it in v.get("items", []):
                m = re.search(r"Symbol (\S+) Pin (\S+)", str(it.get("description", "")))
                if m and f"{m.group(1)}.{m.group(2)}" not in wired:
                    return False
            return True

        def or_topology(v):
            """Two or more power_out pins sharing a net is usually two drivers fighting over it, a real error. It
            is also exactly how ideal-diode / power-path 'OR'-ing controllers combine several sources: each one
            independently blocks reverse current, so several of their outputs on one net is the intended way to
            build a diode-OR, not a conflict. Downgrade only when every pin in the conflict belongs to a recognised
            part of that kind, so two real regulators mistakenly tied together still stop the run."""
            desc = str(v.get("description", ""))
            if "Power output" not in desc or "connected" not in desc:
                return False
            for it in v.get("items", []):
                m = re.search(r"Symbol (\S+) Pin", str(it.get("description", "")))
                c = by_ref.get(m.group(1)) if m else None
                if not c or not POWER_OR_PARTS.search(" ".join(str(c.get(k, "")) for k in ("symbol", "mpn", "value"))):
                    return False
            return True

        def downgrade(v):
            return bookkeeping(v) or or_topology(v)
        errs = [v for v in viol if v.get("severity") == "error" and not downgrade(v)]
        warns = [v for v in viol if v.get("severity") != "error" or downgrade(v)]
        for v in viol[:25]:
            where = ", ".join(str(i.get("description", "")) for i in v.get("items", [])[:2])
            self.log(f"  {'warning' if downgrade(v) else v.get('severity')}: {v.get('description')}" + (f" ({where})" if where else ""))
        if any(bookkeeping(v) for v in viol):
            self.log("  (an undriven power pin on a wired net means the drawing lacks a PWR_FLAG there; the circuit is unaffected, so it does not stop the run)")
        if any(or_topology(v) for v in viol):
            self.log("  (power outputs tied together on a recognised ideal-diode/power-path part are an intentional OR, not a conflict)")
        self.files["erc"] = "out/erc.json"
        self.metrics["erc"] = {"errors": len(errs), "warnings": len(warns)}
        if errs and "force" not in self.o.flags:
            self.stage("ERC", "fail", f"{len(errs)} errors, {len(warns)} warnings")
            raise StageError("ERC errors; fix the design or rerun with the 'force' flag")
        self.stage("ERC", "fail" if errs else ("warn" if warns else "ok"), f"{len(errs)} errors, {len(warns)} warnings")

    def write_project(self):
        """Net classes and fab rules go in the .kicad_pro, which pcbnew reads."""
        b = self.spec["board"]
        fab = dict(FABS.get(b.get("fab") or "jlcpcb", FABS["jlcpcb"]))
        if int(b.get("layers", 2)) >= 4 and (b.get("fab") or "jlcpcb") == "jlcpcb":
            fab.update(min_drill=0.2, min_via=0.45)      # JLC multilayer limits
        pro = self.path(".kicad_pro")
        data = json.loads(pro.read_text()) if pro.exists() else {}
        base = {"bus_width": 12, "clearance": fab["clear"], "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
                "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
                "name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
                "track_width": fab["track"], "via_diameter": fab["via"], "via_drill": fab["drill"], "wire_width": 6}
        classes = [dict(base)]
        patterns = []
        notes = []
        skipped = []
        pour = self.pour_net() if "pour" in self.o.flags else None
        gaps = specmod.pad_gaps(self.spec)
        MAX_AUTO = 3.0          # wider than this, a track won't fit between parts; pour it instead

        def assign(cls, nets):
            for n in nets:
                patterns.append({"netclass": cls, "pattern": "/" + n})
                patterns.append({"netclass": cls, "pattern": n})

        oz = float(b.get("copper_oz", 1))
        for nc in self.spec.get("netclasses", []):
            track = float(nc.get("track_mm") or fab["track"])
            track0 = track
            cur = float(nc.get("current_a") or 0)
            nets = list(nc.get("nets", []))
            if pour and pour in nets and cur:
                # ground is poured on every layer (and is a plane on 4 layers): the pour carries the current
                nets.remove(pour)
                classes.append(dict(base, name="Ground", track_width=max(track0, fab["track"]),
                                    clearance=float(nc.get("clearance_mm") or fab["clear"])))
                assign("Ground", [pour])
                notes.append(f"{pour}: left out of {nc['name']} current sizing; the ground pour carries it")
            if cur and "ipc" in self.o.flags:
                need = physics.ipc2221_width_mm(cur, self.o.temp_rise_c, oz)
                if need > track:
                    if need > MAX_AUTO:
                        notes.append(f"{nc['name']}: {cur} A needs {need:.2f} mm at {self.o.temp_rise_c:.0f} C rise, too wide to "
                                     f"autoroute; capped at {MAX_AUTO} mm. Pour these nets or use 2 oz copper (the IPC check will flag them)")
                        track = MAX_AUTO
                    else:
                        notes.append(f"{nc['name']}: widened {track:.2f} -> {need:.2f} mm for {cur} A at {self.o.temp_rise_c:.0f} C rise")
                        track = round(need + 0.005, 2)
            clr = float(nc.get("clearance_mm") or fab["clear"])
            g = min([gaps[n] for n in nets if n in gaps] or [9e9])
            if clr < 0.5 and g + 0.005 < clr:
                new = max(fab["min_clear"], math.floor(g * 1000) / 1000)
                notes.append(f"{nc['name']}: clearance {clr} mm is wider than the {g:.3f} mm between pads on its own parts; "
                             f"lowered to {new} mm")
                clr = new
            if nc["name"] == "Default":
                classes[0].update(track_width=track, clearance=clr)
                continue
            c = dict(base, name=nc["name"], track_width=track, clearance=clr)
            if track > fab["via"] * 0.9:
                c["via_diameter"] = round(max(fab["via"], track * 0.8), 2)
                c["via_drill"] = round(max(fab["drill"], c["via_diameter"] / 2), 2)
            classes.append(c)
            assign(nc["name"], nets)
        if self.spec.get("rf") and "rf" in self.o.flags:
            h, er, t = physics.top_microstrip_params(int(b["layers"]), float(b.get("thickness_mm", 1.6)), oz)
            for rf in self.spec["rf"]:
                z = float(rf.get("impedance_ohm", 50))
                w = physics.microstrip_width_mm(z, h, er, t)
                net = rf["net"]
                if re.search(r"USB|D[PN]\b|D\+|D-|ETH|LVDS|HDMI|MIPI|_[PN]$", net, re.I):
                    skipped.append(f"{net}: looks like a differential pair; single-ended microstrip width doesn't apply, "
                                   "routed at default width (hand-route as a 90 ohm pair if it needs it)")
                    continue
                if w > self.o.max_rf_width:
                    skipped.append(f"{net}: {z:.0f} ohm needs {w:.2f} mm on this stackup, wider than the {self.o.max_rf_width} mm limit; "
                                   "not applied (use 4 layers for ~0.35 mm, or hand-route as grounded coplanar waveguide)")
                    continue
                name = f"RF_{int(z)}R"
                if not any(c["name"] == name for c in classes):
                    classes.append(dict(base, name=name, track_width=w, clearance=max(fab["clear"], 0.3)))
                assign(name, [net])
                notes.append(f"{net}: {z:.0f} ohm microstrip = {w:.3f} mm (h={h} mm, er={er})")
        data.setdefault("net_settings", {})
        data["net_settings"]["classes"] = classes
        data["net_settings"]["netclass_patterns"] = patterns
        data["net_settings"].setdefault("meta", {"version": 3})
        rules = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})
        rules.update({"min_track_width": fab["min_track"], "min_clearance": fab["min_clear"],
                      "min_via_diameter": fab["min_via"], "min_through_hole_diameter": fab["min_drill"],
                      "min_via_annular_width": fab["annular"], "min_copper_edge_clearance": fab["edge"],
                      "min_hole_to_hole": fab["h2h"], "min_hole_clearance": 0.25,
                      "min_resolved_spokes": 1})
        data.setdefault("meta", {"filename": pro.name, "version": 1})
        pro.write_text(json.dumps(data, indent=2))
        if any(str(p.get(k, "")).startswith("kipipe_parts:") for p in self.spec.get("components", []) for k in ("symbol", "footprint")):
            from . import libs
            libs.project_tables(pro.parent)
        self.metrics["netclasses"] = {c["name"]: c["track_width"] for c in classes}
        for n in notes:
            self.log("  " + n)
        for n in skipped:
            self.log("  warning: rf " + n)
        self.stage("Rules & net classes", "warn" if skipped else "ok", f"{b.get('fab', 'jlcpcb')} rules, " + ", ".join(f"{c['name']} {c['track_width']} mm" for c in classes))

    def board(self):
        pcb = self.path(".kicad_pcb")
        if self.o.pcb_file:
            src = Path(self.o.pcb_file)
            if src.resolve() != pcb.resolve():
                shutil.copy(src, pcb)
            self.log(f"  hybrid mode: starting from {src.name}; locked footprints and tracks are kept")
        b = self.spec["board"]
        job = {"board": str(pcb), "netlist": str(self.dir / "out/netlist.net"), "layers": int(b["layers"]),
               "fp_libs": {k: self.env.to_sandbox(v) for k, v in kl.footprint_libs().items()},
               "edges": {c["ref"]: c["edge"] for c in self.spec["components"] if c.get("edge")},
               "mounting_holes": b.get("mounting_holes", "none"),
               "width": b.get("width_mm"), "height": b.get("height_mm"),
               "shape": outline.parse(b),
               "placement": {"clearance": 0.5, "edge_margin": 1.0, "iterations": self.o.placement_iterations,
                             "edge_clr": FABS.get(b.get("fab") or "jlcpcb", FABS["jlcpcb"])["edge"] + 0.1},
               "presets": (self.spec.get("derive") or {}).get("presets") or {},
               "report": str(self.dir / "out/build.json")}
        ncl = {}
        for nc_ in self.spec.get("netclasses") or []:
            try:
                c = float(nc_.get("clearance_mm") or 0)
            except (TypeError, ValueError):
                continue
            for n in nc_.get("nets", []):
                ncl[n] = max(ncl.get(n, 0.0), c)
        job["net_clearance"] = ncl
        plan = json.loads(json.dumps(self.spec.get("floorplan") or {}))
        for reg in plan.get("regions", []):
            reg["rect"] = specmod.area_rect(reg.get("area"))
        job["floorplan"] = plan
        # which pins are supply pins (for pinning decoupling caps); needs the symbols
        power = {}
        for c in self.spec["components"]:
            if c.get("symbol"):
                try:
                    power[c["ref"]] = [p.number for p in kl.symbol_pins(kl.get_symbol(c["symbol"])) if p.etype == "power_in"]
                except KeyError:
                    pass
        job["power_pins"] = power
        if {"video", "review"} & self.o.flags:
            job["frames"] = str(self.dir / "out/frames.json")
        self.pcbnew("build", job)
        rep = json.loads((self.dir / "out/build.json").read_text())
        self.metrics["board"] = rep
        extra = []
        if rep.get("grids"):
            extra.append(f"{rep['grids']} grid(s)")
        if rep.get("decoupling"):
            extra.append(f"{rep['decoupling']} decoupling caps pinned")
        if rep.get("antennas"):
            extra.append("antenna: " + ", ".join(rep["antennas"]))
        if rep.get("relaxed"):
            extra.append("constraints dropped for " + ", ".join(rep["relaxed"]))
        self.stage("Placement", "warn" if rep.get("relaxed") else "ok",
                   f"{rep['width']:.0f} x {rep['height']:.0f} mm, {rep['footprints']} footprints, {rep['locked']} locked"
                   + ("; " + "; ".join(extra) if extra else ""))
        self.files["pcb"] = pcb.name
        self.files["project"] = self.path(".kicad_pro").name

    def review(self):
        """Model checks a render of the placement; a revised floorplan triggers one re-place."""
        from . import llm, video
        if self.o.sch_file:
            self.stage("Review", "skip", "existing schematic input has no floorplan to revise")
            return
        for rnd in range(self.o.review_rounds):
            png = self.dir / "out" / f"placement_{rnd}.png"
            regions = {}
            for i, reg in enumerate(self.spec["floorplan"].get("regions", [])):
                rect = specmod.area_rect(reg.get("area"))
                if rect:
                    regions[f"region {i + 1}: {', '.join(reg.get('refs', [])[:4])}{'...' if len(reg.get('refs', [])) > 4 else ''}"] = rect
            video.render_placement(self.dir / "out/frames.json", png, regions)
            self.files[f"placement_{rnd}"] = f"out/{png.name}"
            rep = self.metrics.get("board", {})
            comps = "\n".join(f"  {c['ref']}: {c['value']} [{c['footprint'].split(':')[-1]}]"
                               + (f" edge={c['edge']}" if c.get("edge") else "") for c in self.spec["components"])
            grids = [f"GRID{i + 1} = {g['refs'][0]}..{g['refs'][-1]}" for i, g in enumerate(self.spec["floorplan"].get("grids", []))]
            context = (f"User request:\n{self.o.prompt or self.spec.get('description', '')}\n\n"
                       f"Board {rep.get('width', 0):.0f} x {rep.get('height', 0):.0f} mm, {self.spec['board']['layers']} layers.\n"
                       f"Components:\n{comps}\n\n"
                       + (f"Grid blocks in the image: {'; '.join(grids)} (auto-detected matrices appear as GRIDn too)\n" if grids else "")
                       + f"Current floorplan:\n{json.dumps(self.spec['floorplan'], indent=1)}\n"
                       + (f"Constraints the placer had to drop: {rep.get('relaxed')}\n" if rep.get("relaxed") else ""))
            try:
                ans = llm.review_placement(self.o, self.dir, png, context, self.log)
            except Exception as e:
                self.stage("Review", "warn", f"skipped: {str(e)[:160]}")
                return
            issues = ans.get("issues") or []
            for i in issues:
                self.log("  review: " + str(i))
            if not hasattr(self, "review_log"):
                self.review_log = []
            entry = {"round": rnd + 1, "verdict": ans.get("verdict", "?"), "issues": [str(i) for i in issues],
                     "edges": ans.get("edges") or {}, "floorplan_changed": isinstance(ans.get("floorplan"), dict)
                     and ans["floorplan"] != self.spec.get("floorplan"), "image": f"out/{png.name}"}
            self.review_log.append(entry)
            if ans.get("verdict") != "revise":
                self.stage("Review", "ok", "model accepted the placement" + (f" ({len(issues)} notes)" if issues else ""))
                return
            new = json.loads(json.dumps(self.spec))
            for ref, edge in (ans.get("edges") or {}).items():
                for c in new["components"]:
                    if c["ref"] == ref:
                        c["edge"] = edge if edge in ("left", "right", "top", "bottom") else None
            if isinstance(ans.get("floorplan"), dict):
                new["floorplan"] = ans["floorplan"]
            new = specmod.normalize(new)
            check = specmod.validate(new)
            if not check.ok:
                self.log("  review proposed an invalid floorplan, keeping the original:\n    " + check.text().replace("\n", "\n    "))
                self.stage("Review", "warn", "revision rejected (invalid)")
                entry["outcome"] = "revision rejected: the proposed floorplan failed validation"
                return
            (self.dir / f"spec_v{rnd}.json").write_text(json.dumps(self.spec, indent=2))
            self.spec = new
            (self.dir / "spec.json").write_text(json.dumps(new, indent=2))
            pcb = self.path(".kicad_pcb")
            if not self.o.pcb_file:
                pcb.unlink(missing_ok=True)
            self.log(f"  re-placing with the revised floorplan ({len(issues)} issues)")
            entry["outcome"] = "board re-placed with the revised floorplan"
            self.board()
            self.stage("Review", "ok", f"revised: {'; '.join(str(i) for i in issues)[:200]}")

    def pour_net(self):
        nets = self.spec.get("nets", {})
        if "GND" in nets:
            return "GND"
        return next((n for n in nets if n.upper() in ("GND", "VSS", "0V", "AGND")), None)

    def route(self):
        pcb = self.path(".kicad_pcb")
        dsn, ses = self.dir / "out/board.dsn", self.dir / "out/board.ses"
        ses.unlink(missing_ok=True)
        pour = self.pour_net() if "pour" in self.o.flags else None
        layers = int(self.spec["board"].get("layers", 2))
        plane = pour if (pour and layers >= 4) else None
        self.pcbnew("dsn", {"board": str(pcb), "dsn": str(dsn), "plane_net": plane})
        if plane:
            self.log(f"  {plane} is a plane on In1.Cu: the router drops vias into it instead of routing it as tracks")
        conns = sum(max(0, len(p) - 1) for n, p in self.spec["nets"].items() if n != plane)
        timeout = self.o.router_timeout
        if timeout == 300:          # the default: scale with the size of the job
            timeout = int(min(1500, max(300, 240 + 1.5 * conns)))
            if timeout != 300:
                self.log(f"  router time limit {timeout} s for {conns} connections")
        t0 = time.time()
        router.route(dsn, ses, passes=self.o.passes, timeout=timeout, log=self.log, use_cache=not self.o.fresh)
        dt = time.time() - t0
        self.pcbnew("finish", {"board": str(pcb), "ses": str(ses), "pour_net": pour, "report": str(self.dir / "out/route.json")})
        rep = json.loads((self.dir / "out/route.json").read_text())
        self.metrics["routing"] = dict(rep, seconds=round(dt, 1))
        status = "ok" if rep["unrouted"] == 0 else "fail"
        self.stage("Autoroute", status, f"{rep['tracks']} segments, {rep['vias']} vias, {rep['unrouted']} unrouted, {dt:.0f}s")
        if rep["unrouted"]:
            self.route_failed = True
            self.log(f"  {rep['unrouted']} connections unrouted: enlarge the board, add layers, raise passes, or move locked parts")

    def drc(self):
        out = self.dir / "out/drc.json"
        if self.env.has_drc:
            args = ["pcb", "drc", "--format", "json", "--severity-all", "-o", out]
            if self.path(".kicad_sch").exists():
                args.append("--schematic-parity")
            self.run_cli(*args, self.path(".kicad_pcb"))
        else:
            self.log(f"  kicad-cli {self.env.major} has no DRC command; using pcbnew's DRC engine")
            self.pcbnew("drc", {"board": str(self.path(".kicad_pcb")), "out": str(out)})
        data = json.loads(out.read_text())
        # library-table complaints are about this machine's config, not the board
        data["violations"] = [v for v in data.get("violations", []) if v.get("type") != "lib_footprint_issues"]
        viol = data.get("violations", [])
        errs = [v for v in viol if v.get("severity") == "error"]
        unconn = data.get("unconnected_items", [])
        parity = data.get("schematic_parity", [])
        for v in (errs + unconn + parity)[:30]:
            self.log(f"  {v.get('severity', '')}: {v.get('description')}")
        self.files["drc"] = "out/drc.json"
        n_err = len(errs) + len(unconn) + len([p for p in parity if p.get("severity") == "error"])
        self.metrics["drc"] = {"errors": len(errs), "warnings": len(viol) - len(errs), "unconnected": len(unconn), "parity": len(parity)}
        detail = f"{len(errs)} errors, {len(viol) - len(errs)} warnings, {len(unconn)} unconnected, {len(parity)} parity"
        own = {}
        for v in errs:                      # holes that are part of a library footprint, not something the router made
            if v.get("type") == "drill_out_of_range":
                for it in v.get("items", []):
                    m = re.search(r"pad \S+ .*of (\S+)", str(it.get("description", "")))
                    if m:
                        own[m.group(1)] = own.get(m.group(1), 0) + 1
        if own:
            b = self.spec.get("board", {})
            self.log(f"  {sum(own.values())} of the errors are holes inside the footprint of {', '.join(own)} (thermal vias) that are smaller than "
                     f"{b.get('fab') or 'jlcpcb'}'s minimum for a {b.get('layers', 2)}-layer board. Nothing was routed wrong: choose 4 layers "
                     "(smaller holes are allowed), another fab, or run with the force option if your fab accepts them")
            detail += f"; {sum(own.values())} are small holes inside {', '.join(own)}'s own footprint"
        self.stage("DRC", "fail" if n_err else ("warn" if viol else "ok"), detail)
        return n_err == 0

    def geometry(self):
        out = self.dir / "out/geometry.json"
        self.pcbnew("geometry", {"board": str(self.path(".kicad_pcb")), "out": str(out)})
        return json.loads(out.read_text())

    def check_ipc(self, geo):
        b = self.spec["board"]
        oz = float(b.get("copper_oz", 1))
        rows, bad = [], []
        for nc in self.spec.get("netclasses", []):
            cur = float(nc.get("current_a") or 0)
            if not cur:
                continue
            need = physics.ipc2221_width_mm(cur, self.o.temp_rise_c, oz)
            for n in nc.get("nets", []):
                widths = [t["w"] for t in geo["tracks"] if t["net"].lstrip("/") == n]
                if not widths:
                    rows.append(f"{n}: no tracks (pour or pads only)")
                    continue
                wmin = min(widths)
                rise = physics.trace_temp_rise_c(cur, wmin, oz)
                ok = wmin + 1e-6 >= need
                rows.append(f"{n}: {cur} A, narrowest {wmin:.2f} mm, needs {need:.2f} mm, est. rise {rise:.1f} C {'OK' if ok else 'TOO NARROW'}")
                if not ok:
                    bad.append(n)
        for r in rows:
            self.log("  " + r)
        if not rows:
            self.stage("IPC-2221 current", "skip", "no net class declares current_a")
        else:
            self.stage("IPC-2221 current", "fail" if bad else "ok", f"{len(rows)} nets checked" + (f", too narrow: {', '.join(bad)}" if bad else ""))

    def fab_outputs(self, register=True):
        fab = self.dir / "fab"
        if fab.exists():
            shutil.rmtree(fab)
        fab.mkdir()
        pcb = self.path(".kicad_pcb")
        cu = ["F.Cu", "B.Cu"] if int(self.spec["board"]["layers"]) == 2 else ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        layers = cu + ["F.Paste", "B.Paste", "F.Silkscreen", "B.Silkscreen", "F.Mask", "B.Mask", "Edge.Cuts"]
        self.run_cli("pcb", "export", "gerbers", "--no-protel-ext", "--use-drill-file-origin", "-l", ",".join(layers), "-o", str(fab) + "/", pcb)
        self.run_cli("pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th", "--drill-origin", "plot",
                     "--generate-map", "--map-format", "gerberx2", "-o", str(fab) + "/", pcb)
        self.run_cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--use-drill-file-origin",
                     "-o", fab / f"{self.name}-pos.csv", pcb)
        zpath = self.dir / "out" / f"{self.name}-gerbers.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(fab.iterdir()):
                if not f.name.endswith("-pos.csv"):
                    z.write(f, f.name)
        if register:
            self.files["gerbers_zip"] = f"out/{zpath.name}"
            self.stage("Fab outputs", "ok", f"{len(list(fab.iterdir()))} files")
        else:
            zpath.unlink()

    def renders(self):
        pcb = self.path(".kicad_pcb")
        top = ["F.Cu", "F.Silkscreen", "Edge.Cuts"]
        bot = ["B.Cu", "B.Silkscreen", "Edge.Cuts"]
        for name, layers, extra in (("pcb_top", top, []), ("pcb_bottom", bot, ["--mirror"]),
                                    ("pcb_copper", ["F.Cu", "B.Cu", "Edge.Cuts"], [])):
            out = self.dir / f"out/{name}.svg"
            p = self.run_cli("pcb", "export", "svg", "--exclude-drawing-sheet", "--page-size-mode", "2",
                             "-l", ",".join(layers), *extra, "-o", out, pcb, check=False)
            if out.exists():
                self.files[name] = f"out/{name}.svg"
        if "render3d" in self.o.flags:
            if self.env.major >= 9:
                notes = []
                out = self.dir / "out/render.png"
                p = self.run_cli("pcb", "render", "-o", out, "--quality", "high", "--width", "1600", "--height", "1000", pcb, check=False)
                if out.exists():
                    self.files["render3d"] = "out/render.png"
                    notes.append("render")
                # interactive model; option names differ between KiCad versions, so try richest first
                glb = self.dir / "out/board.glb"
                base = ["pcb", "export", "glb", "-f"]
                org = self.metrics.get("board", {}).get("origin")
                origin = ["--user-origin", f"{org[0]:.4f}x{org[1]:.4f}mm"] if org else []
                rich = ["--subst-models", "--include-tracks", "--include-pads", "--include-zones",
                        "--include-silkscreen", "--include-soldermask"]
                q, used_origin = None, False
                for extra in ([*rich, *origin], rich, ["--subst-models", "--include-tracks", "--include-zones"],
                              ["--subst-models"], []):
                    glb.unlink(missing_ok=True)
                    q = self.run_cli(*base, *extra, "-o", glb, pcb, check=False)
                    if glb.exists() and glb.stat().st_size > 0:
                        self.files["model3d"] = "out/board.glb"
                        used_origin = bool(origin) and "--user-origin" in extra
                        notes.append(f"interactive model ({glb.stat().st_size // 1024} KB)")
                        break
                if "model3d" in self.files:
                    (self.dir / "out/model3d.json").write_text(json.dumps({"board_origin": used_origin,
                                                                           "origin_mm": org if used_origin else None}))
                    self.files["model3d_meta"] = "out/model3d.json"
                    self.stage("3D render", "ok", ", ".join(notes))
                elif notes:
                    err = ((q.stderr if q else "") or "").strip().splitlines()[-1:] or ["no output"]
                    self.log("  GLB export failed: " + err[0][:200])
                    self.stage("3D render", "warn", "static render only; interactive model export failed: " + err[0][:120])
                else:
                    self.stage("3D render", "warn", ((p.stderr or "") + ((q.stderr if q else "") or ""))[-200:])
            else:
                self.stage("3D render", "skip", "needs KiCad 9+")

    def openems(self, geo):
        from . import ems
        if not self.spec.get("rf"):
            self.stage("openEMS", "skip", "no rf nets in spec")
            return
        res = ems.prepare_and_run(self, geo)
        self.stage("openEMS", res["status"], res["detail"])
        for k, v in res.get("files", {}).items():
            self.files[k] = v

    def _svg2png(self, svg, png, width):
        try:
            import cairosvg
            cairosvg.svg2png(url=str(svg), write_to=str(png), output_width=width, background_color="white")
            return True
        except Exception:
            for tool in (["rsvg-convert", "-w", str(width), "-b", "white", "-o", str(png), str(svg)],):
                if shutil.which(tool[0]):
                    return subprocess.run(tool).returncode == 0
        return False

    # -- driver ---------------------------------------------------------------
    def run(self) -> dict:
        def front():
            self.save_options()
            self.get_spec()
            self.confirm_gate()
            self.schematic()
            self.erc()
            self.write_project()
            self.board()
            if "review" in self.o.flags:
                self.review()
            self.back_half()
        return self._execute(front)

    def preflight(self):
        """Before the design: questions the request leaves open, and what the planned parts are missing here."""
        o = self.o
        if "ask" not in o.flags or getattr(o, "_preflight_done", False) or len((o.prompt or "").strip()) < 12:
            return
        from . import preflight as pfm
        try:
            pf = pfm.assess(pfm.plan(o, self.dir, self.log))
        except Exception as e:
            self.log(f"  questions before the design were skipped: {type(e).__name__}: {e}")
            self.stage("Questions", "skip", f"{type(e).__name__}: {str(e)[:80]}")
            return
        (self.dir / "out").mkdir(exist_ok=True)
        (self.dir / "out/preflight.json").write_text(json.dumps(pf, indent=1))
        self.files["preflight"] = "out/preflight.json"
        o.plan = [{k: p.get(k) for k in ("name", "role", "symbols", "footprint", "footprint_ok", "record")} for p in pf["parts"]]
        need = [p for p in pf["parts"] if p["needs"]]
        for q in pf["questions"]:
            self.log(f"  question: {q['q']}")
        for p in need:
            self.log(f"  {p['name']} ({p.get('role', '')}): no {', no '.join(p['needs'])}")
        if not pf["open"]:
            self.stage("Questions", "ok", f"nothing open; {len(pf['parts'])} part(s) planned, each with a datasheet record and a library symbol")
            return
        detail = f"{len(pf['questions'])} question(s), {len(need)} of {len(pf['parts'])} planned part(s) missing a datasheet or library part"
        ask = getattr(o, "_ask", None)
        if ask:
            answers, sheets, libs_ = ask(pf)
            self.apply_preflight(pf, answers, sheets, libs_)
            self.stage("Questions", "ok", detail + "; answered in the terminal")
        elif getattr(o, "_gate", False):
            self.stage("Questions", "warn", detail + ": waiting for you")
            raise Paused("preflight")
        else:
            o.answers = pfm.answers_text(pf, {})
            self.stage("Questions", "warn", detail + "; nobody to ask, so the first option of each question was assumed and missing parts were left missing")

    def apply_preflight(self, pf, answers, sheets, libs_, names=None):
        from . import preflight as pfm
        o = self.o
        o.datasheet_names.update(names or {})
        if pf.get("questions"):
            o.answers = pfm.answers_text(pf, answers or {})
        for z in libs_ or []:
            try:
                from . import libs as libmod
                rep = libmod.import_archive(z, log=self.log)
                self.log(f"  library {Path(z).name}: {len(rep['symbols'])} symbol(s), {len(rep['footprints'])} footprint(s), {len(rep['models'])} 3D model(s)")
            except Exception as e:
                self.log(f"  could not import {Path(str(z)).name}: {e}")
        for s in sheets or []:
            if s not in o.datasheets:
                o.datasheets.append(s)
        if libs_:
            kl._load_lib.cache_clear() if hasattr(kl._load_lib, "cache_clear") else None
            pfm.assess(pf)
            o.plan = [{k: p.get(k) for k in ("name", "role", "symbols", "footprint", "footprint_ok", "record")} for p in pf["parts"]]
        o._preflight_done = True

    def continue_preflight(self, answers, sheets, libs_, names=None) -> dict:
        """Carry on after the user answered (web app). The whole run starts again; the questions are not asked twice."""
        try:
            pf = json.loads((self.dir / "out/preflight.json").read_text())
        except Exception:
            pf = {"questions": [], "parts": []}
        self.apply_preflight(pf, answers, sheets, libs_, names)
        self.o.plan = [{k: p.get(k) for k in ("name", "role", "symbols", "footprint", "footprint_ok", "record")} for p in pf.get("parts", [])]
        self.stage("Questions", "ok", f"{len(pf.get('questions', []))} answered, {len(sheets or [])} datasheet(s) and {len(libs_ or [])} library file(s) added")
        return self.run()

    def confirm_gate(self):
        """Expert level: stop once the circuit is designed, before any layout work, so the user can read the spec,
        the assumptions and the rule checks, and either continue or revise. Nothing about the design differs."""
        o = self.o
        if o.level != "expert" or not getattr(o, "_gate", False) or o.spec_file or o.sch_file or getattr(self, "_confirmed", False):
            return
        if "checks" in o.flags:
            self.design_checks()
        self.write_connections() if hasattr(self, "write_connections") else None
        n = len(self.spec.get("assumptions") or [])
        self.stage("Your review", "warn", f"stopped before layout: read the spec{f', {n} assumption(s)' if n else ''} and the checks, then Continue or Revise")
        raise Paused("spec")

    def resume(self) -> dict:
        """Continue a run that stopped at the expert gate, with the spec as it stands in the job folder."""
        def front():
            self._revised = json.loads((self.dir / "spec.json").read_text())
            self._confirmed = True
            self.get_spec()
            self.schematic()
            self.erc()
            self.write_project()
            self.board()
            if "review" in self.o.flags:
                self.review()
            self.back_half()
        return self._execute(front)

    def guide(self):
        """Review guide (free) for guided and standard runs; guided also gets the plain-language walkthrough."""
        if self.o.level == "expert":
            return
        try:
            from . import addons
            job = addons.Job(self.dir, self.spec)
            rec = addons.run(job, "gen.guide", log=self.log)
            self.files["guide"] = "out/guide.json"
            detail = rec.get("summary", "")
            if self.o.level == "guided":
                w = addons.run(job, "aid.explain", {"_options": self.o, "level": "guided"}, log=self.log)
                detail += "; walkthrough " + ("written" if w["status"] in ("ok", "warn") else "not written: " + w.get("summary", "")[:80])
            self.stage("Guide", "ok", detail)
        except Exception as e:
            self.log(f"  guide not written: {type(e).__name__}: {e}")

    def reroute(self, moves: dict, replace_others: bool = False) -> dict:
        """Apply manual moves (board mm: {ref: {x, y, rot}}), lock the moved parts, and route again.
        replace_others re-runs placement for every unlocked part around the moved ones."""
        def front():
            self.spec = json.loads((self.dir / "spec.json").read_text())
            if (self.dir / "out/build.json").exists():
                self.metrics["board"] = json.loads((self.dir / "out/build.json").read_text())
            self.files.update({"spec": "spec.json", "schematic": self.path(".kicad_sch").name,
                               "pcb": self.path(".kicad_pcb").name, "project": self.path(".kicad_pro").name})
            if (self.dir / "connections.txt").exists():
                self.files["connections"] = "connections.txt"
            if (self.dir / "out/schematic.svg").exists():
                self.files["schematic_svg"] = "out/schematic.svg"
            self.pcbnew("apply", {"board": str(self.path(".kicad_pcb")), "moves": moves})
            n = len(moves)
            if replace_others:
                self.o.pcb_file = str(self.path(".kicad_pcb"))
                self.board()
                self.stage("Manual edit", "ok", f"{n} part(s) moved and locked; the rest re-placed around them")
            else:
                self.stage("Manual edit", "ok", f"{n} part(s) moved and locked; everything else kept" if n
                           else "board edited outside kipipe; rerouting as is")
            self.back_half()
        return self._execute(front)

    def revise(self, request: str, keep_layout: bool = True) -> dict:
        """Change an existing design from a plain-language request. Unchanged parts keep their place."""
        def front():
            from . import llm
            old = json.loads((self.dir / "spec.json").read_text())
            self.spec = old
            old_pcb = self.path(".kicad_pcb")
            self.o.prompt = (self.o.prompt + "\n\n" if self.o.prompt else "") + "Revision: " + request.strip()
            self.save_options()
            new = llm.revise_spec(self.o, self.dir, old, request, self.log)
            was = {c["ref"]: c for c in old["components"]}
            now = {c["ref"]: c for c in new["components"]}
            added = sorted(set(now) - set(was), key=specmod._natkey)
            removed = sorted(set(was) - set(now), key=specmod._natkey)
            changed = sorted([r for r in set(now) & set(was) if now[r].get("footprint") != was[r].get("footprint")
                              or now[r].get("value") != was[r].get("value")], key=specmod._natkey)
            same = [r for r in set(now) & set(was) if now[r].get("footprint") == was[r].get("footprint")]
            bits = [f"{len(added)} added", f"{len(removed)} removed", f"{len(changed)} changed"]
            self.stage("Revision", "ok", ", ".join(bits))
            for label, refs in (("added", added), ("removed", removed), ("changed", changed)):
                if refs:
                    self.log(f"  {label}: {', '.join(refs)}")
            for ch in (new.get("rationale") or {}).get("changes") or []:
                self.log("  change: " + str(ch))
            self._revised = new
            self.get_spec()
            self.schematic()
            self.erc()
            self.write_project()
            if keep_layout and old_pcb.exists():
                rep_f = self.dir / "out/revise.json"
                self.pcbnew("revise", {"board": str(old_pcb), "lock": same, "remove": removed, "report": str(rep_f)})
                temp = json.loads(rep_f.read_text()).get("temp_locked", []) if rep_f.exists() else []
                self.o.pcb_file = str(old_pcb)
                try:
                    self.board()
                    if temp:
                        self.pcbnew("unlock", {"board": str(old_pcb), "refs": temp})
                except StageError as e:
                    self.log(f"  the revised parts don't fit around the kept ones ({str(e)[:120]}); placing everything again")
                    old_pcb.unlink(missing_ok=True)
                    self.o.pcb_file = None
                    self.board()
            else:
                old_pcb.unlink(missing_ok=True)
                self.o.pcb_file = None
                self.board()
                if "review" in self.o.flags:
                    self.review()
            self.back_half()
        return self._execute(front)

    def back_half(self):
        self.route()
        drc_ok = self.drc()
        need_geo = bool({"ipc", "openems", "video", "inspect"} & self.o.flags)
        geo = self.geometry() if need_geo else None
        if "ipc" in self.o.flags:
            self.check_ipc(geo)
        self.renders()
        self.layout_dump()
        if "checks" in self.o.flags:
            self.design_checks()
        if "inspect" in self.o.flags:
            self.inspect(geo)
        if "video" in self.o.flags and (self.dir / "out/frames.json").exists():
            from . import video
            v = video.make(self)
            if v:
                self.files["video"] = v
            self.stage("Video", "ok" if v else "warn", v or "no frames")
        if (drc_ok and not self.route_failed) or "force" in self.o.flags:
            self.fab_outputs()
        else:
            why = "board not fully routed" if self.route_failed else "DRC failed"
            self.stage("Fab outputs", "skip", f"{why}; rerun with 'force' to export anyway")
        if "openems" in self.o.flags:
            self.openems(geo)

    def design_checks(self):
        """Rule checks on the circuit and the placed board (add-ons marked auto). Findings never stop a build."""
        try:
            from . import addons
            from .addons import config as acfg
            job = addons.Job(self.dir, self.spec)
            skip = {x.strip() for x in str(acfg.get("checks.skip", "") or "").split(",") if x.strip()}
            for a in list(addons.load().values()):
                if a.kind == "check" and a.id in skip:
                    a.auto = False
            out = addons.run_checks(job, log=self.log)
            self.files["checks"] = "out/checks.json"
            c = out["counts"]
            self.metrics["checks"] = c
            for rec in out["checks"]:
                for f in rec["findings"]:
                    if f["severity"] != "note":
                        self.log(f"  {f['severity']}: {f['title']}")
            detail = f"{len(out['checks'])} checks: {c['error']} errors, {c['warning']} warnings, {c['note']} notes"
            if out["broken"]:
                detail += f"; could not run: {', '.join(out['broken'])}"
            self.stage("Design checks", "warn" if out["status"] != "ok" else "ok", detail + " (see Toolbox)")
        except Exception as e:
            self.log(f"  design checks did not run: {type(e).__name__}: {e}")
            self.stage("Design checks", "skip", f"{type(e).__name__}: {e}")

    def inspect(self, geo):
        """Coarse-to-fine visual inspection of the routed board by the model."""
        from . import inspect_board, llm
        lay_f = self.dir / "out/layout.json"
        if not lay_f.exists() or geo is None:
            self.stage("Inspection", "skip", "no layout/geometry to inspect")
            return
        outdir = self.dir / "out/inspect"
        if outdir.exists():
            shutil.rmtree(outdir)
        layout = json.loads(lay_f.read_text())
        passes = inspect_board.build_passes(layout, geo, outdir)
        drc = {}
        if (self.dir / "out/drc.json").exists():
            drc = json.loads((self.dir / "out/drc.json").read_text())
        errs = [v for v in drc.get("violations", []) if v.get("severity") == "error"]
        unconn = drc.get("unconnected_items", [])
        comps = "; ".join(f"{c['ref']} {c.get('value', '')}" for c in self.spec.get("components", []))
        context = (f"Request: {self.o.prompt or self.spec.get('description', '')}\n"
                   f"Board: {layout['board'][0]:.0f} x {layout['board'][1]:.0f} mm, {self.spec.get('board', {}).get('layers', 2)} layers. "
                   f"Net classes: {self.metrics.get('netclasses', {})}.\nParts: {comps}\n"
                   f"Routing: {self.metrics.get('routing', {})}\n"
                   f"DRC errors ({len(errs)}): " + "; ".join(sorted({v.get('description', '') for v in errs}))[:600] + "\n"
                   + (f"Unrouted connections ({len(unconn)}): " + "; ".join(
                       " / ".join(i.get("description", "")[:60] for i in u.get("items", [])) for u in unconn[:12]) + "\n" if unconn else ""))
        try:
            results = llm.inspect_board(self.o, self.dir, passes, context, self.log)
        except Exception as e:
            self.stage("Inspection", "warn", f"skipped: {str(e)[:160]}")
            return
        findings, final, dismissed = [], {}, {}
        for r in results:
            for f in r["data"].get("findings", []) or []:
                if isinstance(f, dict) and f.get("issue"):
                    findings.append(dict(f, pass_=r["pass"], model_=r.get("model")))
            for fid, why in (r["data"].get("dismissed") or {}).items():
                dismissed[str(fid)] = f"{why} ({r['pass'].split(':')[0]}, {r.get('model')})"
            final = r["data"]
        for f in findings:
            if f.get("id") in dismissed:
                f["dismissed"] = dismissed[f["id"]]
        live = [f for f in findings if not f.get("dismissed")]
        parts = {p["ref"]: p for p in layout["parts"]}
        moves = {}
        for ref, m in (final.get("moves") or {}).items():
            try:
                x, y = float(m["x"]), float(m["y"])
                rot = float(m.get("rot", parts[ref]["rot"]))
            except (KeyError, TypeError, ValueError):
                continue
            if ref in parts and not parts[ref].get("fixed") and -1 <= x <= layout["board"][0] + 1 and -1 <= y <= layout["board"][1] + 1:
                moves[ref] = {"x": round(x, 3), "y": round(y, 3), "rot": rot}
        report = {"verdict": final.get("verdict", "?"), "summary": final.get("summary", ""), "findings": findings,
                  "moves": moves, "passes": [{"name": p["name"], "tiles": [{"id": t["id"], "crop": t["crop"],
                  "file": str(Path(t["file"]).relative_to(self.dir))} for t in p["tiles"]]} for p in passes]}
        (outdir / "review.json").write_text(json.dumps(report, indent=1))
        if moves:
            (outdir / "moves.json").write_text(json.dumps(moves, indent=1))
            self.files["inspect_moves"] = "out/inspect/moves.json"
        md = [f"# Inspection: {self.name}", "", f"**Verdict:** {report['verdict']}", "", report["summary"], ""]
        for sev in ("error", "warning", "note"):
            items = [f for f in live if f.get("severity") == sev]
            if items:
                md.append(f"## {sev}s ({len(items)})")
                for f in items:
                    where = f"({f.get('x', '?')}, {f.get('y', '?')}) mm" if f.get("x") is not None else ""
                    md.append(f"- **{', '.join(f.get('refs') or [])}** {where}: {f['issue']}" + (f" Fix: {f['fix']}" if f.get("fix") else ""))
                md.append("")
        gone = [f for f in findings if f.get("dismissed")]
        if gone:
            md.append(f"## Dismissed by a later pass ({len(gone)})")
            md += [f"- ~~{', '.join(f.get('refs') or [])}: {f['issue']}~~ Dismissed: {f['dismissed']}" for f in gone]
            md.append("")
        if moves:
            md.append("## Suggested moves")
            md += [f"- {r}: x {m['x']}, y {m['y']}, rot {m['rot']}" for r, m in moves.items()]
        (outdir / "review.md").write_text("\n".join(md) + "\n")
        self.files["inspect"] = "out/inspect/review.json"
        self.files["inspect_md"] = "out/inspect/review.md"
        n_err = sum(1 for f in live if f.get("severity") == "error")
        n_warn = sum(1 for f in live if f.get("severity") == "warning")
        for f in live:
            if f.get("severity") in ("error", "warning"):
                self.log(f"  inspect {f['severity']}: {', '.join(f.get('refs') or [])} {f['issue'][:140]}")
        self.stage("Inspection", "warn" if n_err or n_warn else "ok",
                   f"verdict {report['verdict']}: {n_err} errors, {n_warn} warnings, "
                   f"{len(live) - n_err - n_warn} notes" + (f", {len(moves)} suggested moves" if moves else "")
                   + (f", {len(findings) - len(live)} dismissed" if len(live) < len(findings) else ""))

    def write_reasoning(self):
        """Everything that explains the design choices, for the Reasoning tab."""
        spec = getattr(self, "spec", None)
        if not spec:
            return
        from . import llm
        b = self.metrics.get("board", {})
        data = {
            "source": ("model" if not (self.o.spec_file or self.o.sch_file) else
                       "spec file" if self.o.spec_file else "existing schematic"),
            "request": self.o.prompt or "",
            "description": spec.get("description", ""),
            "rationale": spec.get("rationale") or {},
            "components": [{k: c.get(k) for k in ("ref", "value", "symbol", "footprint", "edge")} for c in spec.get("components", [])],
            "floorplan": spec.get("floorplan") or {},
            "netclasses": spec.get("netclasses") or [],
            "rf": spec.get("rf") or [],
            "placer": {k: b.get(k) for k in ("width", "height", "footprints", "locked", "grids", "decoupling",
                                             "antennas", "relaxed", "rules") if b.get(k) not in (None, [], {}, 0)},
            "reviews": getattr(self, "review_log", []),
            "models": ({r: llm.model_for(self.o, r) for r in llm.ROLES} if data_source_model(self.o) else {}),
        }
        (self.dir / "out").mkdir(exist_ok=True)
        (self.dir / "out/reasoning.json").write_text(json.dumps(data, indent=1))
        self.files["reasoning"] = "out/reasoning.json"

    def layout_dump(self):
        """Current placement for the web editor."""
        try:
            self.pcbnew("layout", {"board": str(self.path(".kicad_pcb")), "out": str(self.dir / "out/layout.json")})
            self.files["layout"] = "out/layout.json"
        except StageError as e:
            self.log(f"  layout export for the editor failed: {str(e)[-200:]}")

    def save_options(self):
        d = {k: (sorted(v) if isinstance(v, set) else v) for k, v in self.o.__dict__.items() if k != "api_key" and not k.startswith("_")}
        (self.dir / "options.json").write_text(json.dumps(d, indent=1))

    def _execute(self, front) -> dict:
        t0 = time.time()
        ok = True
        error = None
        paused = None
        try:
            self.log(f"KiCad {self.env.version} | flags: {', '.join(sorted(self.o.flags))}")
            front()
        except Paused as e:
            paused = str(e)
            self.log("paused for your review before layout")
        except StageError as e:
            ok, error = False, str(e)
            self.log("STOPPED: " + error)
        except Exception as e:
            import traceback
            ok, error = False, f"{type(e).__name__}: {e}"
            self.log(traceback.format_exc())
        try:
            self.write_reasoning()
        except Exception as e:
            self.log(f"  (reasoning notes not written: {e})")
        if not paused:
            self.guide()
        failed = [s for s in self.stages if s["status"] == "fail"]
        result = {"ok": ok and not failed, "error": error, "paused": paused, "level": self.o.level, "name": self.name, "stages": self.stages,
                  "files": self.files, "metrics": self.metrics, "seconds": round(time.time() - t0, 1),
                  "dir": str(self.dir)}
        (self.dir / "result.json").write_text(json.dumps(result, indent=1))
        self.zip_all()
        result["files"]["project_zip"] = f"out/{self.name}-project.zip"
        (self.dir / "result.json").write_text(json.dumps(result, indent=1))
        self.log(f"done in {result['seconds']}s: {'OK' if result['ok'] else 'FAILED'}")
        try:
            from . import addons
            addons.load()
            addons.emit("job_done", log=self.log, job=addons.Job(self.dir, self.spec or None), result=result)
        except Exception as e:
            self.log(f"  (add-on hooks not run: {e})")
        return result

    def zip_all(self):
        zpath = self.dir / "out" / f"{self.name}-project.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(self.dir.rglob("*")):
                rel = f.relative_to(self.dir)
                if f.is_file() and not f.name.startswith("job_") and f != zpath and not str(rel).startswith("input") \
                        and not f.name.endswith("-project.zip"):
                    z.write(f, f"{self.name}/{rel}")


def load_options(job_dir: Path) -> Options:
    o = Options()
    f = Path(job_dir) / "options.json"
    if f.exists():
        for k, v in json.loads(f.read_text()).items():
            if hasattr(o, k):
                setattr(o, k, set(v) if k == "flags" else v)
    o.prompt = o.prompt or ""
    o.images, o.spec_file, o.sch_file, o.pcb_file = [], None, None, None
    o.flags.discard("review")          # nothing to review in a manual edit
    return o


def copy_job(src: Path, dst: Path) -> Path:
    """Start an edit revision as a copy, so the original run stays intact."""
    def skip(_, names):
        return [n for n in names if n.endswith(("-project.zip", ".mp4", ".gif")) or n.startswith("job_")
                or n in ("result.json", "log.txt")]
    shutil.copytree(src, dst, ignore=skip)
    return dst
