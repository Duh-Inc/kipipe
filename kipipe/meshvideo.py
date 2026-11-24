"""Build video with the parts' real 3D models (STEP files from the footprints).

Each footprint's (model ...) entry in the finished .kicad_pcb is resolved to a
STEP file in the KiCad 3D library, loaded with trimesh (via cascadio) and cached
as a .glb. Placement frames move those models around exactly as the placer did;
routing adds copper tracks; the video ends with a full orbit. Rendered offscreen
with pyrender (EGL on a GPU, or Mesa in software).

Optional: needs `pip install pyrender cascadio shapely` (and PyOpenGL >= 3.1.7).
Anything missing -> video.make falls back to the box renderer. A part whose
model can't be found is drawn as a coloured box.

Coordinates: placer frames are "canvas" mm (y down). The 3D world is x right,
y up, z up, in canvas mm: world = (cx, -cy, z). Board file positions are
canvas + metrics.board.origin.
"""
from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import sexpr
from .video import (BG, F_CU, B_CU, FPS, PAD, Renderer, _body_color, _encode, _heights_colors)

CACHE = Path(os.environ.get("KIPIPE_CACHE", Path.home() / ".cache/kipipe")) / "meshes"
BOARD_RGB = (0.08, 0.30, 0.17)
SLAB = 1.6


class Unavailable(Exception):
    pass


def _gl():
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    try:
        import pyrender  # noqa: F401
        import trimesh  # noqa: F401
    except Exception as e:
        raise Unavailable(f"pyrender/trimesh not installed ({e})")
    import pyrender
    import trimesh
    return pyrender, trimesh


# ------------------------------------------------------------ model lookup

def model_dirs(env=None) -> list[Path]:
    out = []
    for k, v in os.environ.items():
        if k == "KIPIPE_MODEL_DIR" or re.fullmatch(r"KICAD\d+_3DMODEL_DIR", k):
            out.append(Path(v))
    for fp in getattr(env, "footprint_dirs", []) or []:
        out.append(Path(fp).parent / "3dmodels")
    for root in (Path.home() / ".local/share/flatpak", Path("/var/lib/flatpak")):
        out += [Path(p) for p in glob.glob(str(root / "runtime/org.kicad.KiCad.*/*/*/active/files/share/kicad/3dmodels"))]
        out += [Path(p) for p in glob.glob(str(root / "app/org.kicad.KiCad/current/active/files/share/kicad/3dmodels"))]
    out += [Path("/usr/share/kicad/3dmodels"), Path("/usr/local/share/kicad/3dmodels")]
    major = getattr(env, "major", 0)
    if major:
        out.append(Path.home() / f".local/share/kicad/{major}.0/3rdparty/3dmodels")
    return [p for p in dict.fromkeys(out) if p.exists()]


def resolve(path: str, dirs: list[Path], project: Path) -> Path | None:
    """${KICADx_3DMODEL_DIR}/Lib.3dshapes/Name.wrl -> an existing .step/.stp."""
    rel = None
    m = re.match(r"\$\{([^}]+)\}/?(.*)", path)
    if m:
        var, rest = m.groups()
        if var == "KIPRJMOD":
            bases = [project]
        else:
            bases = ([Path(os.environ[var])] if os.environ.get(var) else []) + dirs
        rel = rest
    else:
        p = Path(path)
        bases = [Path("/")] if p.is_absolute() else [project] + dirs
        rel = str(p).lstrip("/") if p.is_absolute() else str(p)
    stem = re.sub(r"\.(wrl|step|stp|STEP|STP|WRL)$", "", rel)
    for b in bases:
        for ext in (".step", ".stp", ".STEP", ".STP"):
            c = b / (stem + ext)
            if c.exists():
                return c
    return None


def footprints(pcb: Path) -> dict:
    """ref -> {at:(x,y,rot), back:bool, models:[{path, offset, scale, rotate}]}"""
    tree = sexpr.parse(pcb.read_text())
    out = {}
    for fp in sexpr.find_all(tree, "footprint"):
        ref = None
        for prop in sexpr.find_all(fp, "property"):
            if len(prop) > 2 and prop[1] == "Reference":
                ref = prop[2]
        for t in sexpr.find_all(fp, "fp_text"):
            if len(t) > 2 and t[1] == "reference":
                ref = ref or t[2]
        if not ref:
            continue
        at = sexpr.find(fp, "at") or ["at", 0, 0]
        layer = sexpr.find(fp, "layer")
        models = []
        for m in sexpr.find_all(fp, "model"):
            if any(isinstance(x, list) and x and x[0] == "hide" for x in m) or "hide" in m[2:]:
                continue

            def xyz(name, default):
                n = sexpr.find(m, name)
                v = sexpr.find(n, "xyz") if n else None
                return tuple(float(a) for a in v[1:4]) if v else default
            models.append({"path": m[1], "offset": xyz("offset", (0, 0, 0)), "scale": xyz("scale", (1, 1, 1)),
                           "rotate": xyz("rotate", (0, 0, 0))})
        out[ref] = {"at": (float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0),
                    "back": bool(layer and str(layer[1]).startswith("B.")), "models": models}
    return out


def _R(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    M = np.eye(4)
    i, j = {"x": (1, 2), "y": (2, 0), "z": (0, 1)}[axis]
    M[i, i], M[i, j], M[j, i], M[j, j] = c, -s, s, c
    return M


def _T(x, y, z):
    M = np.eye(4)
    M[:3, 3] = (x, y, z)
    return M


def load_step(path: Path, trimesh):
    """STEP -> trimesh.Scene in mm, cached as .glb (STEP parsing is slow)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{path}:{path.stat().st_mtime}".encode()).hexdigest()[:16]
    glb = CACHE / f"{path.stem}-{key}.glb"
    if glb.exists():
        return trimesh.load(glb, force="scene")
    sc = trimesh.load(path, force="scene")          # cascadio gives metres
    sc = sc.scaled(1000.0)
    try:
        sc.export(glb)
    except Exception:
        pass
    return sc


def local_mesh(ref, info, dirs, project, trimesh, height, color):
    """All models of one footprint, in footprint-local 3D coords (pin 1 frame,
    y up, z up). Falls back to a box when nothing resolves."""
    parts = []
    for m in info["models"]:
        p = resolve(m["path"], dirs, project)
        if not p:
            continue
        try:
            sc = load_step(p, trimesh)
        except Exception:
            continue
        rx, ry, rz = m["rotate"]
        sx, sy, sz = m["scale"]
        ox, oy, oz = m["offset"]
        M = _T(ox, oy, oz) @ _R("z", -rz) @ _R("y", -ry) @ _R("x", -rx) @ np.diag([sx, sy, sz, 1.0])
        sc = sc.copy()
        sc.apply_transform(M)
        parts.append(sc)
    return parts


# ------------------------------------------------------------ renderer

def _look_at(eye, target, up=(0, 0, 1)):
    eye, target, up = map(np.asarray, (eye, target, up))
    f = target - eye
    f = f / np.linalg.norm(f)
    r = np.cross(f, up)
    r = r / np.linalg.norm(r)
    u = np.cross(r, f)
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = r, u, -f, eye
    return M


class MeshScene:
    def __init__(self, pl, data, size):
        pyrender, trimesh = _gl()
        self.pr, self.tm = pyrender, trimesh
        pcb = pl.path(".kicad_pcb")
        if not pcb.exists():
            raise Unavailable("no board file")
        self.size = size
        self.data = data
        self.outline = data.get("outline")
        self.fps = footprints(pcb)
        dirs = model_dirs(getattr(pl, "env", None))
        hs, cs = _heights_colors(pl, data["geo"].keys())
        self.scene = pyrender.Scene(bg_color=np.array([*BG, 255]) / 255, ambient_light=np.array([0.35, 0.35, 0.35]))
        self.nodes, self.found, self.missing = {}, 0, []
        for ref in data["geo"]:
            info = self.fps.get(ref, {"models": [], "back": False})
            pieces = local_mesh(ref, info, dirs, pcb.parent, trimesh, hs.get(ref, 1.5), cs.get(ref))
            meshes = []
            for sc in pieces:
                for name, g in sc.geometry.items():
                    for node in sc.graph.geometry_nodes.get(name, []):
                        T, _ = sc.graph[node]
                        meshes.append((pyrender.Mesh.from_trimesh(g, smooth=False), T))
            if pieces:
                self.found += 1
            else:
                if info["models"]:
                    self.missing.append(ref)
                b = data["geo"][ref]["0"]["box"]
                w, h = (b[2] - b[0]) * 0.8, (b[3] - b[1]) * 0.8
                hh = hs.get(ref, 1.5)
                box = trimesh.creation.box((w, h, hh))
                box.apply_translation(((b[0] + b[2]) / 2, -(b[1] + b[3]) / 2, hh / 2))
                col = np.array([*cs.get(ref, (50, 50, 54)), 255]) / 255
                mat = pyrender.MetallicRoughnessMaterial(baseColorFactor=col, roughnessFactor=0.7)
                meshes.append((pyrender.Mesh.from_trimesh(box, material=mat), np.eye(4)))
            # pads under the part (local, rotation 0)
            pads = data["geo"][ref]["0"]["pads"]
            if pads:
                pm = []
                for _, dx, dy in pads:
                    q = trimesh.creation.box((0.9, 0.9, 0.04))
                    q.apply_translation((dx, -dy, 0.02))
                    pm.append(q)
                pad = trimesh.util.concatenate(pm)
                mat = pyrender.MetallicRoughnessMaterial(baseColorFactor=np.array([*PAD, 255]) / 255, metallicFactor=0.6,
                                                         roughnessFactor=0.4)
                meshes.append((pyrender.Mesh.from_trimesh(pad, material=mat), np.eye(4)))
            parent = pyrender.Node(name=ref, matrix=np.eye(4))
            self.scene.add_node(parent)
            for mesh, T in meshes:
                self.scene.add(mesh, pose=T, parent_node=parent)
            self.nodes[ref] = parent
        self.board_node, self.board_key = None, None
        self.track_nodes, self.n_tracks = [], 0
        self.cam = self.scene.add(pyrender.PerspectiveCamera(yfov=math.radians(36), aspectRatio=size[0] / size[1]))
        self.light = self.scene.add(pyrender.DirectionalLight(intensity=3.2))
        self.r = pyrender.OffscreenRenderer(size[0], size[1])
        self.hdr = Renderer(data, size, title=pl.name)

    def set_board(self, fr, solid):
        ox, oy = fr["off"]
        w, h = fr["board"]
        key = (round(ox, 2), round(oy, 2), w, h, solid)
        if key == self.board_key:
            return
        tm, pr = self.tm, self.pr
        if self.outline:
            poly = [(x - ox, -(y - oy)) for x, y in self.outline]
        else:
            poly = [(-ox, oy), (w - ox, oy), (w - ox, -(h - oy)), (-ox, -(h - oy))]
        try:
            from shapely.geometry import Polygon
            mesh = tm.creation.extrude_polygon(Polygon(poly), SLAB if solid else 0.05)
        except Exception:                        # no shapely: bounding slab
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            mesh = tm.creation.box((max(xs) - min(xs), max(ys) - min(ys), SLAB))
            mesh.apply_translation(((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, SLAB / 2))
        mesh.apply_translation((0, 0, -(SLAB if solid else 0.05)))
        col = (*BOARD_RGB, 1.0) if solid else (0.16, 0.17, 0.18, 1.0)
        mat = pr.MetallicRoughnessMaterial(baseColorFactor=col, roughnessFactor=0.8)
        if self.board_node:
            self.scene.remove_node(self.board_node)
        self.board_node = self.scene.add(pr.Mesh.from_trimesh(mesh, material=mat))
        self.board_key = key

    def place(self, fr, drop=None):
        ox, oy = fr["off"]
        for ref, node in self.nodes.items():
            x, y, rot, placed = fr["parts"].get(ref, (0, 0, 0, False))
            if not placed:
                self.scene.set_pose(node, _T(0, 0, -1000))
                continue
            z = drop if (drop is not None and ref == fr.get("active")) else 0.0
            back = self.fps.get(ref, {}).get("back")
            M = _T(x - ox, -(y - oy), (-SLAB if back else 0.0) + z) @ _R("z", rot)
            if back:
                M = M @ _R("y", 180)
            self.scene.set_pose(node, M)

    def add_tracks(self, tracks, upto, V):
        tm, pr = self.tm, self.pr
        new = tracks[self.n_tracks:upto]
        if not new:
            return
        for layer, col, z in (("F.Cu", F_CU, 0.02), ("B.Cu", B_CU, -SLAB - 0.02)):
            segs = []
            for t in new:
                if t["layer"] != layer:
                    continue
                (ax, ay), (bx, by) = V(*t["a"]), V(*t["b"])
                ay, by = -ay, -by
                L = math.hypot(bx - ax, by - ay)
                if L < 1e-6:
                    continue
                s = tm.creation.box((L + t["w"], t["w"], 0.035))
                s.apply_transform(_T((ax + bx) / 2, (ay + by) / 2, z) @ _R("z", math.degrees(math.atan2(by - ay, bx - ax))))
                segs.append(s)
            if segs:
                mat = pr.MetallicRoughnessMaterial(baseColorFactor=np.array([*col, 255]) / 255, metallicFactor=0.3,
                                                   roughnessFactor=0.5)
                self.track_nodes.append(self.scene.add(pr.Mesh.from_trimesh(tm.util.concatenate(segs), material=mat)))
        self.n_tracks = upto

    def shot(self, bounds, az, el, header):
        x0, y0, x1, y1 = bounds
        cx, cy = (x0 + x1) / 2, -(y0 + y1) / 2
        span = math.hypot(x1 - x0, y1 - y0)
        d = span / 2 / math.tan(math.radians(36) / 2) * 1.05
        a, e = math.radians(az - 90), math.radians(el)
        eye = (cx + d * math.cos(e) * math.cos(a), cy + d * math.cos(e) * math.sin(a), d * math.sin(e))
        pose = _look_at(eye, (cx, cy, 0))
        self.scene.set_pose(self.cam, pose)
        self.scene.set_pose(self.light, pose)
        col, _ = self.r.render(self.scene)
        img = Image.fromarray(col[:, :, :3])
        d_ = ImageDraw.Draw(img)
        d_.rectangle((0, 0, self.size[0], self.hdr.top - 4), fill=BG)
        self.hdr.header(d_, *header)
        return img


def make(pl) -> str | None:
    """Raises Unavailable when it can't run; video.make then uses the box renderer."""
    out = pl.dir / "out"
    data = json.loads((out / "frames.json").read_text())
    gpath = out / "geometry.json"
    geo = json.loads(gpath.read_text()) if gpath.exists() else {"tracks": [], "vias": []}
    size = tuple(int(v) for v in getattr(pl.o, "video_size", "1280x720").lower().split("x"))
    frames = data["frames"]
    S = MeshScene(pl, data, size)
    if not S.found:
        raise Unavailable("no 3D models found for any part (set KIPIPE_MODEL_DIR to your KiCad 3dmodels folder)")
    pl.log(f"  video: real 3D models for {S.found}/{len(S.nodes)} parts"
           + (f"; missing: {', '.join(S.missing[:8])}" if S.missing else ""))

    keep = []
    refine = [i for i, f in enumerate(frames) if f["label"] == "refine"]
    step = max(1, len(refine) // 90)
    for i, f in enumerate(frames):
        if f["label"] == "refine" and (refine.index(i) % step):
            continue
        keep.append(f)
    split = next((i for i, f in enumerate(keep) if f["label"] == "outline"), None)
    before = keep[:split] if split is not None else keep
    after = keep[split:] if split is not None else []
    final = keep[-1]
    fox, foy = final["off"]
    fw, fh = final["board"]
    boundsB = (-fox, -foy, fw - fox, fh - foy)
    boundsA = S.hdr.extent(before) if before and split is not None else boundsB
    tracks, vias = geo.get("tracks", []), geo.get("vias", [])
    total = len(before) * 3 + 24 + len(after) * 2 + 20 + (min(150, len(tracks)) + 30 if tracks else 0) + 150
    EL = 42.0

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="kipipe_video_", dir=out))
    n = 0

    def az():
        return -30.0 + 60.0 * min(1.0, n / total)

    def emit(img, times=1):
        nonlocal n
        for _ in range(times):
            img.save(tmp / f"{n:05d}.png", compress_level=1)
            n += 1

    def stage(fr):
        lab = fr["label"]
        st = "annealing" if lab.startswith("refine") else "connectors" if lab.startswith("edge") else "placement"
        k = sum(1 for v in fr["parts"].values() if v[3])
        return st, f"{lab}  ·  {k}/{len(fr['parts'])} parts  ·  weighted wirelength {fr['hpwl']} mm"

    lerp = lambda A, B, t: tuple(a + (b - a) * (t * t * (3 - 2 * t)) for a, b in zip(A, B))   # noqa: E731
    solid0 = split is None
    for f in before:
        S.set_board(f, solid0)
        drops = [9.0 * (1 - (k + 1) / 5) ** 2 for k in range(5)] if f["label"].startswith(("place", "edge")) and f.get("active") else [None]
        for z in drops:
            S.place(f, z)
            emit(S.shot(boundsA, az(), EL, (*stage(f), n / total)))
    if split is not None:
        f = keep[split]
        S.set_board(f, True)
        S.place(f)
        for i in range(24):
            emit(S.shot(lerp(boundsA, boundsB, i / 23), az(), EL, (*stage(f), n / total)))
        for f in after:
            S.set_board(f, True)
            S.place(f)
            emit(S.shot(boundsB, az(), EL, (*stage(f), n / total)), 2 if f["label"].startswith("edge") else 1)
    S.set_board(final, True)
    S.place(final)
    for _ in range(20):
        emit(S.shot(boundsB, az(), EL, (*stage(final), n / total)))

    origin = pl.metrics.get("board", {}).get("origin", [0, 0])
    rt = pl.metrics.get("routing", {})
    stats = f"{rt.get('vias', len(vias))} vias  ·  {rt.get('unrouted', '?')} unrouted"
    V = lambda x, y: (x - origin[0] - fox, y - origin[1] - foy)      # noqa: E731
    if tracks:
        steps = min(150, len(tracks))
        for i in range(1, steps + 1):
            k = math.ceil(len(tracks) * i / steps)
            S.add_tracks(tracks, k, V)
            emit(S.shot(boundsB, az(), EL, ("routing", f"{k}/{len(tracks)} segments  ·  {stats}", n / total)))
        for _ in range(30):
            emit(S.shot(boundsB, az(), EL, ("routing", f"{len(tracks)}/{len(tracks)} segments  ·  {stats}", n / total)))
    drc = pl.metrics.get("drc", {})
    bw = pl.metrics.get("board", {})
    done = f"{bw.get('width', fw):.0f} x {bw.get('height', fh):.0f} mm  ·  {stats}  ·  DRC {drc.get('errors', '?')} errors"
    a0 = az()
    for i in range(150):                                   # one full orbit, tilting a little
        emit(S.shot(boundsB, a0 + 360.0 * i / 150, EL + 10 * math.sin(math.pi * i / 150), ("done", done, 1.0)))
    S.r.delete()
    return _encode(pl, tmp, out, n)
