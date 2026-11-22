"""Local web UI. `kipipe serve` then open http://127.0.0.1:8765"""
from __future__ import annotations

import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

from . import kicad_lib as kl
from . import router
from .pipeline import ALL_FLAGS, DEFAULT_FLAGS, FABS, Options, Pipeline, copy_job, load_options

DATA = router.DATA
JOBS = DATA / "jobs"
STATIC = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)
_jobs: dict[str, dict] = {}
_run_lock = threading.Lock()      # one pipeline at a time; KiCad/Freerouting are heavy


def environment() -> dict:
    info = {}
    try:
        env = kl.detect()
        info["kicad"] = {"version": env.version, "major": env.major, "erc": env.has_erc, "drc": env.has_drc}
        from .pipeline import PKG_ROOT
        argv, extra = env.python_cmd(str(PKG_ROOT))
        # also proves the sandbox (if any) can see kipipe's own files
        probe = ("import pcbnew, json, sys\n"
                 "from kipipe import pcbgen\n"
                 "libs = json.loads(sys.argv[1])\n"
                 "fp = pcbnew.FootprintLoad(pcbgen.fp_lib_path('Resistor_SMD', libs), 'R_0805_2012Metric')\n"
                 "assert fp is not None, 'FootprintLoad returned None'\n"
                 "print(pcbnew.GetBuildVersion())")
        libs = {k: env.to_sandbox(v) for k, v in kl.footprint_libs().items() if k == "Resistor_SMD"}
        pc = subprocess.run([*argv, "-c", probe, json.dumps(libs)],
                            capture_output=True, text=True, env=kl.clean_env(extra), timeout=120)
        info["pcbnew"] = pc.stdout.strip().splitlines()[-1] if pc.stdout.strip() else None
        # load the schematic engine too: the pcbnew import alone doesn't exercise it
        import tempfile
        with tempfile.TemporaryDirectory(dir=Path.home()) as td:
            sch = Path(td) / "probe.kicad_sch"
            sch.write_text(f'(kicad_sch (version {env.sch_version}) (generator "kipipe") (paper "A4"))\n')
            pr = subprocess.run([*env.cli_cmd, "sch", "export", "netlist", "-o", str(Path(td) / "p.net"), str(sch)],
                                capture_output=True, text=True, env=kl.clean_env(), timeout=120)
            info["eeschema"] = (Path(td) / "p.net").exists()
            if not info["eeschema"]:
                info["eeschema_error"] = (pr.stderr or pr.stdout).strip()[-300:]
        if not info["pcbnew"]:
            info["pcbnew_error"] = (pc.stderr or "").strip()[-900:]
        info["kicad"]["flatpak"] = env.flatpak
        info["kicad"]["symbols"] = str(env.symbol_dirs[0])
    except Exception as e:
        info["kicad"] = {"error": str(e)}
        info["pcbnew"] = None
    jv = router.java_version()
    info["java"] = jv
    info["freerouting"] = router.pick_release(jv)
    info["claude_cli"] = bool(shutil.which("claude"))
    info["api_key"] = bool(os.environ.get("ANTHROPIC_API_KEY"))
    info["codex_cli"] = bool(shutil.which("codex"))
    info["openai_key"] = bool(os.environ.get("OPENAI_API_KEY"))
    info["gerber2ems"] = bool(shutil.which("gerber2ems"))
    info["ollama_host"] = os.environ.get("KIPIPE_OLLAMA_HOST", "http://localhost:11434")
    info["flags"] = ALL_FLAGS
    info["default_flags"] = sorted(DEFAULT_FLAGS)
    info["fabs"] = list(FABS)
    return info


@app.get("/")
def index():
    r = send_from_directory(STATIC, "index.html", max_age=0)
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/static/<path:p>")
def static_files(p):
    # the app's own code must never come from a stale browser cache after an update;
    # vendored libraries and fonts never change, so those can be cached
    r = send_from_directory(STATIC, p, max_age=0 if not p.startswith("vendor/") else 86400)
    if not p.startswith("vendor/"):
        r.headers["Cache-Control"] = "no-cache"
    return r


@app.get("/api/env")
def api_env():
    return jsonify(environment())


@app.post("/api/run")
def api_run():
    job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    d = JOBS / job_id
    (d / "input").mkdir(parents=True)
    f = request.form
    opts = Options()
    opts.prompt = f.get("prompt", "")
    try:
        _model_opts(f, opts)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    opts.sch_style = f.get("sch_style") if f.get("sch_style") in ("wired", "labels") else "wired"
    opts.layers = int(f["layers"]) if f.get("layers") else None
    opts.fab = f.get("fab") or None
    opts.width = float(f["width"]) if f.get("width") else None
    opts.height = float(f["height"]) if f.get("height") else None
    opts.holes = f.get("holes") or None
    opts.shape = f.get("shape") or None
    opts.flags = set(json.loads(f.get("flags", "[]"))) & set(ALL_FLAGS)
    opts.passes = int(f.get("passes") or 30)
    opts.video_size = f.get("video_size") or "1280x720"
    opts.video_style = f.get("video_style") or "3d"
    opts.fresh = f.get("fresh") in ("1", "on", "true")
    opts.router_timeout = int(f.get("timeout") or 300)
    for link in re.split(r"[\s,]+", f.get("datasheet_urls") or ""):
        if re.match(r"https?://", link, re.I) and link not in opts.datasheets:
            opts.datasheets.append(link)
    opts.level = f.get("level") if f.get("level") in ("guided", "standard", "expert") else "standard"
    opts._gate = True                       # the web app can show a Continue button; the terminal cannot
    opts.context = (f.get("context") or "").strip()
    if (f.get("project") or "").strip():
        from .addons import projects
        opts.project = projects.create(f.get("project").strip())["slug"]
    for i, img in enumerate(request.files.getlist("images")):
        if img.filename:
            p = d / "input" / f"upload{i}{Path(img.filename).suffix.lower()}"
            img.save(p)
            opts.images.append(str(p))
    for up in request.files.getlist("files"):
        if not up.filename:
            continue
        name = Path(up.filename).name
        p = d / "input" / name
        up.save(p)
        if name.endswith(".kicad_sch"):
            opts.sch_file = str(p)
        elif name.endswith(".kicad_pcb"):
            opts.pcb_file = str(p)
        elif name.endswith(".json"):
            opts.spec_file = str(p)
        elif name.lower().endswith(".pdf"):
            opts.datasheets.append(str(p))
    if not (opts.prompt.strip() or opts.images or opts.sch_file or opts.spec_file):
        shutil.rmtree(d)
        return jsonify({"error": "give a description, an image, a spec.json or a .kicad_sch"}), 400

    _start(job_id, d, opts, lambda pl: pl.run(), opts.prompt[:200])
    return jsonify({"id": job_id})


def _model_opts(f, opts):
    """Backend and model fields shared by design runs and datasheet reads."""
    opts.backend = f.get("backend", "claude-code")
    opts.model = f.get("model") or None
    opts.models = f.get("models") or "mixed"
    opts.local_model = (f.get("local_model") or "").strip() or "qwen3.5:9b"
    from .llm import parse_models
    parse_models(opts.models)
    opts.api_key = f.get("api_key") or None
    opts.ollama_host = f.get("ollama_host") or environment()["ollama_host"]


# ---- part database ----
_ds_tasks: dict = {}


@app.get("/api/parts")
def api_parts():
    from . import partdb
    q = request.args.get("q", "")
    return jsonify({"parts": partdb.search(q, 200),
                    "tasks": [{k: v for k, v in t.items() if k != "thread"} for t in _ds_tasks.values()]})


@app.get("/api/parts/<path:mpn>")
def api_part(mpn):
    from . import partdb
    r = partdb.load_record(mpn)
    return (jsonify(r), 200) if r else (jsonify({"error": "not found"}), 404)


@app.delete("/api/parts/<path:mpn>")
def api_part_rm(mpn):
    from . import partdb
    return jsonify({"removed": partdb.remove(mpn)})


@app.post("/api/parts")
def api_parts_add():
    """Datasheet PDF upload (field 'file', optional 'mpn' and model fields). Read in the background."""
    from . import partdb
    up = request.files.get("file")
    link = (request.form.get("url") or "").strip()
    if link and not partdb.is_url(link):
        return jsonify({"error": "that is not an http(s) link"}), 400
    if not link and (not up or not up.filename.lower().endswith(".pdf")):
        return jsonify({"error": "upload a datasheet PDF or give a direct link to one"}), 400
    opts = Options()
    try:
        _model_opts(request.form, opts)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    inbox = partdb.DB / ".uploads"
    inbox.mkdir(parents=True, exist_ok=True)
    if link:
        path, shown = link, link.split("?")[0].rstrip("/").split("/")[-1] or link
    else:
        path = inbox / (uuid.uuid4().hex[:6] + "-" + Path(up.filename).name)
        up.save(path)
        shown = Path(up.filename).name
    tid = uuid.uuid4().hex[:8]
    task = {"id": tid, "file": shown, "status": "reading", "log": [], "mpn": None, "started": time.time()}
    _ds_tasks[tid] = task
    mpn = (request.form.get("mpn") or "").strip() or None
    force = request.form.get("force") == "1"

    def work():
        try:
            r = partdb.read_datasheet(path, opts, log=task["log"].append, mpn=mpn, force=force)
            task.update(status="done", mpn=r["identity"]["mpn"], verification=r.get("verification"))
        except Exception as e:
            task.update(status="failed", error=str(e))
        finally:
            if not link:
                path.unlink(missing_ok=True)
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"task": tid})


@app.get("/api/part-circuit/<path:mpn>")
def api_part_circuit(mpn):
    """The stored application circuit drawn as a schematic (SVG)."""
    from . import partdb
    try:
        p = partdb.render_circuit(mpn)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not p:
        return jsonify({"error": "no application circuit stored"}), 404
    return send_file(p, mimetype="image/svg+xml", max_age=0)


@app.get("/api/part-figure/<path:mpn>")
def api_part_figure(mpn):
    """The datasheet page the application circuit was read from (PNG)."""
    from . import partdb
    p = partdb.render_figure(mpn)
    if not p:
        return jsonify({"error": "no datasheet page to show"}), 404
    return send_file(p, mimetype="image/png", max_age=0)


@app.get("/api/parts-export")
def api_parts_export():
    from . import partdb
    out = partdb.DB / "export"
    files = partdb.export_csv(out)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in files:
            z.write(f, f.name)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="kipipe_partdb_csv.zip")


def _start(job_id, d, opts, action, label=""):
    """Run a pipeline action in a worker thread, streaming log/stages/result over SSE."""
    q: queue.Queue = queue.Queue()
    job = {"id": job_id, "dir": str(d), "status": "queued", "log": [], "result": None, "q": q,
           "prompt": label, "created": time.time()}
    _jobs[job_id] = job

    def log(msg):
        job["log"].append(msg)
        q.put(("log", msg))

    def worker():
        with _run_lock:
            job["status"] = "running"
            q.put(("status", "running"))
            pl = Pipeline(opts, d, log=log)
            orig_stage = pl.stage

            def stage(name, status, detail=""):
                orig_stage(name, status, detail)
                q.put(("stage", json.dumps({"name": name, "status": status, "detail": detail})))
            pl.stage = stage
            res = action(pl)
            job["result"] = res
            job["status"] = "done" if res["ok"] else "failed"
            q.put(("result", json.dumps(res)))
            q.put(("end", job["status"]))

    threading.Thread(target=worker, daemon=True).start()


@app.post("/api/jobs/<job_id>/reroute")
def api_reroute(job_id):
    """Body: {"moves": {ref: {x, y, rot}}, "replace": bool}. Runs as a new revision of the job."""
    src = JOBS / job_id
    if not (src / "spec.json").exists() or not list(src.glob("*.kicad_pcb")):
        return jsonify({"error": "job has no board to edit"}), 400
    body = request.get_json(force=True) or {}
    base = job_id.split("-e")[0] if "-e" in job_id else job_id
    n = 1 + max([int(p.name.rsplit("-e", 1)[1]) for p in JOBS.glob(base + "-e*") if p.name.rsplit("-e", 1)[1].isdigit()] or [0])
    new_id = f"{base}-e{n}"
    d = copy_job(src, JOBS / new_id)
    opts = load_options(src)
    moves = body.get("moves") or {}
    replace = bool(body.get("replace"))
    _start(new_id, d, opts, lambda pl: pl.reroute(moves, replace), f"edit of {job_id}")
    return jsonify({"id": new_id})


@app.post("/api/jobs/<job_id>/revise")
def api_revise(job_id):
    """Body: {"request": "what to change", "keep_layout": bool}. Runs as a new revision of the job."""
    src = JOBS / job_id
    if not (src / "spec.json").exists():
        return jsonify({"error": "this job has no design to revise"}), 400
    body = request.get_json(force=True) or {}
    req = (body.get("request") or "").strip()
    if not req:
        return jsonify({"error": "describe what to change"}), 400
    base = job_id.split("-r")[0] if "-r" in job_id else job_id
    n = 1 + max([int(p.name.rsplit("-r", 1)[1]) for p in JOBS.glob(base + "-r*") if p.name.rsplit("-r", 1)[1].isdigit()] or [0])
    new_id = f"{base}-r{n}"
    d = copy_job(src, JOBS / new_id)
    opts = load_options(src)
    opts.flags = set(json.loads((src / "options.json").read_text()).get("flags", opts.flags)) if (src / "options.json").exists() else opts.flags
    keep = bool(body.get("keep_layout", True))
    if keep:
        opts.flags.discard("review")
    _start(new_id, d, opts, lambda pl: pl.revise(req, keep), f"revision of {job_id}: {req[:80]}")
    return jsonify({"id": new_id})


@app.get("/api/libs")
def api_libs():
    from . import libs
    try:
        return jsonify({"parts": libs.parts(), "dir": str(libs.LIB_DIR)})
    except Exception as e:
        return jsonify({"parts": [], "error": str(e)})


@app.post("/api/libs")
def api_libs_add():
    """Multipart 'file' (one or more): vendor zips, .kicad_sym, .lib, .kicad_mod, .step."""
    from . import libs
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "no file"}), 400
    total = {"symbols": [], "footprints": [], "models": [], "warnings": []}
    for f in files:
        name = Path(f.filename or "upload.zip").name
        if not name.lower().endswith((".zip", ".kicad_sym", ".lib", ".kicad_mod", ".mod", ".step", ".stp", ".wrl")):
            total["warnings"].append(f"{name}: not a library file (zip, kicad_sym, lib, kicad_mod, step)")
            continue
        up = DATA / "libs" / ".uploads"
        up.mkdir(parents=True, exist_ok=True)
        dst = up / (uuid.uuid4().hex[:6] + "-" + name)
        f.save(dst)
        try:
            rep = libs.import_archive(dst)
        except Exception as e:
            rep = {"warnings": [f"{name}: {e}"]}
        finally:
            dst.unlink(missing_ok=True)
        for k in total:
            total[k] += rep.get(k, [])
    return jsonify(total)


@app.delete("/api/libs/<path:name>")
def api_libs_rm(name):
    from . import libs
    return jsonify({"removed": libs.remove(name)})


# ---- add-ons: checks, generators, design aids ----
@app.get("/api/addons")
def api_addons():
    from . import addons
    return jsonify(addons.catalog())


@app.get("/api/jobs/<job_id>/addons")
def api_job_addons(job_id):
    from . import addons
    d = JOBS / job_id
    if not (d / "spec.json").exists():
        return jsonify({"addons": [], "error": "this run has no spec yet"})
    chk = d / "out/checks.json"
    return jsonify({"addons": addons.job_state(d), "plugin_errors": addons.PLUGIN_ERRORS, "plugin_dir": str(addons.PLUGIN_DIR),
                    "checks": json.loads(chk.read_text()) if chk.exists() else None})


@app.post("/api/jobs/<job_id>/addons/<path:addon_id>")
def api_run_addon(job_id, addon_id):
    """Run one add-on on a job (or every check with addon_id 'checks'). Body: its options."""
    from . import addons
    d = JOBS / job_id
    if not (d / "spec.json").exists():
        return jsonify({"error": "this run has no spec yet"}), 400
    opts = request.get_json(silent=True) or {}
    lines = []
    if addon_id == "checks":
        return jsonify(addons.run_checks(d, log=lines.append, only_auto=False))
    a = addons.load().get(addon_id)
    if a is None:
        abort(404)
    if "model" in a.needs:
        o = load_options(d)
        f = opts.pop("_model", None) or {}
        try:
            if f:
                _model_opts(f, o)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        opts["_options"] = o
    rec = addons.run(d, addon_id, opts, log=lines.append)
    rec["log"] = lines[-30:]
    return jsonify(rec)


@app.post("/api/jobs/<job_id>/continue")
def api_continue(job_id):
    """Continue a run that stopped for review before layout (expert level)."""
    d = JOBS / job_id
    try:
        res = json.loads((d / "result.json").read_text())
    except Exception:
        res = {}
    if not res.get("paused"):
        return jsonify({"error": "this run is not waiting for you"}), 400
    opts = load_options(d)
    saved = json.loads((d / "options.json").read_text())
    opts.flags = set(saved.get("flags", opts.flags))
    if res["paused"] == "preflight":
        f = request.form
        try:
            _model_opts(f, opts)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        opts.images = [p for p in saved.get("images") or [] if Path(p).exists()]
        opts._gate = True
        inp = d / "input"
        inp.mkdir(exist_ok=True)
        names, sheets, libs_ = {}, [], []
        for item in json.loads(f.get("links") or "[]"):
            url, part = (item if isinstance(item, list) else [item, ""])[:2]
            if re.match(r"https?://", str(url), re.I):
                sheets.append(url)
                if part:
                    names[url] = part
        parts_of = json.loads(f.get("file_parts") or "[]")
        for i, up in enumerate(request.files.getlist("files")):
            p = inp / Path(up.filename).name
            up.save(p)
            (sheets if p.suffix.lower() == ".pdf" else libs_).append(str(p))
            if p.suffix.lower() == ".pdf" and i < len(parts_of) and parts_of[i]:
                names[str(p)] = parts_of[i]
        answers = json.loads(f.get("answers") or "{}")
        _start(job_id, d, opts, lambda pl: pl.continue_preflight(answers, sheets, libs_, names), "continued with your answers")
        return jsonify({"id": job_id})
    f = (request.get_json(silent=True) or {}).get("_model") or {}
    try:
        if f:
            _model_opts(f, opts)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _start(job_id, d, opts, lambda pl: pl.resume(), "continued after review")
    return jsonify({"id": job_id})


@app.get("/api/projects")
def api_projects():
    from .addons import projects
    out = []
    for p in projects.all_projects():
        boards = []
        for name, jid in (p.get("boards") or {}).items():
            b = {"name": name, "job": jid, "exists": (JOBS / jid / "spec.json").exists()}
            b["build"] = projects.build_state(JOBS / jid).get("status")
            boards.append(b)
        out.append({**p, "boards": boards})
    return jsonify({"projects": out, "rules": projects.rules_text()})


@app.post("/api/projects")
def api_project_save():
    """Body: {name, context} creates or updates; {slug, delete: true} removes; {slug, drop_interface: name}."""
    from .addons import projects
    b = request.get_json(silent=True) or {}
    if b.get("delete"):
        return jsonify({"ok": projects.delete(b.get("slug", ""))})
    name = (b.get("name") or b.get("slug") or "").strip()
    if not name:
        return jsonify({"error": "give the project a name"}), 400
    p = projects.load(b.get("slug") or name) or projects.create(name)
    if "context" in b:
        p["context"] = str(b["context"]).strip()
    if b.get("drop_interface"):
        (p.get("interfaces") or {}).pop(b["drop_interface"], None)
    if b.get("drop_board"):
        (p.get("boards") or {}).pop(b["drop_board"], None)
    return jsonify(projects.save(p))


@app.post("/api/rules")
def api_rules():
    from .addons import projects
    projects.set_rules((request.get_json(silent=True) or {}).get("rules", ""))
    return jsonify({"rules": projects.rules_text()})


@app.post("/api/approaches")
def api_approaches():
    """One small model call: the different ways to build what the request asks for. Form: prompt + model fields."""
    from .addons import guide, projects
    f = request.form
    if len((f.get("prompt") or "").strip()) < 12:
        return jsonify({"error": "describe the circuit first"}), 400
    o = Options()
    try:
        _model_opts(f, o)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    o.context = (f.get("context") or "").strip()
    o.project = projects.slugify(f.get("project")) if (f.get("project") or "").strip() else None
    try:
        return jsonify(guide.approaches(f.get("prompt"), o, projects.prompt_block(o, JOBS / "_")))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"[:300]}), 500


@app.get("/api/blocks")
def api_blocks():
    from .addons import blocks
    lib = blocks.library()
    return jsonify({"blocks": [blocks.public(b) for b in lib.values()], "user_dir": str(blocks.USER)})


@app.get("/api/blocks/<bid>/check")
def api_block_check(bid):
    from .addons import blocks
    b = blocks.library().get(bid)
    if b is None:
        abort(404)
    return jsonify({"id": bid, "problems": blocks.check(b)})


@app.post("/api/brief")
def api_brief():
    from .addons import aids
    return jsonify({"questions": aids.brief((request.get_json(silent=True) or {}).get("prompt", ""))})


@app.get("/api/starters")
def api_starters():
    from .addons import aids
    return jsonify(aids.STARTERS)


@app.get("/api/usage")
def api_usage():
    from .addons import usage
    job = request.args.get("job") or None
    return jsonify(usage.summary(int(request.args.get("days") or 30), job))


@app.get("/api/jobs")
def api_jobs():
    out = []
    for d in sorted(JOBS.glob("*"), reverse=True)[:40]:
        r = d / "result.json"
        item = {"id": d.name}
        if r.exists():
            try:
                res = json.loads(r.read_text())
                item.update(name=res.get("name"), ok=res.get("ok"))
            except Exception:
                pass
        elif d.name in _jobs:
            item.update(name="(running)", ok=None)
        out.append(item)
    return jsonify(out)


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    d = JOBS / job_id
    if not d.exists():
        abort(404)
    j = _jobs.get(job_id)
    res = json.loads((d / "result.json").read_text()) if (d / "result.json").exists() else None
    log = j["log"] if j else ((d / "log.txt").read_text().splitlines() if (d / "log.txt").exists() else [])
    return jsonify({"id": job_id, "status": j["status"] if j else ("done" if res and res["ok"] else "failed"),
                    "result": res, "log": log})


@app.get("/api/jobs/<job_id>/events")
def api_events(job_id):
    j = _jobs.get(job_id)
    if not j:
        abort(404)

    def gen():
        for line in list(j["log"]):
            yield f"event: log\ndata: {json.dumps(line)}\n\n"
        if j["result"]:
            yield f"event: result\ndata: {json.dumps(j['result'])}\n\n"
            yield "event: end\ndata: \"done\"\n\n"
            return
        while True:
            try:
                kind, payload = j["q"].get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            data = payload if kind in ("stage", "result") else json.dumps(payload)
            yield f"event: {kind}\ndata: {data}\n\n"
            if kind == "end":
                return
    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/jobs/<job_id>/<path:p>")
def job_file(job_id, p):
    d = (JOBS / job_id).resolve()
    f = (d / p).resolve()
    if not str(f).startswith(str(d)) or not f.is_file():
        abort(404)
    return send_file(f)


@app.post("/api/jobs/<job_id>/open")
def api_open(job_id):
    """Open the project in KiCad on this machine (only when served locally)."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    d = JOBS / job_id
    pros = list(d.glob("*.kicad_pro"))
    if not pros:
        abort(404)
    subprocess.Popen(["xdg-open", str(pros[0])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/folder")
def api_folder(job_id):
    if request.remote_addr not in ("127.0.0.1", "::1"):
        abort(403)
    subprocess.Popen(["xdg-open", str(JOBS / job_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True})


def serve(host="127.0.0.1", port=8765, open_browser=False):
    JOBS.mkdir(parents=True, exist_ok=True)
    url = f"http://{host}:{port}"
    print(f"kipipe UI on {url}  (jobs in {JOBS})")
    if open_browser:
        threading.Timer(1.0, lambda: subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)).start()
    app.run(host=host, port=port, threaded=True, debug=False)
