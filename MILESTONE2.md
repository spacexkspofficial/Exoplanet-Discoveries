# Milestone 2: multi-sector residual search

Completed on 2026-07-22.

## What changed

- `analyze --sector` now accepts an explicit list of sectors.
- Download metadata records the inferred TIC ID and actual sectors received.
- Reports include separated high-power peaks and checks at 1/3, 1/2, 2, and 3
  times the strongest period.
- `hunt` queries current NASA Exoplanet Archive TOI and confirmed-planet
  ephemerides, masks all usable known transit windows, and searches the residual
  light curve.
- Full-BJD catalog epochs are converted to the BTJD time base used by TESS.
- Mask widths are expanded by 1.5 by default to allow for duration uncertainty,
  ephemeris drift, and transit-timing variations.
- The residual triage rejects low-S/N signals, secondary eclipses, odd/even
  mismatches, strong harmonic ambiguity, and periods close to masked signals.

## Real TOI-700 test

In Sector 3 alone, the original benchmark selected an 8.025-day half-period
alias for TOI-700 c. Combining Sectors 1, 3, 4, and 5 recovered:

- Catalog period: 16.051137 days
- Recovered period: 16.0510406 days
- Difference: about 8.3 seconds
- Recovered depth: 2736 ppm
- Observed transits: 4
- Half-period peak power relative to the correct peak: 0.438

The residual run then masked all four catalogued TOI-700 planets. It removed
1,714 of 62,067 measurements (2.76%). Its strongest remaining peak was near
38.25 days with a white-noise depth S/N of 6.89, several strong harmonic
alternatives, and proximity to the masked TOI-700 d period. It therefore fails
automated triage and is **not** a new candidate.

This is the intended behavior: known signals disappear, aliases are exposed,
and an unimpressive residual is rejected instead of promoted.

## Commands

```powershell
.\.venv\Scripts\exohunt.exe analyze --target "TOI-700" --sector 1 3 4 5 --min-period 5 --max-period 18 --output-dir results\multisector

.\.venv\Scripts\exohunt.exe hunt --target "TOI-700" --sector 1 3 4 5 --min-period 0.5 --max-period 40
```

The next milestone is target-list construction and batch execution with a saved
selection rule, followed by target-pixel/difference-image vetting for survivors.

