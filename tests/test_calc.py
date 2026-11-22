"""Calculators: fixed answers for fixed goals."""
import pytest

from kipipe import calc


def test_led_resistor_rounds_up_so_current_stays_under_target():
    r = calc.led_resistor(3.3, 2.0, 0.005)
    assert r["r"] == 270 and r["i"] <= 0.005


def test_divider_and_feedback_land_close():
    d = calc.divider(4.2, 1.0)
    assert abs(d["err"]) < 0.01
    f = calc.feedback(5.0, 0.596)
    assert abs(f["vout"] - 5.0) / 5.0 < 0.01


def test_i2c_pullups_match_common_practice():
    assert calc.i2c_pullup(3.3, 100)["r"] == 4700
    assert 967 <= calc.i2c_pullup(3.3, 400)["r"] <= 2360


def test_crystal_caps():
    assert calc.crystal_caps(12)["c"] == pytest.approx(18e-12)


def test_snap_and_format():
    assert calc.snap(260, "E24", "up") == 270
    assert calc.snap(4.6e3, "E12") == 4700
    assert [calc.fmt(v) for v in (4700, 1e-7, 22e-12, 1e6)] == ["4.7k", "100n", "22p", "1M"]


def test_apply_fills_values_and_is_repeatable():
    def spec():
        return {"components": [{"ref": "R1", "value": "?"}, {"ref": "D1", "value": "LED red"},
                               {"ref": "C1", "value": "? 50V C0G"}, {"ref": "C2", "value": "?"}],
                "nets": {"+3V3": ["R1.1"], "A": ["R1.2", "D1.2"], "GND": ["D1.1"]},
                "calc": [{"kind": "led", "resistor": "R1", "led": "D1", "current_ma": 2},
                         {"kind": "crystal", "caps": ["C1", "C2"], "load_pf": 8}]}
    a, b = spec(), spec()
    calc.apply(a)
    calc.apply(b)
    vals = {c["ref"]: c["value"] for c in a["components"]}
    assert vals == {"R1": "680", "D1": "LED red", "C1": "10p 50V C0G", "C2": "10p"}
    assert a["components"] == b["components"]


def test_bad_entry_becomes_a_report_error():
    class R:
        errors = []
    s = {"components": [{"ref": "R1", "value": "?"}], "nets": {}, "calc": [{"kind": "led", "resistor": "R1"}]}
    calc.apply(s, R)
    assert R.errors and "missing" in R.errors[0]
