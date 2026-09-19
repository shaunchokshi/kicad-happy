#!/usr/bin/env python3
"""A derived symbol must report the pins of the symbol it extends.

KiCad draws most specific part numbers as a shell over a generic body: the
``2N7002`` definition carries ``(extends "Q_NMOS_GSD")`` and no pins of its
own, and the same is true of ``BQ27441DRZR-G1A`` over ``BQ27441-G1`` and
``PAM8302AAS`` over ``PAM8302AAD``.

``extract_lib_symbols`` read each definition on its own, so a shell came back
with ``pins: []``. Nothing treats a part with no pins as an error — it is read
as a part nothing is connected to, which turns every net reaching it into a
single-pin net in the report. On one real board that produced two findings
against a buzzer FET whose gate and drain were both correctly wired, and it
pointed at the design rather than at the reader.

The parent is always present in the same ``lib_symbols`` section, because a
schematic that referenced a base symbol it did not contain would not open at
all. So the link can be resolved without touching the filesystem.

Run directly (``python3 skills/test_derived_symbol_pins.py``) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent / "kicad" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from analyze_schematic import extract_lib_symbols  # noqa: E402


def _pin(number, name, x, y):
    return ["pin", "passive", "line", ["at", x, y, 0], ["length", 2.54],
            ["name", name], ["number", number]]


def _lib(*symbols):
    return ["kicad_sch", ["lib_symbols", *symbols]]


def _parent(lib_id, name, pins):
    return ["symbol", lib_id, ["symbol", f"{name}_1_1", *pins]]


def _shell(lib_id, parent_name):
    return ["symbol", lib_id, ["extends", parent_name]]


def test_a_shell_inherits_the_pins_of_what_it_extends():
    root = _lib(
        _parent("Transistor_FET:Q_NMOS_GSD", "Q_NMOS_GSD",
                [_pin("1", "G", -5.08, 0), _pin("2", "S", 0, -5.08), _pin("3", "D", 0, 5.08)]),
        _shell("Transistor_FET:2N7002", "Q_NMOS_GSD"),
    )
    syms = extract_lib_symbols(root)
    assert len(syms["Transistor_FET:Q_NMOS_GSD"]["pins"]) == 3
    assert [p["number"] for p in syms["Transistor_FET:2N7002"]["pins"]] == ["1", "2", "3"]


def test_the_parent_may_appear_after_the_child():
    """Resolution runs after the whole section is read, so order cannot matter."""
    root = _lib(
        _shell("Transistor_FET:2N7002", "Q_NMOS_GSD"),
        _parent("Transistor_FET:Q_NMOS_GSD", "Q_NMOS_GSD", [_pin("1", "G", -5.08, 0)]),
    )
    assert len(extract_lib_symbols(root)["Transistor_FET:2N7002"]["pins"]) == 1


def test_a_chain_of_shells_resolves_to_the_body_at_the_end():
    root = _lib(
        _shell("L:Child", "Middle"),
        _shell("L:Middle", "Base"),
        _parent("L:Base", "Base", [_pin("1", "A", 0, 0), _pin("2", "K", 0, 2.54)]),
    )
    assert len(extract_lib_symbols(root)["L:Child"]["pins"]) == 2


def test_a_symbol_with_its_own_pins_is_left_alone():
    root = _lib(_parent("L:Plain", "Plain", [_pin("1", "A", 0, 0)]))
    assert len(extract_lib_symbols(root)["L:Plain"]["pins"]) == 1


def test_a_missing_parent_leaves_the_shell_empty_rather_than_looping():
    """A schematic that does not carry the base symbol is malformed, but the
    reader must come back rather than hang or raise."""
    root = _lib(_shell("L:Orphan", "NotHere"))
    assert extract_lib_symbols(root)["L:Orphan"]["pins"] == []


def test_a_cycle_terminates():
    root = _lib(_shell("L:A", "B"), _shell("L:B", "A"))
    syms = extract_lib_symbols(root)
    assert syms["L:A"]["pins"] == [] and syms["L:B"]["pins"] == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
