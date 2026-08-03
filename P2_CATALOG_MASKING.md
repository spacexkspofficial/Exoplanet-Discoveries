# P2 catalog ephemeris masking measurement

Measured 2026-07-27 through the real `batch-hunt` path. This report closes the
shipping correctness bug recorded in `PROGRESS.md` correction 12. It does not
claim novelty, validate either diagnostic survivor, or complete P2.

## Decision

The previous pipeline treated every catalog row with a period, epoch, and
duration as safely maskable. On the affected Sector 100 targets, catalog epochs
predated the light curves by 70–1,616 cycles. A fixed duration mask could
therefore remove arbitrary phase while leaving the known transit in the data.

The replacement behavior is:

1. Query the NASA Exoplanet Archive TOI and Planetary Systems tables for the
   asymmetric period, transit-midpoint, and duration uncertainties.
2. Collapse each asymmetric pair to the larger absolute value.
3. Propagate a conservative linear timing envelope to the complete observation
   window: `epoch_error + max_abs_cycles * period_error`.
4. When the propagated error is at most one reported transit duration, project
   the epoch into the observation window and add the propagated error to both
   sides of the duration mask.
5. When uncertainty is missing, invalid, or larger than one duration, remove
   zero cadences and report the signal as unmaskable. The normal residual search
   stops. `--allow-no-known` may continue only as a recovery-only scan with
   promotion disabled.
6. Injection-recovery and sector-coherence commands refuse to run after any
   unsafe catalog mask.

The one-duration ceiling and linear propagation rule are part of the scientific
configuration and therefore change campaign identity.

The uncertainty columns are the official archive fields documented for the
[TOI table](https://exoplanetarchive.ipac.caltech.edu/docs/API_TOI_columns.html)
and [Planetary Systems table](https://exoplanetarchive.ipac.caltech.edu/docs/API_PS_columns.html).

## Locked regression cohorts

| Product arm | Input | Product-targets | Catalog signals |
|---|---|---:|---:|
| SPOC 120 s | `targets/p2_catalog_masking_spoc_7.csv` | 7 | 15 |
| TESScut 158 s | `targets/p2_catalog_masking_tesscut_21.csv` | 21 | 22 |
| **Total** | — | **28** | **37** |

These are the 28 product-targets whose prior reports rejected a signal solely
for proximity to a catalog period or harmonic while clearing S/N 7.1. TIC
23434737 appears in both product arms, so the cohorts contain 27 unique stars.

## Results

| Product arm | Safely masked | Explicitly unmaskable | Survivors | Rejected | Errors |
|---|---:|---:|---:|---:|---:|
| SPOC | 13 | 2 | 0 | 7 | 0 |
| TESScut | 17 | 5 | 2 | 19 | 0 |
| **Total** | **30** | **7** | **2** | **26** | **0** |

Permanent safety checks over every report found:

- zero masks whose propagated error exceeded the configured ceiling;
- zero unmaskable events that removed any measurements;
- zero reports labelled `catalog-masked residual` while carrying an
  unmaskable event; and
- zero execution errors.

A second fresh-output execution reproduced the earlier uncertainty-aware run
for all 28 product-targets: zero differences in `strongest_residual_signal`,
`automated_triage`, or `followup_classification`.

The two TESScut survivors are diagnostic outputs, not candidates. Mask
correctness only makes the next catalog-matching measurement meaningful; it
does not establish that a residual is astrophysical or novel.

Raw, gitignored evidence:

- `results/p2_gates/catalog_mask_uncertainty_spoc_v2/`
- `results/p2_gates/catalog_mask_uncertainty_tesscut_v2/`
- `results/p2_gates/catalog_mask_uncertainty_spoc_execute/`
- `results/p2_gates/catalog_mask_uncertainty_tesscut_execute/`

## Verification

Focused masking, catalog, campaign, and reporting tests:

```text
42 passed
```

Full suite at the masking decision point from the P2 worktree with its own
`src` first on `PYTHONPATH`:

```text
186 passed in 29.01s
```

The added tests enter the shipping single-target path, verify recovery-only
report fields, prove unmaskable masks remove zero data, verify cache refresh
behavior, and prevent sector vetting from measuring flux after an unsafe mask.

## Remaining work

Exact-period catalog matching has now been measured without changing production
behavior: four of five safely masked exact-period relations are phase-distinct,
and two are rejected solely by the period-only rule. See
`P2_CATALOG_MATCHING.md`. The masking patch must be committed separately before
that narrow matcher is wired; harmonic relations remain unmeasured.

The detrending behavior change remains independently blocked:
support-weighted biweight worsened artifact-epoch survivor leakage, while the
owner-selected quarter-window/event-support fallback missed the retention gate
and still raised artifact-aligned survivors from one to three. Both were
reverted; see `P2_EDGE_DIAGNOSTIC.md`.
