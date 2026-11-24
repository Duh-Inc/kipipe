"""Design calculators: the model states the goal, code computes the part value.

Asking a model for "the resistor for a 5 mA LED" gives a slightly different answer each time; these functions
give the same one. A spec carries the goals in its "calc" list; apply() fills in the values before validation,
so every run of the same spec gets the same parts.

    "calc": [
      {"kind": "led",        "resistor": "R3", "led": "D2", "current_ma": 5},
      {"kind": "divider",    "top": "R5", "bottom": "R6", "vin": 4.2, "vout": 1.0},
      {"kind": "feedback",   "top": "R7", "bottom": "R8", "vout": 5.0, "vref": 0.596},
      {"kind": "i2c_pullup", "resistors": ["R1", "R2"], "speed_khz": 400},
      {"kind": "crystal",    "caps": ["C5", "C6"], "load_pf": 12},
      {"kind": "rc",         "r": "R9", "c": "C9", "fc_hz": 1000}
    ]

Voltages may be numbers or net names ("+3V3", "VBAT" with a rails entry). Results are logged on the component
("calc" field) so the guide and report can show the working.
"""
from __future__ import annotations

import math
import re

E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0, 3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8,
       7.5, 8.2, 9.1]
E96 = [1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30, 1.33, 1.37, 1.40, 1.43, 1.47, 1.50,
       1.54, 1.58, 1.62, 1.65, 1.69, 1.74, 1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32,
       2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09, 3.16, 3.24, 3.32, 3.40, 3.48, 3.57,
       3.65, 3.74, 3.83, 3.92, 4.02, 4.12, 4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49,
       5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32, 7.50, 7.68, 7.87, 8.06, 8.25, 8.45,
       8.66, 8.87, 9.09, 9.31, 9.53, 9.76]
SERIES = {"E12": E12, "E24": E24, "E96": E96}
LED_VF = (("INFRA|IR\\b", 1.3), ("RED|ORANGE|AMBER|YELLOW", 2.0), ("BLUE|WHITE|UV|RGB|PINK", 3.0), ("GREEN", 2.2))
I2C_RISE_NS = {100: 1000, 400: 300, 1000: 120}


class CalcError(ValueError):
    pass


def values(series="E24", lo=1.0, hi=10e6):
    s = SERIES[series]
    out, dec = [], 10 ** math.floor(math.log10(lo))
    while dec <= hi:
        out += [round(m * dec, 12) for m in s if lo <= m * dec <= hi]
        dec *= 10
    return out


def snap(v, series="E24", mode="nearest"):
    """Standard value near v. mode: nearest (by ratio), up (>= v), down (<= v)."""
    if v <= 0:
        raise CalcError(f"cannot pick a standard value for {v}")
    dec = 10 ** math.floor(math.log10(v))
    cands = [m * d for d in (dec / 10, dec, dec * 10) for m in SERIES[series]]
    if mode == "up":
        return round(min(c for c in cands if c >= v * (1 - 1e-9)), 12)
    if mode == "down":
        return round(max(c for c in cands if c <= v * (1 + 1e-9)), 12)
    return round(min(cands, key=lambda c: abs(math.log(c / v))), 12)


def fmt(v, unit=""):
    """10000 -> '10k', 4700 -> '4.7k', 1e-7 -> '100n', 2.2e-11 -> '22p'."""
    for p, s in ((1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(v) >= p * 0.9999:
            x = v / p
            t = f"{x:.3g}"
            return f"{t}{s}{unit}"
    return f"{v:.3g}{unit}"


def led_resistor(v_supply, vf, i_a, series="E24"):
    """Series resistor: the next standard value up, so the current never exceeds the target."""
    if v_supply <= vf:
        raise CalcError(f"supply {v_supply:g} V is not above the LED's forward voltage {vf:g} V")
    r = snap((v_supply - vf) / i_a, series, "up")
    i = (v_supply - vf) / r
    return {"r": r, "i": i, "p": i * i * r, "why": f"({v_supply:g} V - {vf:g} V) / {i_a * 1e3:g} mA = "
            f"{(v_supply - vf) / i_a:.4g} ohm, next {series} value up {fmt(r)} -> {i * 1e3:.3g} mA"}


def divider(vin, vout, series="E96", total=(10e3, 1e6)):
    """Top/bottom pair (both from the series) whose ratio is closest to vout/vin, smallest total within range
    among equally good pairs (lower impedance, less noise)."""
    if not 0 < vout < vin:
        raise CalcError(f"a divider needs 0 < vout < vin (got vin {vin:g}, vout {vout:g})")
    target = vout / vin
    best = None
    for bot in values(series, total[0] / 100, total[1]):
        top = snap(bot * (1 / target - 1), series)
        t = top + bot
        if not total[0] <= t <= total[1]:
            continue
        err = abs(bot / t - target) / target
        key = (round(err, 5), t)
        if best is None or key < best[0]:
            best = (key, top, bot)
    if not best:
        raise CalcError("no divider in range")
    _, top, bot = best
    v = vin * bot / (top + bot)
    return {"top": top, "bottom": bot, "vout": v, "err": (v - vout) / vout,
            "why": f"{fmt(top)} over {fmt(bot)} from {vin:g} V gives {v:.4g} V (target {vout:g} V, "
                   f"{(v - vout) / vout * 100:+.2f} %)"}


def feedback(vout, vref, series="E96"):
    """Regulator feedback divider: vout = vref * (1 + top / bottom)."""
    d = divider(vout, vref, series)
    real = vref * (d["top"] + d["bottom"]) / d["bottom"]
    d.update(vout=real, why=f"{fmt(d['top'])} / {fmt(d['bottom'])} with Vref {vref:g} V sets {real:.4g} V "
                            f"(target {vout:g} V, {(real - vout) / vout * 100:+.2f} %)")
    return d


def i2c_pullup(vdd, speed_khz=100, bus_pf=150, series="E12"):
    """E12 value at 60 % of the rise-time limit (I2C spec: tr = 0.8473 RC), never below the 3 mA sink limit.
    150 pF is a cautious guess for a board plus a short cable; 100 kHz gives 4.7k."""
    tr = I2C_RISE_NS.get(int(speed_khz), 1000 if speed_khz <= 100 else 300 if speed_khz <= 400 else 120) * 1e-9
    rmax = tr / (0.8473 * bus_pf * 1e-12)
    rmin = (vdd - 0.4) / 3e-3
    if rmin > rmax:
        raise CalcError(f"no pull-up meets both limits ({fmt(rmin)} min, {fmt(rmax)} max); lower the bus capacitance")
    r = snap(rmax * 0.6, series, "down")
    r = max(r, snap(rmin, series, "up"))
    return {"r": r, "why": f"{speed_khz:g} kHz, {bus_pf:g} pF bus: between {fmt(rmin)} (3 mA sink) and "
                           f"{fmt(rmax)} (rise time); {fmt(r)} leaves 40 % margin"}


def crystal_caps(load_pf, stray_pf=3.0, series="E12"):
    c = 2 * (load_pf - stray_pf)
    if c <= 0:
        raise CalcError(f"load capacitance {load_pf:g} pF is below the assumed stray {stray_pf:g} pF")
    v = snap(c * 1e-12, series)
    return {"c": v, "why": f"2 x ({load_pf:g} pF - {stray_pf:g} pF stray) = {c:g} pF, nearest {series} {fmt(v, 'F')}"}


def rc(fc_hz, r=None, c=None, series="E12"):
    if r is None and c is None:
        r = 10e3
    if c is None:
        c = snap(1 / (2 * math.pi * r * fc_hz), series)
    elif r is None:
        r = snap(1 / (2 * math.pi * c * fc_hz), "E24")
    f = 1 / (2 * math.pi * r * c)
    return {"r": r, "c": c, "fc": f, "why": f"1 / (2 pi {fmt(r)} {fmt(c, 'F')}) = {f:.4g} Hz (target {fc_hz:g} Hz)"}


# ---------------------------------------------------------------- applying "calc" entries to a spec

def _volts(spec, x):
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        from .addons.core import net_voltage
        rail = (spec.get("rails") or {}).get(x)
        if isinstance(rail, dict) and isinstance(rail.get("volts"), (int, float)):
            return float(rail["volts"])
        if isinstance(rail, (int, float)):
            return float(rail)
        try:
            return float(x)
        except ValueError:
            return net_voltage(x)
    return None


def _pin_nets(spec):
    out = {}
    for net, pins in (spec.get("nets") or {}).items():
        for p in pins:
            out[p] = net
    return out


def _set(comps, ref, value, why, entry):
    c = comps.get(ref)
    if c is None:
        raise CalcError(f"{ref} is not a component")
    old = str(c.get("value") or "").strip()
    tail = re.sub(r"^(\?|[\d.]+\s*[pnumkMGRrF]?[0-9]*)(\s+|$)", "", old) if re.match(r"^(\?|[\d.])", old) else ""
    c["value"] = value + (" " + tail if tail else "")             # keep '50V C0G' style suffixes
    c["calc"] = why
    entry.setdefault("_set", []).append(ref)


def apply(spec: dict, report=None) -> list[str]:
    """Compute every "calc" entry, write the values into the components. Returns the log lines; problems go to
    report.errors (so the model fixes the entry) when a report is given, else raise."""
    log = []
    comps = {c.get("ref"): c for c in spec.get("components") or []}
    pn = _pin_nets(spec)

    def net_of(ref, pin):
        return pn.get(f"{ref}.{pin}")

    def fail(msg):
        if report is None:
            raise CalcError(msg)
        report.errors.append("calc: " + msg)

    for e in spec.get("calc") or []:
        k = e.get("kind")
        try:
            if k == "led":
                rref, dref = e["resistor"], e["led"]
                vs = _volts(spec, e.get("supply"))
                if vs is None:                    # the rail at the far end of the resistor or the LED anode
                    rnets = [net_of(rref, p) for p in ("1", "2")]
                    for n in rnets + [net_of(dref, p) for p in ("A", "2", "1")]:
                        v = _volts(spec, n) if n else None
                        if v:
                            vs = v
                            break
                if vs is None:
                    raise CalcError(f"no supply voltage for {dref}: add \"supply\" to the entry")
                vf = e.get("vf")
                if vf is None:
                    val = str(comps.get(dref, {}).get("value", "")).upper()
                    vf = next((v for pat, v in LED_VF if re.search(pat, val)), 2.0)
                res = led_resistor(vs, float(vf), float(e.get("current_ma", 5)) / 1e3, e.get("series", "E24"))
                _set(comps, rref, fmt(res["r"]), res["why"], e)
            elif k in ("divider", "feedback"):
                if k == "divider":
                    vin = _volts(spec, e.get("vin"))
                    if vin is None:
                        raise CalcError("divider needs vin (volts or a net with a known voltage)")
                    res = divider(vin, float(e["vout"]), e.get("series", "E96"))
                else:
                    if e.get("vref") is None:
                        raise CalcError(f"feedback for {e.get('top')}/{e.get('bottom')} needs vref from the regulator's datasheet")
                    res = feedback(float(e["vout"]), float(e["vref"]), e.get("series", "E96"))
                _set(comps, e["top"], fmt(res["top"]), res["why"], e)
                _set(comps, e["bottom"], fmt(res["bottom"]), res["why"], e)
            elif k == "i2c_pullup":
                refs = e["resistors"]
                vdd = _volts(spec, e.get("supply"))
                if vdd is None:
                    for ref in refs:
                        for p in ("1", "2"):
                            v = _volts(spec, net_of(ref, p)) if net_of(ref, p) else None
                            if v:
                                vdd = v
                                break
                        if vdd:
                            break
                if vdd is None:
                    raise CalcError("i2c_pullup needs supply")
                res = i2c_pullup(vdd, float(e.get("speed_khz", 100)), float(e.get("bus_pf", 150)))
                for ref in refs:
                    _set(comps, ref, fmt(res["r"]), res["why"], e)
            elif k == "crystal":
                res = crystal_caps(float(e["load_pf"]), float(e.get("stray_pf", 3)))
                for ref in e["caps"]:
                    _set(comps, ref, fmt(res["c"]), res["why"], e)
            elif k == "rc":
                from .addons.core import parse_value
                r0 = parse_value(comps.get(e["r"], {}).get("value")) if e.get("fix") == "r" else None
                c0 = parse_value(comps.get(e["c"], {}).get("value")) if e.get("fix") == "c" else None
                res = rc(float(e["fc_hz"]), r0, c0)
                _set(comps, e["r"], fmt(res["r"]), res["why"], e)
                _set(comps, e["c"], fmt(res["c"]), res["why"], e)
            else:
                raise CalcError(f"unknown calc kind {k!r} (led, divider, feedback, i2c_pullup, crystal, rc)")
            log.append(f"{k}: {res['why']}")
        except KeyError as ex:
            fail(f"{k} entry is missing {ex.args[0]!r}: {e}")
        except CalcError as ex:
            fail(f"{k}: {ex}")
    spec["_calc_log"] = log
    return log


PROMPT_DOC = """Computed values ("calc"): never work out these values yourself. Add the part with value "?" and a
"calc" entry; code computes a standard value the same way every time and writes it in.
  {"kind": "led", "resistor": "R3", "led": "D2", "current_ma": 5}                 (supply found from the nets, or "supply": "+3V3")
  {"kind": "divider", "top": "R5", "bottom": "R6", "vin": "VBAT", "vout": 1.0}    (ADC and sense dividers)
  {"kind": "feedback", "top": "R7", "bottom": "R8", "vout": 5.0, "vref": 0.596}  (adjustable regulators; vref from the datasheet)
  {"kind": "i2c_pullup", "resistors": ["R1", "R2"], "speed_khz": 400}
  {"kind": "crystal", "caps": ["C5", "C6"], "load_pf": 12}                       (load capacitance from the crystal's datasheet)
  {"kind": "rc", "r": "R9", "c": "C9", "fc_hz": 1000}                            (RC low-pass)"""
