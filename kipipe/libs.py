"""Your own parts: import vendor library downloads (SnapEDA/SnapMagic, Ultra Librarian, SamacSys / Mouser
Library Loader, DigiKey) into one kipipe library that the model, the validator and KiCad all see.

Everything lands in ~/.local/share/kipipe/libs:
  kipipe_parts.kicad_sym      symbols (legacy .lib files are converted with kicad-cli)
  kipipe_parts.pretty/        footprints (legacy .mod converted too)
  kipipe_parts.3dshapes/      STEP / WRL models, linked from the footprints by absolute path
Symbols get their Footprint field pointed at the imported footprint when the archive makes the pairing clear.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import kicad_lib as kl
from . import legacy as legacy_conv
from . import sexpr
from .router import DATA
from .sexpr import Q, find_all

LIB = "kipipe_parts"
LIB_DIR = DATA / "libs"
SYM = LIB_DIR / f"{LIB}.kicad_sym"
FP = LIB_DIR / f"{LIB}.pretty"
MODELS = LIB_DIR / f"{LIB}.3dshapes"
MODEL_EXT = (".step", ".stp", ".wrl")


def _cli(*args):
    env = kl.detect()
    args = [env.to_sandbox(a) if isinstance(a, Path) else str(a) for a in args]
    return subprocess.run([*env.cli_cmd, *args], capture_output=True, text=True, env=kl.clean_env(), timeout=300)


# symbol library format each KiCad writes; a library newer than the running KiCad won't load
SYM_LIB_VERSION = {7: "20220914", 8: "20231120"}


def _lib_version() -> str:
    return SYM_LIB_VERSION.get(kl.detect().major, "20241209")


def _load_sym_lib(path: Path) -> list:
    if path.exists():
        tree = sexpr.parse(path.read_text(encoding="utf-8", errors="replace"))
    else:
        tree = ["kicad_symbol_lib", ["version", _lib_version()], ["generator", "kipipe"]]
    for x in tree:
        if isinstance(x, list) and x and x[0] == "version":
            x[1] = _lib_version()
    return tree


def _downgrade(node):
    """KiCad 7 can't read KiCad 8+ symbol syntax: (hide yes) -> hide, drop newer-only fields."""
    if kl.detect().major >= 8 or not isinstance(node, list):
        return node
    out = []
    for x in node:
        if isinstance(x, list) and x:
            if x[0] in ("exclude_from_sim", "embedded_fonts", "embedded_files"):
                continue
            if x[0] == "hide" and len(x) == 2:
                if str(x[1]) == "yes":
                    out.append("hide")
                continue
            if x[0] in ("pin_numbers", "pin_names"):
                x = [e for e in x if not (isinstance(e, list) and e[:1] == ["hide"] and str(e[1]) == "no")]
                x = ["hide" if isinstance(e, list) and e[:2] == ["hide", "yes"] else e for e in x]
            out.append(_downgrade(x))
        else:
            out.append(x)
    return out


def _symbols(tree) -> list:
    return [s for s in tree[1:] if isinstance(s, list) and s and s[0] == "symbol"]


def _prop(sym, key):
    for p in find_all(sym, "property"):
        if str(p[1]) == key:
            return p
    return None


def _fp_name_from_prop(val: str) -> str:
    return val.split(":", 1)[-1].strip()


def import_archive(path, log=print) -> dict:
    """Import a zip (or a single .kicad_sym / .lib / .kicad_mod / .step). Returns a report."""
    path = Path(path)
    rep = {"symbols": [], "footprints": [], "models": [], "warnings": []}
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    FP.mkdir(exist_ok=True)
    MODELS.mkdir(exist_ok=True)
    # temp dir under the data folder, not /tmp: a flatpak KiCad has its own private /tmp
    with tempfile.TemporaryDirectory(dir=LIB_DIR, prefix=".import-") as tmp:
        tmp = Path(tmp)
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as z:
                for m in z.infolist():
                    # stay inside the temp dir whatever the archive says
                    target = (tmp / m.filename).resolve()
                    if not str(target).startswith(str(tmp.resolve())) or m.is_dir():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(m) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        else:
            shutil.copy(path, tmp / path.name)
        files = [p for p in tmp.rglob("*") if p.is_file()]

        # 3D models first, so footprints can be linked to them
        models = {}
        for p in files:
            if p.suffix.lower() in MODEL_EXT:
                dst = MODELS / p.name
                shutil.copy(p, dst)
                models[p.stem.lower()] = dst
                rep["models"].append(p.name)

        # footprints: .kicad_mod directly; legacy .mod libraries through kicad-cli fp upgrade
        fps = [p for p in files if p.suffix == ".kicad_mod"]
        for legacy in [p for p in files if p.suffix.lower() == ".mod"]:
            out = tmp / (legacy.stem + "_upgraded.pretty")
            r = _cli("fp", "upgrade", "--force", "-o", out, legacy)
            if not (out.exists() and list(out.glob("*.kicad_mod"))):
                # older kicad-cli only reads new-format libraries: use the built-in converter
                try:
                    out.mkdir(exist_ok=True)
                    for tree in legacy_conv.convert_mod(legacy.read_text(encoding="utf-8", errors="replace")):
                        (out / (re.sub(r"[^\w.+-]", "_", str(tree[1])) + ".kicad_mod")).write_text(sexpr.dumps(tree) + "\n")
                except Exception as e:
                    rep["warnings"].append(f"could not convert legacy footprint {legacy.name}: {e}")
            fps += list(out.glob("*.kicad_mod"))
            if not list(out.glob("*.kicad_mod")):
                rep["warnings"].append(f"no footprints found in {legacy.name}")
        imported_fps = {}
        for p in fps:
            tree = sexpr.parse(p.read_text(encoding="utf-8", errors="replace"))
            name = str(tree[1]) if len(tree) > 1 else p.stem
            name = re.sub(r'^.*:', "", name)
            tree[1] = Q(name)
            _link_model(tree, name, models, rep)
            (FP / f"{name}.kicad_mod").write_text(sexpr.dumps(tree) + "\n")
            imported_fps[name.lower()] = name
            rep["footprints"].append(name)

        # symbols: .kicad_sym directly; legacy .lib through kicad-cli sym upgrade
        syms_files = [p for p in files if p.suffix == ".kicad_sym"]
        for legacy in [p for p in files if p.suffix.lower() == ".lib" and not p.name.endswith("-cache.lib")]:
            out = tmp / (legacy.stem + "_upgraded.kicad_sym")
            r = _cli("sym", "upgrade", "--force", "-o", out, legacy)
            if not out.exists():
                try:
                    tree = legacy_conv.convert_lib(legacy.read_text(encoding="utf-8", errors="replace"))
                    if len(tree) > 3:
                        out.write_text(sexpr.dumps(tree) + "\n")
                except Exception as e:
                    rep["warnings"].append(f"could not convert legacy symbol library {legacy.name}: {e}")
            if out.exists():
                syms_files.append(out)
            else:
                rep["warnings"].append(f"no symbols found in {legacy.name}")
        lib = _load_sym_lib(SYM)
        have = {str(s[1]): i for i, s in enumerate(lib) if isinstance(s, list) and s and s[0] == "symbol"}
        for f in syms_files:
            src = sexpr.parse(f.read_text(encoding="utf-8", errors="replace"))
            for s in _symbols(src):
                name = str(s[1])
                fp_prop = _prop(s, "Footprint")
                want = _fp_name_from_prop(str(fp_prop[2])) if fp_prop is not None else ""
                pick = imported_fps.get(want.lower()) if want else None
                if not pick and len(imported_fps) == 1:
                    pick = next(iter(imported_fps.values()))      # one footprint in the archive: it's this part's
                if pick:
                    if fp_prop is None:
                        s.append(["property", Q("Footprint"), Q(f"{LIB}:{pick}"), ["at", "0", "0", "0"],
                                  ["effects", ["font", ["size", "1.27", "1.27"]], "hide"]])
                    else:
                        fp_prop[2] = Q(f"{LIB}:{pick}")
                elif want and not (fp_prop is not None and ":" in str(fp_prop[2]) and kl.footprint_path(str(fp_prop[2]))):
                    rep["warnings"].append(f"{name}: footprint {want!r} wasn't in the archive; set it in the spec or import it")
                s = _downgrade(s)
                if name in have:
                    lib[have[name]] = s
                else:
                    have[name] = len(lib)
                    lib.append(s)
                rep["symbols"].append(name)
        if syms_files:
            SYM.write_text(sexpr.dumps(lib) + "\n")
    kl.symbol_libs.cache_clear()
    kl.footprint_libs.cache_clear()
    for fn in ("_load_lib", "_flat"):
        f = getattr(kl, fn, None)
        if f is not None and hasattr(f, "cache_clear"):
            f.cache_clear()
    if not (rep["symbols"] or rep["footprints"]):
        rep["warnings"].append("no KiCad symbols or footprints found; download the part in KiCad format (V6 or later)")
    return rep


def _link_model(tree, name, models, rep):
    """Point the footprint's 3D model at the imported file (by filename, else the only model in the archive)."""
    nodes = find_all(tree, "model")
    if nodes:
        for n in nodes:
            stem = Path(str(n[1]).replace("\\", "/")).stem.lower()
            dst = models.get(stem) or (next(iter(models.values())) if len(models) == 1 else None)
            if dst:
                n[1] = Q(_host_path(dst))
    elif models:
        dst = models.get(name.lower()) or (next(iter(models.values())) if len(models) == 1 else None)
        if dst:
            tree.append(["model", Q(_host_path(dst)), ["offset", ["xyz", "0", "0", "0"]], ["scale", ["xyz", "1", "1", "1"]],
                         ["rotate", ["xyz", "0", "0", "0"]]])
    if nodes or models:
        rep.setdefault("linked", []).append(name)


def _host_path(p) -> str:
    """Path as KiCad sees it (inside the flatpak sandbox if that's how KiCad runs)."""
    return kl.detect().to_sandbox(str(p))


def parts() -> list:
    """[{symbol, footprint, value, description, pins: [(number, name, type)]}] for every imported symbol."""
    if not SYM.exists():
        return []
    out = []
    lib = sexpr.parse(SYM.read_text(encoding="utf-8", errors="replace"))
    for s in _symbols(lib):
        name = str(s[1])
        try:
            full = kl.get_symbol(f"{LIB}:{name}")
            pins = [(p.number, p.name, p.etype) for p in kl.symbol_pins(full) if p.style in (0, 1)]
        except Exception:
            pins = []
        fp = _prop(s, "Footprint")
        desc = _prop(s, "Description") or _prop(s, "ki_description")
        val = _prop(s, "Value")
        out.append({"symbol": f"{LIB}:{name}", "footprint": str(fp[2]) if fp is not None else "",
                    "value": str(val[2]) if val is not None else name,
                    "description": str(desc[2]) if desc is not None else "", "pins": pins})
    return out


def remove(name: str) -> bool:
    """Remove an imported symbol (and its footprint if no other symbol uses it)."""
    name = name.split(":", 1)[-1]
    if not SYM.exists():
        return False
    lib = sexpr.parse(SYM.read_text(encoding="utf-8", errors="replace"))
    keep, gone = [], None
    for item in lib:
        if isinstance(item, list) and item and item[0] == "symbol" and str(item[1]) == name:
            gone = item
            continue
        keep.append(item)
    if gone is None:
        return False
    SYM.write_text(sexpr.dumps(keep) + "\n")
    fp = _prop(gone, "Footprint")
    if fp is not None and str(fp[2]).startswith(LIB + ":"):
        fpn = str(fp[2]).split(":", 1)[1]
        still = any(str(_prop(s, "Footprint")[2] if _prop(s, "Footprint") is not None else "") == f"{LIB}:{fpn}"
                    for s in _symbols(keep))
        if not still:
            (FP / f"{fpn}.kicad_mod").unlink(missing_ok=True)
    kl.symbol_libs.cache_clear()
    kl.footprint_libs.cache_clear()
    return True


def prompt_hint(limit: int = 40) -> str:
    """Imported parts for the model's instructions, with pins, so it needs no lookups for them."""
    ps = parts()[:limit]
    if not ps:
        return ""
    lines = ["Parts the user imported (exact ids; prefer them when they fit the design):"]
    for p in ps:
        pins = ", ".join(f"{n}={nm}" for n, nm, _ in p["pins"][:40]) + (" ..." if len(p["pins"]) > 40 else "")
        lines.append(f"- {p['symbol']} footprint {p['footprint'] or '(none: pick one)'}"
                     + (f" ({p['description'][:80]})" if p["description"] else "") + f"; pins {pins}")
    return "\n".join(lines)


def project_tables(job_dir: Path):
    """sym-lib-table / fp-lib-table in a job folder, so KiCad finds imported parts when the project is opened."""
    if not SYM.exists() and not FP.exists():
        return
    env = kl.detect()
    sym = env.to_sandbox(str(SYM)) if hasattr(env, "to_sandbox") else str(SYM)
    fp = env.to_sandbox(str(FP)) if hasattr(env, "to_sandbox") else str(FP)
    (job_dir / "sym-lib-table").write_text(
        f'(sym_lib_table\n  (version 7)\n  (lib (name "{LIB}")(type "KiCad")(uri "{sym}")(options "")(descr "kipipe imported parts"))\n)\n')
    (job_dir / "fp-lib-table").write_text(
        f'(fp_lib_table\n  (version 7)\n  (lib (name "{LIB}")(type "KiCad")(uri "{fp}")(options "")(descr "kipipe imported parts"))\n)\n')
