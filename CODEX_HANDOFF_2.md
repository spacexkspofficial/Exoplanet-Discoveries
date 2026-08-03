# Codex handoff 2: P2 rewiring after measured rejections

Supersedes `CODEX_HANDOFF.md` for **state and work queue**. That document's
§1 step 2 (structure-only extraction) is now complete. Its first detrending
rewiring and the later owner-selected narrow-guard/event-support fallback were
both measured and rejected. Everything else in it
— the P2 exit gates, the P3 plan, the monotransit detector, the claim ceiling —
remains current and you should read it.

**Are we ready for science? Still no.** The catalog-mask correctness bug is now
fixed and measured in the working tree, but the detrending behavior remains
blocked and the downstream kernel rewiring/calibration gates remain open.

---

## Verified state you inherit (re-verify, don't trust)

```powershell
cd "C:\Users\alexa\OneDrive\Desktop\Codex\Exoplanet Discoveries\.codex-pr-staging\p2-catalog-matching-2"
& "C:\Users\alexa\OneDrive\Desktop\Codex\Exoplanet Discoveries\.venv\Scripts\python.exe" -m pytest -q
# expect: 216 passed
```

- Branch `codex/p2-catalog-matching` lives in
  `.codex-pr-staging/p2-catalog-matching-2` because the primary checkout's
  `.git` metadata is read-only to this task. **Not merged to main. Not pushed.**
- `main` is at `a45403a "idk"` — the owner committed the pinned cohort files
  there mid-session and pushed. That commit contains only
  `targets/p2_artifact_regression_cohort.{csv,json}`. It is merged into the
  branch; there was no conflict.
- 216 tests pass from the staging checkout.
- `cli.py` is **2,468 lines**, down from 3,607 (−32%).
- **No behaviour change has shipped to `main`.** The branch contains three
  separately measured behavior changes: uncertainty-propagated catalog
  masking, exact-period epoch matching, and controlled harmonic matching. The
  two detrending mechanisms and the earlier invalid catalog-matching attempts
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
| latest branch commit | half/double/triple event-number adjudication; one-third held |

`PROGRESS.md` corrections 9–17 are the substance. Read them before the code.

---

## What is done

The **structure-only decomposition is complete**. `cli.py` no longer owns
photometry, screening, campaign orchestration, campaign support, or target-list
construction. Each slice was equivalence-gated: the first three by byte-identical
per-target JSON on the pinned 150-target cohort, the campaign-support slice
additionally by diffing the campaign-published artifacts, and the target-list
slice by a direct A/B against the pre-move tree (the 150-target rerun cannot gate
it — `batch-hunt` never calls target-list code).

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
$env:EXOHUNT_CACHE_DIR = "C:\Users\alexa\OneDrive\Desktop\Codex\Exoplanet Discoveries\data\lightkurve"
$env:PYTHONPATH = "<worktree>\src"
.venv\Scripts\python.exe -m exohunt.cli batch-hunt `
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

1. The remaining kernel rewirings — search grids from
   `search.py`, alias adjudication, T3 vetoes from `vetoes.py`, dip registry
   from `population.py`. Each is its own behaviour commit with its own measured
   effect. None depends on the detrending question.
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
  changes ship byte-identical. The 216 tests stay green; if one must change,
  say in the commit whether it pinned a constant or behaviour.
