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


# -- the package, for choosing a land pattern ---------------------------------

def test_the_package_a_distributor_states_is_read():
    """A package name does not determine a land pattern, and the pipeline has to
    produce one. What a distributor states about the package narrows the search
    where a bill of materials, which carries only the name, cannot."""
    got = norm({"Parameters": [
        {"Parameter": "Package / Case", "Value": "24-WFQFN Exposed Pad"},
        {"Parameter": "Supplier Device Package", "Value": "24-WQFN (4x4)"},
        {"Parameter": "Size / Dimension", "Value": '0.157" L x 0.157" W (4.00mm x 4.00mm)'},
        {"Parameter": "Height - Seated (Max)", "Value": '0.031" (0.80mm)'},
    ]})
    assert got["package"] == "24-WFQFN Exposed Pad"
    assert got["supplier_package"] == "24-WQFN (4x4)"
    assert got["body_mm"] == {"length": 4.0, "width": 4.0}
    assert got["height_mm"] == 0.8


def test_the_metric_half_is_the_one_read():
    """Distributors state both systems. Reading the imperial figure would put a
    body of 0.157mm on a 4mm part."""
    got = norm({"Parameters": [
        {"Parameter": "Size / Dimension", "Value": '0.276" L x 0.209" W (7.00mm x 5.30mm)'},
    ]})
    assert got["body_mm"] == {"length": 7.0, "width": 5.3}


def test_a_lone_measurement_in_a_size_field_is_not_guessed_at():
    """It could be either dimension, and a wrong body size in a field that looks
    authoritative is worse than an absent one."""
    assert "body_mm" not in norm({"Parameters": [
        {"Parameter": "Size / Dimension", "Value": "4.00mm"},
    ]})


def test_packaging_is_still_not_a_package():
    """`Packaging` is the reel. The label is matched exactly for this reason —
    a substring test would have read 'Tape & Reel (TR)' as the part's package."""
    got = norm({"ProductAttributes": [
        {"AttributeName": "Packaging", "AttributeValue": "Tape & Reel (TR)"},
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


# -- package data that is not a row in a parameter list -----------------------

def test_lcsc_states_the_package_as_a_plain_field():
    """jlcsearch puts it on the component, not in a parameter container.

    The attribute walk only traverses list-valued containers, so for LCSC's
    documented response shape the package branch was never reached at all and
    `--json` came back without one — on a request that otherwise looked like it
    had succeeded, which is the worst way to lose a field.
    """
    got = norm({"package": "QFN-24", "mfr": "TPS62840"})
    assert got["package"] == "QFN-24"


def test_lcsc_extra_carries_it_too():
    got = norm({"extra": {"package": "WSON-8"}})
    assert got["package"] == "WSON-8"


def test_a_labelled_row_still_beats_a_bare_scalar():
    """DigiKey states both; "Package / Case" is the more specific of the two."""
    got = norm({
        "package": "QFN",
        "Parameters": [{"Parameter": "Package / Case", "Value": "24-WFQFN Exposed Pad"}],
    })
    assert got["package"] == "24-WFQFN Exposed Pad"


def test_a_package_field_that_is_empty_is_not_a_package():
    assert norm({"package": "  ", "extra": {"package": None}}) == {}


def test_extra_that_is_still_a_json_string_is_not_mistaken_for_a_mapping():
    # LCSC hands `extra` over as a string until _parse_extra has run on it.
    assert norm({"extra": '{"package": "QFN-24"}'}) == {}


# -- Element14 keeps its unit in a separate field -----------------------------

def test_element14_body_size_survives_its_separate_unit():
    """attributeValue "4 x 4", attributeUnit "mm" — the shape it documents.

    Both dimension parsers match on the literal unit, so a value passed on
    without it is declined rather than misread. Safe, and still a total loss of
    the only body figures this supplier returns.
    """
    got = norm({"attributes": [
        {"attributeLabel": "Size / Dimension", "attributeValue": "4 x 4", "attributeUnit": "mm"},
    ]})
    assert got["body_mm"] == {"length": 4.0, "width": 4.0}


def test_element14_height_survives_its_separate_unit():
    got = norm({"attributes": [
        {"attributeLabel": "Height - Seated (Max)", "attributeValue": "0.8", "attributeUnit": "mm"},
    ]})
    assert got["height_mm"] == 0.8


def test_a_value_that_already_carries_its_unit_is_not_given_a_second_one():
    got = norm({"attributes": [
        {"attributeLabel": "Height", "attributeValue": "0.8mm"},
    ]})
    assert got["height_mm"] == 0.8


def test_an_inch_first_string_still_yields_the_metric_pair():
    """The shared-unit fallback must not steal a match from the metric pair."""
    got = norm({"Parameters": [
        {"Parameter": "Size / Dimension", "Value": '0.157" L x 0.157" W (4.00mm x 4.00mm)'},
    ]})
    assert got["body_mm"] == {"length": 4.0, "width": 4.0}


def test_a_pair_with_no_unit_at_all_is_declined():
    assert "body_mm" not in norm({"attributes": [
        {"attributeLabel": "Size / Dimension", "attributeValue": "4 x 4"},
    ]})
