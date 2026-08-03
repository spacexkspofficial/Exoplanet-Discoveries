# P2 event-number-aware harmonic-matching diagnostic

Measured 2026-07-28 PDT without changing production behavior or executing a
new campaign. This extends the exact-period result in
`P2_CATALOG_MATCHING.md`; it does not make any planet or novelty claim.

## Decision

Period ratio alone is not signal identity for harmonic relations. A recovered
signal is phase-distinct when none of its predicted transit windows overlaps
the uncertainty-expanded mask for the referenced catalog signal.

The conservative rule measured here is:

1. For a recovered period that is two or three times the catalog period, the
   recovered events are a subset of catalog events. Call the relation
   consistent only when every recovered event window overlaps the catalog
   mask, with at least two events.
2. For a recovered period that is one-half or one-third of the catalog period,
   partition recovered events by event number modulo two or three. Call the
   relation consistent only when one complete event-number class overlaps the
   catalog mask at least twice and no overlaps occur outside that class.
3. Zero overlaps means phase-distinct. Any partial pattern remains ambiguous
   and stays rejected.
4. Event windows, not just their centers, are compared. The tolerance is the
   known mask half-width plus the recovered transit half-duration, matching the
   conservative exact-period diagnostic.

The real-data cohort supports this rule for half-, double-, and triple-period
relations because each has phase-distinct examples and at least one consistent
control; double and triple also have partial-overlap controls. One-third period
has only one real example, and it is ambiguous rather than a positive or
phase-distinct control. Its production behavior should remain unchanged until
that gap is filled, even though the generic arithmetic has a synthetic test.

## Locked cohort

The builder scanned only the two historical shipping-path Sector 100 campaign
directories:

- `results/campaign/sector100_spoc`
- `results/campaign/sector100_expansion_5000`

Experimental detrending outputs, refactor-equivalence duplicates, and later
catalog diagnostics were intentionally excluded as discovery sources. A
product-target was retained only when its photometry was already cached, its
catalog cache used the uncertainty-aware schema, and the matching current mask
record said the historical catalog signal was safely masked.

| Product arm | Product-targets | Historical harmonic relations |
|---|---:|---:|
| SPOC 120 s | 6 | 6 |
| TESScut 158 s | 14 | 14 |
| **Total** | **20** | **20** |

The cohort contains 19 unique stars because TIC 23434737 appears in both
product arms.

| Relation | Count |
|---|---:|
| Half period | 5 |
| Double period | 5 |
| One-third period | 1 |
| Triple period | 9 |

Locked artifacts:

- `targets/p2_harmonic_matching_spoc_6.csv`
  (`bd5ffd3920af80111f771f1d51c5f466ec36f63fd0714f7914880ea140c79f1a`)
- `targets/p2_harmonic_matching_tesscut_14.csv`
  (`cff12753ec8fa9cab950515edecaaa2044c244c09279446f489616f58bb41102`)
- `targets/p2_harmonic_matching_manifest.json`

The manifest records all selected evidence, current mask records, cached
products, exclusions, and nonexclusive exclusion reasons. Of 43 historical
product-targets with harmonic evidence, 20 met the complete offline rule.
Eighteen lacked the matching cached photometry, 17 had catalog caches requiring
refresh, 22 were outside the current 28-product masking gate, and one otherwise
ready SPOC product had only an explicitly unmaskable historical reference.

## Results

| Verdict | Count | Production interpretation |
|---|---:|---|
| Phase-distinct, zero overlap | **12** | Eligible to continue through all other gates |
| Consistent event-number pattern | **3** | Keep rejected as catalog harmonic |
| Partial/ambiguous overlap | **5** | Keep rejected conservatively |

Breakdown:

| Relation | Distinct | Consistent | Ambiguous |
|---|---:|---:|---:|
| Half period | 4 | 1 | 0 |
| Double period | 3 | 1 | 1 |
| One-third period | 0 | 0 | 1 |
| Triple period | 5 | 1 | 3 |

All 12 phase-distinct historical detections had no automated rejection after
removing the period-only reason. They would therefore have been diagnostic
survivors under this rule. That is a regression result, not an estimated yield:
the cohort was selected because it already contained harmonic rejections and
is not a population sample.

The three positive controls are:

- TIC 23434737 SPOC, double period relative to TOI-1203 b: 3/3 recovered
  event windows overlap.
- TIC 301160638 SPOC, half period relative to TOI-3487.01: one event-number
  class overlaps at 2/2 events and the other class has zero overlap.
- TIC 313675203 SPOC, triple period relative to TOI-4367.01: 3/3 recovered
  event windows overlap.

The five partial cases remain rejected: TIC 260647166 at one-third period;
TIC 261020738, 263486257, and 295233473 at triple period; and TIC 384016413 at
double period.

## Verification and evidence

Five synthetic tests cover:

- complete longer-period alignment;
- one complete shorter-period event-number class;
- zero-overlap phase distinction;
- partial longer-period ambiguity; and
- refusal to infer identity from one overlapping event.

The cohort builder has four additional tests, and the existing exact-period
diagnostic has four. Focused suite: **13 passed**. Complete worktree suite:
**199 passed**.

Reproducible commands:

```powershell
python scripts/build_p2_harmonic_cohort.py `
  --results-root <workspace>\results `
  --historical-dir <workspace>\results\campaign\sector100_spoc `
  --historical-dir <workspace>\results\campaign\sector100_expansion_5000 `
  --cache-root <workspace>\data\lightkurve `
  --catalog-root <workspace>\data\catalogs\nasa_exoplanet_archive `
  --spoc-mask-dir <workspace>\results\p2_gates\catalog_mask_uncertainty_spoc_v2 `
  --tesscut-mask-dir <workspace>\results\p2_gates\catalog_mask_uncertainty_tesscut_v2 `
  --spoc-output targets\p2_harmonic_matching_spoc_6.csv `
  --tesscut-output targets\p2_harmonic_matching_tesscut_14.csv `
  --manifest-output targets\p2_harmonic_matching_manifest.json

python scripts/measure_p2_harmonic_matching.py `
  --manifest targets\p2_harmonic_matching_manifest.json `
  --results-root <workspace>\results `
  --output results\p2_gates\harmonic_epoch_diagnostic\p2_harmonic_matching_measurement.json
```

Raw, gitignored measurement:

`results/p2_gates/harmonic_epoch_diagnostic/p2_harmonic_matching_measurement.json`

## Implementation boundary

The uncertainty-aware masking patch must still land alone. After that:

1. Implement and measure exact-period epoch matching as its own behavior
   change using the locked five-relation exact cohort.
2. Extract the harmonic adjudicator as a pure function and replay this frozen
   20-relation set against it.
3. In a separate behavior change, allow only zero-overlap half-, double-, and
   triple-period relations to continue to the remaining gates. Keep consistent
   and partial cases rejected.
4. Leave one-third-period production behavior unchanged until a real
   consistent control and preferably a partial-overlap control are locked.

No new download or campaign-scale run is required for the frozen replay.
A future live cohort run still requires the owner's explicit approval under
the standing campaign constraint.
