# Automated-survivor vetting workflow

An automated survivor is a transit-like signal that passed the first screening
gates. It is not yet a planet candidate. Survivor vetting is a separate,
bounded workflow so broad survey throughput and focused evidence collection do
not compete for storage or silently change each other's selection rules.

## Order of operations

### 1. Metadata and catalog triage

Run low-storage checks for every queued survivor:

- refresh confirmed-planet and TOI/CTOI matches from the NASA Exoplanet Archive
  and ExoFOP;
- compare the ephemeris against public TESS threshold-crossing events and data
  validation products;
- query Gaia DR3 around the aperture for nearby sources, duplicated-source or
  non-single-star context, astrometric quality, and dilution risk; and
- record which independent light-curve products and observing sectors exist.

These results are small JSON/CSV records. A catalog miss is not proof that the
signal is new.

For one saved signal report, run:

```powershell
.\.venv\Scripts\exohunt.exe context-vet --report path\to\residual.json
```

The command writes a compact report under `results/context_vet/` and does not
download science products. Hubble and Webb matches are treated as sparse,
targeted archival context—not as all-sky transit searches or automatic evidence
for a planet.

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
