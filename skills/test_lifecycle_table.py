#!/usr/bin/env python3
"""The computed score, and the table that carries it alongside human knowledge.

The raw agreement score is honest and unhelpful on its own: where only one
distributor publishes lifecycle at all, a perfectly well-established part can
never exceed 0.55, which reads as a failing grade. The computed score rescales
against what was achievable. The table is where a person's own findings live,
and the property it must never lose is that a pipeline re-run does not
overwrite them.

Run directly (``python3 skills/test_lifecycle_table.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

from lifecycle_cache import compute, user_age_penalty  # noqa: E402
from lifecycle_table import (  # noqa: E402
    DEPARTED_TAG, USER_COLUMNS, is_departed, merge_rows, parse_date,
    read_table, user_row, write_table,
)

NOW = time.time()
DAY = 86400.0


def _ref(status="active", age_days=10):
    return {"status": status, "reference": "https://example.com/part",
            "checked_at": NOW - age_days * DAY}


# -- computed score -------------------------------------------------------

def test_one_capable_source_reads_eighty_not_fiftyfive():
    """The point of the rescale: as good as the available data allows."""
    assert compute({"digikey": "active"}, 1, now=NOW)["computed"] == 80.0


def test_each_extra_api_source_lifts_the_ceiling_by_five():
    two = compute({"digikey": "active", "nexar": "active"}, 2, now=NOW)
    assert two["ceiling"] == 85.0
    assert two["computed"] == 85.0


def test_a_referenced_human_check_is_worth_ten():
    r = compute({"digikey": "active"}, 1, _ref(), now=NOW)
    assert r["ceiling"] == 90.0
    assert r["computed"] == 90.0


def test_adding_a_source_never_lowers_the_score():
    """A corroborating entry that reduced confidence would be backwards, and
    did exactly that before the user source was folded in before scoring."""
    alone = compute({"digikey": "active"}, 1, now=NOW)["computed"]
    with_user = compute({"digikey": "active"}, 1, _ref(), now=NOW)["computed"]
    assert with_user > alone


def test_six_month_old_check_loses_five():
    fresh = compute({"digikey": "active"}, 1, _ref(age_days=10), now=NOW)
    stale = compute({"digikey": "active"}, 1, _ref(age_days=240), now=NOW)
    assert fresh["computed"] - stale["computed"] == 5.0
    assert stale["needs_ack"] is True


def test_year_old_check_loses_ten():
    fresh = compute({"digikey": "active"}, 1, _ref(age_days=10), now=NOW)
    old = compute({"digikey": "active"}, 1, _ref(age_days=730), now=NOW)
    assert fresh["computed"] - old["computed"] == 10.0


def test_a_check_with_no_reference_does_not_count():
    """On a multi-user project someone else has to be able to repeat it."""
    r = compute({"digikey": "active"}, 1,
                {"status": "active", "checked_at": NOW}, now=NOW)
    assert r["user_counted"] is False
    assert r["ceiling"] == 80.0
    assert any("reference" in x for x in r["reasons"])


def test_undated_check_is_treated_as_a_year_old():
    penalty, _ = user_age_penalty(None, NOW)
    assert penalty == 10.0


def test_disagreement_collapses_the_score():
    r = compute({"digikey": "active", "nexar": "obsolete"}, 2, now=NOW)
    assert r["computed"] < 50
    assert r["status"] == "obsolete"
    assert r["needs_ack"] is True


def test_no_data_needs_acknowledgement_but_does_not_block():
    r = compute({}, 1, now=NOW)
    assert r["computed"] == 0.0
    assert r["needs_ack"] is True
    assert r["blocks_fab"] is False


def test_nothing_ever_blocks_fabrication():
    """Missing lifecycle data is a decision for a person to record, not a gate
    a script has the standing to close."""
    for case in ({}, {"digikey": "obsolete"},
                 {"digikey": "active", "nexar": "obsolete"}):
        assert compute(case, 2, now=NOW)["blocks_fab"] is False


# -- the table ------------------------------------------------------------

def _tbl():
    return os.path.join(tempfile.mkdtemp(), "lifecycle.md")


FINDINGS = {
    "STM32U5G9NJH6Q": {"refs": ["U1"], "status": "active", "computed": 80.0,
                       "raw": 0.55, "responding": 1, "capable": 1, "needs_ack": False},
    "MM8108-MF15457": {"refs": ["U_HALOW"], "status": "unknown", "computed": 0.0,
                       "raw": 0.0, "responding": 0, "capable": 1, "needs_ack": True},
}


def test_a_rerun_does_not_overwrite_human_columns():
    """The property the whole design rests on. A table that loses a
    hand-entered reference on the next pipeline run would be worse than no
    table, because people would stop trusting it exactly when it mattered."""
    p = _tbl()
    write_table(p, FINDINGS)
    rows = read_table(p)
    rows["MM8108-MF15457"]["User Status"] = "active"
    rows["MM8108-MF15457"]["Reference"] = "https://morsemicro.com/mm8108"
    rows["MM8108-MF15457"]["Checked On"] = "2026-09-20"
    from lifecycle_table import render_table
    open(p, "w").write(render_table(merge_rows(rows, {})))

    write_table(p, FINDINGS)          # the script runs again
    again = read_table(p)["MM8108-MF15457"]
    assert again["Reference"] == "https://morsemicro.com/mm8108"
    assert again["User Status"] == "active"


def test_script_columns_are_refreshed():
    p = _tbl()
    write_table(p, FINDINGS)
    changed = dict(FINDINGS)
    changed["STM32U5G9NJH6Q"] = dict(FINDINGS["STM32U5G9NJH6Q"], status="nrnd")
    write_table(p, changed)
    assert read_table(p)["STM32U5G9NJH6Q"]["Status"] == "nrnd"


def test_a_part_that_leaves_the_bom_keeps_its_row():
    """Someone wrote a reference into it; deleting their work silently is how
    a table stops being trusted."""
    p = _tbl()
    write_table(p, FINDINGS)
    write_table(p, {"STM32U5G9NJH6Q": FINDINGS["STM32U5G9NJH6Q"]})
    rows = read_table(p)
    assert "MM8108-MF15457" in rows
    assert "no longer in the BOM" in rows["MM8108-MF15457"]["Notes"]


def test_acknowledgement_clears_the_outstanding_list():
    p = _tbl()
    assert write_table(p, FINDINGS)["awaiting_acknowledgement"] == ["MM8108-MF15457"]
    rows = read_table(p)
    rows["MM8108-MF15457"]["Acknowledged"] = "sc 2026-09-20"
    from lifecycle_table import render_table
    open(p, "w").write(render_table(merge_rows(rows, {})))
    assert write_table(p, FINDINGS)["awaiting_acknowledgement"] == []


def test_the_ack_flag_says_yes_only_while_it_is_outstanding():
    """A two-digit score in a monospace edit view does not announce itself —
    57 and 85 look alike at a glance, and the difference between them is
    whether somebody has to do something. The flag says so in words, and
    clears itself once they have."""
    p = _tbl()
    write_table(p, FINDINGS)
    rows = read_table(p)
    assert rows["MM8108-MF15457"]["Ack?"] == "YES"   # needs_ack, unsigned
    assert rows["STM32U5G9NJH6Q"]["Ack?"] == ""      # settled

    rows["MM8108-MF15457"]["Acknowledged"] = "sc 2026-09-20"
    from lifecycle_table import render_table
    open(p, "w").write(render_table(merge_rows(rows, {})))
    write_table(p, FINDINGS)
    assert read_table(p)["MM8108-MF15457"]["Ack?"] == ""


def test_dates_parse_and_missing_ones_do_not_raise():
    assert parse_date("2026-09-20") is not None
    assert parse_date("") is None
    assert parse_date("sometime last spring") is None


def test_user_row_needs_a_status_to_exist():
    assert user_row({"User Status": "", "Reference": "https://x"}) is None
    assert user_row({"User Status": "active", "Reference": "https://x"})["status"] == "active"


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
    print("\n%d failed" % failures if failures else "\nall passed")
    sys.exit(1 if failures else 0)


def test_a_stale_ack_flag_clears_when_the_part_leaves_the_bom():
    """A part off the board never asks anyone for an acknowledgement."""
    existing = {"OLD-PART": {"MPN": "OLD-PART", "Ack?": "YES",
                             "Reference": "https://example.invalid/old",
                             "Notes": ""}}
    row = {r["MPN"]: r for r in merge_rows(existing, {})}["OLD-PART"]
    assert row["Ack?"] == ""
    assert DEPARTED_TAG in row["Notes"]
    # The research survives; only the demand on someone's time goes away.
    assert row["Reference"] == "https://example.invalid/old"


def test_is_departed_reads_the_tag():
    assert is_departed({"Notes": "(%s)" % DEPARTED_TAG}) is True
    assert is_departed({"Notes": "swapped for the 0402"}) is False
    assert is_departed({}) is False
