# Codex handoff 2: P2 rewiring after measured rejections

Supersedes `CODEX_HANDOFF.md` for **state and work queue**. The initial
structure-only orchestration slices are complete; the single-target analysis
path and context/vetting commands still need extraction. The first detrending
rewiring and the later owner-selected narrow-guard/event-support fallback were
both measured and rejected. Everything else in the earlier handoff
— the P2 exit gates, the P3 plan, the monotransit detector, the claim ceiling —
remains current and you should read it.

**Are we ready for science? Still no.** Catalog masking, event-aware catalog
matching, physical search grids, and single-target T3 vetoes are fixed and
measured in the working tree, but detrending remains blocked and the population
veto plus calibration gates remain open.

---

## Verified state you inherit (re-verify, don't trust)

```powershell
git clone https://github.com/spacexkspofficial/Exoplanet-Discoveries.git
cd Exoplanet-Discoveries
python -m pytest -q
# expect: 236 passed
```

- The P2 work was developed on `codex/p2-catalog-matching`; its measured,
  one-concern commit sequence is retained in repository history. Use the
  default branch after the migration consolidation.
- `main` is at `a45403a "idk"` — the owner committed the pinned cohort files
  there mid-session and pushed. That commit contains only
  `targets/p2_artifact_regression_cohort.{csv,json}`. It is merged into the
  branch; there was no conflict.
- 236 tests pass from the staging checkout.
- `cli.py` is **2,584 lines**, down from 3,607 (−28%).
- **No behaviour change has shipped to `main`.** The branch contains five
  committed behavior changes plus the measured T3 change in this working tree:
  uncertainty-propagated catalog masking, exact-period epoch matching,
  controlled harmonic matching, physical search grids, and T3 vetoes. The two
  detrending mechanisms and the earlier invalid catalog-matching attempts
  remain reverted.

### Commits

| commit | what |
|---|---|
| `3d1c283` | campaign support helpers → `campaign.py` (structure only) |
| `e425974` | target-list construction → `targets.py` (structure only) |
| `8350289` | detrending rewiring measured; **does not ship** |
| `7f21921` | first catalog-matching scan (superseded) |
| `b38144d` | second catalog-matching measurement (**superseded — was wrong**) |
| `dfadf8c` | corrected catalog-matching finding; the masks are stale |
| `1ea6e84` | merge main |
| `9f9a860` | uncertainty-aware catalog masking |
| `b29f6e2` | locked P2 matching and detrending diagnostics |
| `223854b` | exact-period transit-epoch adjudication |
| `f90138f` | half/double/triple event-number adjudication; one-third held |
| `f400039` | physical search grids, locked 150-target shipping A/B, and evidence |
| this commit | T3 veto wiring, family-wise secondary correction, and locked 150-target A/B |

`PROGRESS.md` corrections 9–19 are the substance. Read them before the code.

---

## What is done

The **structure-only orchestration slices are complete**. `cli.py` no longer
owns photometry, screening, campaign orchestration, campaign support, or
target-list construction. Each slice was equivalence-gated: the first three by
byte-identical per-target JSON on the pinned 150-target cohort, the
campaign-support slice additionally by diffing the campaign-published
artifacts, and the target-list slice by a direct A/B against the pre-move tree
(the 150-target rerun cannot gate it — `batch-hunt` never calls target-list
code). The single-target analysis path and context/vetting commands remain.

The **physical search-grid rewiring is complete and measured**. The shipping
hunt now uses baseline/minimum-transit period ceilings, an 8% diagnostic
overscan, stellar-density duration grids with a named solar fallback, and
explicit period/duration rail rejection. Target builders and `batch-hunt`
preserve stellar mass/radius into the analysis metadata.

The locked 150-target TESScut A/B produced 150 reports and zero errors in both
new arms. All 150 normalized inputs match; all 81 fallback targets are exact in
the complete science payload; every changed result is confined to the 69
density-backed targets. Golden/fallback/density passes are **35/5/6**. No
overscan or rail fit passes, but 124/120 fallback/density fits choose an
effective rail: the gate safely demotes them without proving sensitivity. The
first A/B under-counted rails because Astropy quantizes durations; the
corrected arms reproduce every fitted fallback signal and compare against the
effective grid. See `P2_SEARCH_GRIDS.md`.

The **single-target T3 veto rewiring is complete and measured**. Every shipping
report now records a versioned T3 evidence block for duration-density
consistency, implied companion radius/EB routing, folded odd/even depths,
full-phase secondaries, and two-sided event support. Missing catalog stellar
parameters make only the dependent physical checks non-evaluable.

The first literal full-phase implementation was rejected before commit: taking
the strongest local phase window above 3 sigma killed 154/500 deterministic
pure-noise folds (30.8%) and marked 97/150 cohort signals as secondaries.
Production now uses median standard errors plus a family-wise correction over
the actual tested phase windows; the same noise gate becomes 1/500 (0.2%).

The corrected 150-target arm is exact to the density-grid baseline in every
pre-T3 science field and has zero errors. Passes move **6 → 1** with no gains.
Three losses have corrected secondaries at local S/N 6.12, 6.961, and 11.278;
two have only one or zero events with two-sided support; the S/N 11.278 case
also has a fatal duration-density ratio. The sole survivor is TIC 54147357 and
remains manual-review-only. See `P2_T3_VETOES.md`.

---

## What remains blocked, and why

### 1. Detrending rewiring — measured, rejected (PROGRESS correction 10)

Wired end to end, calibrated over an 18-point `(window, floor, alpha)` grid,
measured through the real `batch-hunt` path on 371 targets, reverted.

| arm | retention | artifact enrichment | p | survivors | survivors *on* artifact epochs |
|---|---|---|---|---|---|
| Savitzky–Golay + hard guard (ships today) | 0.669 | 1.137 | 0.046 | 24 | **1** |
| biweight support-weighted, α=5 | 0.993 | 1.140 | 0.039 | 51 | **9** |
| quarter-window + two-sided event support | 0.836 | 1.142 | 0.048 | 21 | **3** |

Retention passes overwhelmingly; artifact enrichment does not move; artifact-epoch
survivors rise 1 → 9. §2.3 ships only when all four numbers pass.

**No edge parameter fixes it.** The floor cannot carry the artifact gate — a clean
edge has support `f = 0.5`, above the 0.4 floor, so the floor never fires;
raising it to 0.8 passes the gate but leaves an edge transit 2 of the 5 cadences
it needs, i.e. it passes by destroying the capability. Geometry bounds this: a
floor `F` drops cadences within `2h(F − 0.5)` of an edge, so `F ≥ 0.58` always
eats edge transits at a 1.0 d window. Alpha cannot carry it either.

Support weighting cannot separate edge sensitivity from edge artifacts. The
owner-selected quarter-window/event-support fallback also fails: its event lane
quarantined 34 edge-dependent detections (15 with no other rejection), but
retention missed 85% and three artifact-aligned signals still survived. Local
sampling support does not detect trend-model bias. Both mechanisms are reverted;
do not sweep their parameters again. See `P2_EDGE_DIAGNOSTIC.md`.

### 2. Catalog masking — fixed; exact-period matching measured

The worktree now queries the official period/epoch uncertainty columns,
propagates a conservative linear timing envelope over the complete observation
window, widens safe masks, and removes zero cadences when the prediction is
missing or more uncertain than one transit duration. Unsafe cases stop the
normal residual path; recovery-only reports name them and disable promotion.
Injection-recovery and sector-coherence commands also refuse unsafe masks.

Locked shipping-path gate, 7 SPOC + 21 TESScut product-targets:

| catalog signals | safely masked | explicitly unmaskable | unsafe/silent masks | execution errors |
|---:|---:|---:|---:|---:|
| 37 | 30 | 7 | **0** | **0** |

A second fresh-output execution reproduced all 28 strongest signals, triage
verdicts, and classifications. The two TESScut survivors remain diagnostic
only. See `P2_CATALOG_MASKING.md`.

The exact-period rule is wired as a separate behavior change after masking
commit `9f9a860`.
Of five safely masked exact-period relations, one recovered signal overlaps the
known mask at every fitted event and remains known-signal leakage; four have
zero event-window overlap and are phase-distinct. Two of those four are rejected
solely by the old period-only rule. The production helper reproduces all five
safely masked exact verdicts on both frozen outputs; four reports lose the
catalog reason, and projected automated passes move 2 → 4, exactly the two
predeclared cases. See `P2_CATALOG_MATCHING.md`.

A separate frozen historical cohort covers harmonic identity without a
campaign rerun: 20 cached product-targets, 20 safely masked historical
relations, and an explicit event-number model. The production helper matches
the independent diagnostic on all 19 controlled half-, double-, and
triple-period relations. Twelve zero-overlap cases continue to other gates;
three consistent and four controlled partial cases remain rejected. The
single one-third case is under-controlled and remains period-only. Historical
projected passes move 0 → 12. See `P2_HARMONIC_MATCHING.md`.

---

## Traps — I hit all of these

**Calibrate through the path that ships.** A fast probe harness (detrend + BLS
directly) said α = 5 gave enrichment 1.12 at p = 0.14. Through `batch-hunt` the
same configuration gave 1.140 at p = 0.039 — no improvement. The harness skipped
ephemeris masking and the screening cascade and that inverted the conclusion.
`CODEX_HANDOFF.md` already warns that measuring off the shipping path is what hid
the TESScut disaster; it applies to measurement harnesses too. Use fast harnesses
to rank candidates, never for the number that decides a gate.

**A green suite is not coverage.** The epoch-aware change contained a
`NameError` on a path no test exercises: `py_compile` passed and 180 tests
passed. It would have crashed on exactly the 46 targets it targeted. When you
add a branch, add a test that enters it.

**"0 of N" gates are unachievable on statistically selected cohorts.** §2.3 #1
asks for zero detections at artifact epochs. About a third of this cohort aligns
with an artifact epoch *by chance*, so a flawless pipeline shows ~117 of 371.
The gate is restated as alignment consistent with an empirical null (random
control epochs, same fitted ephemerides). Controls return 0.994×, which is what
validates the statistic. Two arithmetic errors were caught this way — a
factor-of-2 in the chance model, and a biased control range. **Always run the
null.**

**Two invalid tests were built for the catalog question. Do not rebuild them.**
(a) comparing found and catalogued ephemerides directly — meaningless across the
harmonic relations most of these match; (b) asking what fraction of found
transits land on masked windows — reads 0 % both for a genuinely distinct signal
*and* for the true signal whose mask was misplaced. Wired as a gate, (b)
promoted a catalogued TOI to `automated_survivor`.

**`data/lightkurve` must not be deleted.** The owner notes in `PROGRESS.md` call
it orphaned and safe to remove. It holds **1,902 cached SPOC Sector 100 light
curves** and is the only reason the P2 gates run offline. Deleting it forces
re-downloading ~1,900 targets from MAST.

**Working from a git worktree runs main's code.** `.venv` is an editable install
pointing at the main checkout, so `.venv\Scripts\exohunt.exe` and plain
`python -c "import exohunt"` execute **main's** source. Set
`PYTHONPATH=<worktree>\src` and have the runner print `exohunt.cli.__file__` so
the log proves which tree ran. `pytest` is safe (`pythonpath = ["src"]` resolves
against rootdir).

**Never run the test suite while a campaign is running.** `batch-hunt` holds the
machine-wide coordinator lock, so `test_batch_hunt_refuses_a_duplicate_campaign_worker`
takes the "already running" early return and fails spuriously.

---

## Evidence on disk (all gitignored except the cohort)

| path | what |
|---|---|
| `targets/p2_artifact_regression_cohort.{csv,json}` | 371-target cohort + manifest, **committed on main** |
| `results/p2_gates/artifact_baseline_e425974/` | baseline, current code, 371 targets |
| `results/p2_gates/artifact_biweight_alpha5/` | biweight arm, 371 targets |
| `results/p2_gates/artifact_narrow_guard_edge_diagnostic/` | owner-selected fallback, 371 targets, **rejected** |
| `results/p2_gates/catalog_matching_epoch_diagnostic/` | exact-period epoch diagnostic over 28 trustworthy-mask reports |
| `results/p2_gates/catalog_matching_epoch_production_replay/` | shipping-helper replay over both frozen 28-product outputs |
| `results/p2_gates/harmonic_epoch_diagnostic/` | event-number diagnostic over 20 frozen historical harmonic relations |
| `results/p2_gates/harmonic_epoch_production_replay/` | production-helper replay over the frozen harmonic cohort |
| `targets/p2_harmonic_matching_*` | 6 SPOC + 14 TESScut harmonic regression targets and manifest |
| `targets/p2_search_grid_golden_150.{csv,json}` | frozen 150-target identity with 69 complete density pairs and 81 fallbacks |
| `results/p2_gates/search_grid_shipping_railfix_150/` | corrected shipping fallback-grid arm, 150 reports |
| `results/p2_gates/search_grid_shipping_density_railfix_150/` | corrected shipping density-grid arm, 150 reports |
| `results/p2_gates/search_grid_shipping_ab_150.json` | three-arm input, transition, invariance, and boundary measurement |
| `results/p2_gates/t3_shipping_naive_secondary_150/` | rejected naive local-secondary T3 arm, 150 reports |
| `results/p2_gates/t3_shipping_150/` | corrected family-wise T3 arm, 150 reports |
| `results/p2_gates/t3_shipping_ab_150.json` | T3 transition, exact-invariance, and per-check measurement |
| `results/p2_gates/catalog_epoch_after/` | 12-target rerun that exposed the stale-mask bug |
| `results/equivalence/campaign_support_extract_v4/` | structure-slice equivalence + manifest |
| `results/equivalence/target_list_extract_v5/` | structure-slice equivalence + manifest |

The cohort replaces §2.3's "14 real light curves from the edge-safe work", which
**was never recorded** — commit `7a21bf3` states a count, not the targets, so it
is unreconstructable. Selection rule: ledger `common_mode` evidence at BTJD
4074.4 / 4080.8 intersected with cached SPOC Sector 100 light curves.
CSV SHA-256 `b9018206380a65a3…`.

### Running the gates

```powershell
$env:EXOHUNT_CACHE_DIR = "<local-cache-root>"
python -m exohunt.cli batch-hunt `
  --targets targets\p2_artifact_regression_cohort.csv `
  --output-dir results\p2_gates\<name> `
  --author SPOC --cadence-seconds 120 --cache-max-gb 12 `
  --workers 4 --download-workers 2 --allow-no-known
```

~15 minutes, offline from cache, zero errors expected. The 150-target
equivalence cohort and its golden baseline are unchanged; see
`CODEX_HANDOFF.md` for that command.

---

## Suggested next work, in order

1. The remaining kernel rewiring — campaign construction of the dip registry
   from `population.py` plus its per-event window veto. Keep it as its own
   behavior commit with a measured effect; it does not depend on detrending.
2. **Detrending remains blocked**: support weighting and the owner-selected
   narrow-guard/event-support fallback are both ruled out. Any next proposal
   must address trend-model bias directly or require agreement across genuinely
   independent preparations; do not sweep either rejected mechanism.

## Standing constraints

- Claim ceiling is `packet_ready_for_review`; nothing is a candidate before P3
  calibration produces a measured false-alarm rate.
- The owner authorized the **500-target diagnostic cohort** run (2026-07-27).
  It is unspent, and deliberately so: it is §2.3's *fourth* acceptance number
  and is meaningless while the detrending question is open. Announce it when
  you fire it; do not ask again.
- No other campaign-scale runs, injection runs, or science downloads without
  explicit owner approval (`REFACTOR_REVIEW.md` stop condition).
- Behaviour changes ship in their own commit with their measurement. Structure
  changes ship byte-identical. The 236 tests stay green; if one must change,
  say in the commit whether it pinned a constant or behaviour.
