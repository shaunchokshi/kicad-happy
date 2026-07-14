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
