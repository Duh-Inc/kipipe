"""openEMS stage via Antmicro's gerber2ems.

Builds the gerber2ems project folder (fab/ with copper Gerbers, PTH drill,
stackup.json and a *-pos.csv holding Simulation_Port entries at the RF pads,
plus simulation.json), then runs `gerber2ems -a` if it is installed.

Format follows gerber2ems' README: dimensions in micrometres in
simulation.json, port SPn -> port index n-1, positions relative to the
board's bottom-left corner, rotation chosen so the port overlaps the trace.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from . import physics


def _port_rotation(dx_up: float, dy_up: float) -> int:
    # direction from the pad into the trace, y-up board coordinates
    if abs(dy_up) >= abs(dx_up):
        return 0 if dy_up > 0 else 180
    return 270 if dx_up > 0 else 90


def _port(geo, ref_pin: str, net: str):
    ref, _, pin = ref_pin.partition(".")
    pad = next((p for p in geo["pads"] if p["ref"] == ref and p["pin"] == pin), None)
    if pad is None:
        return None, f"pad {ref_pin} not found"
    best = None
    for t in geo["tracks"]:
        if t["net"].lstrip("/") != net or t["layer"] != "F.Cu":
            continue
        for end, other in ((t["a"], t["b"]), (t["b"], t["a"])):
            d = abs(end[0] - pad["x"]) + abs(end[1] - pad["y"])
            if best is None or d < best[0]:
                best = (d, end, other, t["w"])
    if best is None or best[0] > 3.0:
        return None, f"no top-layer trace leaves {ref_pin} (route the RF net on F.Cu)"
    _, end, other, w = best
    dx, dy = other[0] - end[0], -(other[1] - end[1])
    return {"x": pad["x"], "y": pad["y"], "rot": _port_rotation(dx, dy), "w": w}, None


def prepare_and_run(pl, geo) -> dict:
    spec = pl.spec
    b = spec["board"]
    sim = pl.dir / "sim"
    if sim.exists():
        shutil.rmtree(sim)
    fab = sim / "fab"
    fab.mkdir(parents=True)
    src = pl.dir / "fab"
    if not src.exists() or not list(src.glob("*_Cu.gbr")):
        pl.fab_outputs(register=False)
    for f in list(src.glob("*_Cu.gbr")) + list(src.glob("*Edge_Cuts.gbr")) + list(src.glob("*-PTH.drl")):
        shutil.copy(f, fab / f.name)

    layers = physics.stackup(int(b["layers"]), float(b.get("thickness_mm", 1.6)), float(b.get("copper_oz", 1)))
    st = {"layers": [dict({"color": None, "material": None, "epsilon": None, "lossTangent": None}, **l) for l in layers],
          "format_version": "1.0"}
    (fab / "stackup.json").write_text(json.dumps(st, indent=2))

    board = pl.metrics["board"]
    ox, oy = board["origin"]
    h = board["height"]
    ports, traces, rows, problems, nets = [], [], [], [], []
    max_f = 0.0
    for rf in spec["rf"]:
        idx = []
        for k in ("from", "to"):
            if not rf.get(k):
                continue
            port, err = _port(geo, rf[k], rf["net"])
            if err:
                problems.append(err)
                continue
            n = len(ports)
            ports.append({"width": round(port["w"] * 1000), "length": max(800, round(port["w"] * 1000 * 2)),
                          "impedance": float(rf.get("impedance_ohm", 50)), "layer": 0, "plane": 1,
                          "excite": k == "from"})
            rows.append(f"SP{n + 1},Simulation_Port,Simulation_Port,{port['x'] - ox:.4f},{(oy + h) - port['y']:.4f},{port['rot']},top")
            idx.append(n)
        if len(idx) == 2:
            traces.append({"name": rf["net"], "start": idx[0], "stop": idx[1], "nets": ["/" + rf["net"], rf["net"]]})
        max_f = max(max_f, float(rf.get("max_freq_hz", 3e9)))
    if problems or not traces:
        return {"status": "fail", "detail": "; ".join(problems) or "need from/to pins on each rf net"}
    (fab / f"{spec['name']}-top-pos.csv").write_text("Ref,Val,Package,PosX,PosY,Rot,Side\n" + "\n".join(rows) + "\n")
    cfg = {"format_version": "1.2", "frequency": {"start": max_f / 20, "stop": max_f}, "pixel_size": 5,
           "ports": ports, "traces": traces}
    (sim / "simulation.json").write_text(json.dumps(cfg, indent=2))
    files = {"sim_config": "sim/simulation.json"}

    exe = shutil.which("gerber2ems")
    if not exe:
        return {"status": "warn", "files": files,
                "detail": "prepared sim/ (Gerbers, stackup, ports, simulation.json); gerber2ems/openEMS not installed, run `cd sim && gerber2ems -a` after installing"}
    pl.log("  running gerber2ems -a (this can take a long time)")
    p = subprocess.Popen([exe, "-a"], cwd=sim, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        if any(k in line for k in ("Timestep", "ERROR", "Excit", "Saving", "Port")):
            pl.log("  " + line.rstrip()[:160])
    p.wait()
    res = sim / "ems" / "results"
    pngs = sorted(res.glob("*.png")) if res.exists() else []
    for i, f in enumerate(pngs[:6]):
        files[f"ems_{i}"] = str(f.relative_to(pl.dir))
    if p.returncode != 0:
        return {"status": "fail", "files": files, "detail": f"gerber2ems exited {p.returncode}; see log"}
    return {"status": "ok", "files": files, "detail": f"{len(traces)} traces simulated, results in sim/ems/results"}
