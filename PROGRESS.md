# Overhaul execution progress

Tracks execution of [MASTER_PLAN.md](MASTER_PLAN.md) against its own gates.
Started 2026-07-27 after owner approval of all seven §10 decisions.
Test suite at last update: **180 passed** (114 pre-existing + 66 new), bare
`pytest` from a clean checkout.

## Phase status

### P0 — Fence the machine: **complete**

| Plan item | Status | Evidence |
|---|---|---|
| Stray restart automation | Resolved by owner | Disabled; was Codex-side (decision 2) |
| Machine-wide coordinator lock | Done | `lease.py`: named kernel mutex (auto-released on process death), filelock fallback; guards `batch-hunt`, the science runner, and the dashboard server; second instance exits 0 with a message. Two-process exclusion and death-release proven in `test_lease.py` |
| Truth repair for orphaned checkpoints | Done and run | `exohunt repair-checkpoints` repaired the live phantom: `sector100_spoc` `running` → `interrupted` with audit block; all other checkpoints correctly untouched. Dashboard re-export confirmed the phantom live panel is gone |
| Duplicate dashboard process | **Correction to the plan's observation** | The three observed processes are one server: launcher exe → uv-venv shim python → base interpreter. Nothing was killed; the new mutex prevents genuine duplicates |
| Storage relocation | Done | `%LOCALAPPDATA%\exohunt\` created; `EXOHUNT_CACHE_DIR`/`EXOHUNT_DB_PATH` set as user env vars; code defaults moved to the local state root with env overrides; in-workspace caches keep the `data/`-child containment rule |
| sector100_spoc disposition | Decided (owner) | Not restarted; both signatures imported to the ledger; reports intact on disk |

**P0 exit measurement met**: a second coordinator exits 0 with the message
(subprocess test); no checkpoint claims `running` without a live process.

### P1 — Control plane: **complete and deployed**

| Plan item | Status | Evidence |
|---|---|---|
| Schema §7.3 | Done | `ledger.py`: star, evidence (append-only, idempotent on source), star_state projection, lease, event_log, snapshot; SQLite WAL outside OneDrive |
| Scientific signatures | Done | `config.py` `scientific_signature()`; every imported row carries its legacy signature; `ledger-status` partitions by signature (8 legacy signatures now explicit, incl. 1,864 `processed-lc-v2` + 50 `v3-edge-safe`) |
| Historical import | Done | `importer.py` + `exohunt ledger-import`: 43,790 evidence rows under the corrected importer — the prior 43,787 plus 3 measured-science rows that carry display evidence but no status verdict. They remain non-voting. Existing counts otherwise remain: 14 summaries, 7 checkpoints, 1,890 orphan mixed-version reports, 3,542 context winners, 104 science verdicts, 12,038 common-mode rows, 21 human outcomes |
| **Parity gate** | **PASSED on real data** | Ledger projection == exporter counts exactly: 12,168 stars, 16 statuses (541 automated_survivor, 5,615 common_mode_systematic, …). The extended gate also compares every shared display field for every star (only the obsolete raw `context_report` filesystem path is excluded): 0 count, per-star-status, or payload differences. Hermetic version in `test_importer.py` |
| Two-readings model | Done | `status_counts` (current best) vs `evidence_counts` (conclusions logged) — the 541-vs-3,939 distinction is now two queries over one store. Note: raw evidence rows count summary *and* checkpoint sources, so the "conclusions logged" total depends on source granularity; filter by source prefix for the metrics-ledger-equivalent number |
| DB lease v2 | Done | acquire/refresh/deny/takeover with TTL; takeovers audited in event_log; heartbeat-age is the liveness definition |
| Chaos tests | Done | kill -9 mid-write → `integrity_check` ok, committed rows survive, DB writable; blocked second writer fails cleanly then succeeds; double-coordinator exits 0 |
| Dashboard reads DB | Done on branch | `dashboard_api.py` + `dashboard_server.py`: read-only `/api/summary`, paged `/api/stars`, `/api/star/{tic}` full evidence chain, `/api/ops`, and `/api/systematics`. SQLite `mode=ro` + `query_only` rejects writes in tests. The browser polls only the 3.4 KB summary + ops payload and reloads paged stars only when the ledger revision changes; it no longer requests `survey.json`. The offline exporter remains as the parity oracle. Real-ledger measurements: summary 73.1 ms cold / 77.9 ms mean (<100 ms gate), 1,000 full-detail stars in 49.9 ms. Liveness is lease-heartbeat age only; a fake `state: running` checkpoint with no lease tests non-live |

**P1 exit measurement met on the branch**: all 12,168 current states and
shared display fields match the frozen exporter; `/api/summary` is under
100 ms; read-only connections reject writes; the P0 double-coordinator and
kill-mid-write chaos tests remain green. The running dashboard was deliberately
not restarted: main still owns the installed entry point. After the owner
merges, one idempotent `ledger-import --parity` appends the 3 non-voting rows
and creates the new read indexes, then the frontend build/dashboard restart
activates this path.

**Deployment record (2026-07-27, post-merge)**: the branch was independently
verified (180 tests, production build, diff review), committed, and
fast-forward merged to `main`. The idempotent `ledger-import --parity` re-ran
from `main`'s own installed code: parity held at every level (zero count,
star-status, and display-field differences; 43,790 rows). The frontend was
rebuilt in `main` and the dashboard restarted on the merged code — live
measurements: `/api/health` reports `ledger_available: true`; `/api/summary`
137 ms first-hit over HTTP at 3.4 KB; `/api/ops` liveness `absent` (correct:
no coordinator lease); 1,000-star full-detail page 884 ms over HTTP (the
sub-100 ms gate applies to the summary poll; page loads are background
fetches). The owner completed the interactive push after this deployment
record was written; `origin/main` and the research branch are synchronized at
`709bcc9`. Next work is the characterization-first P2 rewiring recorded in
`CODEX_HANDOFF.md`.

### P2 — Science kernel: **foundations built; characterization and cli.py decomposition in progress**

| Plan item | Status | Evidence |
|---|---|---|
| Config module (HANDOFF 6.6) | Done | Every Appendix-A threshold named with rationale in `config.py`; AST tripwire test proves the drift literals (7.1, 13.7, 21.0, 0.15) exist only there (kernel modules; cli.py joins at rewiring) |
| Detrending v2 | Done (synthetic gates) | `detrend.py`: biweight, two prepared fluxes, support-weighted edges (floor 0.4, α=1), transit-masked second pass. Measured in tests: retention ≥85% on the Sector-100 gap anatomy vs 67% under the hard guard; edge transits recoverable with honest uncertainty inflation; quiet-star depth to 10% in one pass; under 2% variability blind erosion 30–50% and masked recovery to ~10% at the 0.4 d active-star window — the residual P3 will measure at scale |
| Search grids | Done | `search.py`: density-derived duration grids (M dwarf max <3 h — the 6 h rail cannot exist), period overscan so the reporting ceiling is not a grid boundary |
| Alias adjudication | Done | Ratio-ladder scoring with significance-gated event fractions and a 1.1× change margin; TOI-700 c half-period case recovered in tests; measured corrections documented in the commit |
| T3 vetoes | Done | `vetoes.py`: duration-density (pass/flag/kill), depth physicality → EB lane, folded odd/even at 3+1 events, full-phase secondary scan (finds a phase-0.3 secondary the old screen missed), per-event support, dip-window veto |
| Dip registry | Done | `population.py`; noise calibration measured in tests moved σ 2→3 and cohort floor 5%→10% (at σ=2, ~5% of pure-noise star-bins tripped) |
| New dependencies | Installed + pinned | wotan, transitleastsquares (+numba), psutil core; batman/emcee/corner as `[fits]` extra; setuptools pinned for batman's distutils import on py3.12 |
| Pinned characterization golden | Done | First 150 ordered rows of `targets/sector100_expansion_5000.csv` frozen at commit `709bcc9` under TESScut/158 s: 150 reports, 35 diagnostic survivors, 115 rejected, 0 errors. Full provenance and target/cohort hashes are in `results/equivalence/golden_v0/golden_manifest.json`. The handoff command required one measured correction: `--allow-no-known` is necessary for this uncatalogued expansion cohort |
| CLI decomposition: photometry acquisition | Done; equivalence passed | Historical source selection, cache/download handling, TESScut extraction, and processed-light-curve stitching moved from `cli.py` to `photometry.py` in `52aa701`. Focused tests: 31 passed. The full 150-target rerun produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files |
| CLI decomposition: screening helpers | Done; equivalence passed | Historical catalog ephemeris projection, known-period coverage, screening flags, follow-up classification, and sensitivity lookup moved from `cli.py` to `screening.py` in `8ad9f70`; inline legacy thresholds were deliberately preserved for this structure-only slice. Focused tests: 31 passed. The full 150-target rerun again produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files |
| CLI decomposition: campaign scheduler | Done; equivalence passed | The threaded `batch-hunt` scheduler loop, bounded prefetch, rolling retention, progress publication, checkpoint resume, and final campaign publication moved from `cli.py` to `campaign.py`; the CLI retains a thin compatibility wrapper and collaborators resolve at call time so established monkeypatch seams remain authoritative. Focused campaign/retention/lease/checkpoint tests: 29 passed. The full 150-target rerun produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files |
| CLI decomposition: campaign support helpers | Done; equivalence passed | Target-CSV ingestion and per-target spec construction, campaign settings/identity and checkpoint-resume reuse, result-row and error-row construction, per-target download/analysis with transient-failure retry, and the campaign-published counts, vetting coverage, throughput snapshot, common-mode quarantine, and follow-up queue moved from `cli.py` to `campaign.py`. `cli.py` 3,607 → 2,919 lines; every non-import change is a deletion, and the scheduler `run_batch_hunt` is byte-identical (AST-diffed, 540 lines). Of 19 moved definitions, 10 moved byte-identical and 9 changed only to resolve CLI-side collaborators at call time. Generic IO (`_atomic_write_json`, `_replace_with_retry`) and search-identity helpers (`_scientific_settings`, `_artifact_stem`) deliberately stayed in `cli.py`: they have many non-campaign callers, and moving them would point the analysis kernel back at campaign orchestration. Focused campaign/retention/lease/checkpoint tests: 29 passed. The full 150-target rerun produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files. Because the per-target reports exercise the analysis path rather than the helpers this slice moved, the gate was extended to the campaign-published artifacts: `batch_summary.json` settings, counts, vetting coverage, common-mode screen and **all 150 result rows**, plus the 74-entry `deep_followup_queue.json`, are identical to both `golden_v0` and the prior slice's rerun |
| CLI decomposition: target-list construction | Done; A/B equivalence passed | Official-target-list reading, observing-sector subset choice, curated catalog selection, small-planet host ranking, and the three `make-*-targets` commands moved from `cli.py` to a new `targets.py`; 9 of 10 definitions moved byte-identical and only `_make_sector_targets` changed, to reach `_atomic_write_json` on the live CLI module. `cli.py` 2,919 → 2,389 lines (3,607 → 2,389 across both of today's slices, −34%). **The pinned 150-target rerun cannot gate this slice** — `batch-hunt` reads a pre-built CSV and never calls target-list construction — so equivalence was proven by direct A/B: the pre-move tree (`3d1c283`, via `git archive`) and the post-move tree were driven through identical inputs with identical deterministic stubs for the network collaborators, and their canonical JSON dumps hash to the same SHA-256 (`6be34854…`). That covers 675 pure-function cases plus all five command paths, including `make-sector-targets` run twice (round-robin and small-star ranking) against the real 13,000-row official Sector 100 list and the real 12,168-entry exclusion ledger, selecting 750 stars each time. Only `created_utc` and the harness's own per-run temp-directory name were normalized. Focused target/pixel/campaign/retention/lease/checkpoint tests: 33 passed. The 150-target rerun still ran as a regression check on the untouched campaign path and again produced 150/150 byte-identical reports, 35/115/0 counts, no temp files, and published artifacts identical to both `golden_v0` and the prior slice |
| **Not yet done** | — | Remaining structure-only extraction (the single-target `_hunt_from_light_curve` analysis path and the context/vetting commands), then separately measured rewiring onto `detrend.py`/`search.py`/`vetoes.py`/`population.py`/epoch-aware adjudication and first-class signature/evidence records; real-data artifact regression; known-planet campaign cohort; monotransit detector; TLS integration into T2; cli.py AST tripwire |

### P3–P5: **not started** (gated behind P2 exit, per plan)

## Measured corrections to the plan (honesty ledger)

1. The "duplicate dashboard" was a process-chain misread (one server, three
   processes). The mutex guard still lands, for real future duplicates.
2. Blind biweight under strong variability erodes depth far more than the
   plan's window sizing assumed; the two-pass mask plus a 0.4 d active-star
   escalation window is now config with measured numbers.
3. Sign-based event counting and no-margin alias switching were both wrong
   in the first implementation; both fixes are measured and tested.
4. Dip-registry initial thresholds were noise-naive; corrected by
   measurement before first use.
5. Count-only dashboard parity was too weak: it hid 6,423 non-flagging
   common-mode measurements and 3 non-verdict science measurements from the
   first DB serializer even though current-state totals matched perfectly.
   Non-voting evidence now remains available to the display, the missing
   science measurements import append-only, and the permanent parity gate
   compares every shared per-star field. This changes evidence rows
   43,787 → 43,790 and changes **zero** current-best statuses.
6. The committed golden-run command omitted `--allow-no-known`, but the pinned
   expansion cohort has no catalogued ephemerides to mask. The prescribed
   command therefore failed 17/17 attempted targets before science analysis.
   That checkpoint is preserved, the corrected flag is frozen in the manifest,
   and the completed golden plus photometry-extraction rerun both have 150
   reports and zero errors. TIC 305567403 missed Lightkurve's retained cache in
   all three refactor reruns, was re-fetched once per run, and still reproduced
   its report byte-for-byte each time.
7. Per-target JSON identity is a necessary but insufficient equivalence gate.
   Those reports are written by the single-target analysis path, so a slice
   that moves *campaign publication* code can pass 150/150 while publishing a
   different summary. Extending the gate to `batch_summary.json` and
   `deep_followup_queue.json` closed that hole — and immediately exposed a
   second error, this one in the gate itself: `run_state` is per-run
   provenance, not science. `golden_v0` was published with 149 reports already
   on disk and reads `resumed`; every refactor rerun starts from an empty
   directory and reads `completed`. Comparing that field against the golden
   baseline is meaningless; it is compared against the prior slice's rerun,
   which ran under the same fresh-directory conditions. Future slices should
   diff published artifacts against **both** baselines for this reason.
8. The pinned 150-target rerun is not a universal gate. It exercises only the
   code `batch-hunt` reaches, so for the target-list slice — whose functions
   run *before* a campaign, building the CSV `batch-hunt` later reads — it
   would have passed no matter what broke. Structure-only slices outside the
   campaign path need a direct A/B against the pre-move tree instead: check
   out the parent commit, drive both trees through identical inputs with
   identical stubs, and compare canonical dumps. Where a slice touches neither
   path, both gates are needed, not either.
9. **The §2.3 artifact-regression gate as written cannot be passed, and the
   cohort it names no longer exists.** §2.3 asks for "0 of 14" detections at
   BTJD 4074.4/4080.8 on "the 14 real light curves from the edge-safe work".
   That set was never recorded — commit `7a21bf3` states a count, not the
   targets — so it is unreconstructable. A replacement cohort is now pinned at
   `targets/p2_artifact_regression_cohort.csv` (371 targets: ledger
   `common_mode` evidence at the artifact epochs, intersected with cached SPOC
   Sector 100 light curves; manifest alongside). On any statistically selected
   cohort "0 of N" is unachievable in principle: with a median period of
   0.98 d and the 0.02 d floor tolerance from `commonmode.py`, ~31% of targets
   align with an artifact epoch **by chance**, so a flawless pipeline still
   shows ~117 of 371. The gate is therefore restated as *artifact-epoch
   alignment consistent with an empirical null* — alignments at random control
   epochs drawn from the observation span, using the same fitted ephemerides.
   This is §2.3 item 4's own "enrichment" statistic with the control added.
   Two arithmetic errors were made and corrected before this number was
   trusted: the chance rate initially counted one epoch's worth for two epochs
   (reported 1.94×, actually 1.14×), and the first control draw came from the
   fitted-epoch range rather than the observation span. The control now
   returns 0.994×, which is what validates the statistic.
10. **The detrending change does not ship, measured through the real
    `batch-hunt` path on 371 targets.** The support-weighted biweight replaces
    Savitzky-Golay plus a hard half-window guard. It was wired end to end
    (photometry, BLS `dy`, scientific identity), calibrated over an 18-point
    `(window, floor, alpha)` grid, and then reverted. Final numbers, 20,000
    null draws:

    | arm | retention | artifact enrichment | p | survivors | survivors *on* an artifact epoch |
    |---|---|---|---|---|---|
    | Savitzky–Golay + hard guard | 0.669 | 1.137 | 0.046 | 24 | **1** |
    | biweight, support-weighted, α=5 | 0.993 | 1.140 | 0.039 | 51 | **9** |

    Retention passes overwhelmingly. Artifact enrichment does not move at all,
    and artifact-epoch **survivors rise from 1 to 9** — the change promotes
    instrument systematics to survivor status nine times more often. §2.3 ships
    the change only when all four numbers pass, so it does not ship. This is
    the failure §2.3 item 3 exists to catch: the retention gain is re-admitting
    the edge cadences that carry the artifact, not recovering real sensitivity.

    No `(window, floor, alpha)` satisfies the three real constraints together.
    `config.py` had labelled these values uncalibrated placeholders; the
    calibration was run and the answer is that the mechanism cannot separate
    edge *sensitivity* from edge *artifacts*:

    - **The floor cannot carry it.** At window 1.0 d a clean edge has support
      f = 0.5, above the 0.4 floor, so the floor never fires (99.3% retention).
      Raising it to 0.8 passes the artifact gate but leaves an edge transit 2
      of the 5 cadences it needs — passing by destroying the capability.
      Geometry bounds this: a floor F drops cadences within 2h(F − 0.5) of an
      edge, so F ≥ 0.58 always eats edge transits at this window.
    - **Alpha cannot carry it either.** Enrichment falls monotonically with α
      on the probe (1.46 → 1.12) and looked like a pass at α = 5, but the real
      path shows no improvement whatsoever.

    A steep α is defensible in principle — the dominant edge error is *bias*
    from the trend extrapolating rather than interpolating, and bias neither
    shrinks with data nor follows the f^-0.5 law variance would, so inflating
    uncertainty to absorb an un-modelled systematic is standard. It simply does
    not work here. The next attempt needs a mechanism that distinguishes the
    two, not another parameter sweep.

11. **A fast probe harness disagreed with the shipping path, and the probe was
    wrong.** The α = 5 configuration was chosen on a harness that ran detrend +
    BLS directly: enrichment 1.12 at p = 0.14 over 200 targets. Through
    `batch-hunt` on the full 371 the same configuration gives 1.140 at
    p = 0.039 — no improvement over baseline. The harness skipped ephemeris
    masking and the screening cascade, and that was enough to invert the
    conclusion. `CODEX_HANDOFF.md` already warns about exactly this ("not the
    separate `validate` path — that divergence is what hid the TESScut
    disaster"); it applies to measurement harnesses too. **Calibrate through
    the path that ships.** Fast harnesses are for ranking candidates, never for
    the number that decides a gate.

12. **Period-only catalog matching rejects 28 residual signals above the S/N
    gate, and not one of them is mask leakage.** Scanning every campaign
    report: 46 rejections cite a period "within 5% of a catalogued transit
    period or simple harmonic". In all 46 the catalogued signal had already
    been masked (146–1,570 measurements removed), so the rejected peak is a
    *residual* found after the known signal was taken out. For 28 the catalog
    match is the sole rejection reason and all 28 clear S/N 7.1.

    The mask itself is the discriminator, and it avoids comparing ephemerides
    across a harmonic relation (which is what made a first attempt at this
    meaningless — that test would have been reported as a result and was
    thrown away instead). Each mask covers
    `epoch + m × period ± mask_width/2`. Taking every transit the *found*
    ephemeris predicts inside the observation window and asking what fraction
    land inside a masked window:

    | overlap with masked events | meaning | n |
    |---|---|---|
    | ≥80% | known signal leaked through the mask; rejection correct | **0** |
    | 20–80% | ambiguous | 9 |
    | <20% | peak sits on cadences the mask never touched | **19** |

    Most of the 19 are at exactly 0%. The two cleanest: TIC 301160638 at
    S/N 126.3 with 0 of 3 predicted transits on masked events, and
    TIC 301248781 at S/N 30.8 with **0 of 26** — the latter matched at
    relation `exact`, so its period is within 5% of the catalogued one while
    none of its twenty-six transits coincide with a removed event. That is not
    a small-number artifact.

    **This does not make them planets.** They may still be systematics,
    eclipsing binaries, or blends; what the measurement establishes is only
    that the *stated reason* for rejecting them — that they are the catalogued
    signal — is false for 19 of 28, and unverified for the remaining 9. Period
    proximity cannot carry that judgement, which is the concrete case for
    epoch-aware matching in `tce.py` and `evidence.py`. Caveats: transit counts
    are small (2–5) for most of the set, so the overlap fraction is coarse
    except where noted, and the reports span mixed pipeline versions.

## Owner notes

- The old FITS cache (`data/lightkurve`, 9.4 GB inside OneDrive) is now
  orphaned: new downloads go to `%LOCALAPPDATA%\exohunt\cache\lightkurve`.
  Everything in it is re-downloadable. Reclaim the space any time with:

  ```powershell
  Remove-Item -Recurse -Force "data\lightkurve"
  ```

- Dashboard launch path (the one true way):
  `.venv\Scripts\exohunt-dashboard.exe` — a second launch now exits
  harmlessly. Default port 8765, loopback only, unchanged.
- The ledger lives at `%LOCALAPPDATA%\exohunt\exohunt.db`; inspect with
  `exohunt ledger-status`, re-import any time with `exohunt ledger-import
  --workspace . --parity` (idempotent).
- After merging this branch, rebuild `dashboard/` once before restarting the
  installed dashboard. The live server was intentionally left on main during
  branch development.
