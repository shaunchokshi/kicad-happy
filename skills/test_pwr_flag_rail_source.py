#!/usr/bin/env python3
"""A PWR_FLAG must be visible to the rail-source audit that looks for it.

``audit_rail_sources`` decides a power rail is sourced when some pin on the
net is ``power_out`` or belongs to a ``#FLG`` component — a PWR_FLAG, the
marker KiCad itself uses to say "this rail is fed from outside the schematic".
It reads those pins from the net map ``build_net_map`` produces. For a long
time that map skipped PWR_FLAG components entirely, on the theory that an ERC
marker is not a real connection — so the audit was searching a map that could
never contain the thing it searched for, and every correctly flagged rail was
reported as having no declared source while ERC passed it.

A PWR_FLAG has one pin. One point can only join the net already at that
point; it cannot bridge two nets. So including it is safe, and these tests
pin down the behaviour end to end: the same rail with and without the flag,
through ``build_net_map`` and ``audit_rail_sources`` exactly as the analyzer
calls them.

Run directly (``python3 skills/test_pwr_flag_rail_source.py``) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent / "kicad" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from analyze_schematic import audit_pwr_flags, build_net_map, build_pin_to_net_map  # noqa: E402
from kicad_types import AnalysisContext  # noqa: E402
from signal_detectors import audit_rail_sources  # noqa: E402


def _pin(number, ptype, x, y, name=""):
    return {"number": number, "name": name, "type": ptype, "x": x, "y": y}


def _load(x, y):
    """A part fed by the rail: one power_in pin, wired to a VIN label."""
    return {
        "reference": "U1", "value": "LOAD", "type": "ic", "lib_id": "Regulator:LOAD",
        "footprint": "", "mpn": "", "dnp": False, "in_bom": True, "x": x, "y": y,
        "pins": [_pin("1", "power_in", x, y, "VIN")],
    }


def _flag(x, y, ref="#FLG01"):
    """PWR_FLAG as the parser classifies it: a power: library symbol, value
    PWR_FLAG, one power_out pin at its origin."""
    return {
        "reference": ref, "value": "PWR_FLAG", "type": "power_symbol",
        "lib_id": "power:PWR_FLAG", "footprint": "", "mpn": "", "dnp": False,
        "in_bom": False, "x": x, "y": y,
        "pins": [_pin("1", "power_out", x, y, "pwr")],
    }


def _nets(with_flag: bool) -> dict:
    x, y = 10.16, 20.32
    components = [_load(x, y)]
    labels = [{"name": "VIN", "type": "label", "x": x, "y": y}]
    power_symbols = []
    if with_flag:
        components.append(_flag(x, y))
        # analyze_schematic also lists every power_symbol-typed component in
        # power_symbols, PWR_FLAG included; build_net_map drops that entry by
        # name so "PWR_FLAG" never becomes a net.
        power_symbols.append({"net_name": "PWR_FLAG", "x": x, "y": y,
                              "lib_id": "power:PWR_FLAG"})
    return build_net_map(components, wires=[], labels=labels,
                         power_symbols=power_symbols, junctions=[], no_connects=[])


def _ctx(nets: dict) -> AnalysisContext:
    return AnalysisContext(components=[_load(0, 0)], nets=nets, lib_symbols={},
                           pin_net=build_pin_to_net_map(nets))


def _rs001(nets: dict) -> list[str]:
    return [f["nets"][0] for f in audit_rail_sources(_ctx(nets))
            if f["rule_id"] == "RS-001"]


def test_flag_pin_lands_on_the_rail_it_sits_on():
    nets = _nets(with_flag=True)
    assert "PWR_FLAG" not in nets, "the flag's own name is not a rail"
    comps = {p["component"] for p in nets["VIN"]["pins"]}
    assert comps == {"U1", "#FLG01"}


def test_a_flag_cannot_bridge_two_nets():
    # Only one pin, only one point: a flag at a place nothing else occupies
    # is its own net, not a link to anything.
    x, y = 10.16, 20.32
    components = [_load(x, y), _flag(50.8, 50.8)]
    labels = [{"name": "VIN", "type": "label", "x": x, "y": y}]
    nets = build_net_map(components, wires=[], labels=labels, power_symbols=[],
                         junctions=[], no_connects=[])
    assert {p["component"] for p in nets["VIN"]["pins"]} == {"U1"}
    lonely = [n for n, i in nets.items() if any(p["component"] == "#FLG01" for p in i["pins"])]
    assert lonely and lonely[0] != "VIN"


def test_unflagged_rail_with_only_power_in_is_reported():
    assert _rs001(_nets(with_flag=False)) == ["VIN"]


def test_flagged_rail_is_sourced():
    assert _rs001(_nets(with_flag=True)) == []


def test_audit_pwr_flags_sees_the_same_flag():
    x, y = 10.16, 20.32
    flagged = _nets(with_flag=True)
    bare = _nets(with_flag=False)
    assert audit_pwr_flags([_load(x, y), _flag(x, y)], flagged, {"VIN"}) == []
    assert [w["net"] for w in audit_pwr_flags([_load(x, y)], bare, {"VIN"})] == ["VIN"]


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
