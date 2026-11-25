"""kipipe command line.

  kipipe serve [--host H] [--port P] [--open]
  kipipe run   -p "description" [-i sketch.png] [--spec f.json | --sch f.kicad_sch] [--pcb old.kicad_pcb]
               [--backend claude-code|anthropic-api|ollama] [--model M] [--flags ipc,rf,pour,openems,render3d,force]
               [--layers 2|4] [--fab jlcpcb] [--size 50x40] [--holes M3] [--passes 30] [-o DIR]
  kipipe doctor
  kipipe fetch-router
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .pipeline import ALL_FLAGS, DEFAULT_FLAGS, FABS, Options, Pipeline


def _docling_row():
    from . import docparse
    cli = docparse.find_cli()
    if cli:
        return f"yes ({cli}); runs on {docparse.accel_text()}"
    return (f"optional, not installed: python3 -m venv {docparse.VENV} && {docparse.VENV}/bin/pip install docling "
            "(tables read exactly instead of by the model)")


def cmd_doctor(_):
    from .app import environment
    e = environment()
    k = e.get("kicad", {})
    rows = [
        ("KiCad (kicad-cli)", k.get("version") or k.get("error"), bool(k.get("version"))),
        ("KiCad install", ("flatpak " + k["flatpak"]) if k.get("flatpak") else "native", True),
        ("KiCad libraries", k.get("symbols", "?"), bool(k.get("symbols"))),
        ("pcbnew + footprint load", e["pcbnew"] or ("failed: " + e.get("pcbnew_error", "") +
            ("  (if this is a sandbox permission error: flatpak override --user --filesystem=home org.kicad.KiCad)" if k.get("flatpak") else "  (set KIPIPE_KICAD_PYTHON)")), bool(e["pcbnew"])),
        ("Schematic engine (CLI)", "ok" if e.get("eeschema") else "failed: " + e.get("eeschema_error", ""), bool(e.get("eeschema"))),
        ("ERC/DRC via kicad-cli", "yes" if k.get("drc") else "no (needs KiCad 8+; pcbnew DRC fallback used)", bool(k.get("drc"))),
        ("Java", str(e["java"] or "missing (sudo apt install openjdk-21-jre)"), e["java"] >= 21),
        ("Docling (datasheet tables)", _docling_row(), True),
        ("OpenSCAD (enclosure STL)", __import__("shutil").which("openscad") or "optional, not installed: sudo apt install openscad (the .scad file is written either way)", True),
        ("Freerouting release", e["freerouting"] or "none compatible", bool(e["freerouting"])),
        ("Claude Code CLI", "found" if e["claude_cli"] else "not installed (Claude plan: npm i -g @anthropic-ai/claude-code; run `claude`)", True),
        ("Codex CLI", "found" if e.get("codex_cli") else "not installed (ChatGPT plan: npm i -g @openai/codex; run `codex login`)", True),
        ("ANTHROPIC_API_KEY", "set" if e["api_key"] else "not set (only for the anthropic-api backend)", True),
        ("OPENAI_API_KEY", "set" if e.get("openai_key") else "not set (only for the openai-api backend)", True),
        ("Model backend", "available" if (e["claude_cli"] or e.get("codex_cli") or e["api_key"] or e.get("openai_key"))
            else "none: install Claude Code or Codex, set an API key, or use --backend ollama", bool(e["claude_cli"] or e.get("codex_cli") or e["api_key"] or e.get("openai_key"))),
        ("gerber2ems/openEMS", "found" if e["gerber2ems"] else "not installed (openems flag prepares files only)", True),
    ]
    for name, val, ok in rows:
        print(f"  {'ok ' if ok else 'XX '} {name:<24} {val}")
    return 0 if all(ok for _, _, ok in rows) else 1


def cmd_fetch(_):
    from . import router
    print(router.jar_path())
    return 0


def cmd_serve(a):
    from .app import serve
    serve(a.host, a.port, a.open)
    return 0


def cmd_run(a):
    o = Options()
    o.prompt = sys.stdin.read() if a.prompt == "-" else (a.prompt or "")
    o.images = a.image or []
    o.datasheets = a.datasheet or []
    o.spec_file, o.sch_file, o.pcb_file = a.spec, a.sch, a.pcb
    o.backend, o.model = a.backend, a.model
    if a.sch_style:
        o.sch_style = a.sch_style
    if a.shape:
        o.shape = a.shape
    if a.local_model:
        o.local_model = a.local_model
    if a.models:
        from .llm import parse_models
        try:
            parse_models(a.models)
        except ValueError as e:
            print(f"--models: {e}", file=sys.stderr)
            return 2
        o.models = a.models
    if a.ollama_host:
        o.ollama_host = a.ollama_host
    o.layers, o.fab, o.holes = a.layers, a.fab, a.holes
    if a.size:
        w, _, h = a.size.lower().partition("x")
        o.width, o.height = float(w), float(h)
    if a.flags is not None:
        flags = {f.strip() for f in a.flags.split(",") if f.strip()}
        bad = flags - set(ALL_FLAGS)
        if bad:
            print(f"unknown flags: {', '.join(bad)}; valid: {', '.join(ALL_FLAGS)}", file=sys.stderr)
            return 2
        o.flags = flags
    o.flags |= set(a.add or [])
    o.passes, o.router_timeout = a.passes, a.timeout
    o.level, o.context = a.level, a.context or ""
    if sys.stdin.isatty() and a.prompt != "-":
        from . import preflight
        o._ask = preflight.ask_terminal
    if a.project:
        from .addons import projects
        o.project = projects.create(a.project)["slug"]
    o.video_size = a.video_size
    o.video_style = a.video_style
    o.video_turntable = a.turntable
    o.video_models = not a.no_models
    o.fresh = a.fresh
    if not (o.prompt.strip() or o.images or o.spec_file or o.sch_file):
        print("nothing to do: give -p, -i, --spec or --sch", file=sys.stderr)
        return 2
    out = Path(a.out or Path.cwd() / ("kipipe-" + time.strftime("%Y%m%d-%H%M%S")))
    res = Pipeline(o, out, log=print).run()
    print()
    for s in res["stages"]:
        mark = {"ok": "ok ", "warn": " ! ", "fail": "XX ", "skip": " - "}.get(s["status"], "?? ")
        print(f"  {mark} {s['name']:<22} {s['detail']}")
    print(f"\n{'OK' if res['ok'] else 'FAILED'} in {res['seconds']}s -> {out}")
    if res["error"]:
        print(res["error"], file=sys.stderr)
    return 0 if res["ok"] else 1


def cmd_revise(a):
    from .pipeline import Pipeline, copy_job, load_options
    src = Path(a.job).resolve()
    if not (src / "spec.json").exists():
        print(f"{src} is not a kipipe job folder (no spec.json)", file=sys.stderr)
        return 2
    req = sys.stdin.read() if a.prompt == "-" else a.prompt
    i = 1
    while Path(f"{src}-r{i}").exists():
        i += 1
    dst = copy_job(src, Path(f"{src}-r{i}"))
    o = load_options(src)
    if (src / "options.json").exists():
        o.flags = set(json.loads((src / "options.json").read_text()).get("flags", o.flags))
    if not a.replace:
        o.flags.discard("review")
    res = Pipeline(o, dst, log=print).revise(req, keep_layout=not a.replace)
    print()
    for s_ in res["stages"]:
        mark = {"ok": "ok ", "warn": " ! ", "fail": "XX ", "skip": " - "}.get(s_["status"], "?? ")
        print(f"  {mark} {s_['name']:<22} {s_['detail']}")
    print(f"\n{'OK' if res['ok'] else 'FAILED'} in {res['seconds']}s -> {dst}")
    return 0 if res["ok"] else 1


def cmd_parts(a):
    from . import partdb
    if a.action == "add":
        if not a.args:
            print("give one or more datasheet PDFs or direct links to them")
            return 2
        o = Options()
        o.backend = a.backend
        if a.models:
            from .llm import parse_models
            parse_models(a.models)
            o.models = a.models
        if a.local_model:
            o.local_model = a.local_model
        if a.ollama_host:
            o.ollama_host = a.ollama_host
        rc = 0
        for f in a.args:
            try:
                partdb.read_datasheet(f, o, log=print, mpn=a.mpn, force=a.force)
            except Exception as e:
                print(f"{f}: {e}")
                rc = 1
        return rc
    if a.action == "list":
        rows = partdb.search(a.args[0] if a.args else "", 500)
        if not rows:
            print(f"no datasheet records yet (folder: {partdb.PARTS})")
        for r in rows:
            print(f"{r['mpn']:22} {r['status'] or '':18} {(r['manufacturer'] or '')[:14]:14} {r['description'] or ''}")
        return 0
    if a.action == "show":
        for m in a.args:
            r = partdb.load_record(m)
            print(partdb.compact(r, 100000) if r else f"{m}: not found")
        return 0
    if a.action == "rm":
        rc = 0
        for m in a.args:
            ok = partdb.remove(m)
            print(f"{'removed' if ok else 'not found'}: {m}")
            rc |= not ok
        return rc
    if a.action == "export":
        out = a.args[0] if a.args else "partdb_csv"
        for f in partdb.export_csv(out):
            print(f)
        return 0
    if a.action == "reindex":
        partdb.reindex()
        print(f"index rebuilt from {len(partdb.all_records())} records")
        return 0
    return 2


def cmd_lib(a):
    from . import libs
    if a.action == "add":
        if not a.files:
            print("give one or more files: vendor zips, .kicad_sym, .lib, .kicad_mod, .step")
            return 2
        bad = 0
        for f in a.files:
            rep = libs.import_archive(f)
            print(f"{f}: {len(rep['symbols'])} symbol(s), {len(rep['footprints'])} footprint(s), {len(rep['models'])} 3D model(s)")
            for s in rep["symbols"]:
                print(f"  {libs.LIB}:{s}")
            for w in rep["warnings"]:
                print(f"  warning: {w}")
            bad += not (rep["symbols"] or rep["footprints"])
        return 1 if bad else 0
    if a.action == "list":
        ps = libs.parts()
        if not ps:
            print(f"no imported parts (library folder: {libs.LIB_DIR})")
        for p in ps:
            print(f"{p['symbol']:40} {p['footprint'] or '(no footprint)':45} {len(p['pins'])} pins")
        return 0
    if a.action == "rm":
        rc = 0
        for n in a.files:
            ok = libs.remove(n)
            print(f"{'removed' if ok else 'not found'}: {n}")
            rc |= not ok
        return rc
    return 2


def cmd_reroute(a):
    from .pipeline import Pipeline, copy_job, load_options
    src = Path(a.job).resolve()
    if not (src / "spec.json").exists():
        print(f"{src} is not a kipipe job folder (no spec.json)", file=sys.stderr)
        return 2
    moves = json.loads(Path(a.moves).read_text()) if a.moves else {}
    if a.in_place:
        dst = src
    else:
        i = 1
        while Path(f"{src}-e{i}").exists():
            i += 1
        dst = copy_job(src, Path(f"{src}-e{i}"))
    o = load_options(src)
    if a.timeout:
        o.router_timeout = a.timeout
    res = Pipeline(o, dst, log=print).reroute(moves, a.replace)
    print()
    for s_ in res["stages"]:
        mark = {"ok": "ok ", "warn": " ! ", "fail": "XX ", "skip": " - "}.get(s_["status"], "?? ")
        print(f"  {mark} {s_['name']:<22} {s_['detail']}")
    print(f"\n{'OK' if res['ok'] else 'FAILED'} in {res['seconds']}s -> {dst}")
    return 0 if res["ok"] else 1


def _job_dir(text):
    """A job folder, a job id under the data directory, or a spec.json."""
    from .app import JOBS
    p = Path(text)
    if p.exists():
        return p
    if (JOBS / text).exists():
        return JOBS / text
    print(f"no such job or file: {text}", file=sys.stderr)
    sys.exit(2)


def _print_record(rec):
    mark = {"ok": "ok ", "warn": " ! ", "fail": "XX ", "error": "ERR", "unavailable": " - "}.get(rec.get("status"), "?? ")
    print(f"  {mark} {rec.get('title', rec['id']):<30} {rec.get('summary', '')}")
    for f in rec.get("findings", []):
        print(f"        [{f['severity']}] {f['title']}")
        if f.get("detail"):
            print(f"                 {f['detail']}")
        if f.get("fix"):
            print(f"                 fix: {f['fix']}")
    for f in rec.get("files", []):
        print(f"        -> {f['path']}")


def cmd_check(a):
    from . import addons
    out = addons.run_checks(_job_dir(a.job), only_auto=not a.all)
    for rec in out["checks"]:
        if a.verbose or rec["findings"] or rec["status"] != "ok":
            _print_record(rec)
    c = out["counts"]
    print(f"\n{len(out['checks'])} checks: {c['error']} errors, {c['warning']} warnings, {c['note']} notes")
    return 1 if c["error"] else 0


def cmd_addon(a):
    from . import addons
    if a.action == "list":
        cat = addons.catalog()
        for kind, head in (("check", "Checks"), ("generate", "Generators"), ("aid", "Design aids")):
            print(head)
            for x in cat["addons"]:
                if x["kind"] == kind:
                    tag = "" if x["status"] == "ready" else f" [{x['status']}]"
                    print(f"  {x['id']:<22}{x['title']}{tag}\n  {'':<22}{x['summary']}")
        print(f"\nYour own add-ons go in {cat['plugin_dir']} (see kipipe/addons/__init__.py for the format).")
        for e in cat["plugin_errors"]:
            print("  plugin error: " + e)
        return 0
    if not a.job or not a.id:
        print("usage: kipipe addon run <job> <add-on id> [-o key=value]", file=sys.stderr)
        return 2
    opts = dict(x.split("=", 1) for x in (a.opt or []) if "=" in x)
    d = _job_dir(a.job)
    ad_ = addons.load().get(a.id)
    if ad_ and "model" in ad_.needs:
        from .pipeline import load_options
        opts["_options"] = load_options(d)
    rec = addons.run(d, a.id, opts, log=print)
    _print_record(rec)
    if rec.get("data", {}).get("answer"):
        print("\n" + rec["data"]["answer"])
    return 0 if rec["status"] in ("ok", "warn") else 1


def cmd_block(a):
    from .addons import blocks
    lib = blocks.library()
    if a.action == "list":
        for b in lib.values():
            print(f"  {b['id']:<20}{b.get('title', '')}  ({b.get('_source')})\n  {'':<20}{b.get('summary', '')}")
        print(f"\nYour own blocks go in {blocks.USER} (copy a built-in one from {blocks.BUILTIN} as a template).")
        return 0
    if a.action == "check":
        bad = 0
        for b in lib.values():
            probs = blocks.check(b)
            bad += bool(probs)
            print(f"  {'XX ' if probs else 'ok '} {b['id']}" + "".join(f"\n        {p}" for p in probs))
        return 1 if bad else 0
    b = lib.get(a.name or "")
    if not b:
        print(f"no block called {a.name}; try: kipipe block list", file=sys.stderr)
        return 2
    print(json.dumps(blocks.public(b), indent=1, ensure_ascii=False))
    return 0


def cmd_project(a):
    from .addons import projects
    if a.action == "new":
        if not a.name:
            print("usage: kipipe project new <name> [context text]", file=sys.stderr)
            return 2
        p = projects.create(a.name, a.text or "")
        print(f"project {p['name']} ({p['slug']}); use it with: kipipe run --project {p['slug']} ...")
        return 0
    if a.action == "rm":
        print("removed" if projects.delete(a.name or "") else "no such project")
        return 0
    if a.action == "show":
        p = projects.load(a.name or "")
        print(json.dumps(p, indent=1, ensure_ascii=False) if p else "no such project")
        return 0 if p else 2
    for p in projects.all_projects():
        print(f"  {p['slug']:<24}{p['name']}: {len(p.get('boards') or {})} board(s), {len(p.get('interfaces') or {})} interface(s)")
    print("House rules: " + (projects.rules_text().replace("\n", " | ") or "(none; kipipe rules \"always use 0805 passives\")"))
    return 0


def cmd_rules(a):
    from .addons import projects
    if a.clear:
        projects.set_rules("")
    for line in a.line or []:
        projects.add_rule(line)
    print(projects.rules_text() or "(no house rules)")
    return 0


def cmd_cache(a):
    from .llm import SPEC_CACHE
    from .router import ROUTE_CACHE
    for name, d in (("designs", SPEC_CACHE), ("routings", ROUTE_CACHE)):
        files = list(d.glob("*")) if d.exists() else []
        if a.action == "clear":
            for f in files:
                f.unlink()
            print(f"cleared {len(files)} cached {name}")
        else:
            print(f"{len(files):>4} cached {name} in {d}")
    if a.action == "show":
        print("Identical requests reuse these, so reruns give the same board. `kipipe run --fresh` skips them once.")
    return 0


def cmd_usage(a):
    from .addons import usage
    s = usage.summary(a.days)
    t = s["total"]
    print(f"Last {a.days} days: {t['calls']} model calls, {t['seconds'] / 60:.1f} min" + (f", ${t['cost']:.3f} reported" if t["cost"] else ""))
    for head, d in (("By step", s["by_role"]), ("By model", s["by_model"])):
        print(head)
        for k, v in sorted(d.items(), key=lambda x: -x[1]["seconds"]):
            print(f"  {k:<34}{v['calls']:>4} calls {v['seconds'] / 60:>7.1f} min" + (f"  ${v['cost']:.3f}" if v["cost"] else "")
                  + (f"  {v['in']} in / {v['out']} out tokens" if v["in"] or v["out"] else ""))
    print("Cost is only what a backend reports (Claude Code gives an API-equivalent figure; a subscription is not billed that way).")
    return 0


def cmd_config(a):
    from .addons import config
    if a.action == "set":
        if not a.key:
            print("usage: kipipe config set <key> <value>   (empty value removes it)", file=sys.stderr)
            return 2
        config.set(a.key, a.value)
    print(f"{config.FILE}")
    print(json.dumps(config.load(), indent=1) if config.load() else "  (nothing set)")
    print("Keys:")
    for k, v in config.KNOWN.items():
        print(f"  {k:<20}{v}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="kipipe", description="text/image -> KiCad schematic -> placed, routed, checked board")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="open the browser")
    s.set_defaults(fn=cmd_serve)

    r = sub.add_parser("run", help="run the pipeline from the terminal")
    r.add_argument("-p", "--prompt", help="description, or - to read stdin")
    r.add_argument("-i", "--image", action="append", help="sketch/photo/diagram (repeatable)")
    r.add_argument("-d", "--datasheet", action="append", help="datasheet PDF to read into the part database first (repeatable)")
    r.add_argument("--spec", help="design spec JSON (skips the model)")
    r.add_argument("--sch", help="existing .kicad_sch (skips the model and schematic generation)")
    r.add_argument("--pcb", help="existing .kicad_pcb: locked footprints/tracks are kept (hybrid mode)")
    r.add_argument("--backend", default="claude-code", choices=["claude-code", "anthropic-api", "codex", "openai-api", "ollama"],
                   help="claude-code: Claude plan; codex: ChatGPT plan (Codex CLI); anthropic-api / openai-api: API keys; ollama: local")
    r.add_argument("--model", help="use this one model for every step (overrides --models)")
    r.add_argument("--shape", choices=["rect", "rounded", "circle", "plus", "hexagon", "octagon"],
                   help="board outline shape (size with --size; for a circle --size 50x50)")
    r.add_argument("--local-model", help="Ollama model for 'local' steps (default qwen3.5:9b), e.g. --models igor")
    r.add_argument("--sch-style", choices=["wired", "labels"], help="schematic drawing: wired (default) or labels on every pin")
    r.add_argument("--models", help="per-step models: mixed (default), opus, sonnet, cheap, or role=model,... "
                                    "roles: spec, fix, escalate, review, inspect (or inspect_detail/_blocks/_final); "
                                    "a value of local or ollama:<tag> runs that step on Ollama; plan igor = Opus + local Qwen")
    r.add_argument("--ollama-host", default=None, help="default $KIPIPE_OLLAMA_HOST or http://localhost:11434")
    r.add_argument("--flags", help=f"replace default flags ({','.join(sorted(DEFAULT_FLAGS))}); all: {','.join(ALL_FLAGS)}")
    r.add_argument("--add", action="append", choices=list(ALL_FLAGS), help="add one flag to the defaults (repeatable)")
    r.add_argument("--layers", type=int, choices=[2, 4])
    r.add_argument("--fab", choices=list(FABS))
    r.add_argument("--size", help="board WxH in mm, e.g. 50x40 (default: fit)")
    r.add_argument("--holes", choices=["none", "M2", "M2.5", "M3"])
    r.add_argument("--video-size", default="1280x720", help="for the video flag, e.g. 1080x1920 for vertical")
    r.add_argument("--video-style", choices=["3d", "flat"], default="3d", help="build video look: 3d orbit (default) or flat top-down")
    r.add_argument("--turntable", type=int, default=36, help="KiCad 9+: real 3D renders in the closing turntable (0 = off)")
    r.add_argument("--fresh", action="store_true", help="ask the model again even if this exact request was designed before")
    r.add_argument("--no-models", action="store_true", help="3D video: draw boxes instead of the parts' STEP models")
    r.add_argument("--passes", type=int, default=30)
    r.add_argument("--level", choices=["guided", "standard", "expert"], default="standard",
                   help="guided: plain-language walkthrough and review guide; expert: no explanations (the web app also stops for review before layout). Never changes the design")
    r.add_argument("--project", help="project this board belongs to (created if new): shared context, other boards, connector interfaces")
    r.add_argument("--context", help="background for this board that is not a request")
    r.add_argument("--timeout", type=int, default=300, help="router time budget in seconds")
    r.add_argument("-o", "--out", help="output directory")
    r.set_defaults(fn=cmd_run)

    e = sub.add_parser("reroute", help="route a job's board again after moving parts (in KiCad or with a moves file)")
    e.add_argument("job", help="job folder")
    e.add_argument("--moves", help='JSON {"U1": {"x": 12.5, "y": 8, "rot": 90}, ...} in board mm from the top-left corner')
    e.add_argument("--replace", action="store_true", help="re-place every unlocked part around the moved/locked ones")
    e.add_argument("--in-place", action="store_true", help="modify the job folder instead of making a -eN copy")
    e.add_argument("--timeout", type=int, help="router time budget in seconds")
    e.set_defaults(fn=cmd_reroute)

    v = sub.add_parser("revise", help="change a finished design from a description; unchanged parts stay put")
    v.add_argument("job", help="job folder")
    v.add_argument("-p", "--prompt", required=True, help="what to change, or - to read stdin")
    v.add_argument("--replace", action="store_true", help="place every part again instead of keeping unchanged ones")
    v.set_defaults(fn=cmd_revise)

    lb = sub.add_parser("lib", help="import parts you downloaded (SnapEDA, Ultra Librarian, SamacSys, DigiKey: KiCad format)")
    lb.add_argument("action", choices=["add", "list", "rm"])
    lb.add_argument("files", nargs="*", help="add: zips or library files; rm: symbol names")
    lb.set_defaults(fn=cmd_lib)

    pd = sub.add_parser("parts", help="part database: datasheets read once, facts reused (add / list / show / rm / export / reindex)")
    pd.add_argument("action", choices=["add", "list", "show", "rm", "export", "reindex"])
    pd.add_argument("args", nargs="*", help="add: PDFs; list: search text; show/rm: part numbers; export: folder")
    pd.add_argument("--mpn", help="add: the part number to file it under (default: what the datasheet says)")
    pd.add_argument("--force", action="store_true", help="add: read again even if this PDF was read before")
    pd.add_argument("--backend", default="claude-code", choices=["claude-code", "anthropic-api", "codex", "openai-api", "ollama"])
    pd.add_argument("--models", help="e.g. igor (local first read), or extract=haiku,extract_check=haiku,extract_resolve=sonnet")
    pd.add_argument("--local-model", help="Ollama model for local reads (default qwen3.5:9b)")
    pd.add_argument("--ollama-host", default=None)
    pd.set_defaults(fn=cmd_parts)

    ck = sub.add_parser("check", help="run the design checks on a job folder, a job id or a spec.json (no model, no KiCad run)")
    ck.add_argument("job")
    ck.add_argument("--all", action="store_true", help="include checks that are not part of the automatic stage")
    ck.add_argument("-v", "--verbose", action="store_true", help="also list checks that found nothing")
    ck.set_defaults(fn=cmd_check)

    ad = sub.add_parser("addon", help="add-ons: checks, generators (BOM, enclosure, pin map, ...) and design aids (list / run)")
    ad.add_argument("action", choices=["list", "run"])
    ad.add_argument("job", nargs="?")
    ad.add_argument("id", nargs="?")
    ad.add_argument("-o", "--opt", action="append", help="option as key=value (repeatable)")
    ad.set_defaults(fn=cmd_addon)

    bl = sub.add_parser("block", help="verified building blocks (list / show <id> / check)")
    bl.add_argument("action", choices=["list", "show", "check"])
    bl.add_argument("name", nargs="?")
    bl.set_defaults(fn=cmd_block)

    pj = sub.add_parser("project", help="projects: shared context, other boards and connector interfaces (list / new / show / rm)")
    pj.add_argument("action", choices=["list", "new", "show", "rm"], nargs="?", default="list")
    pj.add_argument("name", nargs="?")
    pj.add_argument("text", nargs="?", help="context text for a new project")
    pj.set_defaults(fn=cmd_project)

    ru = sub.add_parser("rules", help="house rules applied to every design (show, or add lines)")
    ru.add_argument("line", nargs="*")
    ru.add_argument("--clear", action="store_true")
    ru.set_defaults(fn=cmd_rules)

    ca = sub.add_parser("cache", help="cached designs and routings that make reruns identical (show / clear)")
    ca.add_argument("action", choices=["show", "clear"], nargs="?", default="show")
    ca.set_defaults(fn=cmd_cache)

    us = sub.add_parser("usage", help="model calls, time and reported cost by step and model")
    us.add_argument("--days", type=int, default=30)
    us.set_defaults(fn=cmd_usage)

    cf = sub.add_parser("config", help="add-on settings: notifications, budget, skipped checks (show / set <key> <value>)")
    cf.add_argument("action", choices=["show", "set"], nargs="?", default="show")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(fn=cmd_config)

    sub.add_parser("doctor", help="check dependencies").set_defaults(fn=cmd_doctor)
    sub.add_parser("fetch-router", help="download the Freerouting jar for your Java").set_defaults(fn=cmd_fetch)
    a = p.parse_args(argv)
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
