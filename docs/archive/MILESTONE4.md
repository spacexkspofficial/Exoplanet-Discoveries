# Milestone 4: first injection/recovery calibration

Completed on 2026-07-22.

## Why this milestone came before scaling

A null result is only interpretable if the pipeline's sensitivity is measured.
The new `inject-recover` command therefore adds synthetic transits to a real,
catalog-masked residual light curve and sends the result through the unchanged
BLS detector. It records exact recoveries, simple period aliases, and misses.

## Calibration data and settings

- Target: GJ 341 / TIC 359271092
- Public data: TESS SPOC 120-second light curves, Sectors 36, 37, and 62
- Masked signal: TOI-741.01, period 7.5768334 days
- Search range: 0.5 to 20 days
- Trial-period cap: 100,000 (100,001 samples were produced by the endpoint rule)
- Injected periods: 1, 5, and 12 days
- Injected depths: 100, 300, and 1000 ppm
- Detection gate: BLS depth S/N at least 7.1 and at least two observed transits
- Random seed: 42, producing one deterministic phase per period

Durations scale approximately as period to the one-third power and were 1.17,
2.00, and 2.68 hours at the three periods.

## Results

| Injected depth | 1 day | 5 days | 12 days |
|---:|---|---|---|
| 100 ppm | Exact, S/N 11.43 | Miss | Miss |
| 300 ppm | Exact, S/N 35.53 | Exact, S/N 19.45 | Exact, S/N 15.62 |
| 1000 ppm | Exact, S/N 119.55 | Exact, S/N 67.68 | Exact, S/N 49.01 |

The exact recovery fraction was **7/9 (77.8%)**. There were no harmonic-alias
recoveries. At 100 ppm, the unrecovered 5- and 12-day injections lost to
unrelated residual peaks near 15.76 and 15.62 days even though those peaks had
nominal S/N values above 8.8. This is a useful demonstration that an S/N gate
alone does not establish sensitivity to a planet at the injected ephemeris.

For intuition only, transit depth is approximately `(planet radius / stellar
radius)^2`. With the catalogued 0.53-solar-radius host, 100, 300, and 1000 ppm
correspond to approximately 0.58, 1.00, and 1.83 Earth radii before accounting
for dilution or stellar-radius uncertainty.

The machine-readable results and plot are:

- [`results/completeness/TIC_359271092_s36-37-62_injections.json`](results/completeness/TIC_359271092_s36-37-62_injections.json)
- [`results/completeness/TIC_359271092_s36-37-62_injections.csv`](results/completeness/TIC_359271092_s36-37-62_injections.csv)
- [`results/completeness/TIC_359271092_s36-37-62_injections.png`](results/completeness/TIC_359271092_s36-37-62_injections.png)

## Interpretation and next scale

This is a pipeline calibration, not a survey completeness measurement. A single
box-shaped injection phase does not sample data gaps, phase-dependent masking,
limb darkening, impact parameter, or transit-timing variations.

The next efficient scale is a **10-target campaign using the existing selection
rule**, with a three-phase injection grid at 1, 5, and 12 days and 100, 200, and
300 ppm on every target. That is 27 trials per star (270 total), concentrated
around the measured transition rather than spending most computation on the
already-easy 1000-ppm regime. The search can then report recovery fraction by
period, depth, and target before any residual peak is promoted for manual
vetting.

## Command

```powershell
.\.venv\Scripts\exohunt.exe inject-recover --target "TIC 359271092" --tic 359271092 --sector 36 37 62 --periods 1 5 12 --depths 100 300 1000 --min-period 0.5 --max-period 20
```
