"""Board outline shapes: rectangle, rounded rectangle, circle, plus, hexagon, octagon, or any polygon.

Everything is in board millimetres with (0, 0) at the top-left of the shape's bounding box, y down,
the same frame the placer uses. Pure Python, so both the pipeline and the pcbnew side can import it.
"""
from __future__ import annotations

import math

SHAPES = ("rect", "rounded", "circle", "plus", "hexagon", "octagon", "polygon")


def parse(board: dict) -> dict:
    """Normalise spec['board'] shape fields into {"type", ...params}."""
    sh = board.get("shape") or "rect"
    if isinstance(sh, dict):
        if "polygon" in sh:
            return {"type": "polygon", "points": [[float(x), float(y)] for x, y in sh["polygon"]]}
        out = dict(sh)
        out["type"] = str(out.get("type", "rect")).lower()
        return out
    sh = str(sh).lower().strip()
    out = {"type": sh}
    for k in ("corner_mm", "arm_mm"):
        if board.get(k) is not None:
            out[k] = float(board[k])
    return out


def is_rect(shape: dict | None) -> bool:
    return not shape or shape.get("type", "rect") == "rect"


def area_fraction(shape: dict, w: float = 1.0, h: float = 1.0) -> float:
    pts = polygon(shape, w, h)
    return abs(_area(pts)) / (w * h) if w * h else 1.0


def default_aspect(shape: dict) -> float:
    if shape.get("type") == "polygon":
        xs = [p[0] for p in shape["points"]]
        ys = [p[1] for p in shape["points"]]
        return (max(xs) - min(xs)) / max(1e-6, max(ys) - min(ys))
    return 1.3 if shape.get("type") in ("rect", "rounded") else 1.0


def polygon(shape: dict, w: float, h: float, arc_segments: int = 72) -> list:
    """Outline as a list of (x, y), clockwise in screen coordinates (y down)."""
    t = (shape or {}).get("type", "rect")
    if t == "rect":
        return [(0, 0), (w, 0), (w, h), (0, h)]
    if t == "rounded":
        r = min(float(shape.get("corner_mm") or min(w, h) * 0.12), w / 2, h / 2)
        pts = []
        for cx, cy, a0 in ((w - r, r, -90), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180)):
            for k in range(9):
                a = math.radians(a0 + 90 * k / 8)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts
    if t == "circle":
        rx, ry = w / 2, h / 2
        return [(rx + rx * math.cos(2 * math.pi * k / arc_segments), ry + ry * math.sin(2 * math.pi * k / arc_segments))
                for k in range(arc_segments)]
    if t == "plus":
        aw = min(float(shape.get("arm_mm") or w / 3), w * 0.9)     # width of the vertical arm
        ah = min(float(shape.get("arm_mm") or h / 3), h * 0.9)     # height of the horizontal arm
        x0, x1 = (w - aw) / 2, (w + aw) / 2
        y0, y1 = (h - ah) / 2, (h + ah) / 2
        return [(x0, 0), (x1, 0), (x1, y0), (w, y0), (w, y1), (x1, y1), (x1, h), (x0, h), (x0, y1), (0, y1), (0, y0), (x0, y0)]
    if t == "hexagon":
        return [(w / 4, 0), (3 * w / 4, 0), (w, h / 2), (3 * w / 4, h), (w / 4, h), (0, h / 2)]
    if t == "octagon":
        c = min(w, h) * 0.293
        return [(c, 0), (w - c, 0), (w, c), (w, h - c), (w - c, h), (c, h), (0, h - c), (0, c)]
    if t == "polygon":
        pts = [(float(x), float(y)) for x, y in shape["points"]]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        sx = w / max(1e-6, max(xs) - min(xs))
        sy = h / max(1e-6, max(ys) - min(ys))
        pts = [((x - min(xs)) * sx, (y - min(ys)) * sy) for x, y in pts]
        return pts if _area(pts) > 0 else pts[::-1]
    raise ValueError(f"unknown board shape {t!r}; use one of {', '.join(SHAPES)}")


def _area(pts) -> float:
    """Shoelace; positive for clockwise in y-down screen coordinates."""
    return sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts))) / 2


def point_in(poly, x, y) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def rect_inside(poly, r, margin: float = 0.0) -> bool:
    """True if rect (x0, y0, x1, y1), grown by margin, lies inside the polygon. For a simple polygon that is:
    all four corners inside and no polygon vertex strictly inside the rect."""
    x0, y0, x1, y1 = r[0] - margin, r[1] - margin, r[2] + margin, r[3] + margin
    if not all(point_in(poly, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))):
        return False
    return not any(x0 < px < x1 and y0 < py < y1 for px, py in poly)


def edge_strips(poly, d: float) -> list:
    """Quads along every edge, d wide, on the inside: a keepout ring that follows the outline."""
    out = []
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1e-6:
            continue
        nx, ny = -(y2 - y1) / L, (x2 - x1) / L        # inward normal for clockwise (y-down) polygons
        out.append([(x1, y1), (x2, y2), (x2 + nx * d, y2 + ny * d), (x1 + nx * d, y1 + ny * d)])
    return out


def hole_positions(shape: dict, w: float, h: float, keepout: float, count: int = 4) -> list:
    """Where mounting holes go. Anchors are the outline's outermost points in four directions (the corner
    directions or the side directions, whichever set ends up farther out, so a rectangle gets its corners and a
    plus its arm ends); each is slid toward the middle until the hole's keepout square fits with a margin."""
    poly = polygon(shape, w, h)
    cx, cy = w / 2, h / 2
    half = keepout / 2

    def anchor(dx, dy):
        proj = [(px - cx) * dx + (py - cy) * dy for px, py in poly]
        m = max(proj)
        pts = [p for p, v in zip(poly, proj) if v > m - 0.5]
        return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)

    def slide(ax, ay):
        dx, dy = cx - ax, cy - ay
        L = math.hypot(dx, dy) or 1.0
        for k in range(0, int(L / 0.25)):
            px, py = ax + dx / L * k * 0.25, ay + dy / L * k * 0.25
            if rect_inside(poly, (px - half, py - half, px + half, py + half), 0.3):
                return (round(px, 2), round(py, 2))
        return None

    r2 = math.sqrt(0.5)
    sets = [[(-r2, -r2), (r2, -r2), (r2, r2), (-r2, r2)], [(0, -1), (1, 0), (0, 1), (-1, 0)]]
    best = None
    for dirs in sets:
        spots = [slide(*anchor(dx, dy)) for dx, dy in dirs]
        spots = [p for p in spots if p is not None]
        spots = [p for i, p in enumerate(spots) if all(math.hypot(p[0] - q[0], p[1] - q[1]) > keepout for q in spots[:i])]
        score = (len(spots), sum(math.hypot(p[0] - cx, p[1] - cy) for p in spots))
        if best is None or score > best[0]:
            best = (score, spots)
    return best[1][:count]


def kicad_primitives(shape: dict, w: float, h: float) -> list:
    """What to draw on Edge.Cuts: [('circle', cx, cy, r)] for true circles, else [('poly', points)]."""
    if shape.get("type") == "circle" and abs(w - h) < 1e-6:
        return [("circle", w / 2, h / 2, w / 2)]
    return [("poly", polygon(shape, w, h))]
