# Automated-survivor vetting workflow

An automated survivor is a transit-like signal that passed the first screening
gates. It is not yet a planet candidate. Survivor vetting is a separate,
bounded workflow so broad survey throughput and focused evidence collection do
not compete for storage or silently change each other's selection rules.

If a known transiting system has a catalog period but lacks the epoch or
duration needed for safe masking, `--allow-no-known` performs an explicitly
unmasked recovery-only scan. That scan is forced into a non-promotable review
class and records the incomplete mask fields instead of becoming a permanent
batch error.

## Order of operations

### 1. Metadata and catalog triage

Run low-storage checks for every queued survivor:

- refresh confirmed-planet and TOI/CTOI matches from the NASA Exoplanet Archive;
- compare the ephemeris against official public TESS threshold-crossing-event
  statistics;
- query the live Villanova TESS Eclipsing Binary Catalog by exact TIC and
  compare its period and simple eclipse aliases;
- query SIMBAD object types and aliases;
- query Gaia DR3 variability, eclipsing-binary, planetary-transit, and
  non-single-star records for the TIC/Gaia cross-match;
- inspect nearby TIC/Gaia sources for crowding and dilution risk; and
- record which independent light-curve products and observing sectors exist.

These results are small JSON/CSV records. A catalog miss is not proof that the
signal is new. ExoFOP and literature review remain explicit human checks before
promotion; the software does not claim to have queried a private or unstable
interface.

For one saved signal report, run:

```powershell
.\.venv\Scripts\exohunt.exe context-vet --report path\to\residual.json
```

The command writes a compact report under `results/context_vet/` and does not
download science products. Hubble and Webb matches are treated as sparse,
targeted archival context—not as all-sky transit searches or automatic evidence
for a planet.

After the active campaign finishes, process its complete durable queue with:

```powershell
.\.venv\Scripts\exohunt.exe context-vet-queue `
  --queue results\campaign\sector100_expansion_5000\deep_followup_queue.json `
  --output-dir results\vetting\sector100_expansion_5000\context `
  --workers 2
```

This idempotent job writes a compact context JSON per TIC plus checkpointed
JSON/CSV summaries. It queries which TESS reductions, Kepler/K2 observations,
Hubble/Webb programs, and other MAST holdings exist, but downloads zero science
products. Failed metadata queries remain retryable without repeating completed
targets.

To include all campaigns already scanned under the same rules, build a combined
queue from their durable checkpoints and reports:

```powershell
.\.venv\Scripts\exohunt.exe build-context-queue `
  --campaign-root results\campaign `
  --output results\vetting\all_campaigns\context_queue.json

.\.venv\Scripts\exohunt.exe context-vet-queue `
  --queue results\vetting\all_campaigns\context_queue.json `
  --output-dir results\vetting\all_campaigns\context `
  --workers 2
```

This does not redownload TESS pixels. It carries forward the first-pass signal,
automated triage, odd/even and secondary-eclipse flags, red-noise significance,
event coverage, sensitivity probe, pipeline version, searched sectors, and
initial NASA catalog snapshot. Multiple saved scans of the same TIC are
deduplicated while retaining every source-report reference.

## Metadata disposition lanes

The word `survivor` is only a first-pass queue state. The metadata pass replaces
it with a more specific disposition:

- `known_planet_rediscovery`: period match to a NASA TOI/confirmed planet;
- `known_tce_rediscovery`: period/alias match to an official TESS TCE;
- `known_eb_rediscovery`: exact TIC plus period/alias match to the TESS EB
  Catalog or Gaia EB evidence;
- `known_eb_host_residual_review`: known binary host with a different residual
  period, retained for binary subtraction, ETV, or circumbinary review;
- `known_variable_star_review`: stellar variability remains a live alternative;
- `crowding_contamination_review`: nearby-source localization is required;
- `context_incomplete` or `catalog_coverage_gap`: public checks must be retried
  or supplemented; and
- `unresolved_transit_like_signal`: the checked catalogs do not explain the
  feature, but it is still not a planet candidate.

An EB match takes precedence over a generic TCE match because a TCE records a
detected threshold crossing, not its astrophysical nature.

## Measured science lanes

Metadata dispositions describe what public catalogs say. The pixel and sector
stages below instead measure the actual photometry, so their verdicts replace
the metadata disposition on the dashboard:

- `pixel_offset_contamination`: the difference-image centroid sits more than one
  TESS pixel (about 21 arcseconds) from the target, so the lost light most
  likely belongs to a neighboring source;
- `single_sector_unconfirmed`: the light was lost on target, but only the
  discovery sector supports the fixed ephemeris; and
- `science_vetted_lead`: the light was lost on target *and* at least one
  independently searched sector reproduces the ephemeris.

`science_vetted_lead` is the strongest state this pipeline produces. It is still
not a planet candidate: independent reduction, alias checks, and human review in
steps 3 through 5 remain outstanding. A star is only reclassified when both
gates were actually measured; a single gate on its own leaves the metadata
disposition in place.

## The shared-ephemeris screen

Above every per-star lane sits one population-level question: does this
ephemeris belong to the star, or to the observatory? `common-mode-screen`
answers it by counting how many unrelated targets observed in the same campaign
carry the same period *and* phase, against the uniform-phase expectation.

- `common_mode_systematic`: the ephemeris is shared far beyond chance by targets
  spanning several cameras or a wide area of sky; and
- `localized_coincidence`: the sharing is confined to close neighbours, which
  points at one bright contaminating source rather than the spacecraft.

This verdict outranks the pixel and sector gates. A spacecraft event recurs
identically in every sector, so step 3 below passes it by construction and
cannot be used as a clearance. Run the screen before promoting anything.

Because these verdicts are measurements rather than catalog lookups, a recorded
human outcome (`false_positive`, `vetted_candidate`, `confirmed_planet`) still
outranks them everywhere in the dashboard.

### 2. Pixel-source localization

For the highest-priority survivors, use a target-pixel file or a small TESScut
cube to compare in-transit and out-of-transit images. Measure the difference-
image centroid and test alternate apertures. Reject or downgrade signals that
move toward a neighboring source, detector edge, scattered-light feature, or
background variation.

### 3. Independent extraction and epoch tests

Re-run the saved ephemeris in every available TESS sector and compare multiple
reductions when available:

- mission or TESS-SPOC light curves;
- MIT Quick-Look Pipeline (QLP); and
- TESS-Gaia Light Curves (TGLC), especially in crowded fields.

These are independent reductions of largely the same TESS images, not
independent telescopes. Agreement is valuable evidence against an extraction
artifact; it is not confirmation by itself. Disagreement is a reason to inspect
pixels, apertures, background, and detrending.

### 4. Longer-baseline and cross-survey context

Query other time-domain holdings only for survivors that pass the earlier
checks:

- Kepler or K2 when the sky coverage overlaps;
- ZTF or ASAS-SN for eclipsing-binary, rotation, flare, or long-term variability
  context; and
- later TESS sectors as they become public.

Ground surveys usually do not have the precision or cadence to reproduce every
shallow TESS transit. Their strongest role here is finding variability or deep
eclipses that falsify a planetary interpretation.

### 5. Human disposition and follow-up

A target can move from `automated_survivor` to `vetted_candidate` only after its
evidence packet records:

1. a stable ephemeris or a properly labeled single-event model;
2. adequate event coverage and red-noise-adjusted significance;
3. no odd/even, secondary-eclipse, or implausible-duration veto;
4. an on-target pixel localization with documented contamination limits;
5. no matching known object or public TCE disposition;
6. agreement in an alternate extraction or observing epoch when available; and
7. manual review of the light curve, pixels, catalogs, and saved settings.

Ground photometry, reconnaissance spectroscopy, high-resolution imaging, and
radial velocities belong after this software vetting. Coordination through the
TESS Follow-up Observing Program is appropriate only for genuinely vetted
targets.

## Storage and scheduling policy

- Do not bulk-download another survey.
- Catalog checks run for the queue first and retain only compact responses.
- Pixel files and alternate light curves are fetched target-by-target for a
  small priority batch.
- Re-downloadable FITS products stay in the existing rolling cache.
- Preserve per-target manifests, source URLs, query timestamps, checksums,
  scalar measurements, and compact diagnostic plots.
- The project-wide 20 GB ceiling remains authoritative; focused vetting should
  stop rather than evict durable campaign evidence.
- While the 5,000-star screen is active, follow-up downloads should remain
  paused or heavily throttled so archive bandwidth and the cache serve the
  survey. Catalog-only checks are safe to run concurrently.

## Primary data services

- [MAST TESS-SPOC](https://archive.stsci.edu/hlsp/tess-spoc)
- [MAST QLP](https://archive.stsci.edu/hlsp/qlp)
- [MAST TGLC](https://archive.stsci.edu/hlsp/tglc)
- [NASA Exoplanet Archive TAP](https://exoplanetarchive.ipac.caltech.edu/docs/TAP/usingTAP.html)
- [Gaia DR3 archive documentation](https://gea.esac.esa.int/archive/documentation/GDR3/)
- [IRSA ZTF light-curve API](https://irsa.ipac.caltech.edu/docs/program_interface/ztf_lightcurve_api.html)
- [TESS Follow-up Observing Program](https://tess.mit.edu/followup/)
