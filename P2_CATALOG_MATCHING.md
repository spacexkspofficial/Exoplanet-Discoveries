# P2 epoch-aware catalog-matching implementation

Measured 2026-07-27 PDT (2026-07-28 UTC) through reports produced by the real
`batch-hunt` path after uncertainty-aware catalog masking.

## Decision

The current period-only rule is too broad for **safely masked exact-period
relations**. Exact-period signal identity should also require the recovered
transit windows to overlap the uncertainty-expanded catalog mask.

This result by itself does **not** justify changing harmonic matching. Half-,
double-, third-, and triple-period relations require an explicit event-number
model and were not represented in this fresh 28-product output. A subsequent
frozen historical-report diagnostic now supplies that model and its separate
controls; see `P2_HARMONIC_MATCHING.md`.

The rule is now wired into the shipping single-target path after the catalog
masking behavior was isolated in commit `9f9a860`. This remains a separate
behavior change. Harmonic production behavior is deliberately unchanged.

## Locked inputs

- `catalog_mask_uncertainty_spoc_execute`: 7 SPOC product-targets.
- `catalog_mask_uncertainty_tesscut_execute`: 21 TESScut product-targets.
- Total: **28 product reports, 27 unique stars, 37 catalog signals**.

These are the fresh shipping-path executions from the catalog-masking gate.
They reproduced the earlier v2 runs exactly in strongest signal, triage, and
classification.

## Conservative epoch test

For each safely masked exact-period relation:

1. Generate every recovered transit center inside the report's observation
   window.
2. Fold each center against the catalog period and its propagated epoch.
3. Treat a recovered event as compatible with the known signal when its transit
   window overlaps the complete uncertainty-expanded catalog mask:
   `center offset <= known mask half-width + recovered half-duration`.
4. Classify all events overlapping as `consistent_with_masked_known_signal`,
   zero events overlapping as `phase_distinct_from_masked_known_signal`, and a
   mixture as ambiguous.

Adding the recovered half-duration is deliberately conservative. It keeps a
signal rejected when BLS centers a partial residual immediately outside a mask
boundary, rather than incorrectly calling that boundary leakage distinct.

Unmaskable catalog signals remain recovery-only and are not epoch-adjudicated.
Non-exact harmonics are explicitly left unevaluated.

## Results

| Relation class | Count | Epoch result |
|---|---:|---|
| Safely masked exact-period | 5 | 4 phase-distinct, 1 consistent with known-mask overlap |
| Unmaskable exact-period recovery | 4 | not evaluable; promotion already disabled |
| Safely masked harmonic | 0 | not represented |

Detailed safely masked cases:

| TIC | Known signal | Period error | Recovered events | Overlapping windows | Result | Would pass without period-only rejection? |
|---:|---|---:|---:|---:|---|---|
| 20318757 | TOI-1027.02 | 1.211% | 2 | 0 | phase-distinct | no; low S/N remains |
| 301160638 | TOI-3487.01 | 0.152% | 2 | 2 | consistent with mask-edge leakage | no |
| 313675203 | TOI-4367.01 | 3.786% | 9 | 0 | phase-distinct | no; several other vetoes remain |
| 301248781 | TOI-6753.01 | 0.145% | 24 | 0 | phase-distinct | **yes** |
| 450649506 | TOI-4371.01 | 0.053% | 23 | 0 | phase-distinct | **yes** |

The two restored triage passes would remain automated diagnostic survivors, not
planet candidates or novelty claims. This measurement establishes only that
they are not the safely masked catalog transits at the same phase.

Production-helper replay over all 28 reports gives:

| Measurement | Before | After |
|---|---:|---:|
| Automated triage passes | 2 | **4** |
| Reports losing the period-only rejection | — | **4** |
| New automated triage passes | — | **2** |

The two new passes are the already measured TIC 301248781 and TIC 450649506
cases. The other two phase-distinct reports lose only the catalog reason and
remain rejected by unrelated gates. No other report changes projected triage
state.

## Interpretation

Host identity, period relation, and signal identity are different facts:

- a host can contain multiple periodic signals;
- nearby periods do not imply the same transit phase;
- a trustworthy mask provides the phase/time envelope needed to test identity.

TIC 301160638 is the important conservative control. Its recovered centers sit
just outside the catalog mask centers, but its fitted transit windows overlap
the mask boundary at both events. The epoch rule therefore keeps it classified
as known-signal leakage. The other four have zero overlapping event windows;
their offsets cannot be explained by the catalog masks that were actually
removed.

## Production implementation

Each known-period relation now records:

- an `epoch_verdict`;
- whether `catalog_match_rejects`;
- the uncertainty-expanded overlap tolerance;
- predicted and overlapping event counts; and
- per-event center offsets.

The period-only rejection is retained when any relation is harmonic,
untrustworthy, consistent with the known mask, or partially overlapping. It is
removed only when every applicable relation is safely masked, exact period,
and phase-distinct. Reports also expose their exact rows through
`catalog_epoch_agreement`.

## Verification and evidence

Five pure adjudicator tests cover:

- an exact phase match;
- a clearly shifted exact-period signal;
- conservative partial overlap;
- unchanged harmonic rejection; and
- untrustworthy-mask rejection.

Two additional tests enter the shipping `_hunt_from_light_curve` path and
prove both the retained leakage rejection and the restored phase-distinct
pass. The replay projection itself has a two-report regression test.

Focused exact/masking suite: **15 passed**. Complete worktree suite:
**207 passed**.

Reproducible command:

```powershell
python scripts/measure_p2_catalog_matching.py `
  <spoc-execute-dir> <tesscut-execute-dir> `
  --output results/p2_gates/catalog_matching_epoch_diagnostic/p2_catalog_matching_measurement.json
```

Raw diagnostic measurement:

`results/p2_gates/catalog_matching_epoch_diagnostic/p2_catalog_matching_measurement.json`

Production-helper replay:

`results/p2_gates/catalog_matching_epoch_production_replay/`

The production helper reproduced the complete summary and all five safely
masked exact-relation verdicts and offsets on both the v2 and execute masking
outputs. The only underlying relation-payload differences remain the four
unmaskable cases' renamed status/reason strings
(`unmaskable_ephemeris_drift` versus
`unmasked_ephemeris_uncertainty`); all four remain rejected,
non-evaluable, and non-promotable.

## Next implementation boundary

Keep this exact-period change in its own commit. The next catalog-matching
behavior slice is the separate boundary in `P2_HARMONIC_MATCHING.md`: the
frozen cohort supports half, double, and triple relations, while one-third
period remains under-controlled.
