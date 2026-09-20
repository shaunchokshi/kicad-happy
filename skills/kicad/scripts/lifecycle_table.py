#!/usr/bin/env python3
"""The project's lifecycle table: script findings and human knowledge, together.

A markdown table, because that is what the rest of this project's design
documents are and what the app's edit pane already renders. It lives with the
design files rather than in a cache, so it is reviewed, diffed and travels with
the board.

Two kinds of column share it. The script owns what it measured — status,
confidence, how many sources answered — and rewrites those every run. The
person owns what they went and found out, and the script must never touch
those. That constraint is the whole design: a table that loses a hand-entered
reference the first time someone re-runs the pipeline would be worse than no
table, because people would stop trusting it exactly when it mattered.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

__all__ = ["SCRIPT_COLUMNS", "USER_COLUMNS", "DEPARTED_TAG", "is_departed",
           "read_table", "merge_rows", "render_table", "write_table",
           "parse_date"]

# A part that has left the BOM. Its row survives because someone's research is
# in it, but it is history, not work: nothing about a part that is not on the
# board can require a human decision.
DEPARTED_TAG = "no longer in the BOM"


def is_departed(row: dict[str, str]) -> bool:
    return DEPARTED_TAG in (row.get("Notes") or "")

# Rewritten from the audit on every run. "Ack?" sits directly beside the score
# it follows from, because a two-digit number in a monospace edit view does not
# announce itself — 57 and 85 look alike at a glance, and the difference
# between them is whether somebody has to do something.
SCRIPT_COLUMNS = ["MPN", "Refs", "Status", "Computed", "Ack?", "Raw", "Sources"]

# Never written by the script once a human has put something there.
USER_COLUMNS = ["User Status", "Checked On", "Reference", "Acknowledged", "Notes"]

COLUMNS = SCRIPT_COLUMNS + USER_COLUMNS

_HEADER_NOTE = """<!-- Script columns (MPN, Refs, Status, Computed, Raw, Sources) are rewritten
     on every audit. The five user columns are yours and are never overwritten.

     Computed is out of 100 and is scaled against what was achievable: one
     capable source in full agreement is 80, each further API source raises the
     ceiling by 5, and a referenced human check raises it by 10. A check older
     than six months loses 5, older than a year loses 10.

     User Status accepts: active, nrnd, last_time_buy, discontinued, obsolete.
     Reference is required for a User Status to count at all - someone else has
     to be able to repeat the check.
     Ack? is the script's flag, and it clears itself: it reads YES while a part
     still needs a human decision and goes blank the moment the Acknowledged
     column has something in it. Filter or sort on this column to find the work.

     Acknowledged: put your name and the date when you have accepted a part
     whose lifecycle could not be established. Nothing here blocks fabrication;
     unresolved parts need this acknowledgement instead. -->"""


def parse_date(text: str | None) -> float | None:
    """Epoch seconds from a YYYY-MM-DD cell, or None."""
    if not text:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _split_row(line: str) -> list[str]:
    cells = line.split("|")
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [c.strip() for c in cells]


def read_table(path: str) -> dict[str, dict[str, str]]:
    """Existing rows, keyed by MPN. Missing file is an empty table."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}

    rows: dict[str, dict[str, str]] = {}
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if header is None:
            header = cells
            continue
        if all(set(c) <= set("-: ") for c in cells if c):
            continue  # the ---|--- separator
        row = dict(zip(header, cells))
        mpn = (row.get("MPN") or "").strip().strip("`")
        if mpn:
            rows[mpn] = row
    return rows


def merge_rows(existing: dict[str, dict[str, str]],
               findings: dict[str, dict]) -> list[dict[str, str]]:
    """Script columns from this run, user columns from whatever was there.

    Parts that have left the BOM keep their row and are marked, rather than
    being dropped: someone wrote a reference into that row, and silently
    deleting their work because a part was swapped out is how a table stops
    being trusted.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for mpn in sorted(findings):
        f = findings[mpn]
        prior = existing.get(mpn, {})
        row = {c: prior.get(c, "") for c in USER_COLUMNS}
        row["MPN"] = mpn
        row["Refs"] = ", ".join(f.get("refs") or [])
        row["Status"] = f.get("status", "unknown")
        computed = f.get("computed")
        row["Computed"] = "" if computed is None else "%.0f" % computed
        # Outstanding, not merely warranted: once a person has signed the
        # Acknowledged column the flag clears, so what is left in this column
        # is exactly the work still to do.
        row["Ack?"] = ("YES" if (f.get("needs_ack")
                                 and not (prior.get("Acknowledged") or "").strip())
                       else "")
        raw = f.get("raw")
        row["Raw"] = "" if raw is None else "%.2f" % raw
        row["Sources"] = "%d/%d" % (f.get("responding", 0), f.get("capable", 0))
        out.append(row)
        seen.add(mpn)

    for mpn, prior in sorted(existing.items()):
        if mpn in seen:
            continue
        row = dict(prior)
        row["MPN"] = mpn
        note = row.get("Notes", "")
        if DEPARTED_TAG not in note:
            row["Notes"] = (note + " " if note else "") + "(%s)" % DEPARTED_TAG
        # The flag is a work list, and this is not work. Whatever the last run
        # that still saw this part decided, nobody has to go and establish the
        # lifecycle of something that is no longer on the board.
        row["Ack?"] = ""
        out.append(row)
    return out


def render_table(rows: list[dict[str, str]], title: str = "Lifecycle") -> str:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows
              else len(c) for c in COLUMNS}
    def line(cells):
        return "| " + " | ".join(str(cells[i]).ljust(widths[c])
                                 for i, c in enumerate(COLUMNS)) + " |"
    parts = ["# %s" % title, "", _HEADER_NOTE, "",
             line(COLUMNS),
             "|" + "|".join("-" * (widths[c] + 2) for c in COLUMNS) + "|"]
    for r in rows:
        parts.append(line([r.get(c, "") for c in COLUMNS]))
    parts.append("")
    return "\n".join(parts)


def write_table(path: str, findings: dict[str, dict],
                title: str = "Lifecycle") -> dict:
    """Merge this run's findings into the table on disk, preserving user columns."""
    existing = read_table(path)
    rows = merge_rows(existing, findings)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render_table(rows, title))
    kept = sum(1 for r in rows if any(r.get(c) for c in USER_COLUMNS))
    needs_ack = [r["MPN"] for r in rows
                 if findings.get(r["MPN"], {}).get("needs_ack")
                 and not r.get("Acknowledged")]
    return {"rows": len(rows), "with_user_data": kept,
            "awaiting_acknowledgement": needs_ack}


def user_row(row: dict[str, str]) -> dict | None:
    """The user-supplied half of a table row, shaped for compute()."""
    status = (row.get("User Status") or "").strip().lower()
    if not status:
        return None
    return {"status": status,
            "reference": (row.get("Reference") or "").strip(),
            "checked_at": parse_date(row.get("Checked On"))}
