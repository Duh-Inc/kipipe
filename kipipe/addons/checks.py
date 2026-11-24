"""Design checks: fixed rules about how circuits are normally wired. No model, no datasheet.

These are conventions, not physics: a finding means "this usually goes wrong", and each one says what it looked
at so it can be judged. Datasheet-based checks (pin numbers, ratings, support parts, equations) are in partchecks.py.
"""
from __future__ import annotations

import math
import re

from .core import addon, fmt_si

GROUP = "Design checks"


def _refs(pairs):
    return ", ".join(r for r, _ in pairs)


# ---------------------------------------------------------------------------
@addon(id="check.nets", title="Net hygiene", kind="check", auto=True, group=GROUP,
       summary="Nets that look like a mistake: names that differ only in spelling, one-pin nets, supplies nothing feeds, no ground.")
def nets(r):
    j = r.job
    canon = {}
    for n in j.nets:
        key = re.sub(r"[^A-Z0-9]", "", n.upper().replace("+", ""))
        v = j.voltage(n)
        if v is not None and not j.is_gnd(n) and re.fullmatch(r"[+-]?\d+(\.\d+)?V\d*|\d+V\d+|[+-]?\d+\.\d+V", n.lstrip("/"), re.I):
            key = f"RAIL{v:g}"
        canon.setdefault(key, []).append(n)
    for key, names in canon.items():
        if len(names) > 1:
            r.finding("warning", f"Nets {', '.join(names)} look like the same net spelled differently",
                      "They are separate nets, so parts on one are not connected to parts on the other.",
                      nets=names, fix="Use one name if they are meant to be one net.")
    for n, pins in j.nets.items():
        if len(pins) == 1:
            r.finding("warning", f"Net {n} has one pin ({pins[0]})", "A net with one connection goes nowhere.",
                      refs=[pins[0].split(".")[0]], nets=[n], fix="Connect it, or mark the pin no_connect.")
    nc = set(j.spec.get("no_connect") or [])
    for ref in j.comps:
        loose = [f"{p.number} ({p.name})" for p in j.pins(ref) if p.etype == "power_in" and f"{ref}.{p.number}" not in j.pin_net and f"{ref}.{p.number}" not in nc]
        if loose:
            r.finding("error", f"{ref} has supply or ground pin(s) connected to nothing: {', '.join(loose[:8])}",
                      "Every power pin of a part must be connected, including duplicates of the same name and exposed pads.",
                      refs=[ref], fix="Connect each to its supply or ground net.")
    if j.nets and not any(j.is_gnd(n) for n in j.nets):
        r.finding("warning", "No ground net", "No net is named GND (or AGND, VSS, 0V). The ground pour and several checks look for it.")
    for n, pins in j.nets.items():
        v = j.voltage(n)
        if not v or len(pins) < 2:
            continue
        fed = False
        for p in pins:
            ref, _, num = p.partition(".")
            k = j.kind(ref)
            if k in ("J", "BT", "D", "L", "FB", "F", "Q", "SW", "K", "TP") or j.pin_type(ref, num) == "power_out":
                fed = True
            elif k == "R" and (j.value(ref) or 1e9) <= 10:
                fed = True
            elif k == "U" and re.search(r"OUT|VO\b|SW|LX|VREG|3V3|VBUS", j.pin_name(ref, num), re.I) \
                    and j.pin_type(ref, num) != "power_in":
                fed = True
        if not fed:
            r.finding("warning", f"Nothing feeds {n}",
                      "Every pin on this supply is a load: no connector, regulator output, diode, inductor or switch brings power in.",
                      nets=[n], refs=[p.split(".")[0] for p in pins][:6])
    r.summary = r.summary or ""


# ---------------------------------------------------------------------------
_PWR_NAME = re.compile(r"^(A|D|IO|P)?V(DD|CC|IN|BAT|S|DDA|DDIO|REF\+)|^\+?\d+V\d*$|^3V3$|^VDD|^VCC|^AVDD|^DVDD|^IOVDD|^USB_VDD|^VREG_IN", re.I)


def _power_pins(j, ref):
    """{net: [pin numbers]} for a part's supply pins (not ground)."""
    out = {}
    for num, net in j.nets_of(ref).items():
        if j.is_gnd(net):
            continue
        t, name = j.pin_type(ref, num), j.pin_name(ref, num)
        if t in ("power_in", "power_out") or (not t and _PWR_NAME.match(name or "")) or (t == "passive" and _PWR_NAME.match(name or "")):
            out.setdefault(net, []).append(num)
    return out


@addon(id="check.decoupling", title="Decoupling capacitors", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       options=[{"key": "max_mm", "label": "Furthest a bypass cap may sit from its supply pin (mm)", "type": "number", "default": 6}],
       summary="Every IC supply pin has a capacitor to ground on its net, and once the board is placed, that it sits close to the pin.")
def decoupling(r):
    j = r.job
    limit = r.num("max_mm", 6.0)
    checked = far = 0
    for ref in j.ics():
        for net, nums in _power_pins(j, ref).items():
            checked += 1
            caps = j.caps_to_gnd(net)
            names = "/".join(sorted({j.pin_name(ref, n) or n for n in nums}))
            if not caps:
                r.finding("warning", f"{ref} supply {names} ({net}) has no capacitor to ground",
                          "No capacitor connects this supply net to ground anywhere on the board.",
                          refs=[ref], nets=[net], fix="Add 100 nF next to the pin (plus bulk capacitance if this is the main supply).")
                continue
            if not j.placed(ref):
                continue
            best = None
            for c, _ in caps:
                d = j.pad_distance(c, net, ref, net)
                if d is not None and (best is None or d < best[0]):
                    best = (d, c)
            if best and best[0] > limit:
                far += 1
                r.finding("warning", f"Nearest bypass cap for {ref} {names} is {best[0]:.1f} mm away ({best[1]})",
                          f"Pad to pad on {net}. Beyond about {limit:g} mm the track inductance makes a small bypass cap much less effective.",
                          refs=[ref, best[1]], nets=[net], fix=f"Move {best[1]} next to {ref}, or add a 'near' rule in the floorplan.")
    r.summary = f"{checked} supply pin group(s) checked" + (f", {far} with the capacitor too far away" if far else "")


# ---------------------------------------------------------------------------
_BUS = re.compile(r"(^|[^A-Z])(SDA|SCL)\d*($|[^A-Z])", re.I)


@addon(id="check.i2c", title="I2C pull-ups", kind="check", auto=True, group=GROUP,
       summary="SDA and SCL each have one pull-up to the same supply, with a sensible value.")
def i2c(r):
    j = r.job
    lines = {}
    for n, pins in j.nets.items():
        m = _BUS.search(n)
        kind = m.group(2).upper() if m else None
        if not kind:
            names = {j.pin_name(*p.split(".", 1)).upper() for p in pins}
            kind = next((k for k in ("SDA", "SCL") if any(re.search(rf"(^|[^A-Z]){k}($|[^A-Z])", x) for x in names)), None)
            if kind and sum(1 for p in pins if j.kind(p.split(".")[0]) == "U") < 1:
                kind = None
        if kind:
            lines[n] = kind
    if not lines:
        r.summary = "no I2C bus found"
        return
    rails = {}
    for n, kind in lines.items():
        ups = j.pullups(n)
        if not ups:
            mods = [x for x, _ in j.others(n) if re.search(r"module|breakout|board", str(j.comps[x].get("value", "")), re.I)]
            r.finding("warning", f"{kind} net {n} has no pull-up resistor",
                      "I2C lines are open drain and do not work without pull-ups."
                      + (f" {', '.join(mods)} may carry its own; check before relying on it." if mods else ""),
                      nets=[n], fix="Add 2.2k to 4.7k from the line to the bus supply.")
            continue
        vals = [j.value(x) for x, _ in ups]
        if len(ups) > 1 and all(vals):
            par = 1 / sum(1 / v for v in vals)
            r.finding("note", f"{n} has {len(ups)} pull-ups ({_refs(ups)}): {fmt_si(par, 'Ω')} in parallel",
                      "Fine if intended; below about 1 kΩ at 3.3 V a device may not be able to pull the line low.", refs=[x for x, _ in ups], nets=[n])
            vals = [par]
        for v in vals[:1]:
            if v and not 900 <= v <= 12000:
                r.finding("warning", f"Pull-up on {n} is {fmt_si(v, 'Ω')}", "Usual I2C pull-ups are 1 kΩ to 10 kΩ.", refs=[x for x, _ in ups], nets=[n])
        rails[n] = {o for _, o in ups}
    all_rails = set().union(*rails.values()) if rails else set()
    if len(all_rails) > 1:
        r.finding("warning", f"I2C pull-ups go to different supplies ({', '.join(sorted(all_rails))})",
                  "Both lines of a bus are normally pulled to the same rail; two rails only make sense with a level shifter between them.",
                  nets=sorted(all_rails))
    r.summary = f"{len(lines)} bus line(s) checked"


# ---------------------------------------------------------------------------
_VF = (("INFRA|IR\\b", 1.3), ("RED|ORANGE|AMBER|YELLOW", 2.0), ("BLUE|WHITE|UV|RGB|PINK", 3.0), ("GREEN", 2.2))


def _led_pins(j, ref):
    """(anode net, cathode net) of a two-pin LED or diode, from pin names, else KiCad's convention (1 = K)."""
    ns = j.nets_of(ref)
    a = k = None
    for num, net in ns.items():
        nm = j.pin_name(ref, num).upper()
        if nm in ("A", "ANODE", "+"):
            a = net
        elif nm in ("K", "C", "CATHODE", "-"):
            k = net
    if a is None and k is None and len(ns) == 2:
        k, a = ns.get("1"), ns.get("2")
    return a, k


@addon(id="check.leds", title="LED current and direction", kind="check", auto=True, group=GROUP,
       options=[{"key": "max_ma", "label": "Warn above (mA)", "type": "number", "default": 25}],
       summary="Each LED has a series resistor, faces the right way between its rails, and runs at a reasonable current.")
def leds(r):
    j = r.job
    n_ok = 0
    for ref in j.of_kind("LED"):
        if len(j.nets_of(ref)) != 2:
            continue                    # RGB or addressable: not a two-pin LED
        a, k = _led_pins(j, ref)
        if not a or not k:
            continue
        chain_r, ends = [], {}
        for side, net in (("a", a), ("k", k)):
            end = net
            if j.voltage(net) is None and len(j.nets.get(net, [])) == 2:
                (o, _), = j.others(net, ref)
                if j.kind(o) == "R":
                    tp = j.two_pin(o)
                    end = tp[1] if tp[0] == net else tp[0]
                    chain_r.append(o)
            ends[side] = end
        va, vk = j.voltage(ends["a"]), j.voltage(ends["k"])
        val = str(j.comps[ref].get("value", "")).upper()
        vf = next((v for pat, v in _VF if re.search(pat, val)), None)
        if va is not None and vk is not None and va <= vk:
            r.finding("error", f"{ref} is backwards: anode side is {ends['a']} ({va:g} V), cathode side is {ends['k']} ({vk:g} V)",
                      "The LED is reverse biased and will never light.", refs=[ref], nets=[ends["a"], ends["k"]], fix="Swap its two pins.")
            continue
        if not chain_r:
            drivers = [o for n in (a, k) for o, _ in j.others(n, ref) if j.kind(o) in ("U", "Q")]
            if va is not None and vk is not None:
                r.finding("error", f"{ref} sits directly between {ends['a']} and {ends['k']} with no resistor",
                          "Nothing limits the current.", refs=[ref], fix="Add a series resistor.")
            elif drivers:
                r.finding("note", f"{ref} has no series resistor and is driven by {', '.join(sorted(set(drivers)))}",
                          "Fine for a constant-current LED driver output; a plain logic pin needs a resistor.", refs=[ref] + drivers)
            continue
        if va is None or vk is None:
            n_ok += 1
            continue                    # switched by a pin or transistor: rails unknown, nothing to compute
        ohms = sum(j.value(x) or 0 for x in chain_r)
        if not ohms:
            continue
        lo, hi = ((vf, vf) if vf else (1.8, 3.2))
        i_hi, i_lo = (va - vk - lo) / ohms * 1e3, (va - vk - hi) / ohms * 1e3
        rng = f"{i_hi:.1f} mA" if vf else f"{max(i_lo, 0):.1f} to {i_hi:.1f} mA (forward voltage unknown, 1.8 to 3.2 V assumed)"
        how = f"({va - vk:g} V − Vf) / {fmt_si(ohms, 'Ω')} through {', '.join(chain_r)}"
        if i_hi > r.num("max_ma", 25):
            r.finding("warning", f"{ref} runs at {rng}", how + ". Most indicator LEDs are rated 20 mA.", refs=[ref] + chain_r, fix="Use a larger resistor.")
        elif i_hi <= 0 or (vf and i_hi < 0.2):
            r.finding("warning", f"{ref} will not light: {va - vk:g} V is not enough for its forward voltage", how, refs=[ref] + chain_r)
        else:
            n_ok += 1
            r.finding("note", f"{ref}: {rng}", how, refs=[ref] + chain_r)
    r.summary = f"{len(j.of_kind('LED'))} LED(s)" if j.of_kind("LED") else "no LEDs"


# ---------------------------------------------------------------------------
@addon(id="check.polarity", title="Polarised parts", kind="check", auto=True, group=GROUP,
       summary="Polarised capacitors have their + pin on the more positive net (the classic trap on negative rails).")
def polarity(r):
    j = r.job
    n = 0
    for ref in j.of_kind("C"):
        c = j.comps[ref]
        if not re.search(r"polari[sz]ed|^Device:CP|C_Polar", c.get("symbol", ""), re.I):
            continue
        ns = j.nets_of(ref)
        plus = minus = None
        for num, net in ns.items():
            nm = j.pin_name(ref, num)
            if nm in ("+", "A"):
                plus = net
            elif nm in ("-", "K"):
                minus = net
        if plus is None and minus is None:
            plus, minus = ns.get("1"), ns.get("2")       # KiCad: pin 1 is +
        vp, vm = j.voltage(plus), j.voltage(minus)
        n += 1
        if vp is None or vm is None:
            r.finding("note", f"{ref}: polarity not checked", f"+ is on {plus}, − is on {minus}; at least one of those names does not state a voltage.", refs=[ref])
        elif vp < vm:
            r.finding("error", f"{ref} is reversed: + on {plus} ({vp:g} V), − on {minus} ({vm:g} V)",
                      "A reversed tantalum or electrolytic fails, often violently.", refs=[ref], nets=[plus, minus], fix="Swap its two pins.")
    r.summary = f"{n} polarised capacitor(s)" if n else "no polarised capacitors"


# ---------------------------------------------------------------------------
@addon(id="check.crystal", title="Crystal load capacitors", kind="check", auto=True, group=GROUP,
       summary="Each crystal pin has a load capacitor to ground, and the two match.")
def crystal(r):
    j = r.job
    ys = j.of_kind("Y")
    for ref in ys:
        sig = [net for net in j.nets_of(ref).values() if not j.is_gnd(net)]
        if len(sig) != 2:
            continue
        caps = [j.caps_to_gnd(n) for n in sig]
        if not caps[0] and not caps[1]:
            r.finding("warning", f"{ref} has no load capacitors",
                      "A quartz crystal needs a capacitor from each pin to ground unless the IC provides them internally (some do; its datasheet says).",
                      refs=[ref], nets=sig)
        elif not caps[0] or not caps[1]:
            r.finding("warning", f"{ref} has a load capacitor on one pin only", "", refs=[ref], nets=sig)
        else:
            va, vb = j.value(caps[0][0][0]), j.value(caps[1][0][0])
            if va and vb and abs(va - vb) / max(va, vb) > 0.01:
                r.finding("warning", f"{ref} load capacitors differ: {fmt_si(va, 'F')} and {fmt_si(vb, 'F')}", "They are normally equal.",
                          refs=[ref, caps[0][0][0], caps[1][0][0]])
            for v, c in ((va, caps[0][0][0]), (vb, caps[1][0][0])):
                if v and not 3e-12 <= v <= 50e-12:
                    r.finding("warning", f"{c} is {fmt_si(v, 'F')}, unusual for a crystal load capacitor", "Typical values are 6 pF to 33 pF.", refs=[c, ref])
    r.summary = f"{len(ys)} crystal(s)" if ys else "no crystals"


# ---------------------------------------------------------------------------
@addon(id="check.usb", title="USB connectors", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="USB-C CC pins each have their own 5.1 k resistor, both D+ and both D− pins are joined, the shield goes somewhere.")
def usb(r):
    j = r.job
    found = 0
    for ref in j.of_kind("J"):
        c = j.comps[ref]
        if not re.search(r"USB", c.get("symbol", "") + c.get("footprint", ""), re.I):
            continue
        found += 1
        by_name = {}
        for p in j.pins(ref):
            by_name.setdefault(p.name.upper(), []).append(p.number)
        ns = j.nets_of(ref)
        cc = {nm: ns.get(by_name[nm][0]) for nm in ("CC1", "CC2") if nm in by_name}
        if cc:
            if cc.get("CC1") and cc.get("CC1") == cc.get("CC2"):
                r.finding("error", f"{ref}: CC1 and CC2 are tied together", "With one shared resistor, an e-marked USB-C cable sees the wrong resistance and many chargers supply no power.",
                          refs=[ref], nets=[cc["CC1"]], fix="Give each CC pin its own 5.1 kΩ to ground.")
            for nm, net in cc.items():
                if not net:
                    r.finding("warning", f"{ref}: {nm} is not connected", "A USB-C source only turns VBUS on when it sees 5.1 kΩ to ground on a CC pin. Without it, C-to-C cables give no power.",
                              refs=[ref], fix="5.1 kΩ from each CC pin to ground (for a device that takes power).")
                    continue
                downs = j.pulldowns(net)
                ic = [o for o, _ in j.others(net, ref) if j.kind(o) == "U"]
                if not downs and not ic:
                    r.finding("warning", f"{ref}: {nm} ({net}) has no resistor to ground", "", refs=[ref], nets=[net], fix="5.1 kΩ to ground, one per CC pin.")
                for d, _ in downs:
                    v = j.value(d)
                    if v and abs(v - 5100) / 5100 > 0.06:
                        r.finding("warning", f"{ref}: {nm} pull-down {d} is {fmt_si(v, 'Ω')}", "A USB-C power sink uses 5.1 kΩ.", refs=[ref, d])
        for sig in ("D+", "D-"):
            nums = by_name.get(sig, [])
            got = {ns.get(n) for n in nums}
            if len(nums) > 1 and len(got) > 1:
                r.finding("warning", f"{ref}: the two {sig} pins are on different nets ({', '.join(str(x) for x in got)})",
                          "USB-C has each data pin twice so the plug works both ways up; both must be joined.", refs=[ref])
        for nm in ("SHIELD", "SHLD"):
            for num in by_name.get(nm, []):
                if num not in ns and f"{ref}.{num}" not in (j.spec.get("no_connect") or []):
                    r.finding("note", f"{ref}: shield is not connected", "Usually tied to ground, directly or through 1 MΩ with 4.7 nF in parallel.", refs=[ref])
                    break
    r.summary = f"{found} USB connector(s)" if found else "no USB connectors"


# ---------------------------------------------------------------------------
# Microcontroller families: pins with requirements that are easy to miss. match is tested against symbol + value.
# rule kinds: pullup, pulldown, cap (to ground), tied (must be connected), strap[:high|:low] (level needed at reset),
#             input_only (error if it drives something), avoid (warning if used at all).
MCU_RULES = [
    {"family": "ESP32 (WROOM/WROVER/PICO)", "match": r"ESP32-(WROOM|WROVER|PICO|SOLO)|ESP32-D0WD", "rules": [
        (r"^EN$", "pullup", "warning", "EN needs a pull-up (10 kΩ) or the module never leaves reset."),
        (r"^EN$", "cap", "note", "A capacitor (about 1 µF) from EN to ground delays start-up until the supply is stable."),
        (r"^IO0$", "strap:high", "note", "IO0 is the boot-mode strap: low at reset enters the bootloader. Whatever is on it must not hold it low at power-up."),
        (r"^IO2$", "strap:low", "note", "IO2 is a strap: it must be low or floating to enter the serial bootloader."),
        (r"^IO12$", "strap:low", "note", "IO12 (MTDI) selects the flash voltage at reset. Pulled high at power-up, a 3.3 V module fails to boot."),
        (r"^IO15$", "strap:high", "note", "IO15 is a strap: low at reset silences the boot log."),
        (r"^IO5$", "strap:high", "note", "IO5 is a strap (SDIO timing); it must be high at reset, which its internal pull-up does."),
        (r"^(SENSOR_VP|SENSOR_VN|IO3[4-9])$", "input_only", "error", "GPIO34 to 39 are input only and have no pull resistors."),
        (r"^(SHD/SD2|SWP/SD3|SCS/CMD|SCK/CLK|SDO/SD0|SDI/SD1|IO6|IO7|IO8|IO9|IO10|IO11)$", "avoid", "error",
         "GPIO6 to 11 are wired to the module's own flash; using them crashes it."),
    ]},
    {"family": "ESP32-S3", "match": r"ESP32-S3", "rules": [
        (r"^EN$", "pullup", "warning", "EN needs a pull-up (10 kΩ)."),
        (r"^EN$", "cap", "note", "A capacitor (about 1 µF) from EN to ground delays start-up until the supply is stable."),
        (r"^IO0$", "strap:high", "note", "IO0 is the boot-mode strap: low at reset enters the bootloader."),
        (r"^IO3$", "strap", "note", "IO3 is a strap (JTAG source)."),
        (r"^IO45$", "strap:low", "note", "IO45 selects the flash voltage at reset; it must not be pulled high at power-up."),
        (r"^IO46$", "strap:low", "note", "IO46 is a strap: it must be low to enter the bootloader."),
        (r"^IO(19|20)$", "strap", "note", "IO19 and IO20 are the native USB pins (D− and D+)."),
    ]},
    {"family": "ESP32-C3", "match": r"ESP32-C3", "rules": [
        (r"^EN$", "pullup", "warning", "EN needs a pull-up (10 kΩ)."),
        (r"^(IO|GPIO)2$", "strap:high", "note", "GPIO2 is a strap; it must be high at reset."),
        (r"^(IO|GPIO)8$", "strap:high", "note", "GPIO8 is a strap; it must be high at reset for the bootloader to work."),
        (r"^(IO|GPIO)9$", "strap:high", "note", "GPIO9 is the boot-mode strap: low at reset enters the bootloader."),
    ]},
    {"family": "ESP8266 (ESP-12)", "match": r"ESP-(12|07|01)|ESP8266", "rules": [
        (r"^EN$|^CH_PD$", "pullup", "warning", "EN (CH_PD) needs a pull-up."),
        (r"RST", "pullup", "note", "RST is normally pulled up with 10 kΩ."),
        (r"^GPIO15$", "pulldown", "error", "GPIO15 must be pulled low at reset or the module does not boot."),
        (r"^GPIO0$", "pullup", "warning", "GPIO0 must be high at reset for a normal boot."),
        (r"^GPIO2$", "pullup", "warning", "GPIO2 must be high at reset."),
        (r"^GPIO(9|10)$|^(CS0|MISO|MOSI|SCLK)$", "avoid", "warning", "These pins are wired to the module's flash."),
    ]},
    {"family": "AVR (ATmega/ATtiny)", "match": r"ATmega|ATtiny", "rules": [
        (r"RESET", "pullup", "warning", "RESET has only a weak internal pull-up; 10 kΩ to VCC keeps noise from resetting the chip."),
        (r"^AREF$", "cap", "note", "AREF is normally bypassed with 100 nF when the ADC is used."),
        (r"^AVCC$", "tied", "error", "AVCC must be connected to the supply even when the ADC is unused."),
    ]},
    {"family": "RP2040", "match": r"RP2040", "rules": [
        (r"^RUN$", "pullup", "note", "RUN has an internal pull-up; an external 10 kΩ and a reset button are usual."),
        (r"^QSPI_SS$", "tied", "error", "QSPI_SS must reach the flash chip; the RP2040 has no internal program memory."),
        (r"^VREG_VOUT$", "cap", "error", "VREG_VOUT needs 1 µF to ground and must feed DVDD."),
        (r"^DVDD$", "tied", "error", "DVDD (1.1 V core) must be connected to VREG_VOUT."),
        (r"^TESTEN$", "tied", "warning", "TESTEN should be tied to ground."),
        (r"^XIN$", "tied", "warning", "XIN needs a 12 MHz crystal or clock for USB."),
        (r"^USB_VDD$", "tied", "warning", "USB_VDD must be powered (3.3 V) even if USB is unused."),
    ]},
    {"family": "STM32", "match": r"STM32", "rules": [
        (r"^NRST$", "cap", "warning", "NRST needs 100 nF to ground (it already has an internal pull-up)."),
        (r"^BOOT0$", "pulldown", "error", "BOOT0 floating makes the chip start in the bootloader at random; pull it low (10 kΩ)."),
        (r"^VCAP", "cap", "error", "VCAP pins need their capacitor to ground or the core regulator is unstable."),
        (r"^VDDA$", "tied", "error", "VDDA must be powered even if the ADC is unused."),
        (r"^VBAT$", "tied", "warning", "VBAT should be tied to VDD when no backup battery is used."),
    ]},
]


@addon(id="check.mcu", title="Microcontroller pin rules", kind="check", auto=True, group=GROUP, needs=("spec", "libs"),
       summary="Reset, boot-strap, input-only and flash pins of common microcontrollers (ESP32, ESP8266, AVR, RP2040, STM32).")
def mcu(r):
    j = r.job
    hits = []
    for ref in j.ics():
        c = j.comps[ref]
        text = f"{c.get('symbol', '')} {c.get('value', '')} {c.get('mpn', '')}"
        fam = next((f for f in MCU_RULES if re.search(f["match"], text, re.I)), None)
        if not fam:
            continue
        hits.append(f"{ref} ({fam['family']})")
        ns = j.nets_of(ref)
        nc = set(j.spec.get("no_connect") or [])
        for p in j.pins(ref):
            for pat, kind, sev, why in fam["rules"]:
                if not re.search(pat, p.name, re.I):
                    continue
                net = ns.get(p.number)
                tag = f"{ref} pin {p.number} ({p.name})"
                rest = j.others(net, ref) if net else []
                if kind == "tied":
                    if not net:
                        r.finding(sev, f"{tag} is not connected", why, refs=[ref])
                elif kind == "pullup":
                    if not net or not (j.pullups(net) or (j.voltage(net) or 0) > 0):
                        r.finding(sev, f"{tag} has no pull-up", why, refs=[ref], nets=[net] if net else [])
                elif kind == "pulldown":
                    if not net or not (j.pulldowns(net) or j.is_gnd(net)):
                        r.finding(sev, f"{tag} has no pull-down", why, refs=[ref], nets=[net] if net else [])
                elif kind == "cap":
                    if not net or not j.caps_to_gnd(net):
                        r.finding(sev, f"{tag} has no capacitor to ground", why, refs=[ref], nets=[net] if net else [])
                elif kind.startswith("strap"):
                    want = kind.partition(":")[2]            # level the pin must have at reset, if it has one
                    load = sorted({o for o, _ in rest if j.kind(o) != "TP"})
                    if not net or not load:
                        continue
                    ups, downs = j.pullups(net), j.pulldowns(net)
                    v = j.voltage(net)
                    level = "high" if ups or (v or 0) > 0 else "low" if downs or v == 0 else None
                    if want and level == want:
                        continue                              # held at the level a normal boot needs
                    if want and level:
                        r.finding("warning", f"{tag} is a boot strap held {level} by {_refs(ups or downs) or net}; it must be {want} at reset",
                                  why, refs=[ref] + load, nets=[net])
                    else:
                        r.finding(sev, f"{tag} is a boot strap and is connected to {', '.join(load)}", why, refs=[ref] + load, nets=[net])
                elif kind == "input_only":
                    drives = [o for o, _ in rest if j.kind(o) in ("LED", "Q", "K")]
                    if drives:
                        r.finding(sev, f"{tag} cannot drive {', '.join(sorted(set(drives)))}", why, refs=[ref] + drives, nets=[net])
                elif kind == "avoid":
                    if net and f"{ref}.{p.number}" not in nc:
                        r.finding(sev, f"{tag} is used ({net})", why, refs=[ref], nets=[net])
    r.summary = ("checked " + ", ".join(hits)) if hits else "no microcontroller from the rule table on this board"
    r.data["families"] = [f["family"] for f in MCU_RULES]


# ---------------------------------------------------------------------------
_HEAD_MM = {"2": 2.3, "2.5": 2.75, "3": 3.2, "4": 4.2, "5": 5.0}       # radius a pan-head screw or standoff covers


@addon(id="check.mech", title="Mechanical clearances", kind="check", auto=True, group=GROUP, needs=("spec", "layout"),
       summary="Parts under a screw head or standoff, edge connectors that ended up away from their edge, parts hanging over the outline.")
def mech(r):
    j = r.job
    size = j.size()
    if not size:
        return
    w, h = size
    for hole in j.holes():
        m = re.search(r"_M(\d(?:\.\d)?)", hole.get("lib", ""))
        rad = _HEAD_MM.get(m.group(1) if m else "3", 3.2)
        for ref in j.comps:
            b = j.box(ref)
            if not b:
                continue
            dx = max(b[0] - hole["x"], 0, hole["x"] - b[2])
            dy = max(b[1] - hole["y"], 0, hole["y"] - b[3])
            d = math.hypot(dx, dy)
            if d < rad:
                r.finding("warning", f"{ref} is under the screw head at {hole['ref']}",
                          f"Its outline is {d:.1f} mm from the hole centre; an M{m.group(1) if m else '3'} pan head or standoff covers about {rad:g} mm.",
                          refs=[ref], fix="Move the part or the hole.")
    edge_of = {"left": lambda b: b[0], "right": lambda b: w - b[2], "top": lambda b: b[1], "bottom": lambda b: h - b[3]}
    for ref, c in j.comps.items():
        b = j.box(ref)
        if not b:
            continue
        if c.get("symbol", "").startswith("RF_Module"):
            pass            # its outline includes the antenna keep-out, which is meant to hang over the edge
        elif b[0] < -0.05 or b[1] < -0.05 or b[2] > w + 0.05 or b[3] > h + 0.05:
            over = max(-b[0], -b[1], b[2] - w, b[3] - h)
            if c.get("edge") or j.kind(ref) == "J":
                r.finding("note", f"{ref} overhangs the board edge by {over:.1f} mm", "Normal for an edge connector; check it against the enclosure.", refs=[ref])
            else:
                r.finding("warning", f"{ref} overhangs the board edge by {over:.1f} mm", "", refs=[ref])
        e = c.get("edge")
        if e in edge_of:
            d = edge_of[e](b)
            if d > 3.0:
                r.finding("warning", f"{ref} is meant for the {e} edge but sits {d:.1f} mm inside it",
                          "A connector set back from the edge cannot be reached through an enclosure wall.", refs=[ref])
    r.summary = f"{len(j.holes())} mounting hole(s), {sum(1 for c in j.comps.values() if c.get('edge'))} edge part(s)"


# ---------------------------------------------------------------------------
_HARD = [
    (r"BGA|WLCSP|CSP", 4, "needs reflow and usually X-ray inspection; not hand solderable"),
    (r"LGA|QFN|DFN|SON|MLF|UDFN|WSON|VQFN|TQFN|LCC|_EP|ThermalVias|PowerPAD", 3, "pads under the body: hot air or reflow"),
    (r"0201|_01005|0603Metric", 3, "0201: too small for most hand work"),
    (r"0402|1005Metric|SOT-(5|6|7|8|9)\d\d|SC-70|SOT-323|SOT-363|SOT-563|X2SON", 2, "small: fine tip and magnification"),
    (r"TSSOP|MSOP|SSOP|LQFP|TQFP|QFP|P0\.5mm|P0\.4mm|P0\.65mm", 2, "fine pitch: flux and drag soldering"),
]
_LEVEL = {0: "easy", 1: "easy", 2: "moderate", 3: "hot air or reflow", 4: "reflow only"}


@addon(id="check.handsolder", title="Hand-solderability", kind="check", auto=True, group=GROUP,
       summary="How hard the board is to build by hand: the packages that need hot air or reflow, and parts on both sides.")
def handsolder(r):
    j = r.job
    worst = 0
    groups = {}
    for ref, c in j.comps.items():
        fp = c.get("footprint", "").split(":")[-1]
        for pat, lvl, why in _HARD:
            if re.search(pat, fp, re.I):
                groups.setdefault((lvl, why, fp), []).append(ref)
                worst = max(worst, lvl)
                break
    for (lvl, why, fp), refs in sorted(groups.items(), reverse=True):
        r.finding("warning" if lvl >= 4 else "note", f"{', '.join(refs[:8])}{' ...' if len(refs) > 8 else ''}: {fp}", why[0].upper() + why[1:] + ".", refs=refs)
    back = [p["ref"] for p in j.layout.get("parts", []) if p.get("back")]
    if back:
        r.finding("note", f"{len(back)} part(s) on the bottom side", "Two-sided assembly: two reflow passes, or hand work on one side.", refs=back)
    r.summary = f"{_LEVEL[worst]}" + (f" ({len(groups)} package type(s) to watch)" if groups else ": every package can be soldered with an iron")
    r.data["level"] = worst


# ---------------------------------------------------------------------------
@addon(id="check.buttons", title="Buttons and switches", kind="check", auto=False, group=GROUP, status="beta",
       summary="Switches whose signal line has no resistor holding it when the switch is open (relies on an internal pull).")
def buttons(r):
    j = r.job
    n = 0
    for ref in j.of_kind("SW"):
        nets_ = set(j.nets_of(ref).values())
        rails = {x for x in nets_ if j.voltage(x) is not None}
        sig = nets_ - rails
        if len(rails) != 1 or len(sig) != 1:
            continue
        n += 1
        net, rail = next(iter(sig)), next(iter(rails))
        want_up = j.is_gnd(rail)
        has = j.pullups(net) if want_up else j.pulldowns(net)
        if not has:
            ics = sorted({o for o, _ in j.others(net, ref) if j.kind(o) == "U"})
            r.finding("note", f"{ref} switches {net} to {rail} with no external pull-{'up' if want_up else 'down'}",
                      (f"Works if {', '.join(ics)} enables its internal pull-{'up' if want_up else 'down'} in firmware." if ics
                       else "Nothing defines the level when the switch is open."), refs=[ref] + ics, nets=[net])
    r.summary = f"{n} switch(es) to a rail"


_AUTO_NET = re.compile(r"^/?(Net-\(|N\$|NET\d|N\d+$|unconnected-)", re.I)
_GENERIC_VALUE = re.compile(r"^(R|C|L|D|LED|U|IC|Q|J|CONN|SW|~|\?)?$", re.I)


@addon(id="check.readable", title="Schematic readability", kind="check", auto=True, group=GROUP,
       summary="What makes a schematic hard to review: unnamed nets on connector pins, parts with no value, "
               "ICs and modules with no part number, connector pins with generic names.")
def readable(r):
    j = r.job
    conns = [ref for ref in j.comps if re.match(r"(J|P|CN|CON|X)\d", ref)]
    for ref in conns:
        bad = sorted({n for n, pins in j.nets.items() if any(p.split(".")[0] == ref for p in pins) and _AUTO_NET.match(n)})
        if bad:
            r.finding("note", f"{ref} has pins on unnamed nets: {', '.join(bad[:6])}",
                      "A connector pin is where someone plugs in a cable; the net name is the only label they get.",
                      refs=[ref], nets=bad, fix="Give each connector net a name that says what it carries (SDA, VBAT, MOTOR_A).")
    novalue = [ref for ref, c in j.comps.items() if _GENERIC_VALUE.match(str(c.get("value", "")).strip())]
    if novalue:
        r.finding("note", f"Parts with no real value: {', '.join(novalue[:12])}",
                  "Value is what a reviewer reads first (10k, 100nF, NE555).", refs=novalue,
                  fix="Set each part's value.")
    nompn = [ref for ref in j.ics() if not (j.comps[ref].get("mpn") or j.comps[ref].get("MPN"))
             and not re.search(r"\d", str(j.comps[ref].get("value", "")))]
    if nompn:
        r.finding("note", f"ICs with no part number: {', '.join(nompn[:12])}",
                  "Without an exact part number nobody can check the pinout against the datasheet.", refs=nompn,
                  fix="Put the manufacturer part number in the value or the mpn field.")
