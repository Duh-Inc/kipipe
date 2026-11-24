"""Projects, context and house rules: what a design should know beyond its own request.

A project groups boards that belong to one thing (a CubeSat, a robot). It carries:
  context     free text every board in the project is designed with
  boards      the latest run of each board, summarised for the designer (rails, connectors, pinouts)
  interfaces  named connector pinouts that every board using that connector must follow

Stored one file per project in ~/.local/share/kipipe/projects/. A run joins a project through its options
("project": slug). House rules (~/.local/share/kipipe/rules.txt) apply to every design, in or out of a project.
The cross-board check is plain code: two boards that share a connector are compared pin for pin.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .core import HOME, Job, addon, net_voltage, on
from .draw import natkey

DIR = HOME / "projects"
RULES = HOME / "rules.txt"
BUILD_STATES = ("not built", "ordered", "assembled", "works", "has a problem")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:48] or "project"


def all_projects() -> list:
    out = []
    if DIR.is_dir():
        for f in sorted(DIR.glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
    return out


def load(slug) -> dict | None:
    f = DIR / f"{slugify(slug)}.json" if slug else None
    if not f or not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(p: dict) -> dict:
    DIR.mkdir(parents=True, exist_ok=True)
    p["slug"] = slugify(p.get("slug") or p["name"])
    (DIR / f"{p['slug']}.json").write_text(json.dumps(p, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def create(name: str, context: str = "") -> dict:
    slug = slugify(name)
    p = load(slug)
    if p:
        return p
    return save({"name": name.strip(), "slug": slug, "context": context.strip(), "boards": {}, "interfaces": {},
                 "created": time.strftime("%Y-%m-%d")})


def delete(slug) -> bool:
    f = DIR / f"{slugify(slug)}.json"
    if f.exists():
        f.unlink()
        return True
    return False


def job_project(job_dir) -> str | None:
    try:
        return json.loads((Path(job_dir) / "options.json").read_text()).get("project") or None
    except Exception:
        return None


def rules_text() -> str:
    try:
        return RULES.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def set_rules(text: str):
    HOME.mkdir(parents=True, exist_ok=True)
    RULES.write_text(text.strip() + "\n", encoding="utf-8")


def add_rule(line: str):
    line = " ".join(str(line).split())
    if line and line not in rules_text():
        set_rules((rules_text() + "\n" + line).strip())


def build_state(job_dir) -> dict:
    try:
        return json.loads((Path(job_dir) / "build.json").read_text())
    except Exception:
        return {}


def board_summary(job: Job) -> dict:
    """What another board's designer needs to know about this one."""
    rails = sorted({n for n in job.nets if job.voltage(n) not in (None, 0.0)})
    conns = []
    for ref in sorted(job.of_kind("J"), key=natkey):
        c = job.comps[ref]
        ns = job.nets_of(ref)
        conns.append({"ref": ref, "name": str(c.get("value", "")), "footprint": c.get("footprint", "").split(":")[-1], "edge": c.get("edge"),
                      "pins": {n: ns[n] for n in sorted(ns, key=natkey)}})
    size = job.size()
    b = build_state(job.dir)
    return {"name": job.name, "job": job.dir.name, "description": job.spec.get("description", ""), "size": [round(x, 1) for x in size] if size else None,
            "rails": rails, "connectors": conns, "build": b.get("status"), "problem": b.get("notes") if b.get("status") == "has a problem" else None}


def _board_jobs(p: dict, jobs_dir: Path, skip: str | None = None):
    for name, jid in (p.get("boards") or {}).items():
        d = jobs_dir / jid
        if jid != skip and (d / "spec.json").exists():
            try:
                yield Job(d)
            except Exception:
                pass


def jobs_dir_of(job_dir) -> Path:
    return Path(job_dir).parent


def prompt_block(o, workdir) -> str:
    """Context for the designer model: the run's own context, the project, its other boards, and house rules."""
    parts = []
    try:
        from .. import preflight as _pf
        pre = _pf.designer_block(o)
        if pre:
            parts.append(pre)
    except Exception:
        pass
    ctx = (getattr(o, "context", "") or "").strip()
    if ctx:
        parts.append("CONTEXT FOR THIS BOARD (from the user; background, not a list of things to build):\n" + ctx)
    p = load(getattr(o, "project", None))
    if p:
        lines = [f"PROJECT: {p['name']}. This board is one part of it."]
        if p.get("context"):
            lines.append(p["context"])
        if p.get("interfaces"):
            lines.append("Project interfaces. A connector on this board that serves one of these MUST use the same pin numbers and net names, "
                         "and should carry the interface name as its value:")
            for name, it in p["interfaces"].items():
                lines.append(f"- {name}" + (f" ({it['footprint']})" if it.get("footprint") else "") + ": "
                             + ", ".join(f"{k}={v}" for k, v in sorted(it.get("pins", {}).items(), key=lambda x: natkey(x[0])))
                             + (f". {it['note']}" if it.get("note") else ""))
        others = [board_summary(j) for j in _board_jobs(p, jobs_dir_of(workdir), Path(workdir).name)]
        if others:
            lines.append("Boards already designed in this project (match their connectors and rails where this board connects to them). "
                         "If the request says to use the same outline as one of them or to stack on it, use the spec's \"derive\" section; "
                         "code then copies the outline and holes and fixes the mating connector in place:")
            for b in others:
                lines.append(f"- {b['name']}" + (f" ({b['size'][0]:.0f} x {b['size'][1]:.0f} mm)" if b.get("size") else "") + f": {b['description'][:200]} Rails: {', '.join(b['rails']) or 'none named'}."
                             + (f" KNOWN PROBLEM on the built board: {b['problem']}" if b.get("problem") else ""))
                for c in b["connectors"]:
                    lines.append(f"    {c['ref']} \"{c['name']}\" [{c['footprint']}]: " + ", ".join(f"{k}={v}" for k, v in c["pins"].items()))
        parts.append("\n".join(lines))
    rules = rules_text()
    if rules:
        parts.append("HOUSE RULES (the user's standing preferences; follow them unless the request says otherwise):\n" + rules)
    return "\n\n".join(parts)


def add_board(slug, job: Job):
    p = load(slug)
    if p is None:
        return
    p.setdefault("boards", {})[job.name] = job.dir.name
    save(p)


def _job_done(job=None, result=None, **_):
    if job is None or not (result or {}).get("ok"):
        return
    slug = job_project(job.dir)
    if slug:
        add_board(slug, job)


on("job_done", _job_done)


# ---------------------------------------------------------------------------
def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _same_net(a, b) -> bool:
    if _norm(a) == _norm(b):
        return True
    va, vb = net_voltage(a), net_voltage(b)
    return va is not None and va == vb


def _family(fp: str) -> str:
    m = re.match(r"(PinHeader|PinSocket|JST_[A-Z]+|TerminalBlock|USB_[A-Za-z]+|Molex_[A-Za-z-]+|IDC)", fp)
    pitch = re.search(r"P(\d+\.\d+)mm", fp)
    return (m.group(1) if m else fp.split("_")[0]) + (f" {pitch.group(1)} mm" if pitch else "")


@addon(id="check.project", title="Fits its project", kind="check", auto=True, group="Design checks", status="beta",
       summary="Connectors this board shares with the project's interfaces and its other boards agree pin for pin.")
def check_project(r):
    j = r.job
    p = load(job_project(j.dir))
    if not p:
        r.summary = "this run is not part of a project"
        return
    mine = board_summary(j)["connectors"]
    ifaces = {_norm(k): (k, v) for k, v in (p.get("interfaces") or {}).items()}
    done, compared = set(), 0
    mated = set((j.spec.get("derive") or {}).get("presets") or {})      # checked by geometry below, pad against pad
    mine = [c for c in mine if c["ref"] not in mated]
    for c in mine:
        hit = ifaces.get(_norm(c["name"]))
        if not hit:
            continue
        name, it = hit
        done.add(c["ref"])
        compared += 1
        want = it.get("pins", {})
        bad = [f"pin {k}: {c['pins'].get(k, 'unconnected')} here, {v} in the interface" for k, v in sorted(want.items(), key=lambda x: natkey(x[0]))
               if not _same_net(c["pins"].get(k, ""), v)]
        extra = [k for k in c["pins"] if k not in want]
        if bad or extra:
            r.finding("error", f"{c['ref']} does not follow the project interface \"{name}\"",
                      "; ".join(bad + ([f"pins not in the interface: {', '.join(extra)}"] if extra else [])), refs=[c["ref"]],
                      fix="Rewire it to the interface, or update the interface if this board is right.")
        elif it.get("footprint") and _family(it["footprint"]) != _family(c["footprint"]):
            r.finding("warning", f"{c['ref']} uses a different connector type than the interface \"{name}\"",
                      f"{c['footprint']} here, {it['footprint']} in the interface.", refs=[c["ref"]])
        else:
            r.finding("note", f"{c['ref']} matches the project interface \"{name}\"", "", refs=[c["ref"]])
    for o in _board_jobs(p, jobs_dir_of(j.dir), j.dir.name):
        if o.name == j.name:
            continue
        for oc in board_summary(o)["connectors"]:
            for c in mine:
                if c["ref"] in done or not c["name"] or _norm(c["name"]) != _norm(oc["name"]) or len(c["pins"]) != len(oc["pins"]):
                    continue
                compared += 1
                bad = [f"pin {k}: {v} here, {oc['pins'].get(k, 'unconnected')} on {o.name}" for k, v in c["pins"].items() if not _same_net(v, oc["pins"].get(k, ""))]
                if bad:
                    r.finding("warning", f"{c['ref']} \"{c['name']}\" differs from {oc['ref']} on {o.name}", "; ".join(bad) + ". If a cable or stacking header joins them "
                              "one to one, these must agree.", refs=[c["ref"]], fix="Make one of them a project interface so every board follows it.")
                elif _family(c["footprint"]) != _family(oc["footprint"]):
                    r.finding("warning", f"{c['ref']} \"{c['name']}\" has the same pinout as {oc['ref']} on {o.name} but another connector type",
                              f"{c['footprint']} here, {oc['footprint']} there.", refs=[c["ref"]])
                else:
                    r.finding("note", f"{c['ref']} \"{c['name']}\" matches {oc['ref']} on {o.name}", "", refs=[c["ref"]])
    try:
        from .. import derive
        derive.set_context(job_project(j.dir), jobs_dir_of(j.dir), j.dir.name)
        for sev, title, detail in derive.verify(j):
            r.finding(sev, title, detail)
    except Exception as e:
        r.finding("note", "The stacking check could not run", f"{type(e).__name__}: {e}")
    r.finding("note", "What this cannot see", "For connectors compared by name only: whether they face each other mirrored, and cables that cross pins. "
              "A stack declared with the spec's derive section is checked pad against pad instead. Nothing checks the height between stacked boards.")
    r.summary = f"project {p['name']}: {compared} connector comparison(s), {len(p.get('boards') or {})} board(s), {len(p.get('interfaces') or {})} interface(s)"


@addon(id="aid.interface", title="Make a connector a project interface", kind="aid", group="Project", status="beta",
       options=[{"key": "ref", "label": "Connector (for example J2)", "type": "text", "default": ""},
                {"key": "name", "label": "Interface name (empty: the connector's value)", "type": "text", "default": ""},
                {"key": "note", "label": "Note for other boards (optional)", "type": "text", "default": ""}],
       summary="Record this connector's pinout in the project. Every later board whose connector carries the same name is designed to it and checked against it.")
def make_interface(r):
    j = r.job
    slug = job_project(j.dir)
    p = load(slug)
    if not p:
        r.summary = "this run is not part of a project"
        r.finding("note", "No project", "Pick or create a project in the sidebar before the run, then use this on its boards.")
        return
    ref = str(r.opts.get("ref") or "").strip().upper()
    if ref not in j.comps or j.kind(ref) != "J":
        r.summary = f"{ref or 'that'} is not a connector on this board (connectors: {', '.join(sorted(j.of_kind('J'), key=natkey))})"
        return
    name = str(r.opts.get("name") or "").strip() or str(j.comps[ref].get("value") or ref)
    ns = j.nets_of(ref)
    p.setdefault("interfaces", {})[name] = {"pins": {k: ns[k] for k in sorted(ns, key=natkey)}, "footprint": j.comps[ref].get("footprint", "").split(":")[-1],
                                            "from": f"{j.name} {ref}", "note": str(r.opts.get("note") or "").strip()}
    save(p)
    r.summary = f"interface \"{name}\" saved to project {p['name']}: {len(ns)} pin(s)"
    r.data["interface"] = name


@addon(id="aid.buildlog", title="Build log", kind="aid", group="Project",
       options=[{"key": "status", "label": "Where this board stands", "type": "select", "choices": list(BUILD_STATES), "default": "not built"},
                {"key": "notes", "label": "Notes (what happened, what was wrong)", "type": "text", "default": ""},
                {"key": "rule", "label": "Lesson to apply to every future design (optional, one sentence)", "type": "text", "default": ""}],
       summary="Record what happened to the real board. A problem is passed to later boards of the same project; a lesson you write becomes a house rule.")
def buildlog(r):
    j = r.job
    status = r.opts.get("status") if r.opts.get("status") in BUILD_STATES else "not built"
    rec = {"status": status, "notes": str(r.opts.get("notes") or "").strip(), "at": time.strftime("%Y-%m-%d %H:%M")}
    hist = build_state(j.dir).get("history", [])
    hist.append(rec)
    (j.dir / "build.json").write_text(json.dumps({**rec, "history": hist}, indent=1))
    rule = str(r.opts.get("rule") or "").strip()
    if rule:
        add_rule(rule)
        r.finding("note", "Added to your house rules", rule + " (edit or remove it under My rules in the sidebar)")
    r.data["build"] = rec
    r.summary = f"{status}" + (f": {rec['notes']}" if rec["notes"] else "")
