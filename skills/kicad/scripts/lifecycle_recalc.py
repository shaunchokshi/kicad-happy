#!/usr/bin/env python3
"""Rescore the lifecycle table from the cache, touching no network.

Two things change a part's score without anyone re-querying a distributor: a
person filling in what they found, and the scoring model itself being
corrected. Both happened on this project inside an hour, and re-running the
audit to pick them up would spend part lookups against an evaluation licence
that allows a hundred for the life of the key.

So this reads the answers already on disk and the table already written, and
rewrites the computed columns. It imports the same compute() the audit uses
rather than reimplementing it, because a second scoring path that drifts from
the first is worse than no second path.

    python3 lifecycle_recalc.py <project-dir> [--cache PATH] [--table PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lifecycle_audit import STATUS_CAPABLE, _normalize_status  # noqa: E402
from lifecycle_cache import compute  # noqa: E402
from lifecycle_table import (  # noqa: E402
    read_table, render_table, user_row, is_departed,
)

SEP = "\x1f"


def statuses_from_cache(cache_path: str) -> dict[str, dict[str, str]]:
    """Every cached answer, as {mpn: {source: normalised status}}."""
    with open(cache_path) as fh:
        blob = json.load(fh)
    out: dict[str, dict[str, str]] = {}
    for key, row in (blob.get("entries") or {}).items():
        if SEP not in key:
            continue
        mpn, source = key.split(SEP, 1)
        data = (row or {}).get("data")
        if not isinstance(data, dict):
            continue
        status = _normalize_status(data.get("status"))
        if status != "unknown":
            out.setdefault(mpn, {})[source] = status
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_dir")
    ap.add_argument("--cache", default=None)
    ap.add_argument("--table", default=None)
    ap.add_argument("--sources", default=None,
                    help="Comma-separated source set the scores assume "
                         "(default: every status-capable source)")
    args = ap.parse_args()

    root = os.path.abspath(args.project_dir)
    cache_path = args.cache or os.path.join(root, ".pipeline", "analysis",
                                            "lifecycle_cache.json")
    table_path = args.table or os.path.join(root, "lifecycle.md")

    # An unreadable cache used to come back as an empty dict, and the loop
    # below would then rescore every row to zero and demand an acknowledgement
    # for the whole BOM. Scoring nothing is not the same as scoring badly:
    # without the cache there is nothing to recalculate from, so stop.
    try:
        cached = statuses_from_cache(cache_path)
    except (OSError, ValueError) as exc:
        print("cannot read %s: %s" % (cache_path, exc), file=sys.stderr)
        return 1
    if not cached:
        print("no cached statuses in %s - nothing to recalculate from"
              % cache_path, file=sys.stderr)
        return 1

    rows = read_table(table_path)
    if not rows:
        print("no table at %s" % table_path, file=sys.stderr)
        return 1

    capable = (len([s for s in args.sources.split(",") if s.strip() in STATUS_CAPABLE])
               if args.sources else len(STATUS_CAPABLE))

    changed = 0
    for mpn, row in rows.items():
        per_source = cached.get(mpn, {})
        scored = compute(per_source, capable, user_row(row))
        before = (row.get("Computed"), row.get("Ack?"))
        row["Status"] = scored.get("status", "unknown")
        row["Computed"] = "%.0f" % scored["computed"]
        row["Raw"] = "%.2f" % scored["raw"]
        row["Sources"] = "%d/%d" % (scored["responding"], scored["capable"])
        row["Ack?"] = ("YES" if (scored["needs_ack"]
                                 and not is_departed(row)
                                 and not (row.get("Acknowledged") or "").strip())
                       else "")
        if (row["Computed"], row["Ack?"]) != before:
            changed += 1

    with open(table_path, "w", encoding="utf-8") as fh:
        fh.write(render_table(list(rows.values())))

    outstanding = [m for m, r in rows.items() if r.get("Ack?") == "YES"]
    print("recalculated %d rows from cache, %d changed, no network touched"
          % (len(rows), changed))
    print("outstanding acknowledgements: %d%s"
          % (len(outstanding),
             (" — " + ", ".join(sorted(outstanding))) if outstanding else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
