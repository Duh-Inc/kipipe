"""Fast checks of the logic that has broken before. No model calls, no routing: a few seconds in total.
They need the KiCad symbol and footprint libraries (any version from 7 on).

    python3 -m pytest tests -q
"""
import copy
import json
from pathlib import Path

from conftest import FIX, HOME, spec


def _findings(path_or_spec, tmp_path, only=None):
    from kipipe import addons
    if isinstance(path_or_spec, dict):
        f = tmp_path / "spec.json"
        f.write_text(json.dumps(path_or_spec))
        path_or_spec = f
    out = addons.run_checks(path_or_spec, only_auto=False)
    assert not out["broken"], f"checks crashed: {out['broken']}"
    return [(c["id"], f["severity"], f["title"]) for c in out["checks"] for f in c["findings"] if only is None or c["id"] == only]


# ---- KiCad library handling ----
def test_stacked_pin_numbers_expand():
    from kipipe import kicad_lib as kl
    assert kl.expand_pin_number("[1,15,38,39]") == ["1", "15", "38", "39"]
    assert kl.expand_pin_number("[A1-A3,B2]") == ["A1", "A2", "A3", "B2"]
    assert kl.expand_pin_number("7") == ["7"]


def test_stacked_pins_join_one_net(k10_lib):
    from kipipe import spec as sm
    s = {"name": "k10", "board": {}, "components": [
        {"ref": "U1", "symbol": k10_lib, "footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "value": "K10MOD"},
        {"ref": "J1", "symbol": "Connector_Generic:Conn_01x03", "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical", "value": "IO"}],
        "nets": {"+3V3": ["U1.1", "J1.1"], "GND": ["U1.4", "J1.2"], "OUT": ["U1.3", "J1.3"]}, "no_connect": ["U1.7", "U1.2"]}
    rep = sm.validate(sm.normalize(s))
    assert rep.ok, rep.errors
    assert {"U1.4", "U1.5", "U1.6", "U1.8"} <= set(s["nets"]["GND"])     # the four pads of the one drawn pin


def test_symbol_that_moved_library_is_found():
    from kipipe import spec as sm
    assert sm._relocated("Device:AO3400A") == "Transistor_FET:AO3400A"
    assert sm._relocated("Device:NoSuchSymbolAnywhere") is None


def test_generic_symbol_list_only_names_what_exists():
    from kipipe import kicad_lib as kl, llm
    for lid in llm._generic_symbols().split(", "):
        kl.get_symbol(lid)                                                # raises if the prompt would name a missing symbol


# ---- blocks ----
def test_every_builtin_block_validates_here():
    from kipipe.addons import blocks
    bad = {k: blocks.check(b) for k, b in blocks.library().items() if b.get("_source") == "built in" and blocks.check(b)}
    assert not bad, bad


def test_blocks_expand_in_a_spec():
    from kipipe import spec as sm
    s = spec("blocks_usb_3v3.json")
    rep = sm.validate(sm.normalize(s))
    assert rep.ok, rep.errors
    assert len(s["components"]) >= 9 and "blocks" not in s and len(s["blocks_used"]) == 3


# ---- the correction loop must not let a fix redesign the board ----
class _Fake:
    stateless = True

    def __init__(self, name, replies):
        self.name, self.replies, self.model, self.kind = name, list(replies), name, "fake"

    def ask(self, prompt, system=None, **kw):
        self.last = prompt
        return self.replies.pop(0)


def test_a_fix_that_shrinks_the_design_is_thrown_away(tmp_path, monkeypatch):
    from kipipe import llm
    from kipipe.pipeline import Options
    good = spec("stack_base.json")
    bad = copy.deepcopy(good)
    bad["components"][2]["footprint"] = "Resistor_SMD:R_0805_NOPE"
    tiny = {"name": "x", "board": {}, "components": [bad["components"][2]], "nets": {"A": ["R1.1"], "B": ["R1.2"]}}
    light, strong = _Fake("light", [json.dumps(tiny)]), _Fake("strong", [json.dumps(good)])

    class Pool:
        def get(self, role):
            return light if role == "fix" else strong

        def fallback(self, role):
            return strong
    monkeypatch.setattr(llm, "_who", lambda b: getattr(b, "name", "?"))
    (tmp_path / "input").mkdir()
    out = llm._spec_loop(Pool(), _Fake("designer", []), Options(), tmp_path, json.dumps(bad), lambda m: None)
    assert len(out["components"]) == len(good["components"])
    assert '"J1"' in strong.last                                          # the stronger model was given the full design


# ---- ERC ----
def _erc_metrics(spec_dict, tmp_path):
    from kipipe.pipeline import Pipeline, Options, StageError
    f = tmp_path / "in.json"
    f.write_text(json.dumps(spec_dict))
    o = Options(spec_file=str(f), flags=set())
    p = Pipeline(o, tmp_path / "job", log=lambda m: None)
    p.get_spec()
    p.schematic()
    try:
        p.erc()
    except StageError:
        pass                                                              # errors still stop the run; metrics are set first
    return p.metrics["erc"]


def test_orring_power_controllers_do_not_trip_erc(tmp_path):
    """Two LM66100 ideal-diode controllers with their VOUT tied to one net is a deliberate power-OR, not a
    conflict: KiCad's ERC otherwise reports any two power_out pins on a net as two drivers fighting over it."""
    s = {"name": "or_ok", "board": {"mounting_holes": "none"}, "components": [
        {"ref": "U1", "symbol": "Power_Management:LM66100DCK", "footprint": "Package_TO_SOT_SMD:SOT-363_SC-70-6", "value": "LM66100DCKR", "mpn": "LM66100DCKR"},
        {"ref": "U2", "symbol": "Power_Management:LM66100DCK", "footprint": "Package_TO_SOT_SMD:SOT-363_SC-70-6", "value": "LM66100DCKR", "mpn": "LM66100DCKR"}],
        "nets": {"VIN_A": ["U1.1"], "VIN_B": ["U2.1"], "+5V": ["U1.6", "U2.6"], "GND": ["U1.2", "U2.2"]},
        "no_connect": ["U1.3", "U1.4", "U1.5", "U2.3", "U2.4", "U2.5"]}
    assert _erc_metrics(s, tmp_path)["errors"] == 0


def test_two_real_regulators_tied_together_still_fails_erc(tmp_path):
    """The same net-level pattern (two power_out pins on one net) with ordinary regulators, not a recognised
    OR-ing part, must still stop the run: this is the real two-driver conflict the ERC rule exists to catch."""
    s = {"name": "or_bad", "board": {"mounting_holes": "none"}, "components": [
        {"ref": "U1", "symbol": "Regulator_Linear:L7805", "footprint": "Package_TO_SOT_THT:TO-220-3_Vertical", "value": "L7805", "mpn": "L7805CV"},
        {"ref": "U2", "symbol": "Regulator_Linear:L7805", "footprint": "Package_TO_SOT_THT:TO-220-3_Vertical", "value": "L7805", "mpn": "L7805CV"}],
        "nets": {"VIN_A": ["U1.1"], "VIN_B": ["U2.1"], "+5V": ["U1.3", "U2.3"], "GND": ["U1.2", "U2.2"]}}
    assert _erc_metrics(s, tmp_path)["errors"] == 1


# ---- design checks ----
def test_rule_checks_catch_the_planted_mistakes(tmp_path):
    titles = " | ".join(t for _, _, t in _findings(spec("rules_bad.json"), tmp_path))
    for needle in ("CC1 and CC2 are tied together", "has no pull-up resistor", "is backwards", "with no resistor", "load capacitor on one pin"):
        assert needle in titles, needle


def test_rule_checks_stay_quiet_on_a_sound_board(tmp_path):
    f = _findings(spec("lt1175_bipolar.json"), tmp_path)
    assert not [x for x in f if x[1] == "error" and not x[0].startswith("part.")], f


def test_datasheet_checks_catch_the_planted_mistakes(tmp_path):
    titles = " | ".join(t for i, s, t in _findings(spec("datasheet_bad.json"), tmp_path) if i.startswith("part."))
    for needle in ("outside the absolute maximum", "input pin(s) left floating", "no I2C pull-up", "is too small", "works out to 7.6 V"):
        assert needle in titles, needle


def test_datasheet_checks_pass_the_sound_board(tmp_path):
    f = _findings(spec("datasheet_good.json"), tmp_path)
    assert not [x for x in f if x[0].startswith("part.") and x[1] == "error"], f
    assert any("matches net -5V" in t for _, _, t in f)                   # the LT1175 divider equation was evaluated


def test_fixed_regulator_on_the_wrong_rail(tmp_path):
    from kipipe.addons import partchecks
    assert partchecks._fixed_output("AMS1117-3.3") == 3.3 and partchecks._fixed_output("LM7805") == 5.0
    assert partchecks._fixed_output("AMS1117-ADJ") is None


# ---- stacking geometry ----
def test_mirrored_connector_is_renumbered():
    from kipipe import derive
    hdr = derive._local_pads("Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical")
    target = [(n, f"N{n}", 20 + x, 10 + y) for n, x, y in hdr]           # a header sitting at (20, 10) on the lower board
    rot, x, y, over = derive.align(hdr, target, back=True)                # the same header on the underside of the upper board
    assert sum(1 for a, b in over.items() if a != b) >= 8                 # mirrored: most pads meet a different number
    sock = derive._local_pads("Connector_PinSocket_2.54mm:PinSocket_2x05_P2.54mm_Vertical")
    _, _, _, over2 = derive.align(sock, target, back=True)                # KiCad draws sockets mirrored, so they mate pin for pin
    assert all(a == b for a, b in over2.items())
    assert derive.align(hdr[:6], target, back=True) is None               # a different pad pattern cannot mate


# ---- small pure functions ----
def test_net_voltage_from_names():
    from kipipe.addons.core import net_voltage
    assert net_voltage("+3V3") == 3.3 and net_voltage("-5V") == -5 and net_voltage("GND") == 0 and net_voltage("VBUS") == 5
    assert net_voltage("SDA") is None and net_voltage("VBAT") is None


def test_brief_check_notices_what_is_missing():
    from kipipe.addons import aids
    topics = {q["topic"] for q in aids.brief("ESP32 board driving a relay and a servo motor")}
    assert {"power", "load", "programming"} <= topics
    assert aids.brief("hi") == []


def test_datasheet_link_rejects_non_pdf(tmp_path):
    from kipipe import partdb
    assert partdb.is_url("https://example.com/x.pdf") and not partdb.is_url("/home/me/x.pdf")


# ---- schematic readability ----

def test_long_wires_become_labels_and_netlist_still_matches(tmp_path):
    import shutil
    import subprocess
    from kipipe import kicad_lib as kl, schgen, schwire
    from kipipe import spec as specmod
    s = specmod.normalize(spec("datasheet_good.json"))
    s["schematic"] = {"max_wire_mm": 8}                 # force most connections onto labels
    text, info = schwire.generate(s, "t")
    sch = tmp_path / "t.kicad_sch"
    sch.write_text(text)
    net = tmp_path / "t.net"
    env = kl.detect()
    subprocess.run([*env.cli_cmd, "sch", "export", "netlist", "-o", str(net), str(sch)], capture_output=True, check=True)
    ok, problems = schgen.topology_ok(s, net.read_text())
    assert ok, problems
    s["schematic"] = {"max_wire_mm": 1000}
    assert info["labels"] > schwire.generate(s, "t")[1]["labels"]


def test_readability_check_flags_unnamed_connector_nets(tmp_path):
    s = copy.deepcopy(spec("datasheet_good.json"))
    j = next(c for c in s["components"] if c["ref"].startswith("J"))
    name, pins = next((n, p) for n, p in s["nets"].items() if any(x.startswith(j["ref"] + ".") for x in p))
    s["nets"]["Net-(J1-Pad1)"] = s["nets"].pop(name)
    found = _findings(s, tmp_path, only="check.readable")
    assert any("unnamed" in t for _, _, t in found), found
