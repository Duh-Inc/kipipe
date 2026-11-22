"""Design aids: things that help before the run (brief check, starters) and around it (assumptions and
requirements trace, questions about a finished board)."""
from __future__ import annotations

import json
import re

from . import blocks  # noqa: F401  (registers aid.saveblock)
from .core import addon, run_checks
from .draw import bom, natkey

# ---------------------------------------------------------------------------
# Brief check: what a request leaves open, found with plain pattern matching before any model is called.
_NUM_V = r"\d+(\.\d+)?\s*(v|volt)"
BRIEF = [
    ("power", "Where does power come from, and at what voltage?",
     "Without it the model picks a supply for you (often 5 V USB).", "Power: ",
     lambda t: not re.search(r"usb|batter|barrel|lipo|li-ion|18650|coin cell|cr2032|solar|mains|poe|supply|powered|adapter|wall wart|" + _NUM_V, t)),
    ("load", "How much current do the loads draw?",
     "Motors, LED strips, relays and heaters decide the regulator, the track widths and the connectors.", "Load current: ",
     lambda t: re.search(r"motor|strip|ws2812|neopixel|relay|heater|servo|solenoid|pump|fan|speaker|amplifier|peltier|stepper", t)
     and not re.search(r"\d+(\.\d+)?\s*(a|ma|amp|amps|w|watt|watts)\b", t)),
    ("connectors", "How does it connect to the outside: which connectors, on which edges?",
     "Otherwise connector types and positions are the model's guess.", "Connectors: ",
     lambda t: not re.search(r"connector|header|terminal|jack|usb|jst|plug|socket|pads|qwiic|stemma|grove|screw", t)),
    ("programming", "How will the microcontroller be programmed?",
     "USB, a serial header or a debug header: each needs different parts.", "Programming: ",
     lambda t: re.search(r"esp32|esp8266|stm32|atmega|attiny|rp2040|pico|microcontroller|\bmcu\b|arduino|nrf52|samd", t)
     and not re.search(r"program|usb|uart|serial|swd|isp|icsp|jtag|bootloader|ftdi|ch340|cp210|debug|ota|updi", t)),
    ("size", "Is there a size limit or a case it must fit?",
     "The board is sized from its parts unless you say otherwise.", "Size: ",
     lambda t: not re.search(r"\d+\s*(mm|cm|in|inch|\")|x\s*\d+|compact|small|tiny|fit|enclosure|case|hat|shield|feather|form factor|mount", t)),
    ("assembly", "Who assembles it: by hand with an iron, or a fab's assembly service?",
     "Hand assembly wants 0805 and leaded packages; an assembly service wants parts it stocks.", "Assembly: ",
     lambda t: not re.search(r"hand|solder|0805|0603|1206|through.hole|tht|smd|smt|jlc|pcba|assembl|reflow", t)),
    ("levels", "Do all parts run at the same logic voltage?",
     "Mixing 5 V and 3.3 V parts needs level shifting, which is easy to forget.", "Logic level: ",
     lambda t: re.search(r"\b5\s*v\b", t) and re.search(r"3\.3\s*v|3v3|esp32|rp2040|stm32|nrf52", t) and not re.search(r"level|shift|tolerant|divider", t)),
    ("environment", "Will it be outdoors, in a vehicle or near mains?",
     "Automotive, outdoor and mains-adjacent boards need protection the model does not add unasked.", "Environment: ",
     lambda t: re.search(r"car\b|vehicle|automotive|12\s*v batter|outdoor|garden|rain|mains|230\s*v|120\s*v|ac line|boat|marine|industrial", t)
     and not re.search(r"tvs|fuse|protect|isolat|ip6\d|conformal|surge|transient", t)),
]


def brief(prompt: str) -> list:
    """Questions a request leaves open. [{topic, question, why, stub}] where stub is text to append to the prompt."""
    t = " " + (prompt or "").lower() + " "
    if len(t.strip()) < 12:
        return []
    out = []
    for topic, q, why, stub, test in BRIEF:
        try:
            if test(t):
                out.append({"topic": topic, "question": q, "why": why, "stub": stub})
        except re.error:
            pass
    return out[:5]


STARTERS = [
    {"title": "Sensor node", "tags": ["esp32", "i2c", "battery"],
     "prompt": "ESP32-WROOM-32 sensor node powered from a single LiPo cell through a JST-PH connector, 3.3 V LDO, BME280 on I2C with pull-ups, "
               "one status LED, reset and boot buttons, 4-pin UART header for programming. Hand soldered, 0805 passives. As small as practical, M2.5 mounting holes."},
    {"title": "USB-C breakout with 3.3 V", "tags": ["usb", "power"],
     "prompt": "USB-C power input (5 V sink, 5.1k on each CC pin) feeding an AMS1117-3.3 regulator. Outputs 5 V, 3.3 V and GND on a 3-pin 2.54 mm header on the right edge. "
               "Power LED on the 3.3 V rail. Hand soldered. Under 30 x 25 mm."},
    {"title": "Relay board", "tags": ["relay", "driver"],
     "prompt": "Two-channel 5 V relay board: each relay driven by a logic-level N-MOSFET with flyback diode and an indicator LED, 3.3 V logic inputs on a 4-pin header "
               "(IN1, IN2, 5V, GND) on the left edge, relay contacts (COM, NO, NC) on 5.08 mm screw terminals on the right edge. Relay coils draw 80 mA each. Through-hole relays, 0805 passives."},
    {"title": "LED strip controller", "tags": ["led", "mcu"],
     "prompt": "ATtiny85 driving a WS2812 strip from a 5 V barrel jack, 3 A supply, 1000 µF bulk capacitor at the strip connector, 330 ohm series resistor on the data line, "
               "push button on PB3, 6-pin ISP programming header, 3-pin screw terminal for the strip on the right edge. Hand soldered, DIP-8 socket for the ATtiny."},
    {"title": "Dual-rail op-amp supply", "tags": ["analog", "power"],
     "prompt": "±5 V linear supply for op-amp circuits from two 9 V batteries (centre tap is ground): positive and negative LDO, 300 mA per rail, reverse-polarity protection on both inputs, "
               "power LED per rail, 3-pin screw terminals for input (left) and output (right). 2-layer, hand soldered."},
    {"title": "Motor driver", "tags": ["motor", "driver"],
     "prompt": "DRV8833 dual H-bridge board for two small DC motors (1 A each) from a 2-cell LiPo (7.4 V) on an XT30 connector, logic inputs AIN1/AIN2/BIN1/BIN2 and nSLEEP on a 6-pin header, "
               "motor outputs on two 2-pin screw terminals, bulk capacitor at the supply, fault LED. Assembled by a fab service."},
    {"title": "Audio amplifier", "tags": ["audio", "analog"],
     "prompt": "Mono class-D amplifier with a PAM8302 from 5 V USB-C (power only), 3.5 mm jack input with a volume potentiometer, speaker on a 2-pin screw terminal (4 ohm, 2.5 W), "
               "shutdown jumper, power LED. Hand soldered, 0805 passives, 40 x 30 mm."},
    {"title": "RP2040 minimal board", "tags": ["rp2040", "usb"],
     "prompt": "Minimal RP2040 board: USB-C for power and data, 3.3 V LDO, W25Q16 flash, 12 MHz crystal with load capacitors, BOOTSEL and RESET buttons, all GPIO on two 20-pin 2.54 mm headers, "
               "user LED on GPIO25, SWD on a 3-pin header. Assembled by a fab service; 0402 passives are fine. 2-layer."},
]


# ---------------------------------------------------------------------------
@addon(id="check.requirements", title="Requirements and assumptions", kind="check", auto=True, group="Design checks", status="beta",
       summary="What the designer model said it assumed, and whether each thing you asked for is tied to parts that exist on the board.")
def requirements(r):
    j = r.job
    ass = [str(a) for a in j.spec.get("assumptions") or [] if str(a).strip()]
    reqs = [q for q in j.spec.get("requirements") or [] if isinstance(q, dict) and q.get("text")]
    for a in ass:
        r.finding("note", "Assumed: " + a, "The request did not say; the model chose. Correct it with Revise if it is wrong.")
    met = 0
    inst = {b.get("name"): (b.get("refs") or {}).values() for b in j.spec.get("blocks_used") or []}
    for q in reqs:
        status = str(q.get("status") or "met").lower()
        by = []
        for x in q.get("met_by") or []:          # a block instance name stands for the parts it placed
            by += list(inst[str(x)]) if str(x) in inst else [str(x)]
        refs = [x for x in by if x in j.comps]
        nets_ = [x.replace("net ", "") for x in by if x.replace("net ", "") in j.nets]
        ghost = [x for x in by if x not in j.comps and x.replace("net ", "") not in j.nets]
        if status in ("not met", "unmet", "no"):
            r.finding("warning", "Not met: " + q["text"], str(q.get("note") or "The model reports it could not satisfy this."), refs=refs, nets=nets_)
        elif status in ("partly", "partial"):
            r.finding("warning", "Partly met: " + q["text"], str(q.get("note") or ""), refs=refs, nets=nets_)
        elif ghost:
            r.finding("warning", "Claimed but not traceable: " + q["text"], f"It points at {', '.join(ghost)}, which is not on the board.", refs=refs)
        elif not by:
            r.finding("note", "No parts named for: " + q["text"], "The model marked it met without saying which parts do it.")
        else:
            met += 1
    if not ass and not reqs:
        r.summary = "this spec carries no assumptions or requirements list (hand-written spec or an older run)"
    else:
        r.summary = f"{met} of {len(reqs)} requirement(s) traced to parts, {len(ass)} assumption(s)"
    r.data.update(assumptions=ass, requirements=reqs)


# ---------------------------------------------------------------------------
ASK_SYSTEM = """You answer questions about one finished PCB design. Everything you know about it is in the message:
the spec (parts, nets by pin), the bill of materials, rule-check findings and the designer's own notes.
Rules: answer from that data only. Name parts by reference designator and nets by name. If the data does not
contain the answer (a datasheet figure that is not given, how firmware behaves), say so plainly and say what would
settle it. Show any arithmetic. Be brief: a few sentences, or a short list when the question asks for one."""


def ask_context(j) -> str:
    spec = {k: v for k, v in j.spec.items() if k in ("name", "description", "board", "netclasses", "rationale", "assumptions", "requirements")}
    lines = ["DESIGN", json.dumps(spec, ensure_ascii=False), "", "BILL OF MATERIALS"]
    lines += [f"{g['qty']}x {g['value']} [{g['footprint']}] {g['mpn']}: {' '.join(g['refs'])}" for g in bom(j)]
    lines += ["", "NETS (net: REF.PIN(pin name) ...)"]
    for n in sorted(j.nets, key=natkey):
        lines.append(f"{n}: " + " ".join(p + (f"({j.pin_name(*p.split('.', 1))})" if j.pin_name(*p.split(".", 1)) not in ("", "~") else "") for p in j.nets[n]))
    try:
        chk = run_checks(j)
        fs = [f"[{f['severity']}] {f['title']}. {f['detail']}" for c in chk["checks"] for f in c["findings"]]
        lines += ["", "RULE-CHECK FINDINGS"] + (fs or ["none"])
    except Exception:
        pass
    m = j.result.get("metrics") or {}
    if m:
        lines += ["", "BUILD RESULT", json.dumps({k: m[k] for k in ("board", "routing", "drc") if k in m}, default=str)[:1500]]
    try:
        from .. import partdb
        hint = partdb.prompt_hint("", j.spec)
        if hint:
            lines += ["", "PART DATABASE RECORDS", hint[:6000]]
    except Exception:
        pass
    return "\n".join(lines)


@addon(id="aid.ask", title="Ask about this board", kind="aid", group="Design aids", status="beta", needs=("spec", "model"),
       options=[{"key": "question", "label": "Question", "type": "text", "default": ""}],
       summary="Ask a question in plain words (why is D3 there, what does J2 pin 3 carry, what limits the output current). A small model answers from this board's own data.")
def ask(r):
    q = str(r.opts.get("question") or "").strip()
    if not q:
        r.summary = "type a question"
        return
    from .. import llm
    from ..pipeline import load_options
    j = r.job
    o = r.opts.get("_options") or load_options(j.dir)
    be = llm.Pool(o, j.dir, r.log).get("ask")
    if hasattr(be, "session"):
        be.session = None
    kw = {"tools": []} if isinstance(be, llm.ClaudeCode) else {}
    out = be.ask(ask_context(j) + "\n\nQUESTION\n" + q, ASK_SYSTEM, **kw).strip()
    r.out.mkdir(parents=True, exist_ok=True)
    log = r.out / "questions.md"
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"## {q}\n\n{out}\n\n")
    r.attach(log, "Questions and answers so far")
    r.data.update(question=q, answer=out, model=llm._who(be))
    r.summary = out[:300]
