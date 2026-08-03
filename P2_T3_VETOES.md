# P2 T3 veto shipping-path implementation

Implemented and measured 2026-07-28 PDT through the real `batch-hunt`
single-target analysis path. This is a reversible signal-screening change,
not a planet, novelty, completeness, or reliability claim.

## Decision

The existing pure functions in `vetoes.py` now produce a versioned
`t3_vetoes` evidence block in every shipping report:

1. catalog-density duration consistency (`pass`, manual-review `flag`, or
   signal `kill`);
2. implied companion radius (`pass`, not evaluable, or eclipsing-binary
   lane);
3. folded odd/even depth consistency using all in-transit cadences in each
   parity class;
4. a full out-of-transit phase scan for secondary eclipses; and
5. per-event sampling support requiring in-transit cadences plus a two-sided
   local baseline.

Missing stellar mass/radius disables only the physical check that needs it.
Folded flux checks still run. Single-sector signals require two supported
events; multi-sector signals require three, matching the search policy.
Duration-density flags do not kill a signal, but they route an otherwise
passing signal to manual review. Implied companions above the configured
planet-lane ceiling are retained explicitly in an eclipsing-binary lane and
cannot enter the planet-like survivor queue.

The complete veto configuration, including
`policy_version=t3-family-wise-secondary-v1`, is part of scientific settings.
Campaign checkpoint reuse therefore cannot mix pre-T3, naive T3, and corrected
T3 reports.

## Measured correction: full-phase look-elsewhere effect

The first literal wiring applied the historical 3-sigma threshold to the
strongest local window found anywhere outside the primary. That is not a
3-sigma global test: it searches many correlated windows and keeps the
maximum.

On 500 deterministic pure-white-noise 27-day folds at 10-minute cadence,
period 3 days, and duration 2.4 hours:

| Rule | Pure-noise kills | Fraction |
|---|---:|---:|
| Naive maximum local S/N > 3 | 154 / 500 | 30.8% |
| Median-error + family-wise phase correction | 1 / 500 | 0.2% |

The corrected rule:

- uses the asymptotic standard error of a median,
  `sqrt(pi/2) * sigma / sqrt(n)`, for folded median depths;
- records the number of tested phase windows and the strongest local S/N;
- Bonferroni-adjusts the one-sided local normal-tail probability across every
  tested window; and
- compares that family-wise probability with the configured global 3-sigma
  tail.

The same median standard-error correction applies to the folded odd/even
comparison. A permanent 500-fold deterministic regression test caps the
secondary scan's observed pure-noise kill fraction at 1%.

The naive cohort arm is retained as gitignored diagnostic evidence. It is not
the shipping decision.

## Locked 150-target result

The final comparison uses the exact ordered cohort in
`targets/p2_search_grid_golden_150.csv`. The baseline is the corrected
density-grid shipping arm from the preceding behavior commit. Search,
photometry, masking, and deeper-vetting outputs must remain exact; only T3
evidence, legacy screening fields replaced by T3 estimators, triage, and
classification may change.

All 150 observation windows, strongest signals, search grids, and the complete
pre-T3 science payload are exact to the baseline arm.

| Arm | Reports | Survivors | Rejected | Errors |
|---|---:|---:|---:|---:|
| Density-grid baseline | 150 | 6 | 144 | 0 |
| Naive local-secondary T3 diagnostic | 150 | 1 | 149 | 0 |
| Corrected family-wise T3 | 150 | 1 | 149 | 0 |

The naive arm marked 97/150 signals as secondaries and lost five of six
baseline survivors. The corrected arm reduces secondary kills to 35/150. Its
unchanged 1-survivor total is not evidence that the naive rule was safe: the
same five baseline survivors have independent, corrected T3 evidence.

| Corrected T3 check | Pass | Flag | Kill / EB | Not evaluable |
|---|---:|---:|---:|---:|
| Duration-density | 4 | 56 | 9 | 81 |
| Depth physicality | 150 | 0 | 0 | 0 |
| Folded odd/even | 92 | 0 | 10 | 48 |
| Full-phase secondary | 84 | 0 | 35 | 31 |
| Two-sided event support | 79 meet minimum | — | 71 below minimum | 0 |

The duration-density distribution is consistent with the preceding
search-grid finding: physical grid endpoints are still the dominant fitted
class. Most duration flags occur on signals already rejected by search-grid
or other gates; they remain recorded for manual review.

Transitions are 144 rejected-to-rejected, five survivor-to-rejected, and one
survivor-to-survivor, with no gains:

| Lost baseline survivor | Corrected T3 evidence |
|---:|---|
| TIC 81699706 | secondary local S/N 6.12 after family-wise correction |
| TIC 158588995 | one supported event; two required |
| TIC 172889501 | duration-density ratio 2.533 (kill) and secondary S/N 11.278 |
| TIC 192543860 | zero supported events; two required |
| TIC 449046160 | secondary local S/N 6.961 after family-wise correction |

TIC 54147357 is the sole remaining automated survivor. It still carries the
pre-existing `needs_manual_review` follow-up tier; T3 does not promote it or
raise the project claim ceiling.

The cohort is a characterization set, not a calibrated known-planet or
inverted-data release gate. These counts measure behavior and guard against
accidental search changes; they do not establish sensitivity or reliability.

## Evidence and verification

Permanent tests cover:

- aggregate T3 decision and evidence retention;
- eclipsing-binary lane routing;
- two-sided event support;
- full-phase eccentric-secondary recovery;
- deterministic look-elsewhere calibration;
- non-finite cadence handling;
- manual-review routing for duration-density flags;
- shipping report/config integration; and
- frozen-arm A/B identity and pre-T3 science invariance.

Complete worktree suite: **236 passed**.

Reproduce the final arm:

```powershell
$env:PYTHONPATH = "<worktree>\src"
$env:EXOHUNT_CACHE_DIR = "<frozen-golden-cache>"
python -m exohunt.cli batch-hunt `
  --targets targets/p2_search_grid_golden_150.csv `
  --output-dir results/p2_gates/t3_shipping_150 `
  --author TESScut --cadence-seconds 158 `
  --max-targets 150 --cache-max-gb 8 `
  --workers 4 --download-workers 2 --allow-no-known
```

Measure the delta:

```powershell
python scripts/measure_p2_t3_ab.py `
  --baseline-dir results/p2_gates/search_grid_shipping_density_railfix_150 `
  --t3-dir results/p2_gates/t3_shipping_150 `
  --output results/p2_gates/t3_shipping_ab_150.json
```

Gitignored evidence:

- `results/p2_gates/t3_shipping_naive_secondary_150/`
- `results/p2_gates/t3_shipping_150/`
- `results/p2_gates/t3_shipping_ab_150.json`
