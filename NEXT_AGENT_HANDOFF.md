# P5 handoff — 2026-08-07 (evening)

Supersedes the P4 handoff of the same date, which described the state before
lane 6.1's first pass had been run.

## What this session did

Ran P5 lane 6.1's first 1,000-star pass, twice: once on a cohort whose stellar
parameters never reached the pipeline, and once repaired. Corrected a
throughput figure that would have re-planned the lane's cadence around a number
that was 45× wrong. Three ledger entries: **corrections 55, 56, 57**.

Nothing here has changed any star's status, and no packet was promoted. The
three governance decisions carried over from P4 remain **open and untouched**.

## Read this first: the throughput figure in the previous handoff was wrong

It said ~5.7 min/target, "download-bound", and "1,000 targets near 4 days",
and recommended considering a one-sector first pass to cut cost. All of that
came from dividing a 27-minute wall clock by 4 targets.

| | 4-target smoke | 100-target ramp | 1,000-star pass |
|---|---:|---:|---:|
| wall clock | 27.0 min | **27.8 min** | 221 min (targets) |
| fixed overhead | ~92% | 55% | ~7% |
| marginal rate | — | **475 stars/hour** | ~475/hour |

25× the targets for 3% more wall clock. The overhead is two synchronous
`roll_cache()` walks of the 88 GB cache (`campaign.py:868`, `campaign.py:1089`)
plus the synchronous final dashboard export — all scaling with **cache size,
not target count**. The pass is analysis-bound, not download-bound, and the
CLI's GIL warning does not bite (475/h achieved against a 4-thread theoretical
438/h), so `--analysis-processes` is unnecessary. Full detail in correction 55.

**The one-sector economy should not be taken.** It would also have changed the
screen — `sde_min_single_sector` 11.5 against `sde_min_multisector` 8.0,
`min_transits` 2 against 3 — so it measures the rediscovery rate over a
different detection population. It is a different question, not a cheaper
route to the same one.

## Results: lane 6.1, first pass

`results/campaign/p5_primary_ncvz_1000/` — **v1, screened with two physical
vetoes inert. Superseded; keep for comparison only.**

| | |
|---|---:|
| targets | 1,000 |
| searched | **953** |
| no processed light curve | **47** |
| survivors | **1** |
| errors other than missing data | 0 |

**The 47 are real absence, not a throttled connection.** All 47 reproduce
identically on a fresh connection 15 minutes later; the cliff sits at exactly
Tmag 13.5 (0 failures in 938 below it, 47 of 62 above); and 15 targets above
13.5 succeeded, interleaved through the same magnitude range and the same
30-minute window. This is the inverse of correction 48 — a failure that wore
the throttling signature and was genuine sky. **The effective cohort is 953**,
and ~6% of the Tmag 12.5–13.53 selection has no SPOC/TESS-SPOC/QLP product,
which is the thinning §6.1's rationale predicts. That belongs in the lane's
sample definition as a measured completeness caveat.

### The candidate — DO NOT CARRY IT AS ONE (correction 68)

The completed calibration measures this cohort's false-alarm rate at **0.42%**
from inverted flux, where a survivor is a false alarm by construction. Across
the 953 real targets searched that predicts **4.01 false alarms**. The first
pass produced **1 survivor** — fewer than noise alone should generate.

Nothing about the object below has changed or been found wrong. What changed is
the denominator. It remains a lead worth vetting; it is not evidence of a
planet, and it cannot be pixel-vetted (correction 60, it is QLP-only).


**TIC 298732908**, and it is **not a rediscovery** — no TOI, no confirmed
planet, no known-signal mask, no relation to a catalogued period.

On the repaired cohort (`results/campaign/p5_verify_survivor/`): P = 14.705 d,
depth 5,742 ppm, duration 3.85 h, 3 transits, red-noise-adjusted S/N 8.73,
implied radius **0.451 R_Jup ≈ 5.1 R⊕** on a 0.598 R☉ host, contamination
0.010. All six T3 checks now return real verdicts and all pass; no review
flags. Its own triage still says, correctly, that passing this gate does not
establish a planet candidate.

Its named follow-up path is exactly the P4 machinery that already exists:
`pixel-vet` (three passes, one per sector), T7 cross-reduction, TCE check.
**None of that has been run on it yet.** That is the first science action
available to the next session.

## The defect that forced a re-run

`build_p5_primary_lane.py` wrote the stellar radius as `radius_solar`; the
campaign lifts stellar parameters off the target-list row by exact key and
reads `stellar_radius_solar`. Every other cohort in `targets/` uses the
canonical name. Consequence: `depth_physicality` and `duration_density` were
`not_evaluable` for **953 of 953** stars — including the primary EB
discriminator — and `stellar_mass_solar` was absent entirely, which
`duration_density` also needs.

This was not cosmetic. With both values present, `search_grid.density_source`
resolves to `catalog_stellar_mass_and_radius` and the duration ladder becomes
density-informed, so **the fits move**: the survivor went 14.669 d → 14.705 d,
7,435 → 5,742 ppm, 2.35 → 3.85 h. Correction 57 has the full table.

**Fixed data-only, deliberately** — `campaign` is on decision 4's
byte-identical list, so teaching the reader an alias would un-match the trusted
release to repair a cohort file. The cohort was re-enriched from the same TIC
cone query matched by TIC id: 1,000/1,000 resolved, **radius unchanged for all
1,000** (so the sample provably did not drift), mass added for all 1,000.
`target_list_sha256` moves `2c51ee23…` → `7e6cfec8…`; the manifest records the
old hash and the reason. The builder is fixed with a comment saying why the
names are load-bearing.

## In flight when this was written

**`results/campaign/p5_primary_ncvz_1000_v2/`** — the full re-run on the
repaired cohort, launched ~19:35 local, ~2.4 hours. It will **exit 1** with
`state: retry_pending`; that is by design (`campaign.py:1155`, `:1159`) because
the same 47 targets have no data. It is not a crash. Compare v1 against v2 to
measure what the revived vetoes actually caught.

## What the first pass cannot answer, and why it matters

**Detection rate: 1/953 = 0.10%. §6.1 predicts 0.3–0.8%.** Three to eight times
below the lane's own forecast.

**The 4-star smoke has since given a provisional answer, and it argues against
killing the lane** (correction 61). Measured promotion completeness is **20%**,
below the 30–60% §6.1's yield arithmetic assumes. Rescaling the prediction by
the measured fraction gives ~0.1–0.5%, which brackets the observed 0.10%. And
the surface collapses with period — at 8 d a 4×-photon-noise transit is
recovered **0%** of the time, at 20 d only 8× depth reaches 60%, and the one
survivor sits at P = 14.7 d. So the shortfall is largely **sensitivity**, the
"completeness is healthy" clause currently reads **false**, and a null result
cannot be used to call the niche thin.

Provisional on n = 4 stars with cells resting on 1–2 trials, and those same
four had a 16× cost spread so they are not a representative draw (see
correction 56 on exactly this trap). The running full calibration replaces it.

The kill criterion's other clause still fails to resolve:

1. **Underpowered.** §6.1's arithmetic gives 3–8 detections per 1,000 stars,
   and the measured catalogued fraction is ~1% (9 TOI hosts, 4 confirmed-planet
   hosts in 953). A rediscovery fraction cannot be separated from 95% with a
   handful of objects. §6.1 quotes its own yield "per **10,000** stars".
2. **No completeness surface for this cohort.** §5.1 line 658: "A campaign
   without its completeness surface cannot leave 'diagnostic' status."
   Injection–recovery exists (P3 built it: 2,840 injections, 14.2% random /
   5.1% promotion-grade completeness) but was measured on P3's cohort.

A yield this far under forecast is exactly the case the criterion was written
to adjudicate, and "the niche is thin" and "our completeness is poor" predict
the same observation. **Only §5.1 separates them.**

### The approved next step

Owner approved running the §5.1 completeness sample on this cohort.
`scripts/run_p3_calibration.py` is the right instrument unmodified — its config
already implements §5.1 exactly (`random_sample_fraction=0.05`,
`archetype_count=50`, 20 random-phase + 20 edge injections per star,
`depth_noise_multipliers=(0.5, 1, 2, 4, 8)`, `impact_parameters=(0, 0.5, 0.8)`,
`photon_noise_hours=3.0`).

Pre-flight on the **repaired** cohort: **94 sampled stars, 3,760 injection
searches** (the archetype sample changed once radius became a real feature —
it had been null for every star), plus baseline/inverted/scrambled on all
1,000. Total **6,760 searches**.

**COMPLETE** (`results/p5/calibration_ncvz_1000/`) — 89 injection stars, 3,560
injections, 950 baseline/inverted/scrambled, 50 no-data errors, ~16.5 h.

**Completeness: 0.1843 raw, 0.0809 promotion.** **Three gates fail**: inverted
survivors 4/950 and scrambled 2/950 against a 0.001 budget, epoch enrichment
4.989 against a ceiling of 2.0, and t3 pass rate below its floor. Depth bias
and edge recovery gap pass. `release_gate_passes: false`, and at full sample
size this is not the n=4 artifact that failed the smoke.

**Read correction 68 before drawing any conclusion from the first pass.** The
false-alarm rate alone accounts for more survivors than the pass produced.
Epoch enrichment at 2.5× its ceiling — against P3's 1.468 on a brighter cohort
— is the most concrete lead for why, and is the single best next investigation.

Cost, measured rather than estimated (correction 58): **172 searches in 120.7
min = 85.5 searches/hour**, so the full 6,760 searches is roughly **20–80
hours** depending on how 4-worker concurrency holds. Per-star cost varied
**16×** across the four smoke stars (10.7, 6.3, ~96, ~104 min) for reasons that
remain unexplained — that spread is the dominant uncertainty and may itself be
a defect worth finding.

It checkpoints (`p3_progress.json`) and can be stopped. If it needs shortening,
the 1,000-star baseline/inverted/scrambled nulls are 3,000 of the 6,760
searches and could be dropped separately from the 94-star injection sample.

**Read `--author auto`, not `SPOC`.** The driver defaults to SPOC, but this
cohort resolves to SPOC 465 / QLP 447 / TESS-SPOC 41. A SPOC-only calibration
would measure completeness on half the sample and the brighter, 2-minute half
at that — a surface systematically better than the lane's reality.

```
python scripts/run_p3_calibration.py \
  --targets targets/p5_primary_m_dwarf_ncvz.csv \
  --output-dir results/p5/calibration_ncvz_1000 \
  --author SPOC --cadence-seconds 120 \
  --workers 4 --download-workers 3 --prefetch 32
```

**It requires a clean worktree** (`require_clean_repository`). `--allow-dirty`
exists but marks the output diagnostic-only, which is circular when the whole
purpose is a surface that lets the campaign leave diagnostic status.

## The known-planet rediscovery test — BUILT AND RUN

The owner asked whether the pipeline could scan known exoplanet hosts and
measure what percentage it independently recreates. It is now built
(`scripts/build_p5_known_planet_recovery.py`,
`scripts/measure_known_planet_recovery.py`) and run. **Headline: the pipeline
blind-recovers 83.6% of known transiting planets on SPOC photometry.**

| | SPOC | TESScut |
|---|---:|---:|
| planets scored | 323 | 48 |
| blind period recovery | **83.6%** | **64.6%** |
| full recovery (period + depth) | 79.9% | 52.1% |
| errors | 0 | 0 |

`results/p5/known_recovery_spoc/`, `results/p5/known_recovery_tesscut/`.
Corrections 62–65 carry the detail. What matters most:

- **Triage rejected 0 of 258 correctly-recovered planets.** The veto stack has
  no measured false-kill rate on real planets, so the P5 cohort's 952
  rejections are unlikely to hide found-then-discarded planets. Losses are in
  the *search*.
- **Recovery is depth-limited, not brightness-limited** — 0.36 below 250 ppm
  rising to 0.95 at 5–10k ppm, but nearly flat against Tmag from 5 to 16.
- **14 of the 53 period misses had the true period among the five recorded BLS
  peaks and were not selected** — near-ties resolved the wrong way, fixable
  without new photometry. The fix lives in the detection kernel, which
  decision 4 freezes, so it is an owner call.
- **TESScut is ~28 points worse and cannot be pixel-vetted** (correction 65 +
  60). That is the half of lane 6.1 the lane is deliberately aimed at.

**Do not quote 83.6% as the survey's completeness.** The catalogue is dominated
by large planets; this is completeness for deep signals. Correction 61's 20%
promotion completeness on faint M dwarfs is the same pipeline at shallow depth,
and the two agree once conditioned on depth.

**Not yet done:** the cohort is the 371 known hosts the survey had already
searched, so sectors were known and photometry cached. Extending to the full
3,571 known transiting hosts needs sector resolution for new stars —
`build_p3_known_planets.py` will not do it (it resolved 4 usable controls
against the 82,339-file offline cache because it demands pre-resolved SPOC
sectors). `make-targets` resolves sectors via MAST at ~2 queries per star.

**Separate finding, worth its own look:** ~11% mask leakage. 54 of 478
already-searched hosts still show a strongest *residual* signal matching the
catalogued period within 1% (allowing 1:2, 2:1, 1:3, 3:1) despite masking.

## Actionable now, in the vetting layer (no kernel re-signing)

Everything below sits outside the frozen detection kernel, so it needs no
re-signed calibration. Ranked by value.

1. **Promote three computed-but-unused flags to vetoes** (correction 72). The
   common-mode screen already records `spacecraft_harmonic`,
   `duration_at_grid_rail` and `period_at_search_ceiling` per target and
   nothing acts on them. Measured across 84,374 screened targets they are
   enriched **1.63×, 2.92× and 2.40×** among automated survivors (5.7σ, 13.8σ,
   7.8σ), and together flag **311 of 990 survivors (31.4%)** against 14.8% of
   the population. This is a status change, so it is an owner call — but the
   evidence is measured, not intuited.

2. **Run the common-mode screen as part of every campaign.** It was never
   invoked on P5 (correction 69) or on 85% of the survey (correction 71). It
   is now caught up, but nothing makes it automatic. **Run it per campaign** —
   `--campaign-root results/campaign` pools everything and silently retracts
   1,665 established systematics by diluting the expectation (correction 71).

3. **Add `camera`/`ccd` to cohort builders.** Their absence left the P5 dip
   registry at `s14-camunknown-ccdunknown` with zero windows (corrections 57,
   69), disabling the second shared-systematic guard.

4. **Emit campaign-format residual reports from the null runs**, so the
   common-mode screen can be applied to inverted/scrambled results. Without
   that, correction 70's "would screening fix the false-alarm gate?" can only
   be answered indirectly.

Not actionable without an owner decision on decision 4: the ~1 d rail-pinning
family, which is the dominant search failure in three independent cohorts
(corrections 63, 65, 66) and lives in the frozen kernel.

## Still open — owner's, not to be settled unilaterally

1. **`--trusted-first-pass` is unsatisfiable for any new cohort**, because the
   signature includes `target_list_hash`. Options A/B/C stand; C
   (diagnostic-only) is what has been running. Note this session **changed**
   `target_list_sha256`, which is directly adjacent to this decision.
2. **Status precedence (correction 38)** — decided in principle, ledger
   authoritative, **not implemented**. Blocks promoting P4 evidence to voting.
3. **TRICERATOPS** — its `not_run` FPP correctly blocks every packet from
   `ready`. Installing it is the fix; relaxing the contract is not.

## Operational notes

- **Always set inline**, in every shell touching science:
  `$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'`
- **`--cache-max-gb` can be silently inert.** `campaign.py:568` derives the
  effective cap as `workspace_max − 1 GB − workspace_size`. With
  `--workspace-max-gb 95` the cache cap landed at 88.04 GB against a live
  88.0400 GB cache — **871 KB of headroom**, and `campaign.py:619` can abort a
  run over it. Now `--workspace-max-gb 200` → 120 GB effective, ~30 GB free.
  E: has 824 GB free; the 95 GB ceiling was self-imposed.
- Cache ~90 GB. Every campaign pays a ~8 min head and ~7 min tail walking it;
  that is fixed cost, not a hang.
- **The checkpoint lags the durable per-target reports.** `batch_status.json`
  can still show the *previous* run's terminal state minutes into a new one.
  Verify liveness by process and by report mtimes, never by the checkpoint
  alone.
- PowerShell here is 5.1: no `&&`, no ternary. Use `;` and `if ($?) { }`.
- Be polite to MAST. Sustained 1,000-target runs at `--download-workers 3`
  produced **zero** transport errors across ~1,100 cold targets tonight.

## The pattern worth reading before trusting anything here

Corrections 46–50 were one failure in four costumes: a plausible result that
was an artifact of how it was obtained. **Correction 55 is the fifth, and it
had already survived one self-correction** — the first reading ("0 of 4 after
20 minutes, stalled") was retracted as a stale checkpoint, correctly, and the
replacement number was then drawn just as carelessly and quoted with more
confidence. *A correction is not evidence that the replacement was measured.*

**Correction 57 is the shape to watch for next.** A veto that cannot run does
not report as failing — it reports as *not blocking*, and in aggregate that is
indistinguishable from a clean pass. The T3 block said `"passes": true,
"rejection_reasons": []` while a third of its checks had never executed.
`not_evaluable` is not a pass, and nothing in the summary path was
distinguishing the two.

And once this session predicted a veto verdict from stale inputs, got the right
answer, and only got it because the margin to the kill span was wide. Where two
independent checks disagree, the disagreement is the signal — and when a gate
starts failing, the first question is whether it has found something.
