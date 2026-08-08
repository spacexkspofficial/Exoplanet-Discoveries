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

### The candidate

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

That number cannot be interpreted yet, and this is the substantive open
question. §6.1's kill criterion is ">95% of detectable signals are
rediscoveries **and** completeness is healthy". Both clauses fail to resolve:

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

**It has not been launched, and the reason is correction 58.** A 4-star smoke
measured the real cost: two stars took 10.7 and 6.3 minutes for their 43
searches each, and two took **over 95 minutes and had not finished**. The
aggregate rate fell 85 → 49 searches/hour as the cheap stars finished first. At
that rate the full run is **~138 hours**, not the ~14 h estimated from the
campaign's per-search cost. An injection search re-detrends — that is §5.1's
requirement, not a defect — so it is nothing like a campaign search.

**This needs an owner decision before it runs**, because a week of machine time
is a different commitment from an overnight job. Options: accept it; drop the
1,000-star nulls (3,000 of the 6,760 searches) and keep only the injection
sample; shrink the sample; or investigate why ~half the stars are >14× more
expensive, which is unexplained and may itself be a defect worth fixing first.

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

## Owner's proposal: a survey-wide known-planet recovery rate

The owner asked whether the pipeline could scan all known exoplanet hosts and
measure what percentage it independently recreates as candidates, labelling
those rediscoveries. **It is not already done, it is the right instrument for
the current blocker, and it is far cheaper than the injection calibration.**
Correction 59 has the evidence; the short version:

- The survey has **already searched 473 confirmed transiting hosts** and 2,220
  TOI hosts, but **476 of 478 had the known signal masked before the search**.
  Recovery is therefore unmeasurable from any existing artifact — verified, not
  assumed.
- `results/p3/known_planets_v8/` (20/20) is a curated regression guard, not a
  rate. A hand-picked passing set cannot estimate completeness.
- `build_p3_known_planets.py` will not scale: against the 82,339-file offline
  cache with `--limit 1500` it resolved **4** usable SPOC controls, because it
  demands pre-resolved SPOC sectors.

**Why it beats injection–recovery for the §6.1 kill criterion:** real planets
carry real variability, dilution and systematics; injected boxes do not, so
§5.1 can report healthy completeness while real planets are still missed. And
it is **one search per star** — campaign-rate, so a few thousand hosts is an
overnight job rather than the calibration's week.

**What it needs built:** a cohort builder that takes `nasa_ps` rows with
`tran_flag=1` (3,602 hosts with a TIC) plus ephemeris columns, resolves TESS
sector coverage without demanding SPOC 2-minute data, and emits the standard
target-list schema — *including* `stellar_radius_solar` and
`stellar_mass_solar`, per correction 57. Then run it unmasked (the mode exists;
"this is an unmasked recovery-only scan" appears 445 times in the metrics) and
score fitted period against the catalogued period with alias tolerance.

**Do not fold its output into survey candidate counts.** It runs unmasked by
design, so every "detection" is a known object.

**Separate finding, worth its own look:** ~11% mask leakage. 54 of those 478
hosts still show a strongest *residual* signal matching the catalogued period
within 1% (allowing 1:2, 2:1, 1:3, 3:1) despite masking.

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
