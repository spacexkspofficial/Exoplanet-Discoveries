# Milestone 3: first reproducible pilot campaign

Completed on 2026-07-22.

## Selection rule

The target list was generated programmatically rather than hand-picked. The
default campaign requires:

- TOI disposition `CP` or `KP`;
- one unique transiting ephemeris after combining the NASA TOI and confirmed-
  planet tables;
- TESS magnitude at most 11.5;
- stellar effective temperature at most 4200 K;
- stellar radius at most 0.8 solar radii;
- distance at most 100 parsecs;
- known period between 0.5 and 20 days;
- complete period, midpoint, and duration for masking;
- at least two public 120-second SPOC sectors.

Candidates are ordered by brightness and TIC ID. Up to three available sectors
are chosen with the most compact deterministic window. The complete manifest is
[`targets/pilot_cool_single_hosts.json`](targets/pilot_cool_single_hosts.json).

## Pilot targets and results

| Host | TIC | Sectors | Strongest residual | Box depth | Approx. S/N | Decision |
|---|---:|---|---:|---:|---:|---|
| GJ 341 | 359271092 | 36, 37, 62 | 0.542558 d | 21 ppm | 6.34 | Rejected: below S/N gate |
| GJ 436 | 138819293 | 22, 49 | 15.239372 d | 198 ppm | 7.81 | Rejected: odd/even mismatch above 3 sigma |
| TOI-260 | 37749396 | 3, 42, 70 | 13.237143 d | 259 ppm | 10.20 | Rejected: within 5% of the masked period/harmonic |

Result: **zero automated survivors**. This is a good pilot outcome: the system
used three different rejection paths and did not promote an attractive-looking
peak merely because its nominal S/N exceeded 7.1.

The batch is resumable. A temporary MAST disconnect affected GJ 436 on the first
attempt; the second invocation reused both completed targets and ran only the
missing one.

## Long-baseline safety

Astropy's automatic BLS grid normally scales with time baseline squared. Sparse
sectors separated by years can therefore create an impractically large grid.
The search now retains the full baseline but adaptively caps the period grid at
100,000 trials and records whether that cap was used. This kept the pilot within
laptop memory while preserving useful minute-scale period resolution.

## Pixel-level validation

The difference-image stage was validated on known TOI-700 c in Sector 1:

- 30 in-transit target-pixel cadences;
- 210 nearby out-of-transit cadences;
- lost-light centroid offset: 0.60 TESS pixel, approximately 12.7 arcseconds;
- result: on target within the one-pixel screening threshold.

The diagnostic is saved at
[`results/pixel/TOI-700_s1_pixel.png`](results/pixel/TOI-700_s1_pixel.png).
An on-target result is necessary but not sufficient because TESS pixels are
large and can contain multiple catalogued stars.

## Data footprint

All TESS light-curve and target-pixel products downloaded across the project so
far occupy about 87 MB, comfortably within the original Wi-Fi/laptop goal.

## Commands

```powershell
.\.venv\Scripts\exohunt.exe make-targets --limit 3 --output targets\pilot_cool_single_hosts.csv

.\.venv\Scripts\exohunt.exe batch-hunt --targets targets\pilot_cool_single_hosts.csv --output-dir results\campaign\pilot_cool_single_hosts

.\.venv\Scripts\exohunt.exe pixel-vet --report results\multisector\TOI-700_s1-3-4-5.json --sector 1
```

The injection/recovery calibration chosen as the next step is documented in
[`MILESTONE4.md`](MILESTONE4.md).
