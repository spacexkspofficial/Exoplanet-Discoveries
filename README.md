# Exoplanet Discoveries

A small, reproducible starting point for finding **transit signals** in public
TESS data. A signal is not automatically a planet: eclipsing binaries, nearby
stars, stellar variability, and spacecraft systematics are common impostors.

## The realistic goal

Start by recovering a known hot Jupiter from one small processed light curve.
Then move to a curated target list, run the same search reproducibly, and vet
the survivors against public catalogs and pixel data. Do not bulk-download raw
full-frame images for this first stage.

## Setup on Windows

Install 64-bit Python 3.11 or 3.12, open PowerShell in this directory, then run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Codex can also prepare the local environment using its bundled Python runtime.

## Local dashboard

The stellar-survey dashboard runs only on this computer. It has no cloud
hosting manifest, external database, analytics, or remote application runtime.

```powershell
Set-Location dashboard
npm.cmd install
npm.cmd run build
Set-Location ..
.\.venv\Scripts\exohunt-dashboard.exe
```

Open `http://127.0.0.1:8765`. The server deliberately binds to `127.0.0.1`,
not `0.0.0.0`, so other LAN devices and the public internet cannot connect.
Each metrics refresh also updates the local dashboard dataset, and the browser
polls that local file every five seconds and whenever the tab regains focus.
The 3D view converts ICRS positions into Sun-centered Galactic XYZ coordinates;
its Milky Way mid-plane disk, distance rings, and vertical scale curves remain
rigidly aligned with the stars while the camera orbits.

## First successful experiment

WASP-18 b is a deliberately easy, already-known signal. The point is to prove
that the download, detrending, period search, and plots all work before hunting
for anything new.

```powershell
.\.venv\Scripts\exohunt.exe analyze --target "WASP-18" --sector 2
```

The command creates a JSON screening report and a PNG diagnostic plot under
`results/`. The JSON says “screening result” on purpose; BLS signal-to-noise is
not confirmation.

Check a TESS Input Catalog ID against the NASA Exoplanet Archive's current TOI
and confirmed-planet tables:

```powershell
.\.venv\Scripts\exohunt.exe catalog-check --tic 100100827
```

No match is **not** proof of novelty. ExoFOP updates faster than the Archive,
and TCE/DV products, papers, neighboring stars, and aliases must also be checked.

Run the synthetic detector test without downloading astronomy data:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run the complete known-planet benchmark (about five ordinary light-curve files):

```powershell
.\.venv\Scripts\exohunt.exe validate
```

It tests a deep hot Jupiter, a small ultra-short-period planet, a shallow
sub-Neptune, a recent dual-cadence sector, and an intentional single-sector
harmonic-alias case. Reports and plots are saved under `results/validation/`.
The latest interpreted results are summarized in [`VALIDATION.md`](VALIDATION.md).

## Multi-sector and residual searches

Combining selected sectors helps distinguish a real period from half/double
period aliases without downloading every observation of a target:

```powershell
.\.venv\Scripts\exohunt.exe analyze --target "TOI-700" --sector 1 3 4 5 --min-period 5 --max-period 18 --output-dir results\multisector
```

For a system with known TOIs, `hunt` retrieves their periods, transit epochs,
and durations from the NASA Exoplanet Archive; expands each transit window by a
safety factor; removes those measurements; and searches only the residual data:

```powershell
.\.venv\Scripts\exohunt.exe hunt --target "TOI-700" --sector 1 3 4 5 --min-period 0.5 --max-period 40
```

The residual report includes the five strongest separated peaks, BLS power at
simple period fractions/multiples, proximity to every masked period, and a
conservative automated triage. Archive masking is only a first pass: ExoFOP can
be fresher, and pixel-level contamination checks remain mandatory.

## Reproducible pilot campaigns

Create a small target list using saved scientific criteria plus actual MAST
sector availability:

```powershell
.\.venv\Scripts\exohunt.exe make-targets --limit 3 --output targets\pilot_cool_single_hosts.csv
```

The default rule selects bright nearby cool stars with exactly one unique
transiting ephemeris across the NASA TOI and confirmed-planet tables and at
least two SPOC light-curve sectors. The CSV and adjacent JSON manifest preserve
the inputs and selection rule.

Run or resume every target in that list:

```powershell
.\.venv\Scripts\exohunt.exe batch-hunt --targets targets\pilot_cool_single_hosts.csv --output-dir results\campaign\pilot_cool_single_hosts
```

Existing per-target reports are reused unless `--force` is supplied, so a
temporary archive/network failure does not discard completed work. Transient
network failures are retried up to three times. A later idempotent rerun reuses
matching rejected JSON reports even after their bulky plots have been pruned,
while survivor reuse still requires its durable diagnostic plot.

### Bounded storage for large campaigns

`batch-hunt` keeps the scientific record without keeping every bulky,
re-downloadable input forever. By default it:

- retains the metrics ledger, campaign JSON/CSV summaries, per-target JSON
  diagnostics, and every survivor plot;
- deletes PNG diagnostics for automatically rejected targets after the
  campaign summary is finalized; and
- removes the oldest downloaded FITS products whenever `data/lightkurve`
  exceeds 2 decimal GB.

Targets are downloaded and analyzed one at a time; the campaign is not
pre-downloaded. Cache pruning runs after every target, so a 5,000-star list
still uses the same bounded FITS allowance instead of staging hundreds of
gigabytes. The CLI process is fully automated and does not require an AI agent
to remain attached.

Change the rolling FITS allowance with `--cache-max-gb`. Use
`--retain-rejected-plots` only when every rejected diagnostic image is needed.
Preview or apply the same policy to existing campaigns with:

```powershell
.\.venv\Scripts\exohunt.exe storage-prune --cache-max-gb 2 --dry-run
.\.venv\Scripts\exohunt.exe storage-prune --cache-max-gb 2
```

The prune command is limited to FITS files under the selected cache and PNGs
explicitly referenced by rejected rows in batch summaries. Survivor and
validation plots are not selected. Removed FITS inputs remain recoverable by
downloading them again from MAST; removed rejected-target plots can be
regenerated deliberately with a forced, targeted rerun from their retained
JSON settings.

### New-sector, zero-catalogued-planet lane

To search stars with no NASA-catalogued TOI or confirmed planet, start from an
official TESS sector target list and make a reproducible small-star sample:

```powershell
.\.venv\Scripts\exohunt.exe make-blank-targets `
  --target-list targets\sector105_2min_targets.csv --sector 105 `
  --limit 20 --pool-size 6000 --max-tmag 11.5 --max-teff 4500 `
  --max-stellar-radius 0.8 --max-distance 150 `
  --output targets\sector105_blank_small_batch.csv
```

For a large overnight screen, build a camera/CCD-balanced list locally from the
same official target file. Completed campaign TIC IDs in the ledger are excluded
by default; NASA catalog rows are checked and known ephemerides are masked during
the subsequent search:

```powershell
.\.venv\Scripts\exohunt.exe make-sector-targets `
  --target-list targets\sector105_2min_targets.csv --sector 105 `
  --limit 1000 --min-tmag 7 --max-tmag 12 `
  --output targets\sector105_overnight_1000.csv
```

For the next non-overlapping batch, repeat the command with
`--exclude-list targets\sector105_blank_small_batch.csv`. The adjacent JSON
manifest records the filters and excluded TIC IDs.

Search a newly public sector directly from its full-frame cutouts when a
target-specific SPOC/QLP light curve is not yet available:

```powershell
.\.venv\Scripts\exohunt.exe batch-hunt `
  --targets targets\sector105_blank_small_batch.csv `
  --output-dir results\campaign\sector105_blank_small_batch `
  --author TESScut --cadence-seconds 158 --allow-no-known `
  --min-period 0.5 --max-period 13
```

`--allow-no-known` changes the search mode from “mask a known planet and look
for another” to “search this zero-catalogued-planet star.” TESScut extraction
uses an 11-by-11-pixel cutout, target aperture, and per-cadence background
subtraction. The former campaign-wide rule that rejected targets merely because
their fitted BLS reference epochs clustered was removed: a fitted reference
epoch is not cadence-level evidence of a spacecraft systematic. Any future
common-mode veto must use shared cadences plus detector or background evidence,
and the current pipeline records that campaign-level screen as not applied.

For an automated survivor, download one target-pixel file and locate the source
of the lost light:

```powershell
.\.venv\Scripts\exohunt.exe pixel-vet --report path\to\candidate.json --sector 1
.\.venv\Scripts\exohunt.exe pixel-vet --report path\to\sector105_signal.json --sector 105 --author TESScut --cadence-seconds 158
```

The difference-image centroid is a screening measurement, not confirmation;
one TESS pixel spans roughly 21 arcseconds.

Test the same ephemeris independently in each sector, then compare it with the
official public MAST threshold-crossing-event tables:

```powershell
.\.venv\Scripts\exohunt.exe sector-vet --report path\to\residual.json
.\.venv\Scripts\exohunt.exe tce-check --report path\to\residual.json
```

The TCE command logs an exact or simple-harmonic rediscovery automatically.
An absent match is useful, but is not proof that a signal is new.

## Logging first; reports only after vetting

Routine searches save compact JSON/CSV evidence and update the cumulative
ledger. Do not generate narrative reports for automated passes. Inspect the
current totals with:

```powershell
.\.venv\Scripts\exohunt.exe metrics-summary
```

The append-only event log is `metrics/events.jsonl`; the current aggregate is
`metrics/current_stats.json`. Campaign reruns and outcome replays are counted
idempotently. The report generator stays available, but should be invoked only
after a signal has been promoted to a genuinely vetted candidate. See
[`REPORTING_GUIDE.md`](REPORTING_GUIDE.md) for that later workflow.

## Measure sensitivity with injected planets

Before expanding a search, inject synthetic transits into the actual residual
light curve and ask whether the unchanged detector recovers them:

```powershell
.\.venv\Scripts\exohunt.exe inject-recover --target "TIC 359271092" --tic 359271092 --sector 36 37 62 --periods 1 5 12 --depths 100 300 1000
```

The command masks catalogued transits, injects deterministic box-shaped events,
runs the normal BLS search, and saves JSON, CSV, and a recovery heatmap under
`results/completeness/`. The first GJ 341 calibration recovered all tested
300- and 1000-ppm signals, plus the 1-day 100-ppm signal; it missed the 5- and
12-day 100-ppm cases. See [`MILESTONE4.md`](MILESTONE4.md).

This small grid uses one phase per period. It measures the behavior of this
pipeline on this light curve, but it is not a publishable completeness result.
A survey-quality measurement needs several phases per cell, more depths and
periods, realistic limb-darkened transit shapes, and the same test on every
searched target.

## A defensible discovery workflow

1. Recover several known planets with different depths and periods.
2. Define a target sample before looking at results—for example, bright nearby
   small stars with processed TESS-SPOC light curves.
3. Search every target with saved settings and keep compact diagnostics for
   every outcome, not only hits; bulky cache files and rejected-target plots
   may roll off under the documented retention policy.
4. Run injection/recovery tests to measure which signals the data and pipeline
   could have found; preserve misses as carefully as candidates.
5. Remove known confirmed planets, TOIs/CTOIs, and existing TESS threshold-
   crossing events (TCEs).
6. Reject common false positives: period aliases; odd/even depth differences;
   secondary eclipses; V-shaped events; signals tied to data gaps; and stellar
   rotation or flares.
7. Inspect the target-pixel file and difference image. TESS pixels are large,
   so a nearby eclipsing binary can contaminate the target aperture.
8. Require the signal to repeat in another sector when possible and reproduce
   it with another light-curve product (for example SPOC versus QLP/TGLC).
9. Ask an experienced exoplanet observer to review the packet before submission.
   A strong community candidate can be entered in ExoFOP and coordinated for
   follow-up; confirmation or statistical validation is a later scientific step.

## Best public resources

- **[MAST / TESS](https://archive.stsci.edu/missions-and-data/tess):** primary
  home for light curves, target-pixel files, full-frame images, and
  data-validation products.
- **[TESS-SPOC](https://archive.stsci.edu/hlsp/tess-spoc):** processed FFI light
  curves for up to roughly 160,000 stars per sector; a good laptop-scale
  starting set when sampled selectively.
- **[QLP](https://archive.stsci.edu/hlsp/qlp) and
  [TGLC](https://archive.stsci.edu/hlsp/tglc):** alternative processed light
  curves, useful for more stars and for independent checks.
- **[NASA Exoplanet Archive](https://exoplanetarchive.ipac.caltech.edu/):**
  confirmed planets, TOIs, parameters, and a TAP API.
- **[ExoFOP-TESS](https://exofop.ipac.caltech.edu/tess/):** the freshest
  TOI/CTOI dispositions and follow-up observations.
- **Gaia / TIC / SIMBAD / ADS:** neighboring-source, stellar-property, alias, and
  literature checks.
- **[Planet Hunters TESS](https://science.nasa.gov/citizen-science/planet-hunters-tess/):**
  the easiest route to learn visual vetting and work with a team that already
  has a follow-up/publication path.

## What transit data can tell you

From the light curve: orbital period, transit time, transit depth, duration,
planet-to-star radius ratio, and constraints on orbital geometry. With good
stellar properties from Gaia/TIC or spectroscopy, you can estimate planet
radius, semimajor axis, incident flux, and equilibrium temperature. Multiple
planets can sometimes show transit-timing variations.

Transit photometry alone generally cannot give a trustworthy mass, bulk density,
composition, surface conditions, or evidence of life. Those need radial velocity,
additional timing, or specialized atmospheric observations. “Habitable zone” is
an incident-energy estimate, not a finding that a planet is habitable.

## Keep the search scientifically useful

- Preserve the exact input target list, software version, settings, and raw
  downloaded files or archive identifiers.
- Record null results and every rejection reason.
- Never call an unvetted dip a discovery or a validated planet.
- Cite the mission, archive, light-curve producer, and data DOI in any public work.
