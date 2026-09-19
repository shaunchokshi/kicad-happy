#!/usr/bin/env python3
"""IO-001 must see ESD protection that only the schematic knows about.

The rule has two ways to satisfy itself: a filter component placed within
25 mm of the connector, or a protection device the schematic analyzer already
matched to one of the connector's signal nets. The second exists precisely
because the first cannot work before layout — an unplaced board has every
footprint sitting wherever the emitter dropped it, typically tens of
millimetres apart, so the proximity test fails on principle rather than on
merit.

That second path was dead. It read ``conn['pads']``, which the PCB analyzer
strips from its output; the consumer-visible field is ``pad_nets``. So
``conn_nets`` came back empty, no protected net could ever match it, and a
board with a correctly wired USBLC6-2SC6 across its USB data lines was told it
had no EMC filtering at all. The same file documents this exact trap six lines
further down, for the power-only gate.

Run directly (``python3 skills/test_io_filtering_schematic_path.py``) or under
pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "emc" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

from emc_rules import check_connector_filtering  # noqa: E402


def _pcb(extra_footprints=()):
    """A board with a USB-C receptacle and nothing placed near it."""
    return {
        "footprints": [
            {
                "reference": "J_USB_C",
                "value": "USB4105-GF-A",
                "lib_id": "Connector_USB:USB_C_Receptacle_GCT_USB4105",
                "x": 47.5,
                "y": 212.5,
                "layer": "F.Cu",
                "pad_nets": {
                    "A1": {"net": "GND"},
                    "A4": {"net": "VBUS"},
                    "A5": {"net": "CC1"},
                    "A6": {"net": "USBC_DP"},
                    "A7": {"net": "USBC_DM"},
                },
            },
            *extra_footprints,
        ],
    }


def _schematic(protected_net="USBC_DM", protected_nets=("USBC_DM", "USBC_DP")):
    return {
        "findings": [
            {
                "detector": "detect_protection_devices",
                "ref": "D_ESD_USB",
                "value": "USBLC6-2SC6",
                "type": "esd_ic",
                "protected_net": protected_net,
                "protected_nets": list(protected_nets),
            },
        ],
    }


def _io_findings(pcb, schematic=None):
    return [f for f in check_connector_filtering(pcb, schematic) if f.get("rule_id") == "IO-001"]


def test_esd_array_in_schematic_satisfies_the_rule():
    """The whole point of the fix: unplaced board, ESD on the data nets."""
    assert _io_findings(_pcb(), _schematic()) == []


def test_secondary_protected_net_also_counts():
    """A device whose *first* sorted net is not the one on the connector."""
    sch = _schematic(protected_net="USBC_DM", protected_nets=("USBC_DM", "USBC_DP"))
    pcb = _pcb()
    pcb["footprints"][0]["pad_nets"] = {"A6": {"net": "USBC_DP"}, "A1": {"net": "GND"}}
    assert _io_findings(pcb, sch) == []


def test_no_protection_still_reports():
    """The rule must not be defanged — an unprotected connector still flags."""
    assert len(_io_findings(_pcb(), {"findings": []})) == 1


def test_protection_on_an_unrelated_net_does_not_count():
    """A TVS on the battery rail says nothing about the USB connector."""
    sch = _schematic(protected_net="VBAT_FUSED", protected_nets=())
    assert len(_io_findings(_pcb(), sch)) == 1


def test_power_and_ground_nets_cannot_be_the_match():
    """GND appears at every connector; matching on it would clear everything."""
    sch = _schematic(protected_net="GND", protected_nets=("GND",))
    assert len(_io_findings(_pcb(), sch)) == 1


def test_placement_path_still_works_without_a_schematic():
    """A bead 3 mm away satisfies the rule with no schematic at all."""
    bead = {
        "reference": "FB_VBUS",
        "value": "BCMS201209A121",
        "lib_id": "Device:FerriteBead",
        "x": 50.0,
        "y": 212.5,
        "layer": "F.Cu",
        "pad_nets": {},
    }
    assert _io_findings(_pcb([bead]), None) == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print("FAIL %s: %s" % (name, exc))
        else:
            print("ok   %s" % name)
    sys.exit(1 if failures else 0)
