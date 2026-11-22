"""Closed-form physics used by the pipeline: trace current capacity and
microstrip impedance. These are standard approximations, not field solves;
the openEMS stage is the field solve.
"""
from __future__ import annotations

import math

OZ_TO_MM = 0.0348   # 1 oz/ft^2 copper ~= 34.8 um


def ipc2221_width_mm(current_a: float, temp_rise_c: float = 10.0, copper_oz: float = 1.0,
                     external: bool = True) -> float:
    """Minimum trace width for a current, IPC-2221 curve fit.

    I = k * dT^0.44 * A^0.725, A in mil^2, k = 0.048 external / 0.024 internal.
    IPC-2221 is more conservative than IPC-2152 for most external traces, so a
    pass here is a safe pass.
    """
    if current_a <= 0:
        return 0.0
    k = 0.048 if external else 0.024
    area_mil2 = (current_a / (k * temp_rise_c ** 0.44)) ** (1 / 0.725)
    thickness_mil = copper_oz * 1.378
    return area_mil2 / thickness_mil * 0.0254


def trace_temp_rise_c(current_a: float, width_mm: float, copper_oz: float = 1.0, external: bool = True) -> float:
    """Inverse of the above: temperature rise for a given width."""
    if current_a <= 0 or width_mm <= 0:
        return 0.0
    k = 0.048 if external else 0.024
    area_mil2 = (width_mm / 0.0254) * copper_oz * 1.378
    return (current_a / (k * area_mil2 ** 0.725)) ** (1 / 0.44)


def microstrip_z0(w_mm: float, h_mm: float, er: float, t_mm: float = 0.035) -> float:
    """Hammerstad-Jensen microstrip impedance with thickness correction."""
    if t_mm > 0:
        w_mm = w_mm + (t_mm / math.pi) * (1 + math.log(4 * math.e / math.sqrt((t_mm / h_mm) ** 2 + (1 / math.pi / (w_mm / t_mm + 1.1)) ** 2)))
    u = w_mm / h_mm
    a = 1 + (1 / 49) * math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) + (1 / 18.7) * math.log(1 + (u / 18.1) ** 3)
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    e_eff = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    return 60 / math.sqrt(e_eff) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))


def microstrip_width_mm(z0: float, h_mm: float, er: float, t_mm: float = 0.035) -> float:
    """Solve for the width giving z0 (bisection on the analysis formula)."""
    lo, hi = 0.01, 50.0
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        if microstrip_z0(mid, h_mm, er, t_mm) > z0:
            lo = mid
        else:
            hi = mid
    return round(math.sqrt(lo * hi), 3)


def stackup(layers: int, thickness_mm: float = 1.6, copper_oz: float = 1.0):
    """Typical stackups. 4-layer follows the common 7628 prepreg build
    (JLC04161H-7628). Returns list of dicts (top to bottom)."""
    cu = round(copper_oz * OZ_TO_MM, 4)
    if layers == 2:
        core = round(thickness_mm - 2 * cu, 4)
        return [{"name": "F.Cu", "type": "copper", "thickness": cu},
                {"name": "dielectric 1", "type": "core", "thickness": core, "material": "FR4", "epsilon": 4.5, "lossTangent": 0.02},
                {"name": "B.Cu", "type": "copper", "thickness": cu}]
    inner = 0.0152
    pp = 0.2104
    core = round(thickness_mm - 2 * cu - 2 * inner - 2 * pp, 4)
    return [{"name": "F.Cu", "type": "copper", "thickness": cu},
            {"name": "dielectric 1", "type": "prepreg", "thickness": pp, "material": "7628", "epsilon": 4.4, "lossTangent": 0.02},
            {"name": "In1.Cu", "type": "copper", "thickness": inner},
            {"name": "dielectric 2", "type": "core", "thickness": core, "material": "FR4", "epsilon": 4.6, "lossTangent": 0.02},
            {"name": "In2.Cu", "type": "copper", "thickness": inner},
            {"name": "dielectric 3", "type": "prepreg", "thickness": pp, "material": "7628", "epsilon": 4.4, "lossTangent": 0.02},
            {"name": "B.Cu", "type": "copper", "thickness": cu}]


def top_microstrip_params(layers: int, thickness_mm: float = 1.6, copper_oz: float = 1.0):
    """(h, er, t) for a top-layer microstrip over the first plane."""
    st = stackup(layers, thickness_mm, copper_oz)
    return st[1]["thickness"], st[1]["epsilon"], st[0]["thickness"]
