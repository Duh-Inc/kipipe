"""Coarse-to-fine visual inspection of the routed board.

Pass 1 looks at small tiles (local detail), pass 2 at larger tiles (blocks),
pass 3 at the whole board (global layout + verdict + suggested moves). All
passes run in one model conversation, so later passes build on earlier ones.

Images are drawn here from kipipe's own geometry (tracks by layer, pads, vias,
courtyards, refs, a mm grid) rather than KiCad's SVG, because the ground pour
in KiCad's render covers the tracks.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (14, 15, 17)
BOARD = (18, 52, 36)
EDGE = (210, 210, 200)
F_CU = (226, 70, 58)
B_CU = (70, 130, 222)
PAD_F = (214, 140, 70)
PAD_B = (110, 150, 220)
VIA = (205, 205, 205)
COURT = (125, 122, 112)
TEXT = (235, 232, 222)
GRID = (60, 80, 70)
DIM = (150, 147, 140)


def _font(px):
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, max(8, int(px)))
    return ImageFont.load_default()


class BoardRender:
    def __init__(self, layout: dict, geometry: dict):
        self.W, self.H = layout["board"]
        self.parts = layout["parts"]
        ob = geometry.get("outline") or [0, 0, self.W, self.H]
        self.ox, self.oy = ob[0], ob[1]
        self.tracks = geometry.get("tracks", [])
        self.outline = layout.get("outline")          # shaped boards: polygon, board mm
        self.vias = geometry.get("vias", [])

    def render(self, crop, px_w, title):
        """crop = (x0, y0, x1, y1) in board mm; returns a PIL image px_w wide."""
        x0, y0, x1, y1 = crop
        head = 54
        s = px_w / (x1 - x0)
        px_h = int((y1 - y0) * s) + head
        img = Image.new("RGB", (px_w, px_h), BG)

        def P(x, y):
            return ((x - x0) * s, (y - y0) * s + head)

        d = ImageDraw.Draw(img)
        if self.outline:
            d.polygon([P(x, y) for x, y in self.outline], fill=BOARD)
        else:
            d.rectangle((*P(0, 0), *P(self.W, self.H)), fill=BOARD)
        # mm grid, every 5 mm (every 10 when zoomed out)
        step = 5 if s > 12 else 10
        f_grid = _font(min(14, max(9, s * 1.2)))
        for gx in range(int(math.floor(x0 / step) * step), int(x1) + step, step):
            d.line((*P(gx, y0), *P(gx, y1)), fill=GRID, width=1)
            d.text((P(gx, y0)[0] + 2, head + 2), f"{gx}", font=f_grid, fill=DIM)
        for gy in range(int(math.floor(y0 / step) * step), int(y1) + step, step):
            d.line((*P(x0, gy), *P(x1, gy)), fill=GRID, width=1)
            d.text((2, P(x0, gy)[1] + 2), f"{gy}", font=f_grid, fill=DIM)

        # copper: bottom layer first, translucent, then top
        for layer, col in (("B.Cu", B_CU), ("F.Cu", F_CU)):
            lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(lay)
            for t in self.tracks:
                if t["layer"] != layer:
                    continue
                a = P(t["a"][0] - self.ox, t["a"][1] - self.oy)
                b = P(t["b"][0] - self.ox, t["b"][1] - self.oy)
                w = max(1, int(t["w"] * s))
                ld.line((a, b), fill=col + (200 if layer == "F.Cu" else 150,), width=w)
                r = w / 2
                for q in (a, b):
                    ld.ellipse((q[0] - r, q[1] - r, q[0] + r, q[1] + r), fill=col + (200 if layer == "F.Cu" else 150,))
            img.paste(lay, (0, 0), lay)
        d = ImageDraw.Draw(img)
        # parts: courtyard, pads, ref
        for p in self.parts:
            g = p["geo"].get(p["rot"]) or next(iter(p["geo"].values()))
            bx = g["box"]
            a, b = P(p["x"] + bx[0], p["y"] + bx[1]), P(p["x"] + bx[2], p["y"] + bx[3])
            d.rectangle((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), outline=COURT, width=1)
            if p.get("fixed"):            # mounting hole: draw as a hole, not a giant pad
                cx, cy = P(p["x"], p["y"])
                rr = max(3, (bx[2] - bx[0]) / 2 * s * 0.75)
                d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=VIA, width=max(2, int(s * 0.4)))
                d.ellipse((cx - rr * 0.7, cy - rr * 0.7, cx + rr * 0.7, cy + rr * 0.7), fill=BG)
                d.text((cx, cy - rr - 2), p["ref"] + " (hole)", font=_font(min(28, max(9, s * 1.1))), fill=DIM, anchor="md")
                continue
            for pd in g["pads"]:
                q0, q1 = P(p["x"] + pd[2], p["y"] + pd[3]), P(p["x"] + pd[4], p["y"] + pd[5])
                d.rectangle((min(q0[0], q1[0]), min(q0[1], q1[1]), max(q0[0], q1[0]), max(q0[1], q1[1])),
                            fill=PAD_B if p.get("back") else PAD_F)
            fs = min(28, max(9, s * 1.1))
            d.text(((a[0] + b[0]) / 2, min(a[1], b[1]) - 1), p["ref"], font=_font(fs), fill=TEXT, anchor="md")
        for v in self.vias:
            c = P(v["x"] - self.ox, v["y"] - self.oy)
            r = max(2, v.get("d", 0.6) / 2 * s)
            d.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), fill=VIA)
            d.ellipse((c[0] - r * 0.45, c[1] - r * 0.45, c[0] + r * 0.45, c[1] + r * 0.45), fill=BG)
        if self.outline:
            d.line([P(x, y) for x, y in self.outline + self.outline[:1]], fill=EDGE, width=2)
        else:
            d.rectangle((*P(0, 0), *P(self.W, self.H)), outline=EDGE, width=2)
        # header
        d.rectangle((0, 0, px_w, head - 4), fill=BG)
        d.text((10, 8), title, font=_font(20), fill=(255, 176, 0))
        d.text((10, 32), f"x {x0:.0f}-{x1:.0f} mm, y {y0:.0f}-{y1:.0f} mm  ·  red = top copper, blue = bottom, "
                         f"orange = pads, grey rings = vias", font=_font(13), fill=DIM)
        return img


def tiles(W, H, size, max_tiles):
    """Overlapping tiles of about `size` mm covering the board."""
    nx, ny = max(1, math.ceil(W / size)), max(1, math.ceil(H / size))
    while nx * ny > max_tiles:
        size *= 1.25
        nx, ny = max(1, math.ceil(W / size)), max(1, math.ceil(H / size))
    tw, th = W / nx, H / ny
    ov = 0.12
    out = []
    for j in range(ny):
        for i in range(nx):
            x0 = max(-1, i * tw - tw * ov)
            y0 = max(-1, j * th - th * ov)
            x1 = min(W + 1, (i + 1) * tw + tw * ov)
            y1 = min(H + 1, (j + 1) * th + th * ov)
            out.append((f"{chr(65 + j)}{i + 1}", (x0, y0, x1, y1)))
    return out


def build_passes(layout, geometry, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    R = BoardRender(layout, geometry)
    W, H = R.W, R.H
    passes = []
    fine = tiles(W, H, 18, 16)
    mid = tiles(W, H, 36, 6) if max(W, H) > 40 else []
    for name, plan, px in (("pass 1: detail", fine, 1024), ("pass 2: blocks", mid, 1200)):
        if not plan or (passes and len(plan) >= len(passes[-1]["tiles"])):
            continue
        items = []
        for tid, crop in plan:
            f = outdir / f"{name.split(':')[0].replace(' ', '')}_{tid}.png"
            R.render(crop, px, f"{name} · tile {tid}").save(f)
            items.append({"id": tid, "crop": [round(v, 1) for v in crop], "file": str(f)})
        passes.append({"name": name, "tiles": items})
    whole = outdir / "pass3_board.png"
    R.render((-2, -2, W + 2, H + 2), 1600, "pass 3: whole board").save(whole)
    passes.append({"name": "pass 3: whole board", "tiles": [{"id": "BOARD", "crop": [0, 0, W, H], "file": str(whole)}]})
    return passes
