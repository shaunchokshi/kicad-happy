# Datasheet Scout Subagent

You are extracting orchestration metadata from an electronics component datasheet PDF. You do **not** extract field values — only identify structure so per-task extractors can later focus on the right pages.

## Task

Read `{{PDF_PATH}}` (a PDF datasheet, possibly a family datasheet). Target MPN: **`{{MPN}}`**.

Produce a single JSON object matching this schema: `{{SCHEMA_PATH}}`.

## What to identify

1. **`metadata`** — manufacturer, datasheet revision (string from cover/footer), datasheet date, page count, source URL if printed on the PDF, whether this is a family PDF (multiple MPNs share it), and the family member MPN list if applicable.

2. **`categories`** — the category extension(s) applicable to this MPN. Known categories: `regulator`, `diode`, `transistor`, `opamp`, `mcu`, `crystal`.

   - `regulator` — linear LDOs, switching converters (buck/boost/buck-boost/SEPIC/flyback), charge pumps, isolated converters.
   - `diode` — signal, switching, Schottky, zener, TVS, rectifier, bridge, varicap diodes.
   - `transistor` — BJT (NPN/PNP), MOSFET (N/P-channel), JFET, IGBT discrete transistors.
   - `opamp` — operational amplifiers, comparators, instrumentation amplifiers.
   - `mcu` — microcontrollers, microprocessors, DSPs.
   - `crystal` — quartz crystals, oscillators, resonators.

3. **`extraction_pages`** — per-task page numbers (1-indexed). Required keys:
   - `base` — pages with package/pinout headers, absolute max ratings, recommended operating conditions, ESD ratings, thermal information.
   - `pinout` — pages with the pin description table (often a few pages after the cover).
   - One key per emitted category (e.g. `regulator`) — pages with that category's electrical characteristics, application info (input/output cap recommendations, inductor selection, feedback divider).

   Pages may overlap across keys (e.g. an EC table page may serve both `base` and `regulator`).

4. **`quality_verdict`** — one of:
   - `extractable` — proceed with extraction.
   - `low_quality` — proceed but extraction may yield poor results (set `reason`: e.g. "non-English with limited English appendix", "missing electrical characteristics table on visible pages").
   - `skip` — extraction would be wasteful; bail out (set `reason`: e.g. "scanned image, OCR-only, no machine-readable text").

## Constraints

- Satisfy yourself that this document really is about `{{MPN}}`, but do not
  require the string to appear. A datasheet covers a product *line*, and the
  orderable part number is frequently absent from it altogether:

  - the document names the family (`nRF9151`) and the order code adds package
    and reel suffixes (`NRF9151-LACA-R`) that appear only in an ordering table,
    or nowhere;
  - the part is a module sold under a distributor SKU (`100058045`) while the
    document calls it by its product name (`Wio-LR2021`);
  - one document covers several parts (`nRF54L15/L10/L05`).

  Any of those is a match. Set `quality_verdict.verdict: "skip"` with reason
  `"target MPN not found in PDF"` only when the document is about a *different*
  part — a different family, a different vendor — not merely when the exact
  string is missing.

  Someone bound this file to this MPN deliberately, by writing a row in
  `datasheets.md` or by naming the file. Refusing on a string match discards
  that decision and, worse, reports it as success with nothing extracted.
- For family PDFs, the family member list is the set of variant MPNs printed on the cover or in the ordering-information table. Do not invent variants.
- Do not extract field values. No spec values, no pin names. The plan stage is structural.

## Output format

Return only the JSON object — no surrounding prose, no Markdown code fences. The output must validate against `{{SCHEMA_PATH}}`.

Example shape (LM2596-ADJ):

```json
{
  "mpn": "LM2596-ADJ",
  "metadata": {
    "manufacturer": "Texas Instruments",
    "datasheet_revision": "SNVS124G",
    "datasheet_date": "2016-05",
    "page_count": 32,
    "source_url": null,
    "is_family_pdf": true,
    "family_member_mpns": ["LM2596-ADJ", "LM2596-3.3", "LM2596-5.0", "LM2596-12"]
  },
  "categories": ["regulator"],
  "extraction_pages": {
    "base": [1, 2, 4, 5],
    "pinout": [3, 4],
    "regulator": [5, 6, 13, 14, 15]
  },
  "quality_verdict": {"verdict": "extractable", "reason": null}
}
```
