"""KiCad installation detection and symbol/footprint library access."""
from __future__ import annotations

import copy
import difflib
import functools
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import sexpr
from .sexpr import Q, find, find_all


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

# extra symbol libraries registered at run time (part database previews), name -> path
EXTRA_SYM_LIBS: dict = {}

# parts imported with `kipipe lib add` (see libs.py)
USER_LIBS = Path(os.environ.get("KIPIPE_HOME", Path.home() / ".local/share/kipipe")) / "libs"

FLATPAK_ID = os.environ.get("KIPIPE_FLATPAK_ID", "org.kicad.KiCad")

# Variables an AppImage (e.g. a terminal emulator) sets for its children. KiCad
# reads APPDIR to decide it is itself an AppImage and then looks for its kiface
# libraries inside that mount, and flatpak passes the variable straight through.
_LEAKY = {"APPDIR", "APPIMAGE", "ARGV0", "OWD", "APPRUN_STARTUP_EXEC", "APPRUN_EXEC_ARGS",
          "LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "QT_PLUGIN_PATH", "GDK_PIXBUF_MODULE_FILE",
          "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR"}


def clean_env(extra: dict | None = None) -> dict:
    """os.environ minus anything an AppImage or other bundle injected."""
    env = {k: v for k, v in os.environ.items()
           if k not in _LEAKY and "/tmp/.mount_" not in v}
    if "/tmp/.mount_" in os.environ.get("PATH", ""):
        env["PATH"] = ":".join(p for p in os.environ["PATH"].split(":") if "/tmp/.mount_" not in p)
    if extra:
        env.update(extra)
    return env


@dataclass
class KiCadEnv:
    cli_cmd: list            # argv prefix that runs kicad-cli
    version: str
    major: int
    py_base: list            # argv prefix that runs a python with `pcbnew` (before PYTHONPATH handling)
    symbol_dirs: list
    footprint_dirs: list
    config_dir: Path
    flatpak: str | None = None
    path_map: list = None    # [(sandbox_prefix, host_prefix)] for flatpak

    @property
    def cli(self) -> str:
        return " ".join(self.cli_cmd)

    def python_cmd(self, pythonpath: str) -> tuple[list, dict]:
        """argv and extra env to run python with pcbnew and kipipe importable."""
        if self.flatpak:
            return (["flatpak", "run", "--command=python3", *[f"--unset-env={v}" for v in ("APPDIR", "APPIMAGE", "LD_LIBRARY_PATH")],
                     f"--env=PYTHONPATH={pythonpath}",
                     "--env=PYTHONDONTWRITEBYTECODE=1", self.flatpak], {})
        return (list(self.py_base), {"PYTHONPATH": pythonpath})

    def to_sandbox(self, host_path) -> str:
        hp = str(host_path)
        for sbx, host in (self.path_map or []):
            if hp == host or hp.startswith(host.rstrip("/") + "/"):
                return sbx + hp[len(host):]
        return hp

    def to_host(self, sbx_path) -> str:
        sp = str(sbx_path)
        for sbx, host in (self.path_map or []):
            if sp == sbx or sp.startswith(sbx.rstrip("/") + "/"):
                return host + sp[len(sbx):]
        return sp

    @property
    def has_drc(self) -> bool:
        return self.major >= 8

    @property
    def has_erc(self) -> bool:
        return self.major >= 8

    @property
    def sch_version(self) -> str:
        # File format version written for generated schematics. KiCad opens
        # files from older versions, so 10 gets the KiCad 9 format.
        return {7: "20230121", 8: "20231120"}.get(self.major, "20250114")

    @property
    def new_hide_syntax(self) -> bool:
        return self.major >= 9


def _flatpak_installed(app: str) -> bool:
    if not shutil.which("flatpak"):
        return False
    return subprocess.run(["flatpak", "info", app], capture_output=True, env=clean_env()).returncode == 0


def _flatpak_paths(app: str, major: int):
    """Ask the sandbox where its libraries are, then find those directories on
    the host. Flatpak mounts the app at /app and app extensions (KiCad ships
    the libraries as one) at /app/<dir>; on the host they live under the
    installation's app/ and runtime/ trees."""
    q = subprocess.run(["flatpak", "run", "--command=sh", app, "-c",
                        "env | grep '^KICAD' ; echo ---; cat /.flatpak-info"],
                       capture_output=True, text=True, timeout=120, env=clean_env())
    env_part, _, info = q.stdout.partition("---")
    kv = dict(l.split("=", 1) for l in env_part.splitlines() if "=" in l)
    app_path = re.search(r"^app-path=(.+)$", info, re.M)
    loc = app_path.group(1).strip() if app_path else ""
    if not loc:
        loc = subprocess.run(["flatpak", "info", "--show-location", app], capture_output=True, text=True, env=clean_env()).stdout.strip()
        loc = loc + "/files" if loc and not loc.endswith("/files") else loc
    inst = loc.split("/app/" + app)[0] if "/app/" + app in loc else ""
    ext_roots = []
    for root in {inst, str(Path.home() / ".local/share/flatpak"), "/var/lib/flatpak"}:
        if root:
            for d in Path(root, "runtime").glob(app + ".*/*/*/active/files"):
                ext_roots.append(str(d))

    path_map: list = []

    def translate(sp: str):
        if not sp:
            return None
        if Path(sp).exists() and not sp.startswith("/app"):
            return Path(sp)
        if not sp.startswith("/app/"):
            return None
        rel = sp[len("/app/"):]
        cands = []
        if loc:
            cands.append(("/app", loc))
        parts = rel.split("/")
        if len(parts) >= 2:
            mount = "/app/" + "/".join(parts[:2])          # e.g. /app/extensions/Library
            for r in ext_roots:
                cands.append((mount, r))
            mount1 = "/app/" + parts[0]                      # e.g. /app/library
            for r in ext_roots:
                cands.append((mount1, r))
        for sbx, host in cands:
            hp = host + sp[len(sbx):]
            if Path(hp).exists():
                if (sbx, host) not in path_map:
                    path_map.append((sbx, host))
                return Path(hp)
        return None

    syms, fps = [], []
    for maj in dict.fromkeys((major, 10, 9, 8, 7)):
        for key, lst in ((f"KICAD{maj}_SYMBOL_DIR", syms), (f"KICAD{maj}_FOOTPRINT_DIR", fps)):
            hp = translate(kv.get(key, ""))
            if hp:
                lst.append(hp)
    if not syms or not fps:
        for guess in ("/app/share/kicad/symbols", "/app/extensions/Library/share/kicad/symbols"):
            hp = translate(guess)
            if hp:
                syms.append(hp)
        for guess in ("/app/share/kicad/footprints", "/app/extensions/Library/share/kicad/footprints"):
            hp = translate(guess)
            if hp:
                fps.append(hp)
    path_map.sort(key=lambda t: -len(t[0]))      # most specific first
    return syms, fps, path_map, kv


@functools.lru_cache(maxsize=1)
def detect(cli: str | None = None, python: str | None = None) -> KiCadEnv:
    cli = cli or os.environ.get("KIPIPE_KICAD_CLI") or shutil.which("kicad-cli")
    flatpak = None
    path_map = []
    if cli:
        cli_cmd = [cli]
    elif _flatpak_installed(FLATPAK_ID):
        flatpak = FLATPAK_ID
        cli_cmd = ["flatpak", "run", "--command=kicad-cli", "--unset-env=APPDIR", "--unset-env=APPIMAGE",
                   "--unset-env=LD_LIBRARY_PATH", FLATPAK_ID]
    else:
        raise RuntimeError("KiCad not found: no kicad-cli on PATH and no org.kicad.KiCad flatpak "
                           "(set KIPIPE_KICAD_CLI or KIPIPE_FLATPAK_ID if it is somewhere else).")
    out = subprocess.run(cli_cmd + ["version"], capture_output=True, text=True, timeout=120, env=clean_env()).stdout.strip()
    m = re.search(r"(\d+)\.(\d+)", out)
    major = int(m.group(1)) if m else 0

    sym_dirs, fp_dirs = [], []
    for var, lst in (("KIPIPE_SYMBOL_DIR", sym_dirs), ("KIPIPE_FOOTPRINT_DIR", fp_dirs)):
        if os.environ.get(var):
            lst.append(Path(os.environ[var]))
    if flatpak:
        s, f, path_map, _ = _flatpak_paths(flatpak, major)
        sym_dirs += s
        fp_dirs += f
        py_base = []
        cfg = Path.home() / ".var/app" / flatpak / "config/kicad" / f"{major}.0"
    else:
        for maj in (major, 10, 9, 8, 7):
            for var, lst in ((f"KICAD{maj}_SYMBOL_DIR", sym_dirs), (f"KICAD{maj}_FOOTPRINT_DIR", fp_dirs)):
                if os.environ.get(var):
                    lst.append(Path(os.environ[var]))
        for base in ("/usr/share/kicad", "/usr/local/share/kicad"):
            sym_dirs.append(Path(base) / "symbols")
            fp_dirs.append(Path(base) / "footprints")
        py_base = [python or os.environ.get("KIPIPE_KICAD_PYTHON") or "/usr/bin/python3"]
        cfg = Path.home() / ".config" / "kicad" / f"{major}.0"
    sym_dirs = [p for p in dict.fromkeys(sym_dirs) if p.exists()]
    fp_dirs = [p for p in dict.fromkeys(fp_dirs) if p.exists()]
    if not sym_dirs or not fp_dirs:
        hint = (f"flatpak run --command=sh {flatpak} -c 'env | grep KICAD'" if flatpak else "check your KiCad install")
        raise RuntimeError("could not locate KiCad symbol/footprint libraries; set KIPIPE_SYMBOL_DIR and "
                           f"KIPIPE_FOOTPRINT_DIR to their host paths ({hint})")
    return KiCadEnv(cli_cmd, out, major, py_base, sym_dirs, fp_dirs, cfg, flatpak, path_map)


def _expand(uri: str, env: KiCadEnv, kind: str) -> str:
    def rep(m):
        var = m.group(1)
        if os.environ.get(var):
            return os.environ[var]
        if var.endswith("_SYMBOL_DIR") and env.symbol_dirs:
            return str(env.symbol_dirs[0])
        if var.endswith("_FOOTPRINT_DIR") and env.footprint_dirs:
            return str(env.footprint_dirs[0])
        if var.endswith("3RD_PARTY"):
            return str(Path.home() / ".local/share/kicad" / f"{env.major}.0" / "3rdparty")
        return m.group(0)
    return re.sub(r"\$\{([^}]+)\}", rep, uri)


def _read_lib_table(path: Path, env: KiCadEnv, kind: str) -> dict[str, Path]:
    libs: dict[str, Path] = {}
    try:
        tree = sexpr.parse(path.read_text())
    except Exception:
        return libs
    for lib in find_all(tree, "lib"):
        name = find(lib, "name")
        uri = find(lib, "uri")
        if name and uri:
            p = Path(env.to_host(_expand(str(uri[1]), env, kind)))
            if p.exists():
                libs[str(name[1])] = p
    return libs


@functools.lru_cache(maxsize=1)
def symbol_libs() -> dict[str, Path]:
    env = detect()
    libs: dict[str, Path] = {}
    for d in env.symbol_dirs:
        for p in sorted(d.iterdir()):
            if p.suffix in (".kicad_sym", ".kicad_symdir"):
                libs.setdefault(p.stem, p)
    table = env.config_dir / "sym-lib-table"
    if table.exists():
        libs.update(_read_lib_table(table, env, "sym"))
    mine = USER_LIBS / "kipipe_parts.kicad_sym"
    if mine.exists():
        libs["kipipe_parts"] = mine
    libs.update(EXTRA_SYM_LIBS)
    return libs


@functools.lru_cache(maxsize=1)
def footprint_libs() -> dict[str, Path]:
    env = detect()
    libs: dict[str, Path] = {}
    for d in env.footprint_dirs:
        for p in sorted(d.iterdir()):
            if p.suffix == ".pretty":
                libs.setdefault(p.stem, p)
    table = env.config_dir / "fp-lib-table"
    if table.exists():
        libs.update(_read_lib_table(table, env, "fp"))
    mine = USER_LIBS / "kipipe_parts.pretty"
    if mine.is_dir():
        libs["kipipe_parts"] = mine
    return libs


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

@dataclass
class Pin:
    number: str
    name: str
    etype: str      # power_in, passive, input, ...
    x: float        # connection point, symbol coordinates (y up)
    y: float
    angle: float
    length: float
    unit: int       # 0 = common to all units
    style: int
    hidden: bool
    raw: str = ""   # the number as written in the library when it is a stacked pin, e.g. "[1,15,38,39]"


def expand_pin_number(num: str) -> list[str]:
    """KiCad 10 writes several pads that share one schematic pin as "[1,15,38,39]" or "[A1-A4,B2]".
    Returns the individual pad numbers; a plain number comes back as itself."""
    s = str(num).strip()
    if not (s.startswith("[") and s.endswith("]")):
        return [s]
    out = []
    for part in s[1:-1].split(","):
        part = part.strip()
        m = re.fullmatch(r"([A-Za-z]*)(\d+)\s*-\s*([A-Za-z]*)(\d+)", part)
        if m and m.group(1) == m.group(3) and int(m.group(4)) >= int(m.group(2)):
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(4)) + 1)]
        elif part:
            out.append(part)
    return out or [s]


@functools.lru_cache(maxsize=None)
def _load_lib(lib: str) -> dict[str, list]:
    path = symbol_libs().get(lib)
    if path is None:
        raise KeyError(lib)
    files = sorted(path.glob("*.kicad_sym")) if path.is_dir() else [path]
    syms: dict[str, list] = {}
    for f in files:
        tree = sexpr.parse(f.read_text(encoding="utf-8"))
        for s in find_all(tree, "symbol"):
            syms[str(s[1])] = s
    return syms


def list_symbols(lib: str) -> list[str]:
    try:
        return list(_load_lib(lib).keys())
    except KeyError:
        return []


def _rename_units(node: list, old: str, new: str) -> None:
    for sub in find_all(node, "symbol"):
        nm = str(sub[1])
        if nm.startswith(old + "_"):
            sub[1] = Q(new + nm[len(old):])


@functools.lru_cache(maxsize=None)
def _flat(lib: str, name: str) -> list:
    syms = _load_lib(lib)
    if name not in syms:
        raise KeyError(f"{lib}:{name}")
    node = syms[name]
    ext = find(node, "extends")
    if not ext:
        return node
    parent = copy.deepcopy(_flat(lib, str(ext[1])))
    parent[1] = Q(name)
    _rename_units(parent, str(ext[1]), name)
    child_props = {str(p[1]): p for p in find_all(node, "property")}
    for i, x in enumerate(parent):
        if isinstance(x, list) and x and x[0] == "property" and str(x[1]) in child_props:
            parent[i] = copy.deepcopy(child_props.pop(str(x[1])))
    insert_at = max(i for i, x in enumerate(parent) if isinstance(x, list) and x and x[0] == "property") + 1
    for p in child_props.values():
        parent.insert(insert_at, copy.deepcopy(p))
        insert_at += 1
    return parent


def get_symbol(lib_id: str) -> list:
    lib, _, name = lib_id.partition(":")
    return copy.deepcopy(_flat(lib, name))


def symbol_pins(sym: list) -> list[Pin]:
    name = str(sym[1])
    pins: list[Pin] = []
    for sub in find_all(sym, "symbol"):
        m = re.match(re.escape(name) + r"_(\d+)_(\d+)$", str(sub[1]))
        unit, style = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        for p in find_all(sub, "pin"):
            at = find(p, "at")
            ln = find(p, "length")
            nm = find(p, "name")
            num = find(p, "number")
            hidden = "hide" in p or any(isinstance(x, list) and x[:2] == ["hide", "yes"] for x in p)
            raw = str(num[1])
            members = expand_pin_number(raw)
            for k, n in enumerate(members):       # one Pin per pad, all at the same spot, like the older stacked-pin style
                pins.append(Pin(n, str(nm[1]), str(p[1]), float(at[1]), float(at[2]),
                                float(at[3]) if len(at) > 3 else 0.0, float(ln[1]) if ln else 2.54,
                                unit, style, hidden or (k > 0 and len(members) > 1), raw if len(members) > 1 else ""))
    return pins


def symbol_units(sym: list) -> list[int]:
    units = sorted({p.unit for p in symbol_pins(sym) if p.unit > 0})
    return units or [1]


def is_power_symbol(sym: list) -> bool:
    return find(sym, "power") is not None


def symbol_property(sym: list, key: str) -> str:
    for p in find_all(sym, "property"):
        if str(p[1]) == key:
            return str(p[2])
    return ""


def _rank(name: str, names: list[str], n: int) -> list[str]:
    """Prefix/substring matches first (PinHeader_1x02 -> PinHeader_1x02_P2.54mm_Vertical), then fuzzy."""
    low = name.lower()
    pre = sorted((c for c in names if c.lower().startswith(low)), key=len)
    sub = sorted((c for c in names if low in c.lower() and c not in pre), key=len)
    fuzzy = difflib.get_close_matches(name, names, n, 0.5)
    return list(dict.fromkeys(pre + sub + fuzzy))[:n]


def suggest_symbols(lib_id: str, n: int = 5) -> list[str]:
    lib, _, name = lib_id.partition(":")
    libs = symbol_libs()
    out = []
    if lib in libs:
        out = [f"{lib}:{s}" for s in _rank(name, list_symbols(lib), n)]
    if len(out) < n:
        # search a few likely libraries by name
        for cand_lib in difflib.get_close_matches(lib, list(libs), 3, 0.4):
            out += [f"{cand_lib}:{s}" for s in difflib.get_close_matches(name, list_symbols(cand_lib), 2, 0.6)]
    return list(dict.fromkeys(out))[:n]


# ---------------------------------------------------------------------------
# Footprints
# ---------------------------------------------------------------------------

def footprint_path(fp_id: str) -> Path | None:
    lib, _, name = fp_id.partition(":")
    d = footprint_libs().get(lib)
    if not d:
        return None
    p = d / f"{name}.kicad_mod"
    return p if p.exists() else None


@functools.lru_cache(maxsize=None)
def footprint_pads(fp_id: str) -> set[str]:
    p = footprint_path(fp_id)
    if not p:
        return set()
    tree = sexpr.parse(p.read_text(encoding="utf-8"))
    return {str(x[1]) for x in find_all(tree, "pad") if str(x[1]) != ""}


@functools.lru_cache(maxsize=None)
def footprint_pad_rects(fp_id: str) -> list[tuple]:
    """[(number, x0, y0, x1, y1)] copper pad rectangles in footprint coordinates (pad rotation applied)."""
    p = footprint_path(fp_id)
    if not p:
        return []
    out = []
    for x in find_all(sexpr.parse(p.read_text(encoding="utf-8")), "pad"):
        num = str(x[1])
        at, size = find(x, "at"), find(x, "size")
        if not num or at is None or size is None or "np_thru_hole" in [str(v) for v in x[:4]]:
            continue
        cx, cy = float(at[1]), float(at[2])
        ang = float(at[3]) if len(at) > 3 else 0.0
        w, h = float(size[1]), float(size[2])
        if round(ang) % 180 == 90:
            w, h = h, w
        elif round(ang) % 90:                     # odd angle: use the bounding square
            w = h = max(w, h)
        out.append((num, cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
    return out


def rect_gap(a, b) -> float:
    """Edge-to-edge distance between two (x0, y0, x1, y1) rectangles (0 if they touch)."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


def suggest_footprints(fp_id: str, n: int = 5) -> list[str]:
    lib, _, name = fp_id.partition(":")
    libs = footprint_libs()
    out = []
    for cand in ([lib] if lib in libs else []) + difflib.get_close_matches(lib, list(libs), 2, 0.4):
        names = [p.stem for p in libs[cand].glob("*.kicad_mod")]
        out += [f"{cand}:{s}" for s in _rank(name, names, n)]
    return list(dict.fromkeys(out))[:n]
