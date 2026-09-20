#!/usr/bin/env python3
"""The cache, the source scheduler, and the confidence model.

These exist because the lifecycle audit was serial with a one-second sleep
before every distributor call: 57 distinct MPNs times four sources is 228
seconds of sleep before a single packet moves, and a board that grew from 91
parts to 109 pushed stage 8 past its 600-second budget. The audit is now
cache-first and concurrent, which is only safe if the parts below behave.

Run directly (``python3 skills/test_lifecycle_cache.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "kicad" / "scripts"))

from lifecycle_cache import (  # noqa: E402
    DEFAULT_TTL_DAYS,
    LADDER,
    LifecycleCache,
    SourceScheduler,
    score,
)

SOURCES = ["lcsc", "digikey", "element14", "mouser"]


def _cache(ttl_days=DEFAULT_TTL_DAYS):
    return LifecycleCache(os.path.join(tempfile.mkdtemp(), "c.json"), ttl_days)


# -- confidence -----------------------------------------------------------

def test_four_agreeing_sources_is_total_confidence():
    r = score({s: "active" for s in SOURCES})
    assert r["confidence"] == 1.0
    assert r["status"] == "active"


def test_two_agreeing_sources_clear_the_settled_bar():
    """0.80 is what marks a part resolved; two distributors must reach it."""
    assert score({"lcsc": "active", "digikey": "active"})["confidence"] >= 0.80


def test_one_source_never_settles():
    """However emphatic. A single distributor is not a second opinion."""
    assert score({"digikey": "active"})["confidence"] < 0.80


def test_no_sources_is_zero_not_active():
    r = score({"lcsc": "unknown", "digikey": "unknown"})
    assert r["confidence"] == 0.0
    assert r["status"] == "unknown"


def test_disagreement_has_a_magnitude():
    """active-vs-nrnd is a quibble; active-vs-obsolete is a contradiction."""
    near = score({"a": "active", "b": "active", "c": "active", "d": "nrnd"})
    far = score({"a": "active", "b": "active", "c": "obsolete", "d": "obsolete"})
    assert near["confidence"] > far["confidence"]
    assert far["confidence"] < 0.60


def test_worst_status_wins():
    """A part one distributor calls obsolete is a procurement problem."""
    assert score({"a": "active", "b": "obsolete"})["status"] == "obsolete"


def test_unknown_is_absence_not_disagreement():
    """A silent source must not look like a dissenting one."""
    both = score({"a": "active", "b": "active"})
    with_silent = score({"a": "active", "b": "active", "c": "unknown"})
    assert both["confidence"] == with_silent["confidence"]


# -- cache ----------------------------------------------------------------

def test_round_trip_through_disk():
    c = _cache()
    c.put("MPN1", "digikey", {"status": "Active"})
    c.save()
    again = LifecycleCache(c.path)
    assert again.get("MPN1", "digikey") == {"status": "Active"}


def test_expired_entries_are_not_served():
    c = _cache(ttl_days=0.0)
    c.put("MPN1", "digikey", {"status": "Active"})
    time.sleep(0.01)
    assert c.get("MPN1", "digikey") is None
    assert c.stale == 1


def test_negative_answers_cache_too():
    """A distributor that does not carry a part still will not tomorrow, and
    re-asking is the expensive half of this audit."""
    c = _cache()
    c.put("MPN1", "lcsc", None)
    assert "lcsc" in c.covered("MPN1", ["lcsc"])


def test_mpn_keys_are_case_and_space_insensitive():
    c = _cache()
    c.put(" stm32u5g9njh6q ", "digikey", {"status": "Active"})
    assert c.get("STM32U5G9NJH6Q", "digikey") is not None


def test_probing_coverage_does_not_inflate_the_hit_rate():
    c = _cache()
    c.put("MPN1", "digikey", {"status": "Active"})
    c.covered("MPN1", ["digikey"], count=False)
    assert c.hits == 0
    c.covered("MPN1", ["digikey"])
    assert c.hits == 1


def test_save_is_atomic_enough_to_survive_a_reload():
    c = _cache()
    for i in range(50):
        c.put("MPN%d" % i, "digikey", {"status": "Active"})
    c.save()
    c.save()
    assert len(LifecycleCache(c.path)._entries) == 50


# -- scheduler ------------------------------------------------------------

def test_cold_start_uses_the_bottom_of_the_ladder():
    """One unresponsive source costs two seconds per part, not ten."""
    s = SourceScheduler(_cache(), SOURCES)
    assert all(s.budget(x) == LADDER[0] for x in SOURCES)


def test_a_slow_source_earns_a_longer_budget():
    c = _cache()
    c.observe("element14", 3.5, True)
    assert SourceScheduler(c, SOURCES).budget("element14") > LADDER[0]


def test_timeouts_climb_the_ladder():
    c = _cache()
    for _ in range(3):
        c.observe("mouser", 0.0, False)
    assert SourceScheduler(c, SOURCES).budget("mouser") > LADDER[0]


def test_ranking_puts_the_fast_source_first():
    c = _cache()
    c.observe("lcsc", 0.4, True)
    c.observe("element14", 3.5, True)
    assert SourceScheduler(c, SOURCES).order()[0] == "lcsc"


def test_a_fully_cached_run_estimates_zero():
    s = SourceScheduler(_cache(), SOURCES)
    assert s.estimate(57, 8, cached_parts=57)["seconds"] == 0.0


def test_estimate_falls_with_concurrency():
    c = _cache()
    for src in SOURCES:
        c.observe(src, 1.0, True)
    s = SourceScheduler(c, SOURCES)
    assert s.estimate(57, 8)["seconds"] < s.estimate(57, 1)["seconds"]


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
