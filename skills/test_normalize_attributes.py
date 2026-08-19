#!/usr/bin/env python3
"""The distributor attribute normalizer, checked against each API's real shape.

Four resolver scripts each carry their own copy of ``_normalize_attributes``.
That duplication is deliberate — the skill scripts are standalone by design and
have no shared library to import from — but duplication drifts. These tests load
all four copies and assert they agree, so a fix applied to one and forgotten in
the others fails here rather than in somebody's bill of materials.

What the normalizer is for: a BOM groups parts for ordering by their electrical
attributes, so where an attribute came from decides how much weight it carries.
A rating a distributor states about a specific MPN is worth more than one typed
into a design document. Anything the normalizer cannot read confidently it
declines to emit — a missing attribute is recoverable, a wrong one is not.

Run directly (``python3 skills/test_normalize_attributes.py``) or under pytest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parent
RESOLVERS = ("mouser", "digikey", "lcsc", "element14")


def _load(name):
    path = SKILLS / name / "scripts" / f"fetch_datasheet_{name}.py"
    spec = importlib.util.spec_from_file_location(f"_res_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # a script that argparses at import time
        pass
    return mod


MODULES = {n: _load(n) for n in RESOLVERS}
# Any copy will do for shape tests; the drift test below proves they agree.
norm = MODULES["mouser"]._normalize_attributes


# -- the shapes each API actually returns ------------------------------------

def test_mouser_product_attributes():
    got = norm({"ProductAttributes": [
        {"AttributeName": "Capacitance", "AttributeValue": "100 nF"},
        {"AttributeName": "Voltage Rating DC", "AttributeValue": "50 VDC"},
        {"AttributeName": "Tolerance", "AttributeValue": "±10 %"},
        {"AttributeName": "Dielectric", "AttributeValue": "X7R"},
    ]})
    assert got == {"value": "100 nF", "voltage_v": 50.0,
                   "tolerance": 10.0, "dielectric": "X7R"}


def test_digikey_parameters():
    got = norm({"Parameters": [
        {"ParameterText": "Voltage - Rated", "ValueText": "50V"},
        {"ParameterText": "Tolerance", "ValueText": "±10%"},
        {"ParameterText": "Temperature Coefficient", "ValueText": "X7R"},
    ]})
    assert got["voltage_v"] == 50.0 and got["dielectric"] == "X7R"


def test_element14_attributes():
    got = norm({"attributes": [
        {"attributeLabel": "Power Rating", "attributeValue": "1/16 W"},
        {"attributeLabel": "Resistance Tolerance", "attributeValue": "± 1%"},
    ]})
    assert got == {"power_w": 0.0625, "tolerance": 1.0}


def test_lcsc_param_vo_list():
    got = norm({"paramVOList": [
        {"paramNameEn": "Voltage Rated", "paramValueEn": "305VAC"},
        {"paramNameEn": "Safety Class", "paramValueEn": "Y2"},
    ]})
    assert got == {"voltage_v": 305.0, "safety_class": "Y2"}


# -- what it declines to say -------------------------------------------------

def test_an_unrecognised_shape_yields_nothing_rather_than_raising():
    """A distributor renaming a field must cost us attributes, not the lookup.
    The datasheet is the reason the script runs; attributes are a bonus."""
    assert norm({"SomethingNew": [{"foo": "bar"}]}) == {}
    assert norm({}) == {}
    assert norm(None) == {}
    assert norm({"Parameters": "not a list"}) == {}


def test_a_capacitor_class_1_is_not_a_safety_class():
    """'Class 1' on a ceramic capacitor is its temperature classification, not
    an X/Y mains-safety rating. Reading it as the latter would let a part be
    grouped with a genuine safety cap — the exact substitution the equivalence
    rules exist to prevent."""
    got = norm({"Parameters": [{"ParameterText": "Class", "ValueText": "Class 1"}]})
    assert "safety_class" not in got


def test_only_xy_ratings_are_read_as_safety_class():
    for value, expect in (("X1", "X1"), ("Y2", "Y2"), ("X2", "X2"), ("Class II", None)):
        got = norm({"Parameters": [
            {"ParameterText": "Safety Rating", "ValueText": value}]})
        assert got.get("safety_class") == expect, value


def test_packaging_and_compliance_are_not_emitted():
    """Passing these through would invite them being trusted for decisions they
    cannot support — reel quantity is not a property of the part."""
    got = norm({"ProductAttributes": [
        {"AttributeName": "Packaging", "AttributeValue": "Cut Tape"},
        {"AttributeName": "RoHS", "AttributeValue": "Compliant"},
        {"AttributeName": "Lead Time", "AttributeValue": "12 weeks"},
    ]})
    assert got == {}


# -- number forms in the wild ------------------------------------------------

def test_power_is_read_in_fraction_decimal_and_milliwatt_forms():
    assert [MODULES["mouser"]._parse_power(t)
            for t in ("1/4W", "0.25 W", "250mW", "1/16 W")] == [0.25, 0.25, 0.25, 0.0625]


def test_a_number_that_cannot_be_read_is_omitted_not_guessed():
    got = norm({"Parameters": [
        {"ParameterText": "Voltage - Rated", "ValueText": "See datasheet"},
        {"ParameterText": "Power", "ValueText": ""},
    ]})
    assert "voltage_v" not in got and "power_w" not in got


# -- the copies must not drift ----------------------------------------------

CASES = [
    {"ProductAttributes": [{"AttributeName": "Tolerance", "AttributeValue": "±5%"}]},
    {"Parameters": [{"ParameterText": "Voltage - Rated", "ValueText": "16V"}]},
    {"paramVOList": [{"paramNameEn": "Safety Class", "paramValueEn": "X2"}]},
    {"attributes": [{"attributeLabel": "Power Rating", "attributeValue": "1/8W"}]},
    {"Unknown": [{"a": "b"}]},
    {},
]


def test_all_four_resolvers_normalize_identically():
    for case in CASES:
        results = {n: m._normalize_attributes(case) for n, m in MODULES.items()}
        distinct = {repr(v) for v in results.values()}
        assert len(distinct) == 1, f"copies disagree on {case}: {results}"


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL {name}: {exc}")
    print(f"\n{failed} failed" if failed else "\nall passed")
    sys.exit(1 if failed else 0)
