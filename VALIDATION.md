# Known-planet validation

Run on 2026-07-22 using five public SPOC light curves at 120-second cadence.
The downloaded FITS files total 9.65 MB. Expected values are NASA Exoplanet
Archive default solutions queried on the same date.

| Planet | Test role | Catalog period | Recovered period | Exact-period error | Recovered box depth | Result |
|---|---|---:|---:|---:|---:|---|
| WASP-18 b | Deep hot Jupiter | 0.941452230 d | 0.941488868 d | 3.17 s | 9262 ppm | Exact |
| LHS 3844 b | Small ultra-short-period planet | 0.462929709 d | 0.462925562 d | 0.36 s | 4055 ppm | Exact |
| pi Men c | Shallow sub-Neptune | 6.267839900 d | 6.266009997 d | 158.10 s | 240 ppm | Exact |
| HD 209458 b | Recent dual-cadence sector | 3.524748590 d | 3.524461932 d | 24.77 s | 12899 ppm | Exact |
| TOI-700 c | Single-sector alias stress test | 16.051137000 d | 8.025096732 d | — | 2917 ppm | Half-period alias |

The first four tests show that the pipeline can recover signals ranging from a
deep hot Jupiter to a roughly 240 ppm shallow transit. Transit depths are
screening estimates from a box model; they are expected to differ from fitted
literature values because real transits have limb-darkened shapes and because
detrending and duration-grid choices affect the box depth.

TOI-700 c is an intentional limitation test. Sector 3 alone has gaps at the
times when an 8-day alias would predict alternating transits, so the strongest
BLS peak occurs at half of the real 16.05-day period. A discovery workflow must
therefore inspect integer multiples and fractions of every candidate period and
combine multiple sectors before reporting an ephemeris.

Re-run the benchmark with:

```powershell
.\.venv\Scripts\exohunt.exe validate
```

Machine-readable details and plots are under `results/validation/`, with the
combined result in `results/validation/summary.json`.

