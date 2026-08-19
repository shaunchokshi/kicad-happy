# Attribution

This repository is derived from **kicad-happy** by Andrew Klofas
(<https://github.com/aklofas/kicad-happy>), used under the MIT License. The
original license text is preserved verbatim in [`LICENSE`](LICENSE).

## Relationship to upstream

This copy was detached from the upstream fork network so it can be adapted to
the Board Layer Pipe Line (BLPL) markdown→KiCad pipeline. It is **not** a
maintained fork and does not track upstream releases. Divergence is expected
and intentional.

The starting point was upstream commit `fc94a3d` (v2.0.0). Anything at or
before that commit is Andrew Klofas's work; changes after it are ours.

If you want the original — actively maintained, broader in scope, and not bent
around one pipeline's assumptions — use upstream. It is the better choice for
general KiCad review.

## What we changed and why

Upstream kicad-happy assumes a human drew the board in KiCad. BLPL *generates*
the board from Markdown design documents, which changes what a finding means: a
schematic/PCB disagreement is a human sync error upstream, but here both files
come from one `hdm.yaml`, so it can only be an emitter bug. BLPL's `stage8_review`
consumes these analyzers and re-classifies their findings accordingly (see
`blpl/core/stage8_review.py`).

### Distributor parametric data on part lookups

The four resolver scripts (`skills/{mouser,digikey,lcsc,element14}`) originally
kept only manufacturer, description and datasheet URL from a part lookup, and
discarded the parametric block every distributor returns alongside it. That is a
reasonable choice upstream, where the goal is the datasheet.

BLPL groups BOM lines for ordering by electrical attributes, so it needs the
ratings *and* their origin: a value stated by a distributor about a specific MPN
carries more weight than one typed into a design document, and only the former
can be checked against the part that will actually arrive. `_normalize_attributes`
therefore emits `value`, `tolerance`, `voltage_v`, `power_w`, `dielectric` and
`safety_class`, and nothing else — packaging, RoHS and lead time are dropped on
purpose, since passing them through would invite them being trusted for
decisions they cannot support.

The block is duplicated across the four scripts rather than factored out,
because these scripts are standalone by design and have no shared library to
import from. `skills/test_normalize_attributes.py` asserts the copies agree.
