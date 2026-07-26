# Reporting and recognition guide

This project produces defensible evidence bundles, not automatic planet
announcements. A transit-like dip becomes scientifically interesting only after
duplicate checks, pixel and neighbor vetting, independent extraction, refined
modeling, and expert review. Confirmation normally requires additional evidence
and a refereed publication.

## Current reporting status

As checked on 2026-07-22, ExoFOP-TESS says creation of new community planet
candidates is temporarily paused while its guidelines are revised. Recheck the
live help page before trying to submit. The current support address shown there
is `exofop-support@ipac.caltech.edu`.

Useful community candidates normally need all of the following positive
quantities for TOI Working Group consideration:

- period;
- transit midpoint in BJD;
- transit depth;
- transit duration;
- supporting material.

The packet generator provides screening estimates and converts TESS BTJD to
BJD_TDB by adding 2457000. It intentionally marks the period, epoch, duration,
and uncertainty fields for a refined transit fit.

## What to send, and when

1. Run the catalog, public-TCE, sector-coherence, and pixel checks.
2. Inspect Gaia/TIC neighbors and the official TESS DV reports.
3. Re-extract the signal with another pipeline or light-curve product.
4. Fit the transit jointly, including red-noise treatment and uncertainties.
5. Ask an experienced reviewer through Planet Hunters TESS or TFOP.
6. Recheck ExoFOP. If candidate intake has reopened, follow its current form and
   naming rules rather than treating the generated CSV as a direct upload file.

Do not assign a lowercase planet letter. ExoFOP guidance reserves that naming
style until confirmation in refereed literature. A public candidate identifier,
credit, and eventual planet designation follow community and publication rules;
there is no automatic personal ownership or guaranteed naming right.

## Generate a candidate bundle after promotion

Do not run this for an automated survivor. First complete the manual checks and
record a justified `vetted_candidate` outcome. Then generate the bundle when it
is actually needed for expert review or a reporting avenue.

```powershell
.\.venv\Scripts\exohunt.exe candidate-packet `
  --report results\campaign\CAMPAIGN\TIC_ID_residual.json `
  --pixel-report results\pixel\TIC_ID\TIC_ID_pixel.json `
  --sector-vet-report results\sector_vet\TIC_ID\TIC_ID_sector_vet.json `
  --tce-check-report results\tce_checks\TIC_ID_tce_check.json `
  --submitter "YOUR NAME" `
  --contact-email "YOUR EMAIL"
```

The bundle contains:

- `candidate_packet.md` - human-readable evidence summary;
- a PDF in `output/pdf/` - shareable review copy;
- `exofop_parameter_worksheet.csv` - fields to refine and transfer manually;
- `submission_checklist.json` - pass/fail/needed status;
- `bundle_manifest.json` - exact provenance.

Creating a packet logs a documentation event. It does not itself increment the
vetted-candidate or confirmed-planet metrics.

## Outcome and success logging

Automated commands log completed campaigns, known-planet validations, matching
public TCEs, and generated candidate packets. Human-reviewed outcomes can be
recorded explicitly:

```powershell
.\.venv\Scripts\exohunt.exe log-outcome vetted_candidate `
  --tic 123456789 --label "13.2-day signal" --notes "Reviewed by NAME"

.\.venv\Scripts\exohunt.exe log-outcome false_positive `
  --tic 123456789 --label "13.2-day signal" --notes "Background EB in pixel vetting"

.\.venv\Scripts\exohunt.exe log-outcome confirmed_planet `
  --tic 123456789 --label "Published planet name" --notes "DOI or ADS bibcode"
```

Never use `vetted_candidate` merely because the automated filter passed.
Record the evidence and reviewer in `--notes`. The append-only ledger lives at
`metrics/events.jsonl`, while `metrics/current_stats.json` is rebuilt from it.

## Official resources

- ExoFOP-TESS: https://exofop.ipac.caltech.edu/tess/
- ExoFOP help and candidate guidance: https://exofop.ipac.caltech.edu/tess/help.php
- TESS Follow-up Observing Program: https://heasarc.gsfc.nasa.gov/docs/tess/tfop.html
- Join TFOP: https://tess.mit.edu/followup/apply-join-tfop/
- Planet Hunters TESS: https://science.nasa.gov/citizen-science/planet-hunters-tess/
- Public MAST TCE tables: https://archive.stsci.edu/tess/bulk_downloads/bulk_downloads_tce.html
