"""Build-process video: placement as it happened, then a routing replay, then
the finished board.

Placement frames are recorded by the placer itself (every greedy drop, every
Nth annealing step, every connector snap). Freerouting doesn't expose its
intermediate states, so routing is a replay of the final tracks in the order
they were imported, net by net. Rendered with Pillow, encoded with ffmpeg
(H.264), or a GIF if ffmpeg is missing.

Two styles (option video_style): "3d" (default) draws the board as a slab
seen from an orbiting camera, parts as boxes with estimated heights that
drop into place; "flat" is the original top-down view. In 3D mode on
KiCad 9+ the video ends with a turntable of real KiCad renders.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (15, 16, 18)
CANVAS = (40, 42, 46)
BOARD = (22, 58, 40)
OUTLINE = (200, 200, 190)
COURT = (120, 118, 110)
ACTIVE = (255, 176, 0)
PAD = (205, 128, 64)
RAT = (90, 160, 210)
TEXT = (230, 226, 216)
DIM = (141, 138, 130)
F_CU = (224, 68, 58)
B_CU = (70, 125, 216)
GROUND = {"GND", "GNDA", "GNDD", "AGND", "DGND", "VSS", "0V"}
FPS = 30


def _font(size):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


class View:
    """mm -> pixel transform fitting a rect into the drawing area. With
    rotate=True the board is turned 90 degrees (wide board in a tall frame)."""

    def __init__(self, rect, W, H, top, rotate=False):
        x0, y0, x1, y1 = rect
        aw, ah = W * 0.92, (H - top) * 0.9
        self.rot = rotate
        bw, bh = (y1 - y0, x1 - x0) if rotate else (x1 - x0, y1 - y0)
        self.s = min(aw / max(bw, 1), ah / max(bh, 1))
        self.cx, self.cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.px, self.py = W / 2, top + (H - top) / 2

    def lerp(self, other, t):
        v = View.__new__(View)
        v.rot = self.rot
        t = t * t * (3 - 2 * t)        # smoothstep
        for k in ("s", "cx", "cy", "px", "py"):
            setattr(v, k, getattr(self, k) * (1 - t) + getattr(other, k) * t)
        return v

    def __call__(self, x, y):
        if self.rot:
            return (self.px + (y - self.cy) * self.s, self.py - (x - self.cx) * self.s)
        return (self.px + (x - self.cx) * self.s, self.py + (y - self.cy) * self.s)


def _box(p, q):
    return (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))


def _mst(points):
    """Prim's MST edges for ratsnest lines."""
    if len(points) < 2:
        return []
    inside = {0}
    edges = []
    dist = {i: (abs(points[i][0] - points[0][0]) + abs(points[i][1] - points[0][1]), 0) for i in range(1, len(points))}
    while dist:
        j = min(dist, key=lambda k: dist[k][0])
        edges.append((points[dist[j][1]], points[j]))
        inside.add(j)
        del dist[j]
        for k in dist:
            d = abs(points[k][0] - points[j][0]) + abs(points[k][1] - points[j][1])
            if d < dist[k][0]:
                dist[k] = (d, j)
    return edges


class Renderer:
    def __init__(self, frames_data, size=(1280, 720), title=""):
        self.W, self.H = size
        self.top = int(self.H * 0.11)
        self.geo = frames_data["geo"]
        self.frames = frames_data["frames"]
        self.outline = frames_data.get("outline")     # shaped boards: polygon in board mm
        self.title = title
        m = min(self.W, self.H)
        self.f_big = _font(max(16, m // 24))
        self.f_small = _font(max(11, m // 44))
        self.f_tiny = _font(max(9, m // 64))

    def _parts_canvas(self, fr):
        """Part rects/pads in canvas coordinates (undo outline shifts)."""
        ox, oy = fr["off"]
        out = []
        for ref, (x, y, rot, placed) in fr["parts"].items():
            if not placed:
                continue
            g = self.geo[ref][str(rot)]
            b = g["box"]
            cx, cy = x - ox, y - oy
            out.append((ref, (cx + b[0], cy + b[1], cx + b[2], cy + b[3]),
                        [(n, cx + dx, cy + dy) for (n, dx, dy) in g["pads"]]))
        return out

    def extent(self, frames):
        xs, ys = [], []
        for fr in frames:
            ox, oy = fr["off"]
            w, h = fr["board"]
            xs += [-ox, w - ox]
            ys += [-oy, h - oy]
            for _, r, _ in self._parts_canvas(fr):
                xs += [r[0], r[2]]
                ys += [r[1], r[3]]
        return (min(xs), min(ys), max(xs), max(ys))

    def _fit(self, d, text, font, width):
        if d.textlength(text, font=font) <= width:
            return text
        while text and d.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    def header(self, d, stage, detail, progress=None):
        d.text((24, 16), "kipipe", font=self.f_big, fill=ACTIVE)
        tw = d.textlength("kipipe  ", font=self.f_big)
        d.text((24 + tw, 16), stage, font=self.f_big, fill=TEXT)
        right = self.W - 24
        if self.title:
            t = self.title[:40]
            ttw = d.textlength(t, font=self.f_small)
            if 24 + tw + d.textlength(stage, font=self.f_big) + 30 + ttw < right:
                d.text((right - ttw, 20), t, font=self.f_small, fill=DIM)
        d.text((24, 16 + self.f_big.size + 6), self._fit(d, detail, self.f_small, right - 24), font=self.f_small, fill=DIM)
        if progress is not None:
            y = self.top - 4
            d.rectangle((0, y, self.W, y + 3), fill=(35, 36, 40))
            d.rectangle((0, y, int(self.W * progress), y + 3), fill=ACTIVE)

    def placement_frame(self, fr, view, final_outline, progress):
        img = Image.new("RGB", (self.W, self.H), BG)
        d = ImageDraw.Draw(img)
        ox, oy = fr["off"]
        w, h = fr["board"]
        a, b = view(-ox, -oy), view(w - ox, h - oy)
        if self.outline:
            pts = [view(x - ox, y - oy) for x, y in self.outline]
            d.polygon(pts, fill=BOARD if final_outline else CANVAS, outline=OUTLINE if final_outline else DIM)
        elif final_outline:
            d.rectangle(_box(a, b), fill=BOARD, outline=OUTLINE, width=2)
        else:
            d.rectangle(_box(a, b), fill=CANVAS)
            self._dashed_rect(d, a, b, DIM)
        parts = self._parts_canvas(fr)
        # ratsnest
        nets = {}
        for _, _, pads in parts:
            for n, x, y in pads:
                if n:
                    nets.setdefault(n, []).append((x, y))
        for n, pts in nets.items():
            col = (50, 80, 100) if n.lstrip("/").upper() in GROUND else RAT
            for p, q in _mst(pts):
                d.line((view(*p), view(*q)), fill=col, width=1)
        for ref, r, pads in parts:
            act = ref == fr.get("active")
            box = _box(view(r[0], r[1]), view(r[2], r[3]))
            d.rectangle(box, outline=ACTIVE if act else COURT, width=3 if act else 1)
            ps = max(2, view.s * 0.45)
            for _, x, y in pads:
                cx, cy = view(x, y)
                d.rectangle((cx - ps, cy - ps, cx + ps, cy + ps), fill=PAD)
            if box[2] - box[0] > 28:
                d.text((box[0] + 2, box[1] + 1), ref, font=self.f_tiny, fill=ACTIVE if act else TEXT)
        label = fr["label"]
        stage = ("placement" if not label.startswith("edge") else "connectors")
        if label.startswith("refine"):
            stage = "annealing"
        n_placed = len(parts)
        self.header(d, stage, f"{label}  ·  {n_placed}/{len(fr['parts'])} parts  ·  weighted wirelength {fr['hpwl']} mm", progress)
        return img

    def _dashed_rect(self, d, a, b, col, dash=8):
        (x0, y0), (x1, y1) = a, b
        for (p, q) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            L = math.hypot(q[0] - p[0], q[1] - p[1])
            n = max(1, int(L // dash))
            for i in range(0, n, 2):
                t0, t1 = i / n, min(1, (i + 1) / n)
                d.line(((p[0] + (q[0] - p[0]) * t0, p[1] + (q[1] - p[1]) * t0),
                        (p[0] + (q[0] - p[0]) * t1, p[1] + (q[1] - p[1]) * t1)), fill=col, width=1)

    def routing_frame(self, base_fr, view, tracks, vias, n_done, origin, stats, progress):
        img = self.placement_frame(dict(base_fr, active=None), view, True, progress)
        d = ImageDraw.Draw(img)
        ox, oy = origin
        foff = base_fr["off"]

        def V(x, y):            # absolute board mm -> canvas -> pixels
            return view(x - ox - foff[0], y - oy - foff[1])
        # erase ratsnest look by redrawing board-only base for routed nets is costly; draw tracks over it
        drawn_nets = set()
        for t in tracks[:n_done]:
            col = F_CU if t["layer"] == "F.Cu" else B_CU
            wpx = max(1, int(t["w"] * view.s))
            d.line((V(*t["a"]), V(*t["b"])), fill=col, width=wpx)
            r = wpx / 2
            for pt in (t["a"], t["b"]):
                cx, cy = V(*pt)
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
            drawn_nets.add(t["net"])
        for v in vias:
            if v["net"] in drawn_nets:
                cx, cy = V(v["x"], v["y"])
                r = max(2, v["d"] / 2 * view.s)
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(200, 200, 200))
                d.ellipse((cx - r / 2.2, cy - r / 2.2, cx + r / 2.2, cy + r / 2.2), fill=BG)
        # repaint header over the placement header
        d.rectangle((0, 0, self.W, self.top - 5), fill=BG)
        self.header(d, "routing", f"{min(n_done, len(tracks))}/{len(tracks)} segments  ·  {stats}", progress)
        return img


def make_flat(pl) -> str | None:
    out = pl.dir / "out"
    fpath = out / "frames.json"
    gpath = out / "geometry.json"
    if not fpath.exists():
        pl.log("  video: no placement frames recorded")
        return None
    data = json.loads(fpath.read_text())
    geo = json.loads(gpath.read_text()) if gpath.exists() else {"tracks": [], "vias": []}
    size = tuple(int(v) for v in getattr(pl.o, "video_size", "1280x720").lower().split("x"))
    R = Renderer(data, size, title=pl.name)
    frames = data["frames"]
    if not frames:
        return None

    # subsample annealing frames so placement lasts ~8 s at most
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
    viewA = View(R.extent(before), R.W, R.H, R.top) if before else None
    final = keep[-1]
    fox, foy = final["off"]
    fw, fh = final["board"]
    rotate = (fw > fh) != (R.W > R.H - R.top) and abs(fw - fh) > 0.15 * max(fw, fh)
    if before:
        viewA = View(R.extent(before), R.W, R.H, R.top, rotate)
    viewB = View((-fox, -foy, fw - fox, fh - foy), R.W, R.H, R.top, rotate)
    if split is None:
        viewA = viewB

    tmp = Path(tempfile.mkdtemp(prefix="kipipe_video_", dir=out))
    n = 0
    total_est = len(before) * 2 + 24 + len(after) * 2 + 150 + 90

    def emit(img, times=1):
        nonlocal n
        for _ in range(times):
            img.save(tmp / f"{n:05d}.png", compress_level=1)
            n += 1

    for f in before:
        hold = 5 if f["label"].startswith(("place", "edge")) else 1
        emit(R.placement_frame(f, viewA, split is None, n / total_est), hold)
    if split is not None:
        f = keep[split]
        for i in range(24):                        # zoom to the real outline
            emit(R.placement_frame(f, viewA.lerp(viewB, i / 23), True, n / total_est))
        for f in after:
            hold = 3 if f["label"].startswith("edge") else 1
            emit(R.placement_frame(f, viewB, True, n / total_est), hold)
    emit(R.placement_frame(final, viewB, True, n / total_est), 20)

    tracks = geo.get("tracks", [])
    vias = geo.get("vias", [])
    origin = pl.metrics.get("board", {}).get("origin", [0, 0])
    rt = pl.metrics.get("routing", {})
    stats = f"{rt.get('vias', len(vias))} vias  ·  {rt.get('unrouted', '?')} unrouted"
    if tracks:
        steps = min(150, len(tracks))
        for i in range(1, steps + 1):
            k = math.ceil(len(tracks) * i / steps)
            emit(R.routing_frame(final, viewB, tracks, vias, k, origin, stats, n / total_est))
        last = R.routing_frame(final, viewB, tracks, vias, len(tracks), origin, stats, 1.0)
        emit(last, 30)
    # finished board (real KiCad render, pours included)
    svg = out / "pcb_copper.svg"
    if svg.exists():
        try:
            import cairosvg
            png = tmp / "board.png"
            cairosvg.svg2png(url=str(svg), write_to=str(png), output_height=int((R.H - R.top) * 0.9))
            board = Image.open(png).convert("RGBA")
            if rotate:
                board = board.rotate(90, expand=True)
                scale = min((R.W * 0.92) / board.width, ((R.H - R.top) * 0.9) / board.height)
                board = board.resize((int(board.width * scale), int(board.height * scale)))
            frame = Image.new("RGB", (R.W, R.H), BG)
            frame.paste(board, ((R.W - board.width) // 2, R.top + (R.H - R.top - board.height) // 2), board)
            d = ImageDraw.Draw(frame)
            drc = pl.metrics.get("drc", {})
            bw = pl.metrics.get("board", {})
            R.header(d, "done", f"{bw.get('width', '?'):.0f} x {bw.get('height', '?'):.0f} mm  ·  {stats}  ·  DRC {drc.get('errors', '?')} errors", 1.0)
            prev = Image.open(tmp / f"{n - 1:05d}.png").convert("RGB")
            for i in range(15):
                emit(Image.blend(prev, frame, (i + 1) / 15))
            emit(frame, 75)
        except Exception as e:
            pl.log(f"  video: final board frame skipped ({e})")

    mp4 = out / f"{pl.name}-build.mp4"
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(tmp / "%05d.png"),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(mp4)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            pl.log("  video: ffmpeg failed: " + r.stderr[-300:])
            mp4 = None
    else:
        mp4 = out / f"{pl.name}-build.gif"
        imgs = [Image.open(p) for p in sorted(tmp.glob("[0-9]*.png"))[::2]]
        imgs[0].save(mp4, save_all=True, append_images=imgs[1:], duration=int(2000 / FPS), loop=0)
        pl.log("  video: ffmpeg not found, wrote a GIF instead (sudo apt install ffmpeg for MP4)")
    shutil.rmtree(tmp, ignore_errors=True)
    if mp4:
        pl.log(f"  video: {n} frames, {n / FPS:.1f} s -> {mp4.name}")
        return f"out/{mp4.name}"
    return None


def render_placement(frames_path, out_png, regions=None, size=(1600, 1100)):
    """Still image of the final placement for the model review: parts with refs,
    ratsnest, board edges labelled, floorplan regions outlined."""
    data = json.loads(Path(frames_path).read_text())
    R = Renderer(data, size, title="")
    R.f_tiny = R.f_small                       # readable refs
    final = data["frames"][-1]
    fox, foy = final["off"]
    fw, fh = final["board"]
    view = View((-fox - 4, -foy - 4, fw - fox + 4, fh - foy + 4), R.W, R.H, R.top)
    img = R.placement_frame(dict(final, active=None, label="placed"), view, True, None)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, R.W, R.top - 5), fill=BG)
    R.header(d, "placement review", f"board {fw:.0f} x {fh:.0f} mm  ·  top of image = top edge of board")
    for name, (x0, y0, x1, y1) in (regions or {}).items():
        a = view(x0 * fw - fox, y0 * fh - foy)
        b = view(x1 * fw - fox, y1 * fh - foy)
        R._dashed_rect(d, a, b, (120, 200, 255), dash=6)
        d.text((min(a[0], b[0]) + 4, min(a[1], b[1]) + 2), name, font=R.f_small, fill=(120, 200, 255))
    for side, (x, y) in {"TOP": (fw / 2, -2.5), "BOTTOM": (fw / 2, fh + 1.5), "LEFT": (-3.5, fh / 2), "RIGHT": (fw + 0.5, fh / 2)}.items():
        px, py = view(x - fox, y - foy)
        d.text((px - d.textlength(side, font=R.f_small) / 2, py), side, font=R.f_small, fill=DIM)
    img.save(out_png)
    return out_png



# ---------------------------------------------------------------- 3D style

SS = 2                        # supersampling factor (drawn at 2x, scaled down)
SLAB = 1.6
SIDE = (16, 40, 28)
BODY = {"U": (34, 34, 38), "Q": (34, 34, 38), "IC": (34, 34, 38), "C": (168, 134, 88), "R": (28, 28, 30),
        "L": (60, 60, 64), "D": (40, 40, 44), "LED": (220, 60, 50), "J": (210, 210, 205), "P": (210, 210, 205),
        "SW": (70, 70, 74), "BT": (180, 180, 186), "Y": (185, 185, 190), "X": (185, 185, 190), "F": (200, 190, 160),
        "H": (180, 160, 60), "TP": (200, 160, 60)}


def _shade(c, k):
    return tuple(max(0, min(255, int(v * k))) for v in c)


def _body_color(ref, fp):
    if re.search(r"LED", fp, re.I):
        return BODY["LED"]
    pre = re.match(r"[A-Za-z]+", ref)
    pre = pre.group(0).upper() if pre else ""
    for k in (pre, pre[:2], pre[:1]):
        if k in BODY:
            return BODY[k]
    return (50, 50, 54)


class Cam:
    """Perspective camera orbiting the board centre. Board mm (x right, y down,
    z up) -> pixels. az turns around the vertical axis, el is the angle above
    the horizon (90 = straight down)."""

    def __init__(self, bounds, W, H, top, az=0.0, el=52.0, zmax=8.0):
        x0, y0, x1, y1 = bounds
        self.cx, self.cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.az, self.el = math.radians(az), math.radians(el)
        self.dist = max(x1 - x0, y1 - y0, 10) * 3.4
        self.s, self.px, self.py = 1.0, 0.0, 0.0
        pts = [self.raw(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (-SLAB, zmax)]
        us, vs = [p[0] for p in pts], [p[1] for p in pts]
        aw, ah = W * 0.9, (H - top) * 0.88
        self.s = min(aw / max(max(us) - min(us), 1e-6), ah / max(max(vs) - min(vs), 1e-6))
        self.px = W / 2 - (max(us) + min(us)) / 2 * self.s
        self.py = top + (H - top) / 2 + (max(vs) + min(vs)) / 2 * self.s

    def raw(self, x, y, z):
        X, Y = x - self.cx, -(y - self.cy)
        ca, sa = math.cos(self.az), math.sin(self.az)
        xr, yr = X * ca - Y * sa, X * sa + Y * ca
        ce, se = math.cos(self.el), math.sin(self.el)
        d = self.dist + yr * ce - z * se
        f = self.dist / max(d, 1e-3)
        return xr * f, (yr * se + z * ce) * f, d

    def __call__(self, x, y, z=0.0):
        u, v, _ = self.raw(x, y, z)
        return (self.px + u * self.s, self.py - v * self.s)

    def depth(self, x, y, z=0.0):
        return self.raw(x, y, z)[2]


def _area(pts):
    return sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts))) / 2


class Renderer3D(Renderer):
    def __init__(self, frames_data, size, title="", heights=None, colors=None):
        super().__init__(frames_data, (size[0] * SS, size[1] * SS), title)
        self.out_size = size
        self.heights = heights or {}
        self.colors = colors or {}

    def cam(self, bounds, az, zmax=None):
        z = zmax if zmax is not None else max(list(self.heights.values()) + [4.0])
        return Cam(bounds, self.W, self.H, self.top, az=az, zmax=min(z, 14))

    def board_poly(self, fr):
        ox, oy = fr["off"]
        w, h = fr["board"]
        if self.outline:
            return [(x - ox, y - oy) for x, y in self.outline]
        return [(-ox, -oy), (w - ox, -oy), (w - ox, h - oy), (-ox, h - oy)]

    def slab(self, d, cam, poly, solid):
        top = [cam(x, y, 0) for x, y in poly]
        if not solid:
            d.polygon(top, fill=CANVAS)
            for i in range(len(top)):
                d.line((top[i - 1], top[i]), fill=DIM, width=SS)
            return
        flip = _area(top) > 0
        sides = []
        for i in range(len(poly)):
            (ax, ay), (bx, by) = poly[i - 1], poly[i]
            q = [cam(ax, ay, 0), cam(bx, by, 0), cam(bx, by, -SLAB), cam(ax, ay, -SLAB)]
            if (_area(q) > 0) == flip:
                continue
            sides.append((cam.depth((ax + bx) / 2, (ay + by) / 2, -SLAB / 2), q))
        for _, q in sorted(sides, key=lambda t: -t[0]):
            d.polygon(q, fill=SIDE)
        d.polygon(top, fill=BOARD, outline=OUTLINE)

    def flat_quad(self, d, cam, x, y, r, col, z=0.02):
        d.polygon([cam(x - r, y - r, z), cam(x + r, y - r, z), cam(x + r, y + r, z), cam(x - r, y + r, z)], fill=col)

    def box(self, d, cam, rect, z0, h, col, active):
        x0, y0, x1, y1 = rect
        ix, iy = min(0.25, (x1 - x0) * 0.12), min(0.25, (y1 - y0) * 0.12)
        x0, y0, x1, y1 = x0 + ix, y0 + iy, x1 - ix, y1 - iy
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        top = [cam(x, y, z0 + h) for x, y in corners]
        flip = _area(top) > 0
        light = (0.62, 0.78, 0.5, 0.7)
        for i in range(4):
            (ax, ay), (bx, by) = corners[i - 1], corners[i]
            q = [cam(ax, ay, z0 + h), cam(bx, by, z0 + h), cam(bx, by, z0), cam(ax, ay, z0)]
            if (_area(q) > 0) != flip:
                d.polygon(q, fill=_shade(col, light[i]))
        d.polygon(top, fill=col, outline=ACTIVE if active else _shade(col, 1.35) if sum(col) < 200 else _shade(col, 0.8))
        if active:
            for i in range(4):
                d.line((top[i - 1], top[i]), fill=ACTIVE, width=2 * SS)
        return top

    def scene(self, fr, cam, solid, drop=None, tracks=(), n_done=0, vias=(), V=None):
        img = Image.new("RGB", (self.W, self.H), BG)
        d = ImageDraw.Draw(img)
        self.slab(d, cam, self.board_poly(fr), solid)
        parts = self._parts_canvas(fr)
        routed = set()
        if tracks:
            for layer, dim in (("B.Cu", True), ("F.Cu", False)):
                for t in tracks[:n_done]:
                    if t["layer"] != layer:
                        continue
                    col = _shade(B_CU, 0.55) if dim else F_CU
                    a, b = V(*t["a"]), V(*t["b"])
                    wpx = max(SS, int(t["w"] * cam.s * 0.9))
                    d.line((cam(*a, 0.01), cam(*b, 0.01)), fill=col, width=wpx)
                    r = wpx / 2
                    for pt in (a, b):
                        cx, cy = cam(*pt, 0.01)
                        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)
            routed = {t["net"] for t in tracks[:n_done]}
            for v in vias:
                if v["net"] in routed:
                    x, y = V(v["x"], v["y"])
                    cx, cy = cam(x, y, 0.02)
                    r = max(SS, v["d"] / 2 * cam.s)
                    d.ellipse((cx - r, cy - r * math.sin(cam.el), cx + r, cy + r * math.sin(cam.el)), fill=(200, 200, 200))
        nets = {}
        for _, _, pads in parts:
            for n, x, y in pads:
                if n:
                    nets.setdefault(n, []).append((x, y))
        for _, _, pads in parts:
            for _, x, y in pads:
                self.flat_quad(d, cam, x, y, 0.42, PAD)
        for n, pts in nets.items():
            if n in routed or (tracks and n.lstrip("/").upper() in GROUND):
                continue
            col = (50, 80, 100) if n.lstrip("/").upper() in GROUND else RAT
            for p, q in _mst(pts):
                d.line((cam(*p, 0.05), cam(*q, 0.05)), fill=col, width=SS)
        order = []
        for ref, r, _ in parts:
            z0 = drop if (drop is not None and ref == fr.get("active")) else 0.0
            h = self.heights.get(ref, 1.5)
            order.append((cam.depth((r[0] + r[2]) / 2, (r[1] + r[3]) / 2, z0 + h / 2), ref, r, z0, h))
        for _, ref, r, z0, h in sorted(order, key=lambda t: -t[0]):
            act = ref == fr.get("active")
            top = self.box(d, cam, r, z0, h, self.colors.get(ref, (50, 50, 54)), act)
            xs, ys = [p[0] for p in top], [p[1] for p in top]
            tw = d.textlength(ref, font=self.f_tiny)
            if max(xs) - min(xs) > tw + 6 * SS:
                c = self.colors.get(ref, (50, 50, 54))
                d.text(((max(xs) + min(xs) - tw) / 2, (max(ys) + min(ys) - self.f_tiny.size) / 2), ref, font=self.f_tiny,
                       fill=ACTIVE if act else (TEXT if sum(c) < 300 else (30, 30, 30)))
        return img, d

    def placement3d(self, fr, cam, solid, progress, drop=None):
        img, d = self.scene(fr, cam, solid, drop)
        label = fr["label"]
        stage = "annealing" if label.startswith("refine") else "connectors" if label.startswith("edge") else "placement"
        parts = sum(1 for v in fr["parts"].values() if v[3])
        self.header(d, stage, f"{label}  ·  {parts}/{len(fr['parts'])} parts  ·  weighted wirelength {fr['hpwl']} mm", progress)
        return img

    def routing3d(self, fr, cam, tracks, vias, n_done, origin, stats, progress):
        ox, oy = origin
        foff = fr["off"]
        V = lambda x, y: (x - ox - foff[0], y - oy - foff[1])     # noqa: E731
        img, d = self.scene(dict(fr, active=None), cam, True, None, tracks, n_done, vias, V)
        self.header(d, "routing", f"{min(n_done, len(tracks))}/{len(tracks)} segments  ·  {stats}", progress)
        return img


def _heights_colors(pl, refs):
    from .addons.gen_mech import part_height
    comps = {c.get("ref"): c for c in (getattr(pl, "spec", {}) or {}).get("components", [])}
    hs, cs = {}, {}
    for ref in refs:
        fp = (comps.get(ref) or {}).get("footprint", "") or ""
        h = part_height(fp) if fp else 1.5
        hs[ref] = max(0.6, min(h, 14.0))
        cs[ref] = _body_color(ref, fp)
    return hs, cs


def kicad_turntable(pl, n, size, tmp):
    """Real 3D renders from kicad-cli (KiCad 9+), n views around the board.
    Returns the PNG paths in order, or [] when this KiCad can't do it."""
    if n <= 0 or getattr(getattr(pl, "env", None), "major", 0) < 9:
        return []
    pcb = pl.path(".kicad_pcb")
    from concurrent.futures import ThreadPoolExecutor
    W, H = size

    def one(i):
        a = 360.0 * i / n
        png = tmp / f"tt_{i:03d}.png"
        pl.run_cli("pcb", "render", "-o", png, "--width", W, "--height", H, "--quality", "basic",
                   "--background", "opaque", "--perspective", "--zoom", "0.9",
                   "--rotate", f"-55,0,{a:.1f}", pcb, check=False)
        return png if png.exists() and png.stat().st_size > 0 else None
    first = one(0)
    if not first:
        pl.log("  video: kicad-cli pcb render failed, ending on the drawn board instead")
        return []
    with ThreadPoolExecutor(max_workers=4) as ex:
        rest = list(ex.map(one, range(1, n)))
    out = [first] + [p for p in rest if p]
    return out if len(out) >= n * 0.8 else []


def make(pl) -> str | None:
    if getattr(pl.o, "video_style", "3d") == "flat":
        return make_flat(pl)
    if getattr(pl.o, "video_models", True):
        try:
            from . import meshvideo
            return meshvideo.make(pl)
        except Exception as e:                   # missing pyrender/models: draw boxes instead
            pl.log(f"  video: real part models unavailable ({str(e)[:160]}); drawing boxes")
    try:
        return make_3d(pl)
    except Exception as e:                       # never lose the video over the new renderer
        pl.log(f"  video: 3D renderer failed ({e}), using the flat style")
        return make_flat(pl)


def make_3d(pl) -> str | None:
    out = pl.dir / "out"
    fpath, gpath = out / "frames.json", out / "geometry.json"
    if not fpath.exists():
        pl.log("  video: no placement frames recorded")
        return None
    data = json.loads(fpath.read_text())
    geo = json.loads(gpath.read_text()) if gpath.exists() else {"tracks": [], "vias": []}
    size = tuple(int(v) for v in getattr(pl.o, "video_size", "1280x720").lower().split("x"))
    frames = data["frames"]
    if not frames:
        return None
    hs, cs = _heights_colors(pl, data["geo"].keys())
    R = Renderer3D(data, size, title=pl.name, heights=hs, colors=cs)

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
    boundsA = R.extent(before) if before and split is not None else boundsB
    tracks, vias = geo.get("tracks", []), geo.get("vias", [])
    turns = int(getattr(pl.o, "video_turntable", 36) or 0)
    total = len(before) * 3 + 24 + len(after) * 2 + 20 + (min(150, len(tracks)) + 30 if tracks else 0) + 90
    AZ0, AZ1 = -28.0, 28.0

    tmp = Path(tempfile.mkdtemp(prefix="kipipe_video_", dir=out))
    n = 0
    last = [None]

    def az():
        return AZ0 + (AZ1 - AZ0) * min(1.0, n / total)

    def emit(img, times=1):
        nonlocal n
        small = img.resize(size, Image.LANCZOS) if img.size != size else img
        last[0] = small
        for _ in range(times):
            small.save(tmp / f"{n:05d}.png", compress_level=1)
            n += 1

    lerp = lambda A, B, t: tuple(a + (b - a) * (t * t * (3 - 2 * t)) for a, b in zip(A, B))   # noqa: E731
    solid0 = split is None
    for f in before:
        if f["label"].startswith(("place", "edge")) and f.get("active"):
            for k in range(5):                                 # drop the new part in
                z = 9.0 * (1 - (k + 1) / 5) ** 2
                emit(R.placement3d(f, R.cam(boundsA, az()), solid0, n / total, drop=z))
        else:
            emit(R.placement3d(f, R.cam(boundsA, az()), solid0, n / total))
    if split is not None:
        f = keep[split]
        for i in range(24):
            emit(R.placement3d(f, R.cam(lerp(boundsA, boundsB, i / 23), az()), True, n / total))
        for f in after:
            emit(R.placement3d(f, R.cam(boundsB, az()), True, n / total), 2 if f["label"].startswith("edge") else 1)
    for _ in range(20):
        emit(R.placement3d(final, R.cam(boundsB, az()), True, n / total))

    origin = pl.metrics.get("board", {}).get("origin", [0, 0])
    rt = pl.metrics.get("routing", {})
    stats = f"{rt.get('vias', len(vias))} vias  ·  {rt.get('unrouted', '?')} unrouted"
    if tracks:
        steps = min(150, len(tracks))
        for i in range(1, steps + 1):
            k = math.ceil(len(tracks) * i / steps)
            emit(R.routing3d(final, R.cam(boundsB, az()), tracks, vias, k, origin, stats, n / total))
        for _ in range(30):
            emit(R.routing3d(final, R.cam(boundsB, az()), tracks, vias, len(tracks), origin, stats, n / total))

    drc = pl.metrics.get("drc", {})
    bw = pl.metrics.get("board", {})
    done = f"{bw.get('width', fw):.0f} x {bw.get('height', fh):.0f} mm  ·  {stats}  ·  DRC {drc.get('errors', '?')} errors"
    tt = kicad_turntable(pl, turns, (size[0], size[1] - int(size[1] * 0.11)), tmp)
    if tt:
        hdr = Renderer(data, size, title=pl.name)
        frames_tt = []
        for p in tt:
            fr_img = Image.new("RGB", size, BG)
            shot = Image.open(p).convert("RGB")
            fr_img.paste(shot, (0, size[1] - shot.height))
            hdr.header(ImageDraw.Draw(fr_img), "done", done + "  ·  KiCad render", 1.0)
            frames_tt.append(fr_img)
        prev = last[0]
        for i in range(15):
            emit(Image.blend(prev, frames_tt[0], (i + 1) / 15))
        hold = max(1, round(150 / len(frames_tt)))           # one turn in about 5 s
        for img in frames_tt:
            emit(img, hold)
        emit(frames_tt[0], 20)
    else:                                                    # finish with a slow orbit of the drawn board
        for i in range(90):
            a = AZ1 + 40.0 * math.sin(math.pi * i / 89) if tracks else AZ1
            img = (R.routing3d(final, R.cam(boundsB, a), tracks, vias, len(tracks), origin, stats, 1.0) if tracks
                   else R.placement3d(final, R.cam(boundsB, a), True, 1.0))
            d = ImageDraw.Draw(img)
            d.rectangle((0, 0, R.W, R.top - 5), fill=BG)
            R.header(d, "done", done, 1.0)
            emit(img)
    return _encode(pl, tmp, out, n)


def _encode(pl, tmp, out, n):
    mp4 = out / f"{pl.name}-build.mp4"
    if shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(tmp / "%05d.png"),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(mp4)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            pl.log("  video: ffmpeg failed: " + r.stderr[-300:])
            mp4 = None
    else:
        mp4 = out / f"{pl.name}-build.gif"
        imgs = [Image.open(p) for p in sorted(tmp.glob("[0-9]*.png"))[::2]]
        imgs[0].save(mp4, save_all=True, append_images=imgs[1:], duration=int(2000 / FPS), loop=0)
        pl.log("  video: ffmpeg not found, wrote a GIF instead (sudo apt install ffmpeg for MP4)")
    shutil.rmtree(tmp, ignore_errors=True)
    if mp4:
        pl.log(f"  video: {n} frames, {n / FPS:.1f} s -> {mp4.name}")
        return f"out/{mp4.name}"
    return None
