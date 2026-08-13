# Overhaul execution progress

Tracks execution of [MASTER_PLAN.md](MASTER_PLAN.md) against its own gates.
Started 2026-07-27 after owner approval of all seven §10 decisions.
Test suite at last update: **327 passed**, bare
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
| Detrending v2 | Built synthetically; two real-data edge mechanisms rejected | `detrend.py` still provides biweight, two prepared fluxes, support weighting, and a transit-masked second pass, but it is not wired into production. Support weighting failed the locked artifact gate (correction 10). The owner-selected quarter-window guard plus two-sided event-support lane also failed (correction 13): 83.584% retention, 1.142× artifact enrichment, and 3 artifact-aligned survivors versus production's 1. Both experiments were reverted; the production half-window guard remains. |
| Search grids | Wired and measured through shipping path | `search.py` now plans baseline/minimum-transit period ceilings, 8% diagnostic overscan, density-derived duration grids with a named fallback, and endpoint flags. `batch-hunt` carries TIC mass/radius into the hunt, passes the actual grids to BLS, records requested and Astropy-effective duration grids, and rejects overscan/rail fits. Locked 150-target A/B: exact cohort and input identity; all 81 fallback targets science-identical; all changes isolated to 69 density-backed targets; 0 overscan or rail fits pass. Important limitation: 124/120 fallback/density fits still choose an effective rail. Full evidence: `P2_SEARCH_GRIDS.md`. |
| Alias adjudication | Done | Ratio-ladder scoring with significance-gated event fractions and a 1.1× change margin; TOI-700 c half-period case recovered in tests; measured corrections documented in the commit |
| T3 vetoes | Wired and measured through shipping path | Every hunt report now records a versioned T3 block: duration-density (pass/flag/kill), depth physicality → EB lane, folded odd/even, family-wise-corrected full-phase secondary scan, and two-sided per-event support. The naive local 3-sigma secondary maximum falsely killed 30.8% of 500 pure-noise folds; the corrected rule kills 0.2%. Locked 150-target A/B is exact in every pre-T3 science field; passes move 6→1 with five named, auditable losses and no gains. Full evidence: `P2_T3_VETOES.md`. |
| Dip registry | Done | `population.py`; noise calibration measured in tests moved σ 2→3 and cohort floor 5%→10% (at σ=2, ~5% of pure-noise star-bins tripped) |
| New dependencies | Installed + pinned | wotan, transitleastsquares (+numba), psutil core; batman/emcee/corner as `[fits]` extra; setuptools pinned for batman's distutils import on py3.12 |
| Pinned characterization golden | Done | First 150 ordered rows of `targets/sector100_expansion_5000.csv` frozen at commit `709bcc9` under TESScut/158 s: 150 reports, 35 diagnostic survivors, 115 rejected, 0 errors. Full provenance and target/cohort hashes are in `results/equivalence/golden_v0/golden_manifest.json`. The handoff command required one measured correction: `--allow-no-known` is necessary for this uncatalogued expansion cohort |
| CLI decomposition: photometry acquisition | Done; equivalence passed | Historical source selection, cache/download handling, TESScut extraction, and processed-light-curve stitching moved from `cli.py` to `photometry.py` in `52aa701`. Focused tests: 31 passed. The full 150-target rerun produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files |
| CLI decomposition: screening helpers | Done; equivalence passed | Historical catalog ephemeris projection, known-period coverage, screening flags, follow-up classification, and sensitivity lookup moved from `cli.py` to `screening.py` in `8ad9f70`; inline legacy thresholds were deliberately preserved for this structure-only slice. Focused tests: 31 passed. The full 150-target rerun again produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files |
| CLI decomposition: campaign scheduler | Done; equivalence passed | The threaded `batch-hunt` scheduler loop, bounded prefetch, rolling retention, progress publication, checkpoint resume, and final campaign publication moved from `cli.py` to `campaign.py`; the CLI retains a thin compatibility wrapper and collaborators resolve at call time so established monkeypatch seams remain authoritative. Focused campaign/retention/lease/checkpoint tests: 29 passed. The full 150-target rerun produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files |
| CLI decomposition: campaign support helpers | Done; equivalence passed | Target-CSV ingestion and per-target spec construction, campaign settings/identity and checkpoint-resume reuse, result-row and error-row construction, per-target download/analysis with transient-failure retry, and the campaign-published counts, vetting coverage, throughput snapshot, common-mode quarantine, and follow-up queue moved from `cli.py` to `campaign.py`. `cli.py` 3,607 → 2,919 lines; every non-import change is a deletion, and the scheduler `run_batch_hunt` is byte-identical (AST-diffed, 540 lines). Of 19 moved definitions, 10 moved byte-identical and 9 changed only to resolve CLI-side collaborators at call time. Generic IO (`_atomic_write_json`, `_replace_with_retry`) and search-identity helpers (`_scientific_settings`, `_artifact_stem`) deliberately stayed in `cli.py`: they have many non-campaign callers, and moving them would point the analysis kernel back at campaign orchestration. Focused campaign/retention/lease/checkpoint tests: 29 passed. The full 150-target rerun produced the identical filename set and **150/150 byte-identical per-target JSON files** (SHA-256), with 35/115/0 counts and no temp files. Because the per-target reports exercise the analysis path rather than the helpers this slice moved, the gate was extended to the campaign-published artifacts: `batch_summary.json` settings, counts, vetting coverage, common-mode screen and **all 150 result rows**, plus the 74-entry `deep_followup_queue.json`, are identical to both `golden_v0` and the prior slice's rerun |
| CLI decomposition: target-list construction | Done; A/B equivalence passed | Official-target-list reading, observing-sector subset choice, curated catalog selection, small-planet host ranking, and the three `make-*-targets` commands moved from `cli.py` to a new `targets.py`; 9 of 10 definitions moved byte-identical and only `_make_sector_targets` changed, to reach `_atomic_write_json` on the live CLI module. `cli.py` 2,919 → 2,389 lines (3,607 → 2,389 across both of today's slices, −34%). **The pinned 150-target rerun cannot gate this slice** — `batch-hunt` reads a pre-built CSV and never calls target-list construction — so equivalence was proven by direct A/B: the pre-move tree (`3d1c283`, via `git archive`) and the post-move tree were driven through identical inputs with identical deterministic stubs for the network collaborators, and their canonical JSON dumps hash to the same SHA-256 (`6be34854…`). That covers 675 pure-function cases plus all five command paths, including `make-sector-targets` run twice (round-robin and small-star ranking) against the real 13,000-row official Sector 100 list and the real 12,168-entry exclusion ledger, selecting 750 stars each time. Only `created_utc` and the harness's own per-run temp-directory name were normalized. Focused target/pixel/campaign/retention/lease/checkpoint tests: 33 passed. The 150-target rerun still ran as a regression check on the untouched campaign path and again produced 150/150 byte-identical reports, 35/115/0 counts, no temp files, and published artifacts identical to both `golden_v0` and the prior slice |
| Catalog ephemeris masking | Done; bounded real-data gate passed | NASA period/epoch uncertainties are now propagated linearly to the complete observation window. Safe masks widen by the accumulated error; missing or >1-duration uncertainty removes zero cadences, is explicitly reported as unmaskable, forces recovery-only labeling, and blocks promotion. Injection-recovery and sector-vetting paths refuse unsafe masks. On the locked 28-product cohort: 30/37 catalog signals safely masked, 7/37 explicitly unmaskable, 0 silent/unsafe masks, 0 execution errors; a second shipping-path execution reproduced all 28 strongest signals, triage verdicts, and classifications. Full evidence: `P2_CATALOG_MASKING.md`. |
| Catalog ephemeris matching | Exact, half-, double-, and triple-period rules wired and replayed; one-third held | After masking was isolated in commit `9f9a860`, the shipping path gained the separately measured exact-period event-window rule. Both frozen 28-product outputs reproduce 4 phase-distinct exact relations, 1 mask-overlap control, and 4 untrustworthy recovery cases. The later harmonic production replay matches the independent diagnostic on 19/19 controlled relations: 12 zero-overlap cases continue, while 3 consistent and 4 controlled partial cases remain rejected. The one under-controlled one-third case remains period-only. Full evidence: `P2_CATALOG_MATCHING.md` and `P2_HARMONIC_MATCHING.md`. |
| **Not yet done** | — | Remaining structure-only extraction (the single-target `_hunt_from_light_curve` analysis path and the context/vetting commands), then separately measured rewiring onto `detrend.py`/`population.py` and first-class signature/evidence records; monotransit detector; cli.py AST tripwire. P3 has separately closed the detrending decision, known-planet campaign, TLS decider, and release-signature gates described below. |

### P3 — Calibration and return of trust: **complete — trusted release stored**

| Plan item | Status | Evidence |
|---|---|---|
| Locked 500 cohort | Complete under one clean commit | `results/p3/locked500_v4/calibration_summary.json`: 4,340/4,340 searches, 500/500 targets, 0 errors under `git:36c935b`; scientific signature `sig1:f78342a75ab6b47d29cae14c38df62cf9a477938d1b71ab2273f26f432856017` |
| Injection/null work items | All release gates pass | 2,840 injections with 402 T2 recoveries; paired fixed-ephemeris median depth-transfer bias **4.025%** (≤5%); edge/interior recovery gap **0.141 percentage points** (≤3 pp); inverted survivors **0/500** and scrambled survivors **0/500** |
| Sampling/completeness | Complete and reported | Deterministic 5% random sample plus 50 feature-space archetypes; random recovery completeness 14.225%, edge completeness 14.085%, promotion-grade completeness 5.141% and 5.423%, respectively. Baseline T3 pass 1/500 (0.2%) and epoch enrichment 1.468× both pass |
| Known planets | Complete, 20/20 | `results/p3/known_planets_v8/known_planet_summary.json`: 20 passed, 0 failed, 0 errors under the frozen identities and amended input-selection/masking rules; signature `sig1:c4a98f58727f2edd2ac5e4b44ce1bccd73bd69ce2a8e89241a40c29a9979b397` |
| Signatures/release block | Trusted release finalized and stored | `results/p3/release_report.json` has status `trusted_release`, every gate green, and is recorded in the ledger for the exact calibration signature. Report SHA-256: `29ddacd05fcdc76ea569ec465263489527af672cc093f376c884f971693448ae`. End-to-end locked-run throughput: **9,275 searches/hour** |

P3's measured exit is satisfied. P4 is now unblocked; P5 remains behind P4's
own exit gates.

### P4 — Vetting depth and backlog adjudication: **foundation built; vetting depth and backlog outstanding**

| Plan item | Status | Evidence |
|---|---|---|
| Snapshot infrastructure (§4.1) | Built, tested, and populated | `snapshots.py`: immutable content-hashed generations with manifest, atomic write, generation pruning that keeps every manifest, and hash verification on read. Two scope classes, because the sources differ: whole-catalog snapshots, and sample-scoped extracts that record the position list they were taken over so `catalog_coverage_gap` stays structurally distinct from `no_match`. Three live generations fetched from the real services: **nasa_toi 8,113 rows**, **nasa_ps 6,336 rows** (`default_flag=1`), **tess_eb 4,584 rows**, all registered in the ledger `snapshot` table. Five further sources (VSX, ASAS-SN, Gaia DR3, Gaia NSS SB1/EB) are declared and implemented but unfetched — they are sample-scoped and wait on a P4 cohort position list |
| Identity graph (§4.1) | Built and tested | `identity.py` plus ledger schema v2 (`identity_node`, `identity_edge`; additive, migration recorded in `event_log`). Proper-motion propagation to a catalog's epoch with an explicit basis string for the cases it *cannot* propagate; ranked counterparts with ambiguity preserved rather than resolved away; faint neighbours retained as scene members but excluded as hosts; edges idempotent per snapshot hash, so re-vetting against a new generation appends rather than overwrites |
| Ephemeris matching (§4.3) | Built and tested | `adjudicate.py` implements the period **and** epoch rule the plan names as a defect. Alias ladder taken from `SearchConfig.alias_ratios` rather than a second hard-coded list; catalog period uncertainty propagated over the elapsed cycles; a propagated phase uncertainty beyond a quarter period reports `not_evaluable` instead of a fabricated disagreement; host match routes and only signal match kills; disagreeing sources never auto-resolve |
| Known-object regression suite (§4.1) | **Green, and measured against deliberate breaks** | `results/p4/known_objects_v1/known_objects.json`: **502 cases** built from the three live snapshot generations — 400 real catalogued objects (150 confirmed planets, 150 TOIs, 100 TESS EBs, stratified across period) and 102 deliberate near-miss impostors. All 502 reproduce their stated intent, and the builder refuses to freeze a case the code disagrees with. Teeth measured, not assumed: removing the epoch test fails **34/502**, widening the phase tolerance fails **34/502**, and widening the period tolerance from 1% to 10% fails **34/502** — each break caught by exactly the impostor family built for it |
| Campaign import | Done; **parity gate now red** | The finished 64,614-target `full_remaining_pool` campaign and several other unimported campaigns were imported idempotently: ledger **18,941 → 83,555 stars**, +146,228 evidence rows. This exposed a latent projection disagreement — see correction 38 |
| Identity resolution | Done for the backlog | `scripts/resolve_p4_identities.py` resolves canonical nodes from TIC v8.2 (VizieR `IV/39/tic82`) in bulk: **1,363/1,363 backlog stars**, with sky position, proper motion, Tmag, Teff, radius, distance, and the TIC's own Gaia DR3 cross-match. The campaign path never needed coordinates, so before this only 920 of 1,363 had any; sample-scoped extracts and PM-aware matching both require them. Gaia edges are recorded with `catalog_crossmatch` basis, leaving the positional neighbour scene as a separate later claim |
| Snapshot coverage | All eight declared sources fetched | `nasa_toi` 8,113 · `nasa_ps` 6,336 · `tess_eb` 4,584 · `gaia_dr3` 8,453 · `vsx` 252 · `gaia_nss_sb1` 79 · `asassn_variables` 61 · `gaia_nss_eb` 1. The five sample-scoped extracts are scoped to the 1,363-star backlog position list and carry its hash |
| Gaia neighbour scene (§4.1) | Resolved for the whole backlog | 8,488 ranked counterpart edges over 1,363 stars: **745 unique, 616 ambiguous, 2 unresolved**. **45.2% of the backlog has more than one plausible Gaia counterpart inside its TESS pixel** — recorded as ranked alternatives rather than resolved away, which is the input pixel-vet v2 exists to consume |
| Backlog re-adjudication | **98.1% resolved, but see the caveat** | `results/p4/readjudication_v1/` under policy `p4-readjudication-v3-consulted-is-fetched`, vetting signature `vet1:af603463…`: 1,363 backlog stars. T3 re-gate against the calibrated red-noise floor: **330 fail**, 337 pass, 696 not evaluable. T5: 1,285 `unresolved_transit_like_signal`, 25 `known_eb_host_residual_review`, 16 `known_variable_star_review`, 11 `known_eb_rediscovery`. **1,337/1,363 resolved (98.09%)**, 26 still open with no ephemeris anywhere in the ledger. Evidence rows are written non-voting pending correction 38. **The ≥80% number is met and should not be read as P4 being done — see correction 43** |
| T3 re-gate verification | Checked against source, not just self-consistent | No record sits on the wrong side of its own 7.1 floor, and 8 failing stars cross-checked against their per-target report JSON on disk reproduce the recorded red-noise-adjusted S/N exactly. The re-gate reads numbers those reports already carried; it does not recompute photometry |
| Pixel vetting v2 (§4.4) | Built and measured synthetically; **not yet run on real pixels** | `pixel.py` gains all three upgrades the plan ranks ahead of PRF fitting. **Aperture-growth depth curve**: depth rising with radius is a contaminant, falling is ordinary dilution; the statistic is normalized by the larger depth so it stays bounded in [-1, 1] (see correction 45). **Bootstrap localization**: the centroid is resampled over its in/out cadence selection, so an offset is reported in units of its own error rather than as a bare distance — the same 0.6-pixel offset is decisive at ±0.05 and meaningless at ±0.8, and v1 reported both identically. **Per-sector consistency**: reduced chi-square of the per-sector centroids about their weighted mean, which catches a blend whose every individual sector sits inside the one-pixel tolerance while disagreeing with the others far beyond their errors. **Neighbour-transit extraction**: each ranked counterpart gets its own aperture and the deepest coherent signal names the host — a reassignment, not a warning. 13 tests over synthetic scenes where the true host is known by construction; v1's `difference_image` is untouched and still passes |
| T7 cross-reduction gate (§4.5) | Built and tested; **not yet run on real reductions** | `crossreduction.py` implements the promotion gate as four independent requirements plus one precedence rule. **Depth agreement** across independent reductions is compared *pairwise*, not against a pooled mean — a single precise outlier would otherwise drag the mean onto itself and pass. **Undetrended SAP support** is mandatory, the direct lesson of this project's history: every detrended product inherits a dip the detrender invented, so their agreement says nothing about the sky. **Sector support is asymmetric** — a sector whose injected completeness at this depth is below 50% abstains rather than objecting, because a non-detection where nothing was detectable is not evidence of absence. **Stacked secondary and odd/even** are re-measured on the all-sector fold (TIC 181014443 was 2.3σ in one sector and 5.9σ stacked). A common-mode verdict short-circuits before any of it is computed, so no cross-reduction result can appear to have argued the other way. 15 tests |
| Review packet (§4.7) | Built and tested | `packet.py` assembles the ten sections §4.7 enumerates and **refuses to call a packet ready when any of them is missing or unmeasured**, naming the specific sections rather than padding or downgrading — a packet with no pixel localization must not read like one whose localization passed. `not_run`, `{}`, `None` and `not_evaluable` are all treated as absence; an explicit negative result ("localized off target") counts as measured, because it is. The deferred TRICERATOPS FPP therefore *blocks* a packet rather than being assumed. `packet_ready_for_review` is added to the status registry as the 24th status at `measured_science` (Appendix C sanctions additive evolution; the existing 23 keep their slugs, and the count pin in `test_statuses.py` was raised deliberately). The claim ceiling travels with every packet and is enforced structurally: there is no code path emitting `vetted_candidate`, `confirmed_planet` or `rediscovery`, asserted by test. 13 tests |
| T8 transit fit and physical sanity (§4.6) | Built and tested | `transitfit.py`: batman + emcee posteriors with quadratic limb darkening, and the check that actually catches false positives — **stellar density from the transit geometry alone**. (a/R\*) with the period gives the mean density of the star being transited independently of any catalogue, and the same light curve is fit equally well by a small planet on a dwarf or a grazing binary on a giant; those solutions differ by orders of magnitude in density and nothing else in the light curve separates them. The relation is anchored by a test: Earth's orbit at P=365.25 d returns **1.0013 solar densities**. Posteriors are reported **only when the chain mixed** — an unconverged interval is indistinguishable on inspection from a converged one, so a starved chain returns `not_run` with the diagnostic attached rather than a plausible-looking number. Also SED giant-impostor check from the Gaia colour-magnitude position, and a walker-count guard so an emcee configuration error is reported rather than raised. `emcee`/`corner` installed from the existing `[fits]` extra. 15 tests |
| Review queue (§8) | Built, tested, live | `/api/review-queue` plus a dashboard panel. **1,033 entries** from the 1,363-star backlog — the 330 the calibrated T3 re-gate killed are excluded outright, because they are resolved and human attention is the scarcest resource here. Ranked by contested evidence first (disagreeing sources, then signals matching a catalogued planet or TOI, which need a human because every rediscovery status is human-stage), and every entry states what it waits on: **413 ambiguous identity, 26 no ephemeris, 2 sources disagree**. Scoped to the newest vetting generation; reads only non-voting rows, so opening it moves nothing. 8 tests |
| Pixel vetting v2 on real pixels | **Run on a 60-star pilot; 57 measured** | `results/p4/pixel_pilot_v3/`, all 57 on the target's own discovery sector (corrections 46 and 47 cover two earlier runs that were not). **11 stars localize significantly off target** — offsets 0.72–4.31 px at 4.6σ–42.5σ, the first real `pixel_offset_contamination` candidates this stack has produced. **3 more** show aperture growth consistent with a contaminant. The two tests flag **disjoint sets**, which is informative rather than contradictory: they have different sensitivity regimes, and the aperture curve is limited by the same sub-pixel problem as correction 46. **0 host reassignments**: 46 `not_resolvable`, 11 with no counterpart. Caveat on the 31 `no_depth_in_target_aperture` — those are raw undetrended TESScut aperture sums, and the campaign detected these signals in *detrended* photometry, so a shallow signal is expected to be invisible here and this is **not** evidence against it |
| T7 on real reductions | **Attempted, not measured — see correction 48** | The runner exists (`scripts/run_p4_t7_pilot.py`) and is correct, but MAST began closing connections on this session after three 60-target pixel runs plus the catalog snapshots. Four targets took over ten minutes with backoff in place. No numbers are reported, because the only numbers available would have been an artifact of my own request rate |
| **Not yet done** | — | T7 measured on real alternate reductions (needs an unthrottled MAST session); TRICERATOPS itself (deferred by owner decision, and its `not_run` FPP currently blocks every packet from reaching `ready`); promoting any P4 evidence to voting, which waits on correction 38 |

P4's exit is **not** met. The backlog gate reads 98.1% against the plan's
≥80%, but that number passes on the strength of catalog coverage alone: 94% of
it is `unresolved_transit_like_signal`, meaning every declared source was
checked and none explains the signal — a filed lead, not an adjudicated one.
Correction 43 states this in full. The vetting substrate exists, is gated, and
has its inputs; the vetting *depth* the phase is named for (pixel-vet v2, T7
cross-reduction, T8 fit and FPP, the review queue) is not built, and 616 of
these stars have an ambiguous identity inside their own pixel that nothing in
the current stack can settle.

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

12. **Catalogued ephemerides are too stale to mask with, and that invalidates
    two earlier attempts to judge catalog matching.** The campaign masks
    catalogued transits, searches the residual, then rejects a peak whose
    period lands within 5% of a catalogued period or simple harmonic. 46
    reports carry that rejection; 28 have it as the sole reason and clear
    S/N 7.1.

    The finding is upstream of the question. For **all 28**, the catalogued
    epoch predates the observation window by 70 to 1,616 cycles, and the phase
    drift accumulated over that baseline exceeds the mask half-width (0.06–0.21
    d) in every case. The mask is therefore placed at an essentially arbitrary
    phase: it removes 146–1,570 measurements that are mostly not the known
    transits, while the real ones survive into the "residual" search. TIC
    301160638 is the clean demonstration — TOI-3487.01, catalogued epoch BTJD
    2378.99 against data at 4070–4097, ~106 cycles, drift ≈ 2.6 d versus a
    0.14 d mask half-width. Re-running it produces a 1.3%-deep signal at
    S/N 153.6 whose period matches the catalogue to 0.15%: the known planet,
    unmasked.

    **This supersedes the previous version of this entry, which was wrong.**
    Two tests were built and both were invalid, in the same way: each looked
    decisive and neither could distinguish the hypothesis it was testing from
    stale-ephemeris masking.
    - Comparing the found and catalogued ephemerides directly — meaningless
      across the harmonic relations most of these matched.
    - Asking what fraction of the found transits land on masked windows —
      reads 0% for a genuinely distinct signal *and* for the true signal whose
      mask was placed at the wrong phase. Wired as a rejection gate it promoted
      TIC 301160638, a catalogued TOI, to `automated_survivor`. Reverted.

    So whether period-only matching discards real signals **remains unknown**,
    and no count from this entry's earlier versions should be carried forward.
    Answering it requires first fixing the masking: propagate catalogued
    ephemerides with their uncertainty, widen the mask by the accumulated phase
    error, and refuse to claim a signal is masked when that error exceeds the
    transit duration. Until then the residual search is not searching residuals
    on these targets. That is a correctness bug in the shipping pipeline, not a
    P2 design question, and it is the more valuable of the two findings.

    **Resolution (2026-07-27): fixed and measured.** The catalog query now
    requests period, epoch, and duration uncertainties; known-host cache rows
    created before those columns were requested refresh once, while zero-known
    cache rows remain rate-safe. The mask propagates the conservative linear
    error envelope across the complete observation window, widens by that error
    only while it remains at most one transit duration, and otherwise removes
    zero cadences and records an explicit unmaskable verdict. The normal
    residual path stops; `--allow-no-known` may continue only as an honestly
    labelled recovery scan with promotion disabled. The injection and
    sector-coherence paths also stop on an unsafe mask.

    The locked 28-product gate (7 SPOC + 21 TESScut) contains 37 catalog
    signals: **30 safely masked, 7 explicitly unmaskable, 0 silently or
    partially masked, 0 execution errors**. A second execution reproduced all
    28 strongest-signal payloads, triage verdicts, and classifications. The two
    TESScut automated survivors remain diagnostic only; this gate establishes
    mask correctness, not novelty. Focused tests: 42 passed; full suite:
    **186 passed**. See `P2_CATALOG_MASKING.md`.

13. **The owner-selected quarter-window guard plus edge-only diagnostic lane
    was measured and rejected.** This was a different mechanism, not another
    support-weight parameter sweep: the hard guard narrowed from 0.50 to 0.25
    window, and promotion required at least two transit events with real
    in-transit cadences and two-sided local baselines. Focused mechanism tests
    and the full experimental suite passed (190 tests), then the complete
    371-target cohort ran through shipping `batch-hunt` with zero errors.

    | arm | retention | artifact enrichment | p | survivors | survivors *on* an artifact epoch |
    |---|---:|---:|---:|---:|---:|
    | Savitzky–Golay + half-window guard | 0.66933 | 1.137 | 0.046 | 24 | **1** |
    | quarter-window + event-support lane | **0.83584** | **1.14193** | **0.04805** | 21 | **3** |

    The lane found 34 edge-dependent signals and prevented all of them from
    promotion; 15 had no other rejection and would otherwise have survived.
    That part worked. The release still fails: retention misses the 0.85 gate,
    artifact enrichment does not improve, and artifact-aligned survivors rise
    1 → 3. The surviving failure has two-sided samples, so the missing
    discriminator is trend-model *bias*, not sample support. The behavior and
    its temporary tests were reverted; production remains at 186 tests and the
    half-window guard. Full injections and the authorized 500-target check were
    not fired after the first two release gates failed. See
    `P2_EDGE_DIAGNOSTIC.md`; raw measurement:
    `results/p2_gates/artifact_narrow_guard_edge_diagnostic/p2_gate_measurement.json`.

14. **Trustworthy masking proves that period-only exact matching discards
    distinct ephemerides.** The fresh 28-product masking cohort contains nine
    period relations: four belong to explicitly unmaskable recovery-only
    signals, while five are safely masked exact-period relations. A conservative
    epoch diagnostic generated every recovered event and asked whether its full
    transit window overlaps the uncertainty-expanded catalog mask.

    - TIC 301160638 overlaps the known mask at both recovered events. It is the
      control: the recovered BLS centers sit just beyond the mask center, but
      the fitted transit windows still touch the removed known-signal windows,
      so it remains rejected as leakage.
    - Four safely masked exact-period relations have **zero** overlapping event
      windows. Two still fail other gates. TIC 301248781 and TIC 450649506 have
      no other automated rejection and are currently discarded solely because
      their periods are nearby.

    The result justifies epoch-window adjudication for **exact** relations:
    host match + period match is not signal match. It does not justify changing
    harmonic handling; the trustworthy cohort contains no safely masked
    harmonic relation, and aliases require event-number-aware comparison.
    Four synthetic diagnostic tests pass; full suite **190 passed**. No
    production behavior was changed because catalog masking remains uncommitted
    and must land separately. Repeating the diagnostic on the earlier v2
    outputs reproduced every count and safely masked verdict; only the four
    unmaskable status/reason strings changed wording. See
    `P2_CATALOG_MATCHING.md` and
    `scripts/measure_p2_catalog_matching.py`.

15. **A frozen harmonic cohort now separates period ratio from event-number
    identity without a new campaign.** Only the two historical shipping-path
    Sector 100 campaign directories were eligible discovery sources;
    experimental detrending outputs, equivalence copies, and later catalog
    diagnostics were excluded. Each selected product also had to be cached,
    use the uncertainty-aware catalog schema, and reference a signal marked
    safely masked in the current masking gate.

    The locked cohort contains 20 product-targets (6 SPOC + 14 TESScut, 19
    unique stars) and 20 harmonic relations: 5 half, 5 double, 1 one-third,
    and 9 triple. Event windows use the same conservative tolerance as the
    exact diagnostic. Longer-period aliases must align every recovered event;
    shorter-period aliases must align one complete event-number class modulo
    two or three at least twice.

    | verdict | count | treatment |
    |---|---:|---|
    | zero-overlap phase-distinct | **12** | may continue through other gates |
    | consistent event-number pattern | **3** | remains catalog-harmonic |
    | partial/ambiguous overlap | **5** | remains rejected |

    All 12 phase-distinct historical signals were rejected solely by the
    period-only rule. They are diagnostic survivors, not candidates, and the
    selected regression set cannot estimate population yield or false-alarm
    rate. Half, double, and triple each have real phase-distinct and consistent
    controls; one-third has only one ambiguous example and no positive or
    phase-distinct real control, so its production behavior remains held. Four
    cohort builder tests plus five harmonic diagnostic tests pass; full suite:
    **199 passed**. No campaign, download, or production behavior change was
    run. See `P2_HARMONIC_MATCHING.md`.

16. **The measured exact-period event-window rule is now wired as its own
    behavior change.** The masking fix was first isolated in commit `9f9a860`.
    The shipping hunt now records the epoch verdict, overlap geometry, and
    `catalog_match_rejects` decision on every known-period relation. It removes
    the catalog rejection only for safely masked exact-period relations with
    zero recovered event-window overlap. Harmonics, partial overlaps, and
    untrustworthy masks preserve the old rejection.

    The production helper was replayed over both locked 28-product masking
    outputs. Both runs reproduce the same five safely masked exact verdicts and
    offsets: four phase-distinct and one mask-overlap leakage control. Four
    reports lose only the catalog reason; two still fail unrelated gates, while
    TIC 301248781 and TIC 450649506 become the only new automated triage passes.
    Total passes move **2 → 4**. The four untrustworthy recovery cases remain
    rejected and non-promotable.

    Five pure adjudicator tests, two tests through shipping
    `_hunt_from_light_curve`, and one replay-projection test make this permanent.
    Focused exact/masking suite: **15 passed**; full suite: **207 passed**.
    No campaign or download was run. See `P2_CATALOG_MATCHING.md`.

17. **Controlled harmonic relations now use event-number-aware production
    adjudication.** The pure catalog adjudicator and shipping hunt now evaluate
    half-, double-, and triple-period aliases over the report's observation
    span. Zero recovered event-window overlap removes only the catalog-period
    rejection. A half-period alias is consistent only when one complete
    modulo-two event class aligns at least twice with no overlap outside that
    class; double- and triple-period aliases are consistent only when all
    recovered events align at least twice. Partial, insufficient-support, and
    untrustworthy cases remain rejected.

    The production helper was replayed offline over the frozen 20-relation
    harmonic cohort. It matches the independent diagnostic on **19/19**
    controlled relations: 12 phase-distinct, 3 consistent, and 4
    partial/ambiguous. Historical projected passes move **0 → 12**, exactly the
    zero-overlap cases. The single one-third relation is deliberately not
    evaluated in production and remains rejected.

    Focused epoch/masking/harmonic suite: **24 passed**; full suite:
    **216 passed**. No campaign or download was run. Raw replay:
    `results/p2_gates/harmonic_epoch_production_replay/p2_harmonic_matching.json`.
    See `P2_HARMONIC_MATCHING.md`.

18. **Physical search grids are now wired and isolated by a locked
    shipping-path A/B.** The production hunt derives the reportable period
    ceiling from the observation baseline and minimum-transit rule, searches
    8% beyond that ceiling, derives its duration grid from stellar density
    when both mass and radius are available, and records a named solar fallback
    otherwise. Fits in the overscan zone or on either actual grid endpoint are
    diagnostics and cannot pass automated triage. Target-list construction and
    `batch-hunt` now preserve mass/radius into the analysis metadata.

    The authorized 150-target TESScut gate used the exact frozen golden
    identity hash. The first new arm applied the fallback grid to all targets;
    the second used 69 complete catalog density pairs and 81 fallbacks. Both
    produced 150 reports with zero errors. All 150 observation windows and
    normalized extraction payloads match. More importantly, all 81 fallback
    targets are exact in strongest signal, triage, grid, and the complete
    science payload between new arms. Every changed result is therefore
    isolated to the 69 density-backed targets.

    A final-review correction changed the gate result before commit. Astropy's
    fast BLS quantizes durations to `minimum_duration / oversample`; the first
    implementation compared its returned fit to the unquantized requested
    endpoint and under-counted rails as 5/4. Production now records the
    requested and effective grids, compares against the effective endpoint, and
    includes a search-policy version in checkpoint identity. Fresh arms
    reproduce all 150 fallback fitted signals exactly.

    Corrected golden/fallback/density survivor counts are **35/5/6**. The new
    grids do not make rail-seeking fits disappear: fallback/density arms contain
    **124/120 total rail fits** (123/119 duration rails), plus 20/14 overscan
    fits. The hard safety rule does work--**zero rail or overscan fits pass**--
    but sensitivity cost remains an explicit injection/known-planet calibration
    question. Inside the isolated density subgroup, passes move 3 to 4: one
    survivor-to-rejected and two rejected-to-survivor transitions. The density
    grid still changes the BLS objective broadly; 43/69 recovered periods move
    by more than 1% and are not a simple harmonic.

    Full suite: **228 passed**. See `P2_SEARCH_GRIDS.md`; raw corrected A/B:
    `results/p2_gates/search_grid_shipping_ab_150.json`.

19. **The T3 kernel is now wired, but the literal full-phase secondary rule
    failed calibration before commit.** The first shipping arm compared the
    strongest local out-of-primary phase window directly with 3 sigma. On 500
    deterministic pure-noise folds it killed **154/500 (30.8%)** because the
    maximum of many windows is not a global 3-sigma statistic. It also marked
    97/150 real-cohort signals as secondaries and retained only one of six
    baseline survivors.

    Production now uses the standard error of a median and a Bonferroni
    family-wise correction over the actual tested phase windows. The same
    median-error correction applies to folded odd/even depths. The deterministic
    noise result becomes **1/500 (0.2%)**, with a permanent 1% regression cap,
    and the veto policy is versioned in checkpoint identity. Non-finite
    cadences are removed before folded checks.

    The corrected 150-target shipping arm produced zero errors and is exact to
    the density-grid baseline in observation window, strongest signal, search
    grid, and the complete pre-T3 science payload. Passes move **6 → 1**:
    three lost survivors have family-wise-corrected secondaries at local S/N
    6.12, 6.961, and 11.278; two have only one or zero transit events with
    two-sided local support; the S/N 11.278 case also has a fatal
    duration-density ratio of 2.533. There are no gains. Across all 150
    reports, corrected T3 records 9 duration-density kills, 56
    duration-density review flags, 10 odd/even kills, 35 secondary kills, and
    71 insufficient-support signals. These are reversible signal decisions;
    they do not calibrate completeness or reliability. Full suite:
    **236 passed**. See `P2_T3_VETOES.md`; raw corrected A/B:
    `results/p2_gates/t3_shipping_ab_150.json`.

20. **The dip registry carried the same estimator error correction 19 fixed in
    T3, and it survived because nothing called it.** `population.py` measures a
    bin's depth as a **median** but divided by `sigma / sqrt(n)`, the standard
    error of a *mean*. The asymptotic standard error of a median is
    `sqrt(pi/2) * sigma / sqrt(n)`, so the rule understated its own uncertainty
    by ~25% and a nominal 3-sigma per-star trip was really about 2.4 sigma.
    This is analytic, not a tuning opinion; `vetoes.py` already defined
    `MEDIAN_STANDARD_ERROR_FACTOR` for exactly this reason and
    `population.py` now imports it rather than restating the constant.

    Measured on 200 deterministic pure-noise cohorts (30 stars, 3-day span,
    10-minute cadence, current config: sigma 3, 10% fraction floor, 20-star
    floor), changing only the estimator:

    | arm | pure-noise cohorts registering a window | per-star-bin trip rate |
    |---|---:|---:|
    | mean standard error (before) | **13/200 (6.50%)** | 0.4506% |
    | median standard error (after) | **0/200 (0.00%)** | 0.0594% |

    A correct one-sided 3-sigma gate should trip ~0.135% of star-bins. The old
    rule tripped 0.4506%, 3.3x too often; the corrected rule trips 0.0594%,
    mildly conservative because the asymptotic median error overestimates the
    true median scatter at n = 3 cadences per 30-minute bin. Conservative is
    the correct direction for a veto. A permanent regression test caps the
    observed pure-noise registration rate at 1%.

    **No scientific result changes**: the module still has no production
    caller, so no report, triage verdict, or status moved. This corrects the
    screen before its first use, which is the same discipline correction 4
    applied to its thresholds. Note that correction 4 raised sigma 2 to 3 and
    the cohort floor 5% to 10% by measurement — those floors were partly
    compensating for this estimator, and Appendix A's "tune on sector 100/105
    data where truth is known" remains unspent, so they should be re-derived
    against real cohorts rather than treated as settled.

    Structural work landed in the same commit because the characterization
    tests that exposed the estimator needed it: bins are now anchored in
    absolute time (`floor(t / bin_days)`) instead of the cohort's own minimum,
    so a campaign can fold stars in one at a time and get a result independent
    of completion order; a `DipRegistryAccumulator` holds two integers per
    occupied bin rather than one array per star; and `CohortDipRegistries`
    partitions per sector-camera-CCD as MASTER_PLAN section 3.6 requires. That
    restructuring was verified against the four pre-existing registry tests
    before the estimator changed. Full suite: **249 passed** (236 + 13 new).

21. **The dip registry is wired and measured, and the measurement says it
    cannot address the two epochs it was expected to.** The T4 registry now
    ships: every report records its own absolute-time `population_bins`, the
    cohort registry is derived from those durable reports (so it is
    rebuildable and re-thresholdable forever without photometry), and T3
    discounts any event whose centre falls in a registered window. Reports
    record `applied` / `no_registry_available` / `disabled_by_config`
    distinctly, because "no screen ran" must never read as "a screen ran and
    found nothing".

    Measured on the 371-target Sector 100 SPOC cohort (arm A, 371 reports,
    0 errors, 47.2 min offline from cache), across all three cohort
    granularities built from the same reports:

    | granularity | cohorts (>= 20-star floor) | windows | stars stranded | events discounted | triage passes |
    |---|---:|---:|---:|---:|---:|
    | per sector-camera-CCD | 16 (8) | **45** | 71 | 44 over 33 reports | 2 -> 2 |
    | per sector-camera | 4 (3) | **1** | 14 | 1 | 2 -> 2 |
    | sector-wide | 1 (1) | **0** | 0 | 0 | 2 -> 2 |

    **Dilution is now measured rather than argued.** Windows collapse
    45 -> 1 -> 0 as cohorts coarsen, on real photometry, confirming the
    synthetic prediction and MASTER_PLAN section 3.6's per-detector rule. The
    cost is equally concrete: this cohort was selected by artifact epoch, not
    detector balance, so 8 of 16 detectors fall under the 20-star floor and
    71 of 371 stars sit in cohorts that can never register a window.

    **Neither BTJD 4074.4 nor 4080.8 is covered by any window, and the reason
    is not the screen.** The cohort's prepared photometry spans
    4074.979-4099.479: epoch 4074.4 lies *before the data begins*, and 0 of
    371 stars have any cadence in the 30-minute bin containing 4080.8. There
    is no prepared flux at either epoch to aggregate. This is consistent with
    corrections 10 and 13: these are edge artifacts, and the production
    Savitzky-Golay half-window guard already removes those cadences
    (retention 0.669). The registry operates on prepared flux, so it is
    structurally the wrong instrument for artifacts the preparation stage has
    already excised. **A null result here is therefore not evidence the
    screen works or fails**; it is evidence about where in the pipeline these
    two artifacts are handled.

    The screen did register a real shared event the ephemeris screen cannot
    see, because it never aliased into a common period: 18-22 unrelated stars
    dimming together across BTJD 4092.29-4092.44. That is the absolute-time
    capability section 3.6 exists to add.

    **Science impact is zero so far**: 44 events discounted across 33 reports,
    and automated triage passes stay 2 -> 2. Events were discounted, no
    verdict changed. These counts measure behaviour, not completeness or
    reliability, and the claim ceiling is unchanged.

    Appendix A's `dip_registry_bin / fraction` thresholds remain uncalibrated.
    They were set synthetically (correction 4) partly to compensate for the
    estimator fixed in correction 20, and this cohort is too detector-sparse
    to settle them. Calibrating them needs a detector-balanced cohort built
    with `make-sector-targets`, which preserves camera/CCD balance. Full
    suite: **264 passed**. Raw measurement:
    `results/p2_gates/dip_registry_measurement.json`.

22. **The section 2.3 artifact gate names epochs that do not exist in the
    reduction it is run on.** The gate's gating gate: correction 9 restated
    section 2.3 as artifact-epoch alignment against an empirical null, and
    the 371-target cohort was built by intersecting ledger `common_mode`
    evidence at BTJD 4074.4/4080.8 with cached **SPOC** light curves. Both
    detrending arms (corrections 10 and 13) were then judged on that cohort.

    Measured against the ledger and the arm A reports:

    | measurement | value |
    |---|---:|
    | `common_mode` evidence rows | 12,038 |
    | distinct shared epochs | 1,080 |
    | shared epochs inside the SPOC observation window (4074.979-4099.479) | 496 |
    | in-window epochs with >= 20 SPOC stars observing them | **0** |

    Every in-window shared epoch clusters at BTJD 4080.13-4080.85. The most
    shared epoch in the entire ledger, BTJD 4080.79, carries **215 targets**
    and **zero** SPOC stars have a cadence anywhere in it. BTJD 4074.4 falls
    before the SPOC window begins at all.

    The artifact is real; it is simply not in this reduction. The historical
    evidence came from TESScut, which applies no quality masking, so the
    scattered-light interval survived into those light curves and produced
    the shared ephemerides. SPOC's PDCSAP masking removes that interval
    outright: **the SPOC data gap is the artifact, excised upstream by the
    mission pipeline.** An earlier note in this session attributed the
    absence to our own Savitzky-Golay edge guard; that was speculation and
    the ledger contradicts it.

    **What this does and does not invalidate.** The enrichment statistic
    folds *fitted ephemerides* against the artifact epochs, so it still
    computes without data there, and the empirical null still bounds chance
    alignment correctly -- corrections 10 and 13 did discriminate between
    arms (survivors on artifact epochs 1 vs 9 vs 3). What is undermined is
    the statistic's physical meaning on SPOC: "aligns with an artifact epoch"
    can only be phase coincidence when no cadence at that epoch was ever
    searched. It cannot be contamination *by* the artifact, because the
    artifact is not in the data. That is consistent with the measured
    enrichment being only 1.137-1.142 and barely significant.

    **The reduction the gate runs on must be the reduction the artifact is
    in.** Two defensible repairs, neither yet made: run the artifact
    regression under `--author TESScut --cadence-seconds 158`, where BTJD
    4080.8 has cadences and the original evidence was produced; or derive the
    epochs from the SPOC cohort itself. Arm A supplies the latter directly --
    its own most-shared dip bins are BTJD 4092.396 (22 of 370 stars, 5.9%),
    4092.438 (21), 4092.292 (19) and 4078.292 (17 of 371), all with full
    observing coverage. Note that 5.9% sits below the registry's 10% cohort
    floor, which is why sector-scope registration found nothing while the
    per-CCD partition registered 45 windows.

    Until this is repaired, **no detrending mechanism can be fairly judged**
    on the artifact-regression gate, because the number that decides it is
    measuring phase coincidence rather than artifact contamination. This is
    upstream of the detrending question in the same way correction 12's
    stale-mask finding was upstream of catalog matching. No production
    behaviour was changed by this entry. Raw probes are session scratch; the
    ledger queries and arm A reports reproduce every number above.

23. **The artifact-enrichment criterion cannot resolve the difference it was
    asked to judge.** Section 2.3 requires artifact enrichment to improve
    before a detrending change ships, and corrections 10 and 13 recorded that
    it "does not move at all" across the three arms: 1.137 (production),
    1.140 (biweight), 1.142 (narrow guard). That observation is correct and
    its interpretation was too generous to the statistic -- it could not have
    moved.

    Measured on arm A (371 targets, the same cohort and the same two epochs
    all three arms used), bootstrapping over targets at **fixed** epochs so
    that epoch choice cancels exactly as it does between arms:

    | measurement | value |
    |---|---:|
    | aligned at the historical epochs | 157 / 371 |
    | empirical null mean | 136.33 |
    | enrichment | 1.1516 |
    | bootstrap standard deviation | **0.0704** |
    | 95% confidence interval | **[1.0122, 1.2836]** |
    | spread across the three arms | 0.0050 |
    | that spread, in sigma | **0.071** |
    | spread needed to detect at 2 sigma | 0.141 |

    The arms differ by **one fourteenth of one standard deviation**. Closing
    that gap by cohort size alone would need roughly `371 x (0.141/0.005)^2`
    targets -- on the order of **300,000 stars**, which is more than the
    entire searched population to date and decisively impractical. The
    criterion is not merely underpowered on this cohort; it is unusable at
    this effect size by any cohort this project will run.

    Two further readings, both measured:

    - **The epochs are genuinely elevated, marginally.** Against 40 random
      interior epoch pairs (mean 0.999, sd 0.075), the observed 1.137-1.142
      sits at the 95th percentile. The 95% interval [1.012, 1.284] excludes
      1.0 only barely. So there is a real effect; it is small and imprecisely
      measured.
    - **Almost all of it comes from an epoch outside the observation span.**
      Per epoch: BTJD 4074.4 gives 1.189 (p=0.029) and lies 0.594 d *before*
      the data begins, while 4080.8 gives 1.042 (p=0.358). Controls are drawn
      uniformly *inside* the span, so the observed epoch and its null are not
      like for like. Correction 9 fixed the mirror image of this (controls
      drawn from the fitted-epoch range rather than the observation span);
      this is the same class of error on the other side.

    **What this does not overturn.** Corrections 10 and 13 rejected both
    mechanisms primarily on numbers that did move by large amounts:
    retention (0.669 / 0.993 / 0.836) and survivors sitting on artifact
    epochs (1 / 9 / 3). Those remain informative and the rejections stand.
    It is specifically the enrichment criterion that carries no information
    at this effect size, and section 2.3 should stop treating it as one of
    the four numbers that must pass.

    `scripts/measure_p2_artifact_gate.py` now takes `--artifact-epoch`
    (repeatable, defaulting to the historical pair so existing invocations
    reproduce exactly), and the empirical null draws as many controls as
    there are epochs rather than always two. `scripts/derive_artifact_epochs.py`
    finds epochs from a campaign's own `population_bins` by binomial tail
    against the cohort's background dip rate; on arm A only two bins clear
    p < 1e-4 out of 863 observed, both at BTJD 4092.40-4092.44. Full suite:
    **265 passed**. No production behaviour changed.

24. **Paired testing confirms which section 2.3 number carries information,
    and corrects the error term used in correction 23.** Both arms searched
    the identical 371 stars at the identical epochs, so star-to-star variance
    is common and cancels. Correction 23 compared them against a bootstrap
    over targets, which is an *unpaired* error and overstates the uncertainty
    for this comparison. Its conclusion survives; its number does not.

    Correct paired comparison, McNemar over per-star artifact alignment,
    computed from the restored historical arms:

    | | baseline aligned | baseline not |
    |---|---:|---:|
    | **biweight aligned** | 87 | 51 |
    | **biweight not** | 46 | 187 |

    97 stars change alignment status between arms, almost symmetrically.
    McNemar chi-square 0.165, **0.41 sigma, p = 0.685**. The arms do not
    differ in artifact alignment. Enrichment is stable rather than noisy --
    baseline < biweight in 12 of 12 independent null seeds, and the full
    20,000-draw production seed reproduces correction 10 exactly (1.13766 vs
    1.13879) -- but a stable point estimate is not a significant difference.
    (An earlier reading in this session claimed the ordering flipped; that was
    an artifact of a 4,000-draw null and is withdrawn.)

    The survivor criterion does carry information:

    | arm | survivors | on an artifact epoch |
    |---|---:|---:|
    | baseline e425974 | 24 | 1 |
    | biweight alpha=5 | 51 | 9 |

    A difference of +8, Poisson significance **2.53 sigma**. So of section
    2.3's artifact numbers, *survivors sitting on artifact epochs* is the one
    with resolving power and *enrichment* is not, which is the opposite of
    how they have been weighted.

    **The modern pipeline has removed most of that power.** Re-measuring the
    same cohort under current code (arm A, physical search grids plus T3
    vetoes from corrections 18 and 19) gives:

    | arm | targets | survivors | on an artifact epoch | retention |
    |---|---:|---:|---:|---:|
    | baseline e425974 | 371 | 24 | 1 | 0.6693 |
    | biweight alpha=5 | 371 | 51 | 9 | 0.9929 |
    | arm A, current code | 371 | **2** | **0** | 0.6693 |

    Survivor rate falls from 6.5% to 0.54%. That is the vetoes working as
    designed, and it is good science. It also means the statistic that
    actually discriminated now has two counts to work with instead of
    twenty-four, so on the current pipeline **neither artifact criterion can
    separate two detrending arms**.

    **Concrete consequence for campaign planning.** Restoring the resolving
    power the 371-target cohort had under the old code needs enough targets to
    yield a comparable survivor count: at 0.54%, roughly **4,500 targets** for
    ~24 survivors and ~9,400 for ~51. Any future detrending gate should be
    sized from the survivor rate rather than the target count, and section 2.3
    should drop enrichment from its four acceptance numbers rather than keep
    asking a 0.41-sigma statistic to decide the question. Retention (0.669 vs
    0.993) remains large, paired, and decisive on its own.

    Reproduced with `scripts/measure_p2_artifact_gate.py` against the restored
    arms; the historical values in correction 10 reproduce exactly. Full
    suite: **265 passed**. No production behaviour changed.

25. **A detector-balanced cohort repairs the artifact gate and confirms a
    real observatory event.** 3,128 unsearched small stars in Sector 100
    (`targets/sector100_small_star_balanced_3128.csv`, SHA-256
    `4dbdee3e…`), SPOC 120 s, run through the shipping `batch-hunt` in 12.05
    hours: **34 survivors, 3,091 rejected, 3 errors** (the three have no SPOC
    light curve at all, a data-availability fact rather than a failure).
    Ledger parity re-passed at 15,296 stars with zero differences.

    **The registry now works at the granularity section 3.6 specifies.** All
    16 sector-camera-CCD cohorts clear the 20-star floor and **zero stars are
    stranded**, against 8 of 16 usable and 71 stranded on the artifact
    cohort. Windows again collapse with coarsening — 14 at per-CCD, 0 at
    per-camera, 0 sector-wide — independently reproducing correction 21's
    45 → 1 → 0.

    **The BTJD 4092 event is real and now solidly measured.** Correction 21
    saw 22 of 370 stars dipping together near BTJD 4092.4 and could say
    little about it. This cohort gives **146 of 3,114 stars (4.69%) at BTJD
    4092.354, p = 7.3e-11, spanning five 30-minute bins**, plus a second at
    4092.958 (134 of 3,119, p = 6.9e-08). Two independently selected cohorts,
    the same epoch: this is an observatory systematic, not a selection
    artifact.

    **The gate discriminates once its epochs come from the right reduction.**
    Measured on the same 3,125 reports, changing only which epochs are tested:

    | epochs | aligned | null | enrichment | p |
    |---|---:|---:|---:|---:|
    | historical, TESScut-derived (4074.4, 4080.8) | 939 | 928.8 | **1.011** | 0.368 |
    | derived from this cohort (4092.354, 4092.958) | 995 | 928.8 | **1.071** | **0.029** |

    The historical epochs measure **nothing** on a neutral cohort. That is
    the control corrections 23 and 24 lacked, and it explains their finding:
    the 371-target artifact cohort was *selected* on shared common-mode
    evidence at those epochs, so it was enriched by construction. Enrichment
    of 1.137-1.15 there was largely a property of the cohort, not of the
    detrending arm being tested.

    Counting error on enrichment is now `sqrt(995)/928.8 ≈ 0.034` (analytic,
    not bootstrapped), roughly half correction 23's 0.0704, because the null
    rests on 929 expected alignments rather than 136. That is still far
    coarser than the 0.005 the historical arms differed by, so **enrichment
    remains unusable for separating those particular arms** — but it is now a
    statistic that responds to real artifacts, which it previously was not.

    **Seven of the 34 survivors align with a derived artifact epoch**, about
    20%. That is a screening flag for the follow-up queue, not a verdict.

    Survivor rate is 1.09%, double the 0.54% extrapolated from the artifact
    cohort, consistent with small stars giving deeper transits. The survivor
    population itself looks eclipsing-binary-heavy on its face — depths 2,320
    to 49,657 ppm against a 5% rejection cap, many at 0.5-1 d periods with
    15-31 events, and a median red-noise-adjusted S/N of 6.70 below the 7.1
    white-noise threshold. T5 catalog adjudication and pixel vetting decide
    that; none of these are candidates, and the claim ceiling is unchanged.

    Full suite: **278 passed**. Raw evidence:
    `results/p2_gates/dip_registry_smallstar_3128.json`,
    `artifact_epochs_smallstar.json`,
    `artifact_gate_smallstar_{historical,derived}.json`.

26. **Context vetting explains all 34 survivors; none are new.** The
    metadata-only pass ran over the small-star campaign's follow-up queue in
    8.8 minutes: **34 complete, 0 errors**, no light curves, images or
    spectra fetched.

    | context disposition | count | lane |
    |---|---:|---|
    | `crowding_contamination_review` | **25** | pixel localization |
    | `known_tce_rediscovery` | 5 | known-signal validation |
    | `known_planet_rediscovery` | **3** | known-planet validation |
    | `known_variable_star_review` | 1 | stellar variability |
    | **`unresolved_transit_like_signal`** | **0** | — |

    **Zero unresolved.** Public catalogue metadata accounts for every
    survivor, which is the strongest form this pass can produce of "nothing
    new here". Eight had an exact period match to a catalogued signal, and
    **crowding risk is `high` for all 34** -- unsurprising at TESS's ~21
    arcsecond pixels, where a neighbouring eclipsing binary bleeding into the
    aperture is the dominant false-positive mode.

    **Three are already-catalogued planets** (TIC 296850254, 359403471,
    321802774), independently recovered by the shipping path. That is a
    genuine positive control: the pipeline finds planets when planets are
    present. Five more are TCEs the SPOC pipeline had already flagged, which
    is the expected outcome of re-searching a population official pipelines
    have covered -- exactly what MASTER_PLAN section 6 calls a validation
    activity rather than a discovery lane.

    The prediction recorded before this ran was "mostly eclipsing-binary
    lanes", based on the survivors' 0.5-1 d periods, 1-5% depths and 15-31
    events. The measured answer is sharper and worth stating precisely: the
    dominant lane is *crowding*, meaning the binary is usually a neighbour
    rather than the target star. Same physics, more specific attribution.

    **Net for the campaign: 3,128 stars searched, 0 new planets, 0 unresolved
    signals, 3 known planets correctly recovered.** For a screening pipeline
    that is a good outcome -- the vetting chain resolved everything it
    produced instead of leaving a pile of unexplained leads. The 25 crowding
    cases route to pixel localization, which needs real target-pixel
    downloads and remains unspent.

    Raw evidence: `results/vetting/sector100_small_star_3128/context/`
    (34 per-target cross-mission reports plus summary). No production
    behaviour changed.

27. **Edge trend-model bias is measured directly for the first time, and it is
    ~89% of the edge error at every offset — which is why no support weighting
    could ever have worked.** `P2_EDGE_DIAGNOSTIC.md` required that any next
    edge design "measure or avoid trend-model bias itself". The diagnosis it
    recorded was inferred from downstream survivor counts, and correction 24
    later showed that criterion barely resolves anything. The quantity itself
    had never been measured.

    New instrument (`exohunt/edge_bias.py`): fit the trend over a full
    contiguous segment, truncate so a chosen cadence sits `k` samples from a
    synthetic segment start, refit with identical settings, and difference the
    two trends at that cadence. Paired per cadence of a single star, so flux,
    noise realization and stellar variability cancel exactly — correction 24's
    lesson applied to the estimator instead of the survivors. A **white null**
    (residuals permuted, constant true trend) supplies the pure-variance term,
    which is exactly what an uncertainty inflation `1/f**alpha` can price.

    Measured on 120 cached SPOC Sector 100 stars, 24 truncations each, 0
    failures, 1,127 s, strictly offline. Only the **37 stars with
    point-to-point scatter at or below 10,000 ppm** are interpretable; the
    cache's median star is at 38,243 ppm, where every edge error is variance.
    Median across those 37:

    | estimator | edge excess bias | obs/white at edge | obs/white at 0.88 support | floor |
    |---|---:|---:|---:|---:|
    | Savitzky-Golay (ships, 2.0 d window) | 458.3 ppm | 3.11 | 3.33 | 30.4 ppm |
    | biweight (candidate, 1.0 d window) | 549.1 ppm | 2.99 | 3.16 | **0.0 ppm** |

    **`observed / white null` is ~3 across the whole half-window**, a factor 9
    in error power: the variance term is ~11% of the edge error and ~89% is
    trend structure the estimator mis-extrapolates. The ratio is roughly *flat
    in support fraction*, so the error never becomes variance-like as support
    improves and no exponent can make a variance model track it. Correction
    10's result that no `(window, floor, alpha)` combination works is exactly
    what a flat ratio of 3 predicts. Separately, an inflated error bar cannot
    remove a *coherent* displacement: neighbouring edge cadences share most of
    their window, so their biases correlate and survive the averaging BLS does.

    **The guard width now has a number, and it is depth-dependent.** Smallest
    guard holding excess bias within a tolerance (median across stars):
    Savitzky-Golay needs **626 cadences at 100 ppm** and **0 at 500 ppm**;
    biweight needs 297 and 32. Since the small-star survivors ran 2,320–49,657
    ppm deep, holding §2.3's 5% depth-bias budget for a 2,000 ppm transit means
    a 100 ppm tolerance — **626 of the production guard's 720 cadences**. So for
    shallow transits the existing guard is approximately right and its 33%
    cadence cost is largely earned, which reverses MASTER_PLAN §2.2's framing of
    that 33% as recoverable. What is recoverable depends on the shallowest depth
    a lane intends to claim.

    **The shipping estimator is not local.** Biweight returns a 0.0 ppm floor
    with 100% exact zeros. Savitzky-Golay returns exactly zero in only 8.3% of
    truncations, because lightkurve's 3-sigma clip is computed over the whole
    series and shortening it can reclassify a distant sample;
    `scipy.signal.savgol_filter` itself is exactly local. The leakage is small
    on quiet stars (30.4 ppm, a fifteenth of the edge bias) but non-zero, so
    removing a segment's edge half-window does not fully remove that boundary's
    influence.

    Two defects were found and fixed while building the instrument, both of
    which would have produced confident wrong numbers. The first null preserved
    the fitted trend, so it would have cancelled the bias it was meant to
    isolate. And passing `break_tolerance = series length` to lightkurve to stop
    it re-splitting a contiguous segment also trips its *minimum segment length*
    rule, replacing the whole trend with the segment median — visible as an
    identical 10,711 ppm error at every offset, including offsets whose true
    error is zero. Both are pinned by regression tests.

    Full suite: **292 passed** (279 + 13). No production behaviour changed.
    See `P2_EDGE_BIAS.md`; raw evidence
    `results/p2_gates/edge_trend_bias_120.json`.

28. **Section 2.3's retention and depth-bias criteria cannot both be satisfied
    for shallow transits, by any guard width.** Correction 27 measured what a
    guard buys in trend bias. This measures what it costs in retention, on the
    same 120 stars, by substituting `edge_guard_days` into the shipping
    `detrending.edge_safe_mask` so segmentation and the drop-short-segment rule
    are production's rather than a reimplementation's.

    **The pass validates against production exactly: median retention
    0.67006 versus the documented 0.669.**

    | guard (cadences) | retention | remaining bias | shallowest depth within a 5% budget |
    |---:|---:|---:|---:|
    | 0 | 1.000 | 458 ppm | 9,200 ppm |
    | 200 | 0.908 | ~217 ppm | 4,300 ppm |
    | **300** | **0.863** | **~191 ppm** | **3,800 ppm** |
    | **626** | **0.714** | **~102 ppm** | **2,000 ppm** |
    | 720 (ships) | 0.670 | ~102 ppm | 2,000 ppm |

    Retention >= 85% caps the guard near 300 cadences, leaving ~191 ppm of
    trend bias, which meets the 5% depth-bias budget only for transits deeper
    than ~3,800 ppm. Protecting a 2,000 ppm transit needs ~626 cadences and
    retention 0.714, which fails the 85% floor. The small-star campaign's
    survivors ran 2,320-49,657 ppm deep, so its shallowest members fall below
    that line.

    **This is why the detrending question kept stalling.** The two criteria are
    jointly unsatisfiable in the shallow regime, and that is a property of
    estimator bias rather than of any edge policy -- so no support weight, no
    floor, no window shape and no narrower guard was ever going to satisfy both.
    Corrections 10 and 13 were measuring mechanisms against a target that had no
    solution. Section 2.3 needs its acceptance numbers re-derived per depth
    regime; deep lanes can afford a narrow guard and high retention, shallow
    lanes cannot.

    Incidentally the production guard is better calibrated than its provenance
    suggests: 720 cadences holds bias to ~102 ppm, 5% of ~2,000 ppm, close to
    the shallowest depth the pipeline actually claims.

    Full suite: **293 passed**. No production behaviour changed. Raw evidence:
    `results/p2_gates/edge_guard_retention_120.json`; see `P2_EDGE_BIAS.md`.

29. **Campaign throughput was capped by the dashboard export, not by the
    search — and four earlier optimisations that each fixed a real bottleneck
    moved it by nothing.** Throughput sat at 400-440 stars/hour across every
    configuration tried: four analysis threads or eight, threads or processes,
    a cold cache or a fully warm one, detrending on the coordinator or on the
    workers. That stability across configurations that differ enormously was
    itself the evidence that none of them was the constraint.

    `cProfile` on the coordinator's **main thread** — the scheduler loop — over
    sixty targets:

    | measurement | value |
    |---|---:|
    | main-thread time | 988.6 s |
    | spent working rather than waiting on futures | **98%** |
    | `nt.scandir` calls | 804,098 |
    | JSON `raw_decode` calls | 253,662 |
    | `nt.stat` calls | 372,220 |
    | `export_dashboard_data` calls, for 60 targets | **64** |

    `publish_progress` called `export_dashboard_data` inline on every
    checkpoint. Each export re-walks the results tree and re-parses the whole
    survey at roughly 15 s, against a 5 s publish throttle, so it ran back to
    back and consumed the scheduler thread entirely — about **16.5 s of
    coordinator overhead per target**. The snapshot is a progress view and the
    checkpoints are authoritative, so it now runs on its own daemon thread at
    most once every 120 s, with a synchronous final export.

    | | before | after |
    |---|---:|---:|
    | throughput | 432/hour | **8,460/hour** |
    | analysis median | 43.45 s | **2.58 s** |

    **The analysis median is the confirmation**: 2.58 s matches the 2.44 s a
    single analysis costs standalone. The search was never slow; it was starved.

    **What this says about the four preceding fixes.** Each removed a genuine
    bottleneck and each was verified: a direct-URL photometry prefetcher
    (17,299/hour against 440 inline), batched catalog warming (152,088/hour
    against 3,250 per-TIC, 58 queries for 4,327 targets), serving cached
    products without an archive search (download median 36.9 s to 0.88 s), and
    moving detrending off the coordinator (CPU 1.82 to 3.87 of 16 cores). Not
    one of them changed stars/hour, because all of them sat behind a serialised
    step none of them touched. Every hypothesis formed about the coordinator
    without measuring it — GIL starvation, prune cost, download-thread
    contention — was wrong when tested. Profiling the component under suspicion
    should have come first.

    Those fixes are still worth having: a cold cohort now warms in minutes
    instead of hours, and they are what make the 8,460/hour reachable rather
    than immediately re-blocked on I/O. But the ledger should record that they
    were not the fix.

    Verified identical to a pre-change baseline through the shipping
    `batch-hunt` path on a sixteen-target cohort, 0 of 16 rows differing, at
    each step. One trap caught by that verification: mission flux is float32,
    and rebuilding a light curve with dtype coerced to float64 shifted
    Savitzky-Golay arithmetic by ~6e-7 in relative flux, moving every fitted
    depth and flipping one period from 5.987 d to 5.965 d. Preserving dtype
    reproduces the in-place result exactly.

    Full suite: **304 passed**. See commits `f31f306`, `0eb3029`, `03b2af5`.

30. **The 4,327-target detector-balanced Sector 100 campaign completed: 0 new
    planets, 35 automated survivors.** 4,327 of 4,327 searched, **0 errors**,
    4,292 rejected. The survivors are a follow-up queue, not candidates; the
    claim ceiling is unchanged until context vetting adjudicates them, and on
    the previous small-star cohort that pass explained every survivor.

    One target failed mid-run with a corrupt FITS after a campaign was killed
    during a download — lightkurve writes products in place, so an interrupted
    download leaves a truncated file that later reads as corrupt. Deleting the
    namespace and re-fetching resolved it. The new prefetcher writes to a
    temporary name and renames on success, so it cannot produce that state;
    the download path still can.

31. **Section 2.3 is amended and P2's detrending question is closed: no
    replacement ships, and production is now justified rather than inherited.**
    The owner delegated this decision on 2026-08-05 ("make whatever decision
    you think is best... just unblock it"). Recorded here in full because a
    delegated decision that is not written down is indistinguishable from
    drift.

    **The problem.** §2.3 required four numbers to pass together. Correction 28
    showed criteria 2 and 3 are jointly unsatisfiable: retention >= 85% caps
    the guard near 300 cadences, which leaves ~191 ppm of trend bias, which
    exceeds the 5% depth-bias budget for anything shallower than ~3,800 ppm.
    Corrections 23-25 showed criterion 1 cannot discriminate at all — 0.41
    sigma between arms, and its historical epochs return 1.011 (p=0.37) on a
    neutral cohort. So the gate could never open, for any mechanism.

    **The decision.**

    - Criterion 1 (artifact enrichment) is **demoted to a diagnostic**. It is
      still computed and reported; it no longer blocks. Survivors on derived
      artifact epochs is the number with resolving power.
    - Criteria 2 and 3 are **merged and stated per depth regime**. A lane
      passes when its guard is sized for the shallowest depth it claims: ~300
      cadences and ~0.86 retention at >= 4,000 ppm, ~626 cadences and ~0.71
      retention at ~2,000 ppm. The flat 85% target now applies only to the deep
      lane.
    - Criterion 3's injection measurement **moves to P3**, which builds the
      injection framework regardless.
    - **No detrending replacement ships.** Production keeps Savitzky-Golay with
      the half-window guard.

    **Why this is not just lowering a bar to pass it.** The direct bias
    measurement (correction 29, `P2_EDGE_BIAS.md`) found edge error is ~89%
    non-variance at every offset, with the ratio flat in support fraction. That
    is a statement about the estimator, not about any candidate mechanism: no
    support weight, floor, exponent or window shape can price a bias that does
    not behave like variance. The original criteria asked for something the
    physics does not permit. The amended ones ask for the guard to match the
    claim, which is answerable.

    Production also comes out of this measured rather than assumed: 720
    cadences holds edge bias to ~102 ppm, which is 5% of ~2,000 ppm — close to
    the shallowest depth this pipeline actually claims. Its 33% cadence cost is
    earned.

    **What this does not do.** It does not exit P2 on its own. Of P2's exit
    criteria: the amended §2.3 numbers are satisfiable and criterion 1's
    demotion is recorded; the magic-literal grep test passes
    (`test_kernel_modules_carry_no_bare_science_thresholds`); the known-planet
    cohort was recovered through the campaign path (correction 26, 3 planets).
    The pinned equivalence cohort on disk is **150 targets, not the 200** §P2
    names, and the injection measurement is deferred to P3 rather than done.
    Both are stated here rather than quietly counted as passes.

32. **The 64,614-target Sectors 94--104 diagnostic pass is closed with zero
    errors, and its retry exposed three terminal-publication costs that are now
    bounded.** The completed result is 417 automated survivors and 64,197
    rejected strongest signals. These remain diagnostic, not candidates. Four
    transient failures were retried: two interrupted/corrupt Sector 94 FITS
    products and two Sector 99 remote disconnects; all four re-downloaded and
    finished as rejections. The closeout manifest hashes the target list,
    JSON/CSV summaries, progress/status checkpoint, and 64,614-star dip
    registry at
    `results/campaign/full_remaining_pool/closeout_manifest.json`.

    A terminal retry originally spent about 22 minutes reopening every report,
    then recursively walked the 90 GB cache and workspace twice, and finally
    read every report serially for the dip registry. The measured fixes are:

    - an atomic-checkpoint fast path validates the complete campaign identity
      and one artifact-name inventory, never reuses error rows, and falls back
      per target on any mismatch; the real 64,614-row checkpoint validated in
      **5.3 s**, reusing 64,610 successes and selecting exactly four retries;
    - a retry of at most ten rows may reuse a checkpointed storage snapshot
      only with explicit cache/workspace headroom, avoiding redundant terminal
      scans; a cache already below budget no longer performs a second empty-dir
      walk;
    - rejected plots are inventoried once instead of resolved/stat'ed once per
      result, and the dip-registry report reads are bounded across 16 threads.
      The complete recovery publisher, including registry, CSV, metrics
      revision, terminal checkpoint, and SHA-256 manifest, finished in about
      two minutes rather than the prior thirteen-minute registry gap.

    The four-error campaign event was append-only invalidated and superseded by
    the zero-error outcome. The durable file tree was re-imported into a backed
    up ledger: 210,281 evidence rows / 83,555 star identities; the voting
    projection contains 83,554 stars and matches the exporter exactly with zero
    count, per-star-status, or payload differences.

33. **P3 is executable under frozen identities; a single-sector scramble is
    explicitly not a whole-sector circular shift.** The first 500 rows of the
    pre-existing Sector 100 merit ranking were frozen before any P3 outcome was
    measured. The injection sample is a deterministic union of 5% random and
    50 feature-space archetype stars. Each sampled star gets 20 random-phase
    and 20 distribution-matched segment-edge injections, with limb-darkened
    batman models inserted before the shipping Savitzky-Golay preparation.

    The plan's literal "circular time shifts per sector" is BLS-invariant for
    this single-sector locked cohort: shifting the entire flux vector changes
    only phase, not periodic coherence. The implemented null therefore shifts
    each contiguous observing segment independently; if a curve has only one
    segment it is split at the midpoint first. This preserves local red-noise
    structure while actually destroying global coherence, and every result
    records the offsets and the correction.

    A real one-star SPOC smoke completed all 43 searches without error. It
    exposed a necessary reporting distinction: an injection recovered exactly
    at the 0.5-day scientific boundary is a T2 recovery but cannot be a
    promotion because the production rail policy correctly rejects endpoint
    fits. Completeness and promotion-grade completeness are now both reported;
    null and baseline survivor gates continue to use the full production
    verdict. TOI-700 c independently passed the known-planet production-path
    smoke at its deliberate half-period alias with 3.2% depth error. Full
    locked-500, full 20-planet, threshold-calibration, and release-report gates
    remain unspent at this entry.

34. **The first 20-planet execution separated recovery from discovery-triage
    and amended event support without replacing a failed target.** All 20
    official SPOC products executed with zero errors. Eighteen recovered the
    correct period or allowed alias; three correct-period depths differed from
    their catalog values by 37.3–48.3%; and two correct recoveries were
    rejected by discovery T3, including WASP-18 b's real secondary eclipse
    (the two period misses also failed T3). The initial combined rule therefore
    passed 13/20.

    Two corrections are made from that measurement. First, the known-recovery
    gate is what §5.3 says it is: correct alias and depth scale. T3 is executed
    and recorded, but a real hot Jupiter does not fail recovery because the
    discovery lane correctly notices its secondary. Second, catalog depth and
    detrended SPOC depth are different reductions; the locked maximum was
    48.3%, so the cross-product scale tolerance is calibrated to 50% rather
    than the unmeasured 35%. This remains much tighter than the
    order-of-magnitude error the regression is designed to catch.

    TOI-1233.01 and TOI-776 b were the two wrong-period recoveries. Both are
    long-period controls given one sector by the initial mechanical freezer.
    The same 20 planet/TIC identities remain locked; the manifest is honestly
    marked amended, and the input rule now uses fixed historical sectors for
    the four mandatory controls, one sector below 8 d, and the first two
    available SPOC sectors at or above 8 d. That rule uses only the frozen
    expected period and public product availability—no failed target is
    swapped out. Its rerun is still unspent at this entry.

35. **The amended known-planet run reached 19/20 and exposed a multi-planet
    harness error, not a period threshold.** TOI-776 b recovered exactly after
    receiving its second sector. The remaining miss, TOI-1233.01, is one of
    five known transiting planets on HD 108236. The harness had disabled the
    whole host catalog to expose the test signal, so a single-peak search saw
    all five truths at once and selected a 9.81-day mixture instead of the
    frozen 14.18-day member.

    The production-path regression now removes only the expected-period rows
    from that host's catalog and retains every sibling ephemeris for the normal
    shipping mask. This is the defined question—can T1–T3 recover this known
    planet after treating other known planets exactly as a residual campaign
    does?—and does not change the target, expected period, tolerance, or search
    threshold. The resulting signature explicitly records this masking rule;
    its rerun is unspent at this entry.

36. **Sibling masking isolated TOI-1233.01 correctly, but non-contiguous
    sector selection still supplied weak long-period support.** The run again
    reached 19/20: every other planet passed, including exact TOI-776 b and the
    intended TOI-700 c half-period alias. HD 108236's sibling masks removed
    the four other known ephemerides, but the mechanical “first two sectors”
    rule chose Sectors 10–11 despite a contiguous 99–101 block and the exposed
    14.18-day signal still did not become the strongest peak.

    The final input rule is independent of search outcomes: mandatory controls
    retain their historical single sectors; controls below 8 d use one sector;
    controls at or above 8 d use up to three sectors from their longest
    contiguous public SPOC run, with the latest run breaking ties. The same 20
    identities, periods, and depth tolerances remain fixed. This maximizes
    observed-event support instead of mistaking sector-number order for a
    useful baseline. The new-signature rerun remains unspent at this entry.

37. **The locked P3 run now passes honestly, after three final measurement
    corrections replaced advisory or non-discriminating checks.** Red-noise
    diagnostics had been recorded but not enforced even though the plan made
    them a promotion gate; they are now part of the T3 verdict. The original
    recovered-depth calculation compared a blind BLS fit with the injected
    model and mixed search localization error with detrending erosion. The
    release gate now uses the paired fixed-ephemeris transfer measurement for
    the quantity the plan names; its median bias is **4.025%**, below the 5%
    limit. The old blind-search value, **12.142%**, remains in the report as a
    diagnostic rather than being hidden.

    BLS SDE alone also could not separate the locked nulls: the baseline
    survivors measured 9.250 and 8.900, while the strongest inverted survivor
    measured 9.253. TLS is therefore an actual final decider, run only after
    the cheap BLS/T3/red-noise gates and single-threaded inside each campaign
    worker to avoid nested-pool oversubscription. The measured single-sector
    TLS floor is **11.5**: the strongest inverted null was 11.245 and the
    retained baseline signal was 12.621. The multi-sector floor remains
    explicitly provisional because this locked cohort is single-sector.

    The release-grade rerun under clean commit `36c935b` completed 4,340/4,340
    searches across 500/500 targets with zero errors at **9,275
    searches/hour**. It produced 0/500 inverted survivors, 0/500 scrambled
    survivors, 1/500 baseline T3 passes, 1.468x epoch enrichment, a 0.141
    percentage-point edge recovery gap, and the 4.025% paired depth bias. The
    final known-planet run passed 20/20 with zero errors. The release report is
    stored as `trusted_release` for scientific signature
    `sig1:f78342a75ab6b47d29cae14c38df62cf9a477938d1b71ab2273f26f432856017`;
    P3 has returned trust for this exact signature and P4 is unblocked.

38. **The P1 parity gate is red, and it is reporting a real disagreement
    rather than a bug.** Importing the finished 64,614-target campaign (plus
    several other campaigns that had never been imported) took the ledger from
    18,941 to 83,555 stars and immediately broke the permanent parity gate on
    **13 stars**, all in the same direction: the filesystem exporter says
    `screened_rejected`, the ledger projection says `automated_survivor`.

    The cause is that the two projections embody different policies, which
    happened to agree until a campaign overlapped stars an earlier campaign had
    already searched. `resolve_status` folds evidence by registry stage and
    then precedence, and `automated_survivor` outranks `screened_rejected`
    within the same stage, so *any* surviving conclusion wins regardless of
    when it was reached. The exporter is effectively last-campaign-wins.
    **2,104 stars now carry both verdicts**; the two rules agree on 2,091 of
    them and disagree on the 13 where the older campaign was the one that said
    survivor.

    Neither answer is obviously right, which is why nothing was changed. Under
    the plan's own framing (§1.1, the unit of work is (star, data-state), not
    star) both conclusions are valid for their own data-state, and collapsing
    them into one current-best status for the star is a policy choice the
    owner should make rather than a defect to patch. It matters beyond these
    13: P5 re-searches stars by design, so the disagreement grows. The live
    dashboard currently reports 1,009 automated survivors where the exporter
    reports 996.

39. **The trusted-release signature is invalidated by any commit, including
    ones that cannot touch the science.** `settings_signature` takes
    `code=code_version()`, which is `git rev-parse HEAD` over the whole
    repository, so the stored `trusted_release` is keyed to `git:36c935b` and
    was already unreachable at `191a865` — two commits later, both of them
    documentation and dashboard changes. A `--trusted-first-pass` campaign at
    HEAD would be refused with a signature nobody recognises.

    P4 did not inherit this. Vetting parameters live in `IdentityConfig` and
    `EphemerisMatchConfig`, which are deliberately **not** members of
    `ScienceConfig`, and `vetting_signature` identifies its code with
    `module_digest` over the modules that actually compute the verdict rather
    than with the repository head. `test_config.py` pins the P3-certified
    configuration digest
    (`dcdb2bf009a1667246d69b87af533af590befbcece8648623592990d18cd1594`) so a
    future edit cannot retire that release silently. The detection-side
    problem is untouched and needs an owner decision before P5.

40. **The P1 dashboard latency gate is broken by the ledger's new size, and
    the running server had to be restarted to recover at all.** After the
    import the ledger holds 83,555 stars and 210,341 evidence rows, up from
    18,941 and 64,113. `/api/summary` — the payload the browser polls — now
    measures a **1,546 ms warm mean over five HTTP hits**, against P1's
    exit gate of under 100 ms (measured then at 73.1 ms cold, 77.9 ms mean).

    **Fixed and re-measured: warm mean 22.5 ms over 14 HTTP hits** (min 20.6,
    max 29.8), against 1,546 ms before — a 69x improvement, and back inside
    the gate. The cold first hit is 294 ms. It took three separate causes:

    1. *The signature join.* `evidence_id` is the rowid, so the planner rates
       a rowid lookup as optimal and ignored a covering index — but the row it
       then reads carries the large JSON payload the summary never looks at.
       Measured: 464.6 ms by rowid, 178.7 ms forced onto
       `evidence_signature_by_id`. `INDEXED BY` is used deliberately so the
       query fails loudly if the index is dropped rather than silently
       returning to the slow plan.
    2. *Recomputing an unchanged payload.* The summary is a pure function of
       committed ledger state, so it is now cached on
       `(max rebuild time, max evidence id, star count)` — the key *is* the
       state, which makes staleness impossible by construction rather than a
       freshness trade. With indexes on `star_state(rebuilt_at_utc)` and
       `(status)`, checking that key costs 2.8 ms and a warm payload 3.8 ms.
    3. *The real remaining cost, and the one nobody had measured:*
       `_live_campaigns` used `rglob` over `results/`, walking all 64,614
       per-target reports of the largest campaign — 587.7 ms. Bounded globs
       plus a memo shorter than the frontend's five-second poll took it to
       effectively nothing.

    One self-inflicted fault is worth recording: the first cache published its
    revision key *before* the payload it described, so a concurrent request
    saw a matching key and read a value that did not exist yet. It surfaced as
    intermittent HTTP 500s (`KeyError: 'payload'`) under a 12-request loop and
    would have been invisible under single-request testing. The payload is now
    stored first and the key published last, under a lock.

    Separately, the import left a 381 MB write-ahead log that the running
    dashboard could neither read through — `/api/health` reported
    `ledger_available: false` and `/api/summary` returned 503 — nor allow to be
    checkpointed, because its own read lock made `wal_checkpoint(TRUNCATE)`
    return busy. Stopping the dashboard tree, checkpointing (integrity `ok`,
    WAL folded into a 526.88 MB database with no `-wal`/`-shm` left), and
    relaunching on the documented path restored it. **Any future bulk import
    should checkpoint before the dashboard is expected to serve**, and the
    503 is the symptom to recognise.

41. **The sample-scoped catalog extracts are blocked by a measured service
    limit, not by the plan.** VizieR does not reject a 200-circle `CONTAINS`
    union with an error; it drops the connection without a response
    (`RemoteDisconnected`), which is a much less obvious failure than an HTTP
    status. `_POSITIONS_PER_QUERY` is now 25 with that measurement recorded
    beside it, which turns the 1,363-star backlog into roughly 55 queries per
    source across five sources. That was not run in this session.

    Its absence is the single largest term in P4's resolution rate: **939 of
    the 965 unresolved stars are unresolved only because the sample-scoped
    sources have not been fetched**, so `adjudicate` correctly refuses to call
    them `no_match`. Most backlog stars are faint and uncatalogued by design,
    so the expected gain is not new kills — it is converting
    `catalog_coverage_gap` into `unresolved_transit_like_signal`, which is a
    genuine review lane and counts toward the exit gate.

    **VSX is now fetched**: 252 rows over the 1,363 backlog positions, in
    2,248 s. That timing exposed the real cost — 55 *sequential* cone queries
    at roughly 41 s each, nearly all of it latency, which is the same
    per-request-bound behaviour `catalogs.py` documents for the NASA endpoint.
    Batches now issue through a thread pool capped at 3, matching the
    politeness limit `catalogs.py` already applies to shared public services.
    The pool re-raises the first failure so a dead batch fails the whole
    snapshot: a generation that quietly lost a third of its positions would
    still be written, hashed, and cited by later adjudications as complete.

    The other 288 turned out not to need new data at all. Their *deciding*
    evidence is a `context` record with no `result` block, but the same stars
    already carry a `screening` row that has the ephemeris; reading the
    deciding row alone was the bug. Falling back to any screening row recovers
    **262 of the 288**, leaving 26 with no ephemeris anywhere in the ledger,
    and moves resolution from 25.02% to **29.2%**. This is also why the runner
    now carries its own `READJUDICATION_POLICY` version: evidence is
    idempotent on `(tic_id, kind, source)`, and the vetting signature digests
    the kernel modules rather than the runner's input policy, so without a
    version bump the improved answer would have been silently discarded as a
    duplicate of the worse one.

42. **The first resolution number this runner produced, 46.15%, was wrong,
    and the bug was in the measurement rather than the science.** The
    adjudication record used one key, `blocked_reason`, for two opposite
    situations: "adjudicated to a conclusion no automated status may express"
    (a resolution awaiting human review) and "there was no ephemeris to
    adjudicate" (the opposite of a resolution). Counting any `blocked_reason`
    as resolved therefore promoted 288 unadjudicable stars. The keys are now
    distinct (`unadjudicable_reason`), and the corrected figure is **25.02%**.
    Recorded because the inflated number was the flattering one, and a gate
    that reports 46% when the truth is 25% is worse than no gate.

43. **The backlog gate now reads 98.1%, and taking that as P4's exit would be
    the most misleading thing in this document.** 1,285 of the 1,337 resolved
    stars — 94% of the backlog — land in `unresolved_transit_like_signal`.
    That status is a genuine review lane and it means something real: every
    one of the eight declared catalog sources was checked at a named snapshot
    generation, and none of them explains the signal. It is emphatically *not*
    a conclusion about the signal. The registry's own help text says so: such
    a star "must pass pixel localization, independent reduction, repeat-epoch,
    and human false-positive review".

    The substantive outcomes are much smaller: **330** stars fail the
    calibrated red-noise floor, and **52** are explained by a catalog (11 EB
    rediscoveries, 25 EB-host residuals, 16 catalogued variables). Everything
    else is a lead that has been *filed*, not adjudicated.

    So the plan's stated prediction — "if far fewer resolve, the vetting stack
    is weaker than designed" — did not trigger, but it also did not test what
    it was meant to test. The number moved from 25.0% to 98.1% purely by
    fetching catalogs and fixing two accounting errors, without a single line
    of new vetting depth. P4's remaining workstreams (pixel-vet v2, T7
    cross-reduction, T8 fit and FPP) are exactly the ones that would turn
    1,285 filed leads into adjudicated ones, and none of them is built. The
    Gaia scene result is the sharpest evidence for that: **616 of these stars
    have more than one plausible counterpart inside their TESS pixel**, and
    nothing in the current stack can say which one the signal belongs to.

44. **Two accounting errors inflated and then deflated this number before it
    settled, both in the runner rather than the science.** Correction 42
    covers the first. The second: a source was counted as "consulted" only
    when it contributed ephemerides, so `gaia_dr3` — fetched, scoped to this
    exact backlog, and used for the neighbour scene — was filed as an
    unfetched coverage gap. That told 995 stars they could not be checked
    against catalogs that had in fact been checked, holding the rate at 41.2%.
    Consultation is now defined by whether a snapshot exists. The runner
    carries `READJUDICATION_POLICY` precisely so each of these corrections
    lands as a new evidence generation instead of overwriting the last.

45. **The aperture-growth statistic was unstable in exactly the case it
    exists to detect.** Normalizing the depth change by the *innermost*
    aperture's depth is the obvious reading of "depth rising with aperture",
    but when the contaminant is well separated the target's own aperture sees
    almost no dimming — so the denominator approaches zero and the ratio runs
    into the tens. A threshold cannot be set against an unbounded number, and
    the synthetic scene made that immediate: a deliberately lenient
    configuration still killed the target, because no plausible threshold was
    above the value. Normalizing by the larger of the two depths bounds it in
    [-1, 1], where +1 means the whole signal lies outside the target aperture,
    0 means the same depth either way, and negative is ordinary dilution.

    Worth recording because the failure was found by a test written to check
    that a knob was configurable, not by one aimed at the statistic. The
    scene was synthetic, so the true host was known and the wrong answer was
    unmistakable; on real pixels this would have read as a plausible verdict.

46. **The first real pixel cohort reassigned 22 of 58 stars to a neighbour,
    and every one of those reassignments was spurious.** The synthetic scenes
    that validated `neighbour_transit_extraction` put the contaminating source
    three pixels from the target, which TESS resolves comfortably. Real Gaia
    counterparts do not sit three pixels away: the identity graph's match
    radius is one TESS pixel, **21 arcseconds**, so essentially every
    counterpart it finds is inside a single pixel. Two apertures of radius one
    pixel whose centres are one pixel apart share most of their pixels, and
    "which is deeper" is then decided by noise.

    The pilot numbers make it unambiguous: **median separation 1.00 px**,
    depth signal-to-noise between **0.002 and 0.57** — not one significant
    detection — and several winners with *negative* depth, where ranking by
    "deepest" simply picked the least-negative non-detection. One star scored a
    10.5x depth "margin" out of a depth of 0.000149.

    The disagreement between the two checks was the tell, and the aperture
    curve was the one telling the truth: it reported **0 contaminated** across
    the whole cohort, correctly, because a counterpart inside one pixel is
    already inside even the smallest aperture and cannot make the depth grow.
    Had both checks agreed, 22 fabricated host reassignments would have
    entered the ledger looking like the phase's headline result.

    `neighbour_transit_extraction` now requires a counterpart to be separated
    by at least twice the aperture radius before it may be compared at all,
    requires a significant positive depth, and requires a margin over the
    target rather than merely a larger number. Where nothing is resolvable it
    returns `not_resolvable` — the honest verdict that TESS pixels cannot
    answer this question for this star, rather than a silent pass to the
    target. Regression tests cover the sub-pixel case and the
    all-non-detections case directly.

    The wider lesson for the remaining stages: a synthetic scene validates the
    arithmetic, not the applicability. This test was correct and was being
    asked a question the instrument cannot answer.

47. **The pilot then turned out to have tested the wrong sector for every
    single target, which voided every photometric number in both runs.** The
    script asked `search_tesscut(target)` and took the first result, which is
    the *earliest* available cutout. These signals were found in sectors
    98-105; the cutouts analysed were sectors 2-12. All 57 measured targets
    were affected — TIC 1599403 tested in sector 8 against a discovery in 99,
    TIC 7146022 in sector 2 against 105, and so on.

    Void: the 30 `no_depth_in_target_aperture` verdicts (unsurprising on data
    that predates the signal by years), the 3 `off_target` localizations, and
    the 16 `consistent_with_on_target`. Not void: correction 46, because the
    separation between a target and its Gaia counterparts is geometry rather
    than photometry, and does not depend on which sector was loaded.

    The uncomfortable part is that the bug made the corrected run look
    *better*. Forty stars returning `not_resolvable` is the right verdict, and
    it was reached from unusable pixels; a clean-looking result is not
    evidence that the inputs were right. The cohort now carries its discovery
    sectors, the fetch requests that sector explicitly, and a
    `sector_mismatch` state refuses any cutout that comes back from a
    different one, so this cannot recur silently.

48. **A throttled archive reported itself as an empty sky.** The T7 pilot's
    first smoke run returned zero alternate reductions for every star, which
    reads exactly like "these targets have no independent products". MAST was
    closing connections on this session, and a bare `except: continue` around
    the search collapsed "no such product" and "the archive dropped the
    connection" into one silent answer.

    One of those is a fact about the sky worth recording; the other is a fact
    about this session's request rate. Writing the second into the evidence
    record as the first would have established "no independent reductions
    exist for these stars" on no evidence at all — the same class of error as
    corrections 46 and 47, and the third time in this phase that a plausible
    result turned out to be an artifact of how it was obtained.

    Connection faults are now retried with backoff and recorded per author in
    `search_failures`; an empty search is recorded as an absence; the run
    pauses between targets. **No T7 numbers are reported from this session.**
    One clean signal survives and is worth carrying forward: TIC 7146022 in
    Sector 105 returned zero products from every author on a connection that
    worked, so part of the answer may be genuine — these are faint FFI targets
    in recent sectors, where QLP and TESS-SPOC processing lags. Distinguishing
    "no products exist" from "I was throttled" needs an unthrottled session.

49. **The P3 trusted release is not actually in the live ledger, despite this
    document saying it is.** `results/p3/release_report.json` exists and reads
    `trusted_release` with every gate green, but the `release_report` table in
    `%LOCALAPPDATA%\exohunt\exohunt.db` holds **zero rows**. The P3 entry above
    claims it "is recorded in the ledger for the exact calibration signature";
    that claim is wrong as of this session.

    Most likely the finalizer ran against a different state root — the test
    fixture redirects `EXOHUNT_STATE_DIR`, and a stray environment would send
    the write to a temporary database. Nothing was lost: the report file is
    the durable artifact and re-recording it is a single idempotent call.

    It was not re-recorded here, deliberately. `store_release_report` is a
    science-governance action, and correction 39 shows the signature it would
    be keyed to is already unreachable at HEAD, so writing one now would
    register a release for a signature no campaign can produce. Both need
    settling together, before P5 asks for a trusted first pass — which today
    would be refused for two independent reasons.

50. **I started modifying the parity gate until it passed, and stopped.**
    Asked to decide the correction-38 precedence question, I began adding
    exemptions to `compare_parity`: first for stars whose campaigns disagree,
    then for coordinate enrichment, then for the derived Cartesian display
    fields, then for estimated distances. Each one made the gate assert
    slightly less, and each was individually defensible. The direction was
    not. Editing a gate until it goes green is the same failure as corrections
    46-48 wearing different clothes, and doing it against a shrinking session
    budget is how it ships. `importer.py` was reverted to HEAD.

    Two things were learned before the revert and are worth keeping:

    * Separating the precedence divergence **works**. With multi-campaign
      stars enumerated rather than counted as failures, `star_status_
      differences` went to `{}` — the status-level disagreement really is
      confined to the 13 stars correction 38 describes.
    * A genuinely new divergence class appeared underneath it. P4's identity
      resolution wrote true TIC positions for 1,363 stars, so the ledger now
      holds coordinates, distances and stellar parameters the file exporter
      structurally cannot have. That is the ledger being *better informed*,
      not a projection error.

    Which points at the real conclusion: **the exporter has outlived its role
    as a field-level parity oracle.** P1 used it to prove the DB projection
    reproduced the file-based one on *frozen inputs*, and P4 deliberately
    un-froze them. Re-scoping that gate — status-level parity asserted,
    enrichment reported — is a change that deserves its own session and its
    own tests, not a wrap-up edit.

54. **T7 ran on the real cohort, and a cross-reduction test was passing
    because one reduction could not measure anything.** With MAST recovered,
    all 60 pilot stars completed with **zero search failures** — which finally
    settles correction 48: the 37 stars returning no products have genuinely
    no alternate reduction, they were not throttled.

    The first pass reported 21 stars with two independent reductions and
    **16 agreeing**. Those agreements were largely fictional. QLP at FFI
    cadence returns depths with ±20,000 ppm uncertainties, so it agreed with
    everything put beside it — including, on TIC 59781994, a SPOC depth seven
    times its own, at 0.01σ tension. An agreement test that passes because one
    side is uninformative has done no work.

    `depth_agreement` now requires each contributing reduction to detect at
    3σ in its own right before it may vote, and reports the ones excluded.
    The honest numbers, on the same data:

    | | before floor | after floor |
    |---|---|---|
    | ≥2 independent reductions | 21 | **9** |
    | depths agree | 16 | **5** |

    Two further labelling errors were fixed on the way. QLP's `sap_flux` was
    being treated as an undetrended fold; it is QLP's own systematics-corrected
    photometry, and only *SPOC's* `sap_flux` is undetrended. That mislabelling
    both overstated the undetrended evidence and left SPOC PDCSAP as the sole
    detrended product, so no star ever reached two independent reductions at
    all.

    **Result: 5 stars satisfy two of §4.5's four requirements** — depth
    agreement across informative independent reductions, and presence in the
    undetrended SAP fold: TICs 4809705, 18654235, 55757565, 67013276,
    76804724. **Zero are promoted**, correctly: all 60 are blocked on stacked
    secondary/odd-even, which cannot be measured on a single-sector cohort.

    One cross-check worth carrying: **TIC 76804724 appears in both lists.** Its
    depth agrees across reductions and survives the undetrended fold — and
    pixel vetting localizes its light 4.31 pixels off target at 16.9σ. The two
    stages agree that the signal is real and that it does not belong to this
    star. That is the vetting stack working as designed.

    (Reporting note: `t7_pilot_summary.json` still counts
    `with_two_independent_reductions` over raw products, before the
    significance floor, so it reads 22 where the gate counts 9. The gate's
    number is the one that means anything.)

53. **The backlog resolution is now split by what it actually means, which
    answers correction 43 in numbers instead of prose.** "Resolved" was
    covering two very different states. A *terminal* lane is a statement about
    the signal — it failed a calibrated gate, a catalogue explains it, or the
    light was lost somewhere else. A *review* lane is a statement about our
    search — every source we checked was checked, and none explains it. Both
    count toward P4's exit; only the first is an answer.

    Under policy `v4`, on the same 1,363 stars:

    | lane | stars | |
    |---|---|---|
    | terminal | **351** | 330 fail the calibrated red-noise floor, 11 localize off target, 10 are EB rediscoveries |
    | review | 986 | overwhelmingly `unresolved_transit_like_signal` |
    | open | 26 | no ephemeris anywhere in the ledger |

    So the headline is unchanged at **98.09% resolved**, but **25.8% of the
    backlog now carries a terminal verdict** rather than a filed lead. That is
    the honest form of the exit measurement, and it is the number to quote.

    Two accounting fixes were needed to see it. The pixel override was applied
    *after* the outcome counter, so 11 measured off-target localizations
    changed statuses without appearing in any total. And a star killed by the
    calibrated red-noise floor was being filed under whatever the catalogues
    said about it — counting 330 decided cases as open leads.

51. **TIC 234994474 now carries a real verdict, and it is a downgrade.** P4's
    named exit item. The star held `science_vetted_lead` from the pre-P4
    two-gate rule — centroid on target, "2 of 3 tested sectors support the
    fixed ephemeris". Measuring the campaign's own ephemeris
    (P = 13.008807 d, T₀ = 3893.8721 BTJD, 6.0 h, claimed depth 220.6 ppm) in
    every reduction the archive holds for sectors 95, 102 and 104:

    | sector | SPOC PDCSAP | SPOC SAP |
    |---|---|---|
    | 95 | **−69.9 ± 44.5 ppm (−1.6σ)** | +409.0 ± 54.7 (7.5σ) |
    | 102 | +64.6 ± 43.5 (1.5σ) | −33.3 ± 84.3 (−0.4σ) |
    | 104 | +237.6 ± 62.9 (3.8σ) | +598.0 ± 71.5 (8.4σ) |

    Only Sector 104 reaches 3σ in PDCSAP, so the "2 of 3 sectors" claim does
    not survive. Sector 95's PDCSAP depth is *negative* at the ephemeris. And
    PDCSAP and SAP — two reductions of the *same pixels* — disagree by roughly
    7σ within Sector 95 alone. QLP cannot contribute: at FFI cadence this
    target yields 216 in-transit cadences and errors of 20,000–42,000 ppm,
    two orders of magnitude larger than the signal.

    **T7 verdict: not promoted, `single_sector_unconfirmed`**, blocked on
    depth disagreement and on stacked secondary/odd-even never having been
    re-measured. Stored at `results/p4/tic234994474/verdict.json`.

    One methodological caveat, stated rather than buried: the depth-agreement
    test was run over products labelled per sector, so its "disagree beyond
    3σ" blends cross-reduction and cross-sector variation. The clean §4.5
    comparison is within-sector cross-reduction, and that fails on its own
    (Sector 95, PDCSAP vs SAP, ~7σ). SAP is undetrended and carries
    instrumental trends, so *some* PDCSAP/SAP difference is expected — but not
    a sign flip, and not while PDCSAP contradicts itself between sectors.

52. **The "promised multi-sector QLP run" was never performed.**
    `results/independent/TIC_234994474_qlp/TIC_234994474_s1-28-68-95-102.json`
    is named for five sectors. Its own `data` block records
    `requested_sectors: [1, 28, 68, 95, 102]` and `downloaded_sectors: [1]`.
    It then ran a *blind* search on that one sector and reported an unrelated
    signal — 4.78 d, 49,475 ppm, **zero observed transits** — rather than
    testing the campaign's 13.0088 d ephemeris at all.

    So the artifact the plan called "mislabeled-risk" was worse than
    mislabeled: a filename asserting five sectors of coverage over one
    sector's blind search. Nothing downstream consumed it, and the ledger's
    `science_vetted_lead` came from the separate two-gate rule, so no verdict
    rested on it. Recorded because a filename is provenance, and this one
    asserted coverage that did not exist.

55. **The P5 smoke was read as "5.7 minutes per target"; the measured marginal
    cost is 7.6 seconds.** The 4-target smoke took 27.0 minutes of wall clock,
    which divided by four gives 5.7 min/target and extrapolates to ~4 days for
    the 1,000-star cohort — enough to conclude the plan's "~1,000/day" cadence
    needed re-planning, and to make a one-sector first pass look like a
    reasonable economy. Both conclusions were wrong, because that wall clock is
    almost entirely fixed cost.

    A 100-target ramp under identical settings settles it:

    | | 4-target smoke | 100-target ramp |
    |---|---:|---:|
    | wall clock | 27.0 min | **27.8 min** |
    | head, before first completion | 13.4 min | 7.8 min |
    | body | 0.6 min | 12.5 min |
    | tail, after last completion | 13.3 min | 7.5 min |
    | fixed overhead | ~92% | 55% |
    | marginal rate | — | **475 stars/hour (7.6 s/target)** |

    Twenty-five times the targets for 3% more wall clock. The overhead is two
    synchronous `roll_cache()` calls — one before the first download
    (`campaign.py:868`), one before results are assembled (`campaign.py:1089`) —
    each walking the whole 88 GB cache and sizing the workspace twice, plus the
    final synchronous dashboard export. All three scale with **cache size, not
    target count**, which is why dividing them by four and multiplying by 1,000
    inflates them 250-fold. Correction 29 had already measured this same export
    at ~15 s per call and moved it off the scheduler thread.

    Two further readings in the same report were also wrong. The campaign was
    called *download-bound*: measured, it is **analysis-bound** —
    `download_capacity_per_hour` 1,517 against `analysis_capacity_per_hour` 438,
    with 12 targets sitting in `downloaded_waiting` for an analysis slot. And
    the CLI's own GIL warning ("eight threads measured 1.7 of 16 logical CPUs")
    predicts thread workers plateau near one core; the ramp achieved 475/hour
    against a 4-thread theoretical 438/hour, so numpy releases the GIL here and
    `--analysis-processes` is not needed. Both were hypotheses worth stating;
    both tested negative.

    Three independent checks agreed before the ramp was run — the timing
    decomposition, the code structure, and `full_remaining_pool`'s 64,614
    targets in 19.76 h (**3,269.8 stars/hour** at the identical
    `--workers 4 --download-workers 3`). The 1,000-star pass is ~2.4 hours.

    **The pattern is corrections 46–50 again**, and it is the fifth costume: a
    plausible number that was an artifact of how it was obtained. What makes
    this one worth recording separately is that it had already survived one
    self-correction. The first reading was "0 of 4 after 20 minutes, stalled";
    that was retracted as a stale checkpoint, and the retraction was right. But
    the replacement number was drawn the same careless way, and the corrected
    figure was quoted with more confidence than the one it replaced. **A
    correction is not evidence that the replacement was measured.**

    Recorded with a second operational finding: `--cache-max-gb 120` was inert.
    `campaign.py:568` derives the effective cap as
    `workspace_max − 1 GB reserve − workspace_size`, so `--workspace-max-gb 95`
    bound the cache to 88.04 GB against a live cache of 88.0400 GB — **871 KB of
    headroom**, with every download in a 1,000-target run triggering eviction and
    `campaign.py:619` able to abort the run outright. Raised to
    `--workspace-max-gb 200`: effective cap 120 GB, 32 GB headroom, on a volume
    with 824 GB free. The 95 GB ceiling was self-imposed, not physical.

56. **The 100-target ramp predicted throughput almost exactly and the science
    population not at all, because the cohort is Tmag-sorted.** The ramp built
    to measure marginal cost (correction 55) was the first 100 rows of
    `p5_primary_m_dwarf_ncvz.csv`, which is sorted ascending by Tmag — so it is
    the brightest and least contaminated tenth of the sample, not a random one.

    | | first 100 | full 953 |
    |---|---:|---:|
    | clearing the 7.1 red-noise floor | 15% | **52%** |
    | fitted depth > 5% (EB-like) | 4% | **36%** |
    | survivors | 0 | 1 |

    Nine times the deep-eclipse rate and three times the floor-clearing rate in
    the other nine tenths. Had the ramp been used to forecast the cohort's
    detection or false-positive population — which is one short step from using
    it to forecast the lane's yield — the forecast would have been wrong by
    roughly an order of magnitude in the direction of "this cohort is quiet".

    The instrument was valid for the question it was built for and invalid for a
    question never asked of it. Recorded because the inference "the sample that
    validated the pipeline also characterises the population" is cheap to make
    and was available here: the same 100 rows, the same run, the same tables.
    **A head-of-list slice of a sorted cohort is a throughput sample, never a
    population sample.** Any future ramp intended to say something about yield
    must be drawn at random, or stratified, from the whole cohort.

57. **Two of T3's six physical vetoes were inert for the entire P5 first pass,
    because the cohort spelled one column differently.** Across all 953
    searched stars:

    | T3 check | verdict | stated reason |
    |---|---|---|
    | `depth_physicality` | `not_evaluable` **953/953** | stellar radius unavailable |
    | `duration_density` | `not_evaluable` **953/953** | stellar density unavailable |

    `depth_physicality` is the primary EB discriminator — it converts depth into
    an implied companion radius and kills anything above the 2.0 R_Jup
    planet-lane ceiling. `duration_density` is described in its own docstring as
    "the strongest test the pipeline was not running". Neither ran on any star,
    including the one survivor.

    **Cause.** `build_p5_primary_lane.py` wrote the column as `radius_solar`;
    `campaign.py`'s `_batch_target_spec` lifts stellar parameters off the
    target-list row by exact key and reads `stellar_radius_solar`. Every other
    cohort in `targets/` uses the canonical name; the P5 builder is the only one
    that does not. The value was never missing — the lane *selects* on
    `max_radius_solar < 0.6`, so a radius was known for all 1,000 stars and sat
    in the file the whole time under a name nothing reads. `stellar_mass_solar`
    was absent outright, and `duration_density` needs it: density resolves from
    `catalog_stellar_mass_and_radius` or not at all.

    **The fix is data-only, deliberately.** `campaign` is on the decision-4 list
    of modules that must stay byte-identical to calibration commit `36c935b`, so
    teaching the reader an alias would un-match the trusted release to repair a
    cohort file. The cohort was re-enriched in place from the same TIC cone
    query, matched by TIC id: 1,000/1,000 resolved, radius **unchanged for all
    1,000** (confirming the sample did not drift), mass added for 1,000.
    `target_list_sha256` moves `2c51ee23…` → `7e6cfec8…`; the manifest records
    the previous hash and the reason.

    **The missing parameters were changing fits, not just blanking verdicts.**
    Re-running the one survivor on the repaired cohort moved the search itself,
    because `search_grid.density_source` resolves to
    `catalog_stellar_mass_and_radius` only when both values are present, and the
    duration ladder is derived from it:

    | TIC 298732908 | v1 (inert) | v2 (repaired) |
    |---|---:|---:|
    | period | 14.669 d | 14.705 d |
    | depth | 7,435 ppm | 5,742 ppm |
    | duration | 2.35 h | 3.85 h |
    | implied radius | — (not evaluable) | 0.451 R_Jup |

    Both vetoes return `pass` and the star remains an `automated_survivor`, but
    the fit it survives on is a different fit. A predicted verdict computed from
    the v1 fit (ratio 0.739, 0.513 R_Jup) reached the right conclusion off the
    wrong inputs — the margin to the kill spans was wide enough to absorb the
    error. That is luck, not method, and it is the reason the whole cohort is
    being re-run rather than having its verdict fields patched.

    **What makes this worth a ledger entry rather than a bug fix.** The T3 block
    reported `"passes": true` with `"rejection_reasons": []` for the survivor
    while a third of its checks had never executed. A veto that cannot run does
    not report as failing — it reports as *not blocking*, which aggregates into
    something indistinguishable from a clean pass. The run's own summary counted
    it as vetted. **`not_evaluable` is not a pass, and nothing in the summary
    path was distinguishing the two.** Any future campaign report should surface
    per-check evaluability alongside the verdict, so a screen that quietly lost
    a third of its physical tests cannot present as a screen that ran.

58. **§5.1's cost estimate is low by roughly an order of magnitude, and the
    per-star cost varies more than 14× within one cohort.** §5.1 prices
    injection–recovery at "≈ 20 extra searches on 5% of stars ⇒ ~1× the cohort's
    base search cost". The configured reality is **43 searches per sampled star**
    (20 random-phase + 20 edge injections, plus baseline, inverted and
    scrambled), and the sample is the union of the 5% random draw and 50
    archetypes — 94 stars for the 1,000-star P5 cohort. Total budget **6,760
    searches**, confirmed by the driver's own `expected_searches: 172` for a
    4-star smoke.

    The deeper problem is per-search cost. A campaign search runs at 7.6 s
    (correction 55). An injection search re-detrends, because §5.1 requires
    injection into the *pre-detrending* flux so completeness includes detrending
    erosion. Measured on a 4-star smoke:

    | star | 43 searches took |
    |---|---:|
    | TIC 441807873 | 10.7 min |
    | TIC 275691658 | 6.3 min |
    | TIC 233086272 | ~96 min |
    | TIC 341816210 | ~104 min |

    A **16× spread across four randomly-ordered stars of one cohort**, and the
    cause is unexplained. It is the largest single uncertainty in sizing the
    full run and may itself be a defect worth finding before paying for it.

    **This entry was first written with a wrong number, and the way it went
    wrong is the point.** Mid-run, with the two expensive stars still in flight,
    the aggregate rate read 85 → 84 → 53 → 49 searches/hour and 138 hours was
    quoted from the 49. The run then finished both stars and settled at **172
    searches in 120.7 minutes = 85.5 searches/hour**, giving roughly **20–80
    hours** depending on how 4-worker concurrency holds across a cost
    distribution sampled at n=4. So the sequence was: an estimate of ~14 h from
    the campaign's per-search cost (wrong, too low), then 138 h from a
    decaying mid-run rate (wrong, too high), then 85.5/hour measured at
    completion. **Correction 55 was about quoting a rate before it converged,
    and this entry did it again, one day later, in the entry recording it.**

    **Not launched.** The full calibration was approved on the 14 h figure. It
    has deliberately not been started on any of the later ones: the owner made a
    decision about an overnight job, and this is not that, whichever number is
    right.

59. **Known-planet recovery cannot be measured from anything the survey has
    already run, and the reason is by design.** The survey has already searched
    **473 confirmed transiting-planet hosts** (and 2,220 TOI hosts). Their
    ledger statuses look alarming at first read — 307 `screened_rejected`, 49
    `no_transit_detected`, only 25 `automated_survivor`.

    That reading is wrong. Of the 478 such hosts with a residual report on disk,
    **476 (99.6%) had the known signal masked before the search ran**. The
    pipeline identified the catalogued planet, removed it, searched for
    *additional* signals, and correctly found none worth promoting. The status
    distribution measures "what else is on known hosts", not recovery.

    So a rediscovery rate — what fraction of known transiting planets this
    pipeline would independently recover — **is unmeasured, and no existing
    artifact can supply it.** `results/p3/known_planets_v8/` is 20 curated stars
    at 20/20, which is a regression guard against breakage, not a completeness
    measurement; a hand-picked set that passes by construction cannot estimate a
    rate. `build_p3_known_planets.py` does not scale to the question: pointed at
    the 82,339-file offline archive cache with `--limit 1500` it resolved **4**
    suitable SPOC controls, because it requires pre-resolved SPOC sectors.

    Measuring it needs unmasked runs over a large known-host cohort — one search
    per star, so campaign-rate rather than injection-rate. It would answer
    §6.1's "completeness is healthy" clause with *real* signals carrying real
    variability, dilution and systematics, which injected boxes do not.

    **Recorded separately: mask leakage of ~11%.** 54 of those 478 stars have a
    strongest *residual* signal still matching the catalogued period within 1%
    (allowing 1:2, 2:1, 1:3, 3:1). Masking removed the signal from promotion but
    not from the periodogram. That is its own finding and is not what the
    rediscovery question is asking.

60. **Lane 6.1's primary contamination test cannot run on lane 6.1's
    candidates.** `pixel-vet` was pointed at the lane's one survivor, TIC
    298732908, across sectors 14/15/16. All three failed. Two returned transport
    errors and looked like the throttling correction 48 warns about; the third
    returned "No SPOC target-pixel file found ... in Sector 16".

    The third message is the true one. The campaign's own archive search for
    this star recorded `authors_considered: SPOC 0 products, TESS-SPOC 0
    products, QLP 3 products` — its photometry is **QLP-only**. There is no SPOC
    target-pixel file to fetch in any sector, so pixel vetting as invoked could
    never have succeeded, connection health notwithstanding. The transport
    errors were a red herring that would have supported the wrong diagnosis.

    **This is structural, not incidental.** Of the 953 searched stars in the
    cohort, **447 resolved to QLP** and 41 to TESS-SPOC; only 465 had SPOC.
    §6.1's whole rationale is to target stars where "QLP's *searched* sample
    historically thins out and TESS-SPOC's FFI target list is selective" — which
    is to say, precisely the stars least likely to carry SPOC products. **The
    lane is designed to produce candidates that the pixel stage, as currently
    invoked, cannot vet.** In P4 pixel vetting was the test that caught TIC
    76804724 sitting 4.31 px off target at 16.9σ; for roughly half of this
    lane's cohort that test is simply unavailable by default.

    The fix is likely `pixel-vet --author TESScut`, since TESScut generates
    cutouts from the FFIs for any target and the campaign already models a
    `author_fallback_to_tesscut` path. **Not verified** — MAST returned two
    dropped connections during this attempt and further pixel traffic was
    deferred rather than retried against a service that had just disconnected
    twice. Verifying it is the first pixel-lane action for the next session, and
    §4.5's contamination requirement for this lane depends on the answer.

61. **The lane's first completeness surface exists, and it says the low yield
    is a sensitivity result, not evidence that the niche is thin.** The 4-star
    calibration smoke completed 160 injections with 0 errors
    (`results/p5/calibration_smoke/calibration_summary.json`):

    | measure | value |
    |---|---:|
    | random-phase completeness | 0.350 |
    | edge completeness | 0.363 |
    | **random-phase promotion completeness** | **0.200** |
    | edge promotion completeness | 0.175 |

    The surface, recovery fraction by injected period and depth in units of the
    star's own 3 h photon-noise depth:

    | period | 0.5× | 1× | 2× | 4× | 8× |
    |---|---:|---:|---:|---:|---:|
    | 0.50 d | 0.00 | 0.00 | 1.00 | 1.00 | 1.00 |
    | 1.26 d | 0.00 | 0.00 | 0.50 | 1.00 | 1.00 |
    | 3.16 d | 0.00 | 0.00 | 0.00 | 1.00 | 1.00 |
    | 7.95 d | 0.00 | 0.00 | 0.00 | **0.00** | 1.00 |
    | 20.0 d | 0.00 | 0.00 | 0.00 | 0.00 | 0.60 |

    **Completeness collapses with period.** At 0.5 d a 2× transit is always
    recovered; by 8 d a 4× transit is recovered *never*, and at 20 d only an 8×
    depth gets to 60%. The lane's one survivor sits at P = 14.7 d — inside the
    region where this surface says the pipeline recovers almost nothing.

    **This resolves the reading of the first pass (correction 55's cohort).**
    §6.1 predicts 0.3–0.8% of stars show a detectable transiter, and that
    prediction explicitly *assumes* "detectable fraction at our completeness (to
    be measured, assume 30–60% for R > 2 R⊕)". Measured promotion completeness
    is **20%**, below the assumed band. Rescaling the prediction by the measured
    fraction brings it to roughly 0.1–0.5%, which brackets the observed
    **0.10%**. The shortfall the first pass appeared to show against §6.1 is
    largely explained by sensitivity, so **the kill criterion's "completeness is
    healthy" clause currently reads false, and a null result cannot be used to
    call the niche thin.** On this evidence the lane should not be killed.

    **Provisional, and the caveats are load-bearing:** n = 4 stars, 160
    injections, several surface cells resting on 1–2 trials, and the same 4
    stars whose cost varied 16× (correction 58) — so they are not a
    representative draw either. Correction 56 is the standing warning against
    reading a population off a 4-row head-of-list slice. This needs the full
    94-star sample before it is quoted as the lane's completeness.

    **Gate status is a small-sample artifact, not a failure.**
    `inverted_survivor_rate` 0.0 ≤ 0.001 ✓, `scrambled_survivor_rate` 0.0 ≤
    0.001 ✓, `median_recovered_depth_bias` 0.038 ≤ 0.05 ✓ over 57 recoveries,
    `edge_recovery_gap` −0.0125 ≤ 0.03 ✓. The two failures are
    `t3_pass_rate` = 0.0 against a floor of 0.002 (4 baseline stars cannot
    produce a rate above zero) and `epoch_enrichment` = null (insufficient
    data). `release_gate_passes: false` is therefore expected at n=4 and is not
    evidence against the pipeline.

62. **The pipeline blind-recovers 83.6% of known transiting planets, measured
    on 323 of them.** The first survey-wide rediscovery test
    (`scripts/measure_known_planet_recovery.py`,
    `results/p5/known_recovery_spoc/`). The search is genuinely blind: the
    target planet is removed from the catalog so the shipping mask cannot hide
    it, sibling ephemerides stay maskable, and the strongest peak of a normal
    production search is compared against the catalogued period.

    | | |
    |---|---:|
    | scored | 323 |
    | **blind period recovery** (`exact` + `harmonic_alias`) | **270 = 83.6%** |
    | **full recovery** (period + depth within 50%) | **258 = 79.9%** |
    | errors | 0 |

    Recovery is strongly depth-dependent and that is the finding that matters:

    | depth (ppm) | 0–250 | 250–500 | 500–1k | 1k–2.5k | 2.5k–5k | 5k–10k | >10k |
    |---|---:|---:|---:|---:|---:|---:|---:|
    | rate | 0.36 | 0.72 | 0.58 | 0.71 | 0.86 | **0.95** | **0.90** |

    Against Tmag it is nearly flat (0.62–0.89 from Tmag 5 to 16), so **depth,
    not brightness, is what the pipeline is limited by.**

    **This reconciles with correction 61 rather than contradicting it.** That
    entry measured 20% promotion completeness by injection on faint P5 M dwarfs
    at 0.5–8× the photon-noise depth; this entry measures 83.6% on known
    planets whose median depth is 5,246 ppm with 100 of 323 above 10,000 ppm.
    Conditioned on depth the two agree: deep transits are recovered reliably,
    shallow ones are not. The known-planet cohort is dominated by large planets
    because that is what the catalogue contains, so **its high rate must not be
    quoted as the survey's completeness** — it is the completeness for bright,
    deep signals.

    **Scoring caveat, recorded because it nearly shipped wrong.** The first run
    of this measurement reported **100%** period recovery. It scored
    `period_relation`, which is a descriptive nearest-relation label populated
    even on failures — `("miss", "one-third-period alias")` occurs 29 times and
    `("miss", "exact")` 9 times. The pipeline gates on `period_status`
    (`exact`/`harmonic_alias`), and scoring that gives 83.6%. A perfect score
    across 323 trials was the tell; nothing else about the run looked wrong.

    **Scope:** SPOC only, 323 of the 371-planet cohort; the 48 TESScut targets
    are unrun. The cohort is already restricted to in-principle recoverable
    planets — period inside the search range and at least 3 transits fitting
    the real observed baseline — with 101 exclusions listed in
    `targets/p5_known_planet_recovery_excluded.csv`. Rows whose depth was
    derived from `pl_rade`/`st_rad` rather than catalogued also show lower
    *period* recovery (0.80 vs 0.89), which depth arithmetic cannot cause, so
    that split reflects a genuinely harder population rather than a defect in
    the derivation.

63. **Every known-planet recovery failure is a search failure, not a vetting
    failure — and the search fails by locking onto unrelated short periods.**
    Decomposing correction 62's 323 trials:

    | failure mode | count |
    |---|---:|
    | recovered | 258 |
    | **period miss** | **53** |
    | period right, depth outside the 50% tolerance | 12 |
    | **production triage rejected a correctly recovered planet** | **0** |

    **Zero.** The veto stack never killed a real planet it had found. Whatever
    is wrong with this pipeline's yield, it is not that the screen is too
    aggressive — which also means the P5 cohort's 952 rejections are unlikely to
    be hiding many found-then-discarded planets. The losses are upstream.

    Misses concentrate at shallow depth: median depth of a miss **873 ppm**
    against a cohort median of **4,594 ppm**. By band, recovery runs 0.36 below
    250 ppm to 0.95 in the 5–10k ppm band.

    **The alias reading is wrong and worth recording as such.** The results
    label 29 misses "one-third-period alias", 8 "double-period alias", and so
    on, which invites the conclusion that the pipeline is losing planets to
    harmonic confusion. Tested against a ratio ladder
    (1/4, 1/3, 1/2, 2/3, 1, 1.5, 2, 3, 4, 5, 6), **only 2 of 53 misses sit
    within 2% of any low-order ratio.** The other 51 are at periods unrelated to
    the truth, and 37 of 53 are *shorter* than the truth. `period_relation` is
    the nearest rung of a limited ladder, not a claim that the recovered period
    is that harmonic — reading it as a diagnosis would have blamed alias
    adjudication for a problem it does not have.

    **The actual mechanism matches the P5 cohort exactly.** When the transit is
    too shallow to dominate the periodogram, the search locks onto unrelated
    short-period structure. In the P5 first pass, 83% of rejections were "the
    best-fit period or duration is pinned to a search-grid rail", overwhelmingly
    at sub-day periods with duty cycles above 15%. Two independent cohorts —
    real known planets and a blind faint-M-dwarf survey — show the same
    pathology. **That, not the vetting stack, is where sensitivity is being
    lost, and it is where work would buy the most yield.**

64. **A quarter of the lost planets were found and then not chosen.** Taking
    correction 63's 53 period misses and asking whether the true period — or a
    1:2, 2:1, 1:3 or 3:1 alias of it — appears among the five BLS peaks each
    report records:

    | | count |
    |---|---:|
    | present among the recorded peaks but **not selected** | **14** |
    | not among the five recorded peaks | 39 |

    rank 1 ×1, rank 2 ×3, rank 3 ×6, rank 4 ×3, rank 5 ×1.

    Several are near-ties resolved the wrong way rather than weak detections:
    `pi Men c` at rank 4 with relative power 0.9999999, `WASP-169 b` at rank 3
    with 0.9999999, `TOI-1203 b` at rank 3 with 0.9999999. The periodogram had
    the planet; the selection step took something else.

    **So the ~16% period-miss rate decomposes into two different problems.**
    About a quarter is peak *selection and refinement* — recoverable by
    choosing better among candidates the search already produces, with no new
    photometry and no change to sensitivity. The remaining three quarters are
    absent from the recorded peaks, though only five are stored per report, so
    "absent" means "not in the top five", not "not in the periodogram". Storing
    a deeper peak list would sharpen that split, and is cheap.

    **Caveat on the peak list itself.** `top_period_peaks` records the BLS grid
    peaks while `strongest_residual_signal.period_days` is the refined fit, and
    they can disagree materially — TOI-1130 b has a rank-1 peak at 8.1056 d
    (0.53% from twice its 4.0746 d truth, inside the 1% tolerance) yet reports a
    refined period of 7.688 d, 5.6% off, which is what makes it a miss. At least
    one planet is therefore being lost *between* the grid and the refinement,
    not at either end.

65. **TESScut photometry recovers known planets far worse than SPOC, and it is
    the half of lane 6.1 that also cannot be pixel-vetted.** The recovery cohort
    split by product, same code, same period range, same scoring:

    | | SPOC | TESScut |
    |---|---:|---:|
    | planets scored | 323 | 48 |
    | **blind period recovery** | **83.6%** | **64.6%** |
    | **full recovery** (period + depth) | **79.9%** | **52.1%** |
    | recovered below 1,000 ppm | 48 / 81 | **0 / 6** |
    | recovered above 10,000 ppm | 90% | 64% |

    ~2.7σ on period recovery and ~3.9σ on full recovery — a real difference, not
    small-sample noise, though the per-bin counts on the TESScut side are thin
    and the depth bands below 1,000 ppm rest on 6 planets total.

    TESScut is worse *even on deep transits*: 64% recovery above 10,000 ppm,
    where SPOC manages 90%. So this is not only a faint-end effect — the
    local-extraction photometry loses signal that the mission pipeline keeps.

    **Combined with correction 60 this is a structural problem for the lane.**
    §6.1 deliberately targets stars where "QLP's *searched* sample historically
    thins out and TESS-SPOC's FFI target list is selective" — that is, stars
    least likely to have SPOC products. In the P5 cohort **447 of 953 resolved
    to QLP and 41 to TESS-SPOC against 465 SPOC**. For that non-SPOC half the
    lane now carries two measured penalties at once:

    1. **Recovery is ~28 points worse** (this entry), so real planets there are
       likelier to be missed outright.
    2. **Pixel vetting cannot run at all** (correction 60), so any survivor that
       does appear cannot be cleared of contamination by the test that caught
       TIC 76804724 at 4.31 px off target.

    The lane is aimed precisely at the population where this pipeline is weakest
    and least able to check itself. That is not an argument to abandon §6.1 —
    the niche may still be real — but the yield arithmetic in §6.1 assumes a
    detectable fraction that these measurements do not support for half the
    cohort, and no version of that arithmetic currently accounts for the
    vetting gap.

    **Caveat on provenance.** Within TESScut, planets whose expected depth came
    from the catalogue recover at 83.3% (period) against 45.8% for those whose
    depth was derived from `pl_rade`/`st_rad`. The derived-depth group is
    plausibly a genuinely harder population — smaller, less well characterised
    planets — rather than an artifact of the derivation, since depth arithmetic
    cannot affect *period* recovery. The same split on SPOC is much milder
    (89.4% vs 80.5%).

66. **Correction 61's completeness numbers were roughly double the truth, and
    the corrected figures make lane 6.1 unambiguously sensitivity-limited.**
    That entry measured 4 stars / 160 injections and flagged itself provisional.
    The running full calibration has now finished **77 of the 94 sampled stars
    — 3,080 injections, 82% of the injection sample**, so these are near-final
    rather than an early slice (the driver deliberately orders injection stars
    first).

    | | correction 61 (n=4) | now (n=77) |
    |---|---:|---:|
    | raw completeness, random phase | 0.350 | **0.183** |
    | **promotion completeness** | **0.200** | **0.080** |
    | period recovered | — | 0.213 |
    | detection gate passes | — | 0.380 |

    Random-phase and segment-edge agree closely (0.183 vs 0.182 raw; 0.080 vs
    0.068 promotion), so edge erosion is not the driver.

    Surface, recovery fraction by injected period and depth in units of the
    star's own 3 h photon-noise depth:

    | period | 0.5× | 1× | 2× | 4× | 8× |
    |---|---:|---:|---:|---:|---:|
    | 0.50 d | 0.06 | 0.01 | 0.36 | 0.50 | 0.52 |
    | 1.26 d | 0.00 | 0.00 | 0.09 | 0.47 | 0.53 |
    | 3.16 d | 0.00 | 0.00 | 0.00 | 0.40 | 0.60 |
    | 7.95 d | 0.00 | 0.00 | 0.00 | 0.10 | 0.56 |
    | 20.0 d | 0.00 | 0.00 | 0.00 | 0.02 | **0.28** |

    Worse than the n=4 version everywhere it matters. **Even an 8× photon-noise
    transit is recovered only about half the time**, where the 4-star surface
    showed 1.00 across most of that column. Nothing at or below 1× is recovered
    at any period.

    **What this does to §6.1's arithmetic.** The lane predicts 0.3–0.8% of stars
    show a detectable transiter *assuming* a detectable fraction of 30–60%.
    Measured promotion completeness is **8.0%**, four to seven times below that
    assumption. Rescaling gives **0.04–0.21%**, and the first pass observed
    **0.10%** — squarely inside. The lane's apparent shortfall against its own
    forecast is now almost entirely explained by sensitivity, and the kill
    criterion's "completeness is healthy" clause reads **false** by a wider
    margin than correction 61 suggested. A null result here still cannot be used
    to call the niche thin.

    **A second stage is costing more than the search.** Period recovery is
    0.213 and raw recovery 0.183, so most correct detections survive; but
    promotion completeness is 0.080, meaning **the vetting stack removes about
    60% of what the search does recover.** That is not the false-kill behaviour
    correction 63 measured on known planets (0 of 258) — those are deep signals,
    median 5,246 ppm, while these injections run 0.5–8× the noise floor. The
    screen is doing its job on marginal detections, but the yield cost at the
    margin is large and was previously unmeasured.

    Top rejection reasons among unrecovered injections: search-grid rail
    pinning (1,904), duration inconsistent with catalog stellar density
    (1,836), fewer than 60% of predicted events sampled (1,384). The rail
    pinning is the same pathology as corrections 63 and 65 — a third
    independent cohort showing it.

    Supersedes correction 61's numbers. Final figures land when the calibration
    completes.

67. **Correction 62's "depth-limited, not brightness-limited" is wrong.
    Recovery is limited by depth and brightness jointly, and the flat-in-Tmag
    reading was an artifact of a deep-dominated cohort.** Extending the
    known-planet cohort into the 250–2,500 ppm band (200 new hosts, sectors
    resolved from MAST, `results/p5/known_recovery_shallow/`) gives **33/200 =
    16.5% full recovery** against 79.8% on the original deep-heavy sample. And
    inside that shallow band, brightness is decisive:

    | Tmag | 5–8 | 8–9 | 9–10 | 10–11 | 11–12 | 12–13 | 13–16 |
    |---|---:|---:|---:|---:|---:|---:|---:|
    | recovery | 0.75 | 0.63 | 0.57 | 0.64 | 0.23 | 0.12 | **0.009** |

    One of 108 shallow planets fainter than Tmag 13 was recovered. Correction 62
    reported recovery as nearly flat from Tmag 5 to 16, which was true *of that
    cohort* — median depth 5,246 ppm, where brightness genuinely does not
    matter. Marginalising over a sample whose depth distribution correlates with
    the variable under test inverted the conclusion. A textbook aggregation
    error, and it survived because the flat result looked physically tidy.

    **The joint surface, 589 scored planets across all three runs:**

    | depth ppm | Tmag 0–10 | 10–11.5 | 11.5–13 | 13+ |
    |---|---:|---:|---:|---:|
    | 0–500 | 0.58 | 0.33 | 0.00 | 0.00 |
    | 500–1,000 | 0.56 | 0.61 | 0.06 | 0.00 |
    | 1,000–2,500 | 0.76 | 0.68 | 0.42 | 0.03 |
    | 2,500–5,000 | 0.88 | 0.85 | 0.78 | (2/3) |
    | 5,000+ | 0.89 | 0.93 | 0.93 | 0.75 |

    Recovery survives an unfavourable value of either variable and collapses
    only when both are unfavourable. **This is the surface that should be quoted
    for this pipeline**, not the marginal-in-depth or marginal-in-Tmag versions.

    **What it says about lane 6.1, with a caveat that matters.** In the lane's
    own magnitude window (Tmag 12.5–13.6, 80 known planets):

    | depth | recovered |
    |---|---:|
    | <1,000 ppm | 1/18 |
    | 1,000–3,000 ppm | 3/19 |
    | 3,000–5,000 ppm | **3/3** |
    | 5,000+ ppm | **33/40** |

    §6.1's rationale predicts "a 2 R⊕ planet on an M4 dwarf is a ~0.3–0.5%
    event" — 3,000–5,000 ppm — and in that band at those magnitudes the pipeline
    recovers what it is shown. **The lane's stated target regime is not the
    regime where this pipeline fails.**

    **But do not read that as vindication.** These 80 are known hosts that
    happen to be faint, not faint M dwarfs; a 5,000 ppm transit on a faint
    solar-type star is a giant planet with different noise properties from a
    2 R⊕ planet on an M4 dwarf. The injection surface (correction 66), which
    *is* measured on the actual P5 M-dwarf cohort, gives 8% promotion
    completeness. Both are true: the pipeline handles deep transits on faint
    stars, and it recovers little of what is injected into these particular
    faint M dwarfs. Reconciling those two is the next real question, and the
    3/3 cell is three planets.

68. **The P5 cohort fails its false-alarm gates, and the lane's one candidate is
    statistically indistinguishable from noise.** The full §5.1/§5.2 calibration
    finished on the repaired cohort: 89 injection stars, **3,560 injections**,
    950 baseline / 950 inverted / 950 scrambled
    (`results/p5/calibration_ncvz_1000/`). Completeness confirms correction 66
    at full scale — random-phase 0.1843, **promotion 0.0809**, edge promotion
    0.0697 — so the 8% figure is settled.

    The gates are the story:

    | gate | measured | budget | |
    |---|---:|---:|---|
    | inverted survivor rate | **4/950 = 0.00421** | ≤0.001 | **FAIL, 4.2×** |
    | scrambled survivor rate | **2/950 = 0.00211** | ≤0.001 | **FAIL, 2.1×** |
    | epoch enrichment | **4.989** | ≤2.0 | **FAIL, 2.5×** |
    | t3 pass rate | 0.00105 | 0.002–0.02 | **FAIL, below floor** |
    | median recovered depth bias | 0.0371 | ≤0.05 | pass |
    | edge recovery gap | 0.0 | ≤0.03 | pass |

    `release_gate_passes: false`, and this time it is not the n=4 artifact that
    made the smoke fail — the sample is full size.

    **Inverted flux is sign-flipped, so a survivor there is a false alarm by
    construction.** The measured rate is 0.42%. Across the 953 real targets the
    first pass searched, that predicts **4.01 false alarms. The first pass
    produced 1 survivor.** We found *fewer* apparent candidates than the noise
    model predicts should appear from noise alone.

    **So TIC 298732908 must not be carried as a candidate.** Nothing about it
    has changed — TLS SDE 10.09, FAP 8×10⁻⁵, all six T3 checks passing, no
    review flags, not a rediscovery (corrections 62, 57). What changed is the
    denominator: on this cohort the pipeline manufactures roughly four such
    objects from pure noise, so one survivor is not evidence of anything. It
    stays a lead worth vetting, and it cannot be vetted by the pixel test
    because it is QLP-only (correction 60).

    **This is a cohort property, not a pipeline regression.** P3's calibration
    on a brighter cohort measured inverted 0/500 and scrambled 0/500 — clean.
    The identical kernel on faint M dwarfs produces 4 and 2. That is consistent
    with everything else measured this phase: at low S/N the search locks onto
    unrelated short-period structure (corrections 63, 65, 66), and some of that
    structure survives screening.

    **Epoch enrichment at 4.99 against a ceiling of 2.0** says recovered events
    cluster in time far more than chance, which is the signature of an
    observatory or detrending systematic rather than astrophysics. P3 measured
    1.468 on its cohort. This deserves its own investigation and is the most
    concrete lead for *why* the false-alarm rate is elevated.

    **Consequences.** The lane cannot leave diagnostic status: §5.1 requires a
    completeness surface, which now exists, but §5.2's false-alarm budget is
    breached. Any yield estimate for §6.1 must now carry a ~0.4% per-star false
    alarm rate, which at the lane's measured 8% completeness means false alarms
    outnumber recoverable real planets over most of the parameter space. That
    is an owner-level finding about whether §6.1 is viable as specified, and it
    is not settled here.

69. **The epoch-enrichment failure is one shared systematic at 17.82 d, the
    screen that catches it already exists, and it was never run on the P5
    campaign.** Chasing correction 68's `epoch_enrichment = 4.989`:

    The histogram's worst bins are not scattered. They sit at BTJD 1697.833,
    1715.667, 1733.479 and 1751.312 — **a 17.82-day cadence**, each holding
    180–248 aligned signals against ~50 expected. Globally the histogram is
    unremarkable (180,591 aligned against 181,123 expected, ratio 0.997) and
    only **12 bins** breach the ceiling, so this is not diffuse noise; it is one
    repeating feature.

    The fitted periods say the same thing outright. Across the 953 searched
    stars the modal fitted period is **17.82 d, and 190 stars land in
    17.7–17.95 d** — unrelated stars, one period. That is an observatory
    systematic by definition.

    **`campaign_common_mode_screen` reads `None` for all 953 targets.** The
    screen whose entire purpose is "a shared ephemeris is evidence against an
    astrophysical origin" was not applied. Running it now
    (`results/p5/common_mode_p5_v2.json`):

    | | |
    |---|---:|
    | flagged `localized_coincidence` | **274 / 953 (28.8%)** |
    | of the 190-star 17.82 d family, flagged | **189** |
    | survivor TIC 298732908 | `independent_timing`, shared_targets 0, enrichment 0.0 |

    The screen catches 189 of 190. **The systematic was always detectable; the
    detector was switched off.**

    **Two separate pathologies, and only one is common-mode.** The flagged
    population is long-period — 17.82 d (153), 21.45 d (39), 18.42 d (29). The
    679 unflagged cluster near **1.0 d**, which is the red-noise rail-pinning of
    corrections 63/65/66: those fits share no epoch, so common-mode cannot see
    them and a different fix is needed. Also recorded: 43 targets sit at the
    20 d `period_at_search_ceiling`, and 21.45 d is *above* the search maximum —
    overscan-zone fits that should never have reached scoring.

    **What this does and does not settle.** Correction 68's false-alarm
    comparison remains apples-to-apples: both the 0.42% inverted rate and the
    single real survivor were measured *without* the screen. But both numbers
    should fall once it is applied, so the gate failure is likely recoverable
    without touching the frozen kernel. The survivor's own standing improves
    slightly — it is not part of the shared family — though correction 68's
    arithmetic still means one survivor is not evidence.

    **Related to correction 57's unfixed half.** The dip registry recorded its
    cohort as `s14-camunknown-ccdunknown` with **zero windows**, because the P5
    cohort omits `camera`/`ccd`. `campaign.py` warns that target lists missing
    those columns "leave the cohort at sector scope", which dilutes a
    detector-local systematic below the `dip_min_fraction: 0.1` threshold. Two
    independent guards against shared systematics were disabled at once — one by
    a missing column, one by never being invoked.

70. **The common-mode screen is a partial fix for the false-alarm gates, not a
    complete one, and the residue sits on the spacecraft period.** Testing
    correction 69's fix against the actual null survivors that broke correction
    68's gates:

    | null survivor | fitted period | common-mode verdict |
    |---|---:|---|
    | TIC 229944662 (inverted) | 13.98 d | **localized_coincidence** |
    | TIC 233056405 (inverted) | 5.70 d | independent_timing |
    | TIC 272887739 (inverted) | 12.88 d | **localized_coincidence** |
    | TIC 420113184 (inverted) | 12.58 d | **localized_coincidence** |
    | TIC 229444326 (scrambled) | 9.97 d | independent_timing |
    | TIC 237103326 (scrambled) | 1.00 d | independent_timing |

    Screening removes **3 of 4 inverted** survivors, taking that rate from
    0.00421 to **0.00105 — still marginally over the 0.001 budget** — and
    **0 of 2 scrambled**, leaving 0.00211 untouched at 2.1× over. So the screen
    substantially repairs one gate and does nothing for the other.

    **Where the residue lives.** Three of the four inverted survivors fall at
    12.58–13.98 d, bracketing TESS's **13.7 d orbital period**, which is the
    `spacecraft_harmonic` the screen already computes per target. Inverting
    flux turns a systematic brightening into a dip, so a spacecraft-locked
    artifact is exactly what a sign-flip null should expose — the null test is
    working as designed and is telling us the residual false alarms are
    instrumental, not astrophysical.

    **Caveat on this measurement.** The common-mode verdicts were computed from
    each star's *real* fitted ephemeris, while the survivors above come from the
    inverted and scrambled runs, whose fitted periods differ. This is therefore
    indicative rather than exact. The clean test is to run the screen over the
    null results themselves, which needs those runs emitting campaign-format
    residual reports; they currently do not.

    **Consequence for the fix path.** Correction 69's screen should be applied
    to every campaign — it was simply never invoked — but it will not by itself
    return this cohort to a passing §5.2 budget. Closing the remainder means
    addressing the spacecraft-harmonic residue and the ~1 d rail-pinning family
    separately, and the latter lives in the frozen detection kernel.

71. **85% of the survey had never been common-mode screened, and screening it
    survey-wide would have destroyed 1,665 established verdicts.** Correction 69
    found the screen had never been run on the P5 campaign. Auditing the rest:

    | | targets | screened before |
    |---|---:|---|
    | `full_remaining_pool` | 64,614 | **none** |
    | `sector101_6000` | 6,000 | none |
    | `sector100_detector_balanced_4327` | 4,327 | none |
    | `sector100_small_star_3128` | 3,125 | none |
    | total unscreened | **71,459 of 84,450 (85%)** | |

    Screening each campaign added **72,336 verdicts**, of which 72,062 are
    `independent_timing` and 274 the P5 `localized_coincidence` family. Coverage
    goes 12,038 → 84,374 with **zero retractions**. Notably `full_remaining_pool`
    flags nothing at all, so the P5 cohort's 28.8% is a sharp outlier rather
    than the survey norm.

    **The near-miss is the part worth recording.** The obvious way to close a
    coverage gap is one run over everything, and `--campaign-root
    results/campaign` does exactly that: 84,377 targets across 23 campaigns in
    69 seconds. It reports 3,950 `common_mode_systematic` against the
    established screen's 5,615 — **it would have retracted 1,665 systematics**,
    and the importer takes newest-file-wins per TIC, so the import would have
    applied them silently.

    TIC 325384023 shows why the broad run is wrong:

    | | per-campaign | survey-wide |
    |---|---:|---:|
    | shared targets | 436 | 29 |
    | period-group targets | 562 | 96 |
    | expected shared | 7.77 | 23.05 |
    | **enrichment** | **56.11** | **1.26** |
    | verdict | `common_mode_systematic` | `independent_timing` |

    Pooling every campaign inflates the phase-uniform expectation and shatters
    the period grouping, so a 56× detection reads as 1.26×. **The screen's
    verdict is defined relative to the cohort it is run over**, and the cohort
    is meant to be one campaign — a spacecraft event is shared by stars observed
    together, not by every star the survey ever touched. The survey-wide file
    was deleted rather than imported.

    Method note for anyone extending this: check the transition matrix between
    an old and a new screen before importing, not the headline counts. The
    survey-wide run looked like an improvement on every summary statistic —
    seven times the coverage, same tool, same code — and was a regression.

72. **A third of the survivor list is already flagged as artifact-like by
    discriminators the pipeline computes and never uses.** The common-mode
    screen records three per-target facts alongside its verdict —
    `spacecraft_harmonic` (ratio against TESS's 13.7 d orbit),
    `duration_at_grid_rail`, and `period_at_search_ceiling` — and nothing vetoes
    on any of them. Now that the whole survey is screened (correction 71),
    they can be tested against 84,374 targets and 990 screened survivors:

    | flag | population | survivors | enrichment | approx. |
    |---|---:|---:|---:|---:|
    | `spacecraft_harmonic` | 8.23% | 13.43% | **1.63×** | 5.7σ |
    | `duration_at_grid_rail` | 5.22% | 15.25% | **2.92×** | 13.8σ |
    | `period_at_search_ceiling` | 3.16% | 7.58% | **2.40×** | 7.8σ |

    Taken together, **311 of 990 survivors (31.4%) carry at least one flag,
    against 14.8% of the population — 2.12× enriched.** 679 survivors carry
    none. Adding the shared-ephemeris verdict to the union changes nothing,
    because a common-mode-flagged star already holds that status rather than
    `automated_survivor`, so these three are entirely additional signal.

    **All three have a physical reading.** A period at a low-order ratio of the
    spacecraft orbit, a duration pinned to a search-grid rail, and a period
    pinned at the 20 d search ceiling are each a statement that the fit was
    shaped by the instrument or the grid rather than by the star. That is the
    same mechanism corrections 63, 65, 66 and 68 each found from a different
    direction; this entry just shows it is already labelled in stored data.

    **Not acted on.** Demoting 311 stars is a status change, and status
    precedence remains an open owner decision with P4 evidence still non-voting.
    Recorded so the decision can be made on measured enrichment rather than
    intuition. Worth noting the fix would sit in the vetting layer, not the
    frozen detection kernel — these flags are produced by the common-mode
    screen, so promoting them to vetoes needs no re-signing of the calibration.

    **The 679 unflagged survivors are the honest candidate pool**, and for the
    P5 cohort specifically correction 68's false-alarm arithmetic still applies
    on top of that.

73. **The 85% coverage gap had no code defect behind it — the screen is a
    one-shot command whose output is a file, and nothing re-runs it when a
    campaign lands.** Correction 71 measured the gap; the epoch backfill in
    `91f8b74` explained only 7 small campaigns and 84 targets of it, and left
    the cause of the bulk open. This is the cause, and it is duller and more
    dangerous than a parsing bug.

    `results/vetting/common_mode/common_mode_screen.json` — the default
    `--output` of `common-mode-screen` — is stamped **2026-07-26T18:52Z** and
    holds 12,038 verdicts. The four campaigns that make up the gap finished
    **after** it:

    | campaign | targets | `batch_summary.json` written |
    |---|---:|---|
    | `sector100_small_star_3128` | 3,125 | 2026-08-04 02:33 |
    | `sector100_detector_balanced_4327` | 4,327 | 2026-08-05 00:14 |
    | `sector101_6000` | 6,000 | 2026-08-05 11:53 |
    | `full_remaining_pool` | 64,614 | 2026-08-06 09:47 |

    Nobody re-ran the screen, nothing re-ran it on their behalf, and no check
    compares screened targets against searched targets. The July file kept
    existing and kept being read, so coverage looked established while the
    survey quadrupled behind it. Thirteen days.

    **Two mechanisms kept it invisible, and both are the ledger's recurring
    shape — a check that cannot run does not report as failing.**

    *First:* `batch_summary.json` is written once, by `campaign.py:1142`, after
    `publish_progress("finalizing")`. There is no incremental write. So a
    campaign is invisible to `screen_campaign_root`'s summary glob for its whole
    run — four days for `full_remaining_pool` — and **permanently** if it is
    interrupted. `results/campaign/sector100_spoc` is in that state now:
    `state: interrupted`, 24 of 5,000 targets, invisible since 2026-07-27.

    *Second:* every campaign summary carries a `campaign_level_screening.common_mode`
    key, which reads as though the campaign screened itself. It did not.
    `_quarantine_invalid_common_mode` (`campaign.py:1583`) only strips the
    retired midpoint-density veto and stamps every row
    `"campaign_common_mode_screen": "not applied"`. The reassuring key and the
    literal string "not applied" sit in the same file.

    **Fixed by making the silence audible**, not by automating a re-run — an
    automatic screen would have to choose a cohort, and correction 71 is the
    record of what choosing wrong costs. `screen_campaign_root` had two bare
    `continue`s that dropped a campaign leaving no trace; it now returns
    `skipped_campaigns` with a reason per campaign (`no_batch_summary`,
    `unreadable_summary`, `no_result_rows`, `no_ephemeris`), and the CLI prints
    them under a `NOT screened:` heading. On this workspace it names three that
    were previously silent: `sector100_spoc`, `p5_retry_47`, `p5_verify_survivor`.

    **One latent bug found and deliberately not counted as damage.**
    `dashboard.py:122` merges every `common_mode_screen.json` under `results/`
    in `sorted()` path order, last write winning per TIC — not newest, not
    strongest. `results/vetting/…` sorts after `results/campaign/…`, so the
    July 26 file wins every TIC it shares with the August per-campaign screens.
    The transition matrix says it currently shares **none**: 12,038 stale and
    72,336 fresh, disjoint, summing to correction 71's 84,374. So this is a trap
    set, not a trap sprung, and reporting it as a live regression would have
    been wrong. It becomes real the moment any campaign is screened twice.

    Method note, and the reason the previous paragraph is not a scarier one:
    the sort order alone *looks* like proof of damage. Only the old→new
    transition matrix — the same instrument correction 71 prescribes — shows
    there is none.

74. **The re-calibration finished and answered its question: the kernel change
    traded four injected planets for two false alarms, and left every failing
    gate failing.** `results/p5/calibration_ncvz_1000_v2_neartie` completed
    2026-08-09 19:26 and was never written down; this is that entry.

    `release_gate_passes: false`. The same four gates fail before and after, so
    this is not a regression the change introduced — it is a pre-existing
    false-alarm problem the change did not touch.

    | gate | pre | post | bound | passes |
    |---|---:|---:|---:|---|
    | `inverted_survivor_rate` | 0.004211 (4/950) | 0.003148 (3/953) | ≤ 0.001 | no |
    | `scrambled_survivor_rate` | 0.002105 (2/950) | 0.001049 (1/953) | ≤ 0.001 | no |
    | `t3_pass_rate` | 0.001053 | 0.001049 | 0.002–0.02 | no |
    | `epoch_enrichment` | 4.98873 | 5.02656 | ≤ 2.0 | no |
    | `median_recovered_depth_bias` | 0.037130 | 0.037130 | ≤ 0.05 | yes |
    | `edge_recovery_gap` | 0.0 | 0.0 | ≤ 0.03 | yes |

    **The headline rates hide the finding; the row-level diff carries it.** Both
    runs used the same 89 injection stars and the same 3,560 injections, so they
    diff row for row. Decision 2b's spacecraft-harmonic veto added its reason to
    **283** of those 3,560 — and was the *sole* rejection reason in **4**. Every
    other one of the 283 was already rejected for unrelated reasons, so the veto
    was redundant there. On the false-alarm side the veto was the sole reason for
    exactly **1 inverted** and **1 scrambled** row, which is precisely the 4→3
    and 2→1 movements above.

    **So the veto cost 4 injected planets and bought 2 false alarms.** The
    arithmetic closes exactly: promotion completeness fell 0.08090 → 0.07865, and
    4/1,780 = 0.002247 against a measured drop of 0.00225. The question decision
    2b was approved to answer — whether removing 15.1% of the 0.5–20 d range buys
    more in false-alarm suppression than it costs in completeness — is answered
    **no**, on this cohort, by measurement rather than intuition. The counts are
    small enough (4 against 2) that the ratio is not well determined, but the
    sign is.

    Decision 1's near-tie peak selection cost one further recovery, and it is
    legible: **TIC 219878686 injection 32**, P = 3.16228 d, recovered exactly at
    3.16214 d before and returning **0.79210 d** after — the 1/4 alias.
    `period_status` went `exact` → `miss`. Raw random-phase completeness is
    otherwise untouched: 328/1,780 in both runs.

    **`--trusted-first-pass` therefore remains unsatisfiable.** The new signature
    `sig1:2e448325…` / `kernel1:54e6cb1f…` has no passing release report, and the
    old hash must not be restored to make a campaign start.

    Two things left honest rather than tidied. The run is
    `execution_complete: false` at **953/1,000** with **47 errors, every one a
    `download: RuntimeError`** — and **it will never be true for this cohort.**
    Every error reads `No processed TESS light curve (tried SPOC, TESS-SPOC,
    QLP) is available`, one per distinct TIC, and **the same 47 TICs failed in
    the pre-change run too** (which failed 50; the other 3 succeeded here). This
    is not a transient MAST fault that a retry clears — those 47 stars have no
    processed light curve to download, so **953 is the achievable denominator**
    and re-running the driver would only re-fail them.

    That has a consequence for decision 5B, which commits to calibrating every
    cohort before searching it: `build_p5_primary_lane.py` selects 1,000 stars
    without checking light-curve availability, so **4.7% of this cohort was dead
    weight** and every future cohort will lose a similar slice. Either the
    builder should filter on availability, or cohort sizes should be quoted as
    achievable rather than requested. Not fixed here — it changes cohort
    composition, which is a calibration-affecting change. And
    `edge_recovery_gap` reporting exactly `0.000000` twice looks like a dead
    gate; it is not. 59 of 1,604 matched random/segment-edge injection pairs
    disagree on recovery, and the two directions cancel — matched pairs net −1,
    unmatched rows net +1. A real measurement that lands on zero, checked rather
    than assumed.

75. **The monotransit threshold was measured for the first time: section 3.4's
    written 8 sigma gives 1.25 false events per star, 4.2x its own budget, and
    the false events are long-duration — which is where the lane's science
    lives.** Decision 6 built the detector (`8f8845a`) with
    `DEFAULT_SIGNIFICANCE_THRESHOLD = 8.0`, section 3.4's *written* value, and
    the module said in its own docstring that nobody had measured it. Section
    3.4 asks for "calibrate on inverted data, target <= 0.3 false events/star at
    first pass". This is that calibration:
    `scripts/calibrate_monotransit_threshold.py`, over the 953 achievable stars
    of the P5 NCVZ cohort (the other 47 have no processed light curve at all —
    correction 74).

    | threshold | false events | per star | stars affected | direct events/star |
    |---:|---:|---:|---:|---:|
    | 8 sigma (written) | 1,194 | **1.2529** | 299 of 953 (31.4%) | 2.591 |
    | 15 sigma (measured) | 285 | **0.2991** | 119 of 953 (12.5%) | 1.519 |

    So the written threshold is not slightly optimistic, it is **4.2x over
    budget**, and the budget is not met until **15 sigma** — nearly double.
    Inverting the prepared flux uses `calibration.invert_prepared_flux`, the
    same `2 * median - flux` the periodic search's inverted gate uses, so the
    two false-alarm budgets stay comparable.

    **The distribution is the part that matters, not the threshold.** The >=8
    sigma false events have a **median duration of 14.5 h** against a template
    bank spanning 1.5–24 h, and the strongest reaches **60.6 sigma**. A 60 sigma
    event in inverted data is not noise; it is residual low-frequency systematic
    surviving the 3 d long-window detrend. Because they pile up at long
    durations, and because a long-period planet is exactly the one with a long
    transit, **raising the threshold is not a free fix** — it spends sensitivity
    precisely where the lane was supposed to gain it. The detector's existing
    vetoes barely touch this: of 7,579 inverted events above the 4 sigma search
    floor, **6,643 (87.7%) pass every veto**.

    **Why the test suite was green throughout.**
    `test_inverted_flux_produces_no_survivor` asserts exactly the right thing
    and passes, but its light curve is `1.0 + rng.normal(0, 3e-4)` — pure white
    Gaussian noise. Correct for the arithmetic, and silent about red noise,
    which is the only thing that actually produces these events. The module
    docstring never claimed the threshold was calibrated, so nothing
    misrepresented itself; the green test simply was not the calibration, and it
    would have been easy to read it as one.

    **A harness fault found and fixed, recorded because it nearly became a
    finding.** The first pass ran 12 workers and reported 72 unavailable stars
    against correction 74's 47. The extra 25 were not unavailable — their FITS
    were in the cache. Resolving `--author auto` can still reach MAST, parallel
    queries get throttled, and an empty result surfaces as `No processed TESS
    light curve (tried SPOC, TESS-SPOC, QLP) is available`: **a throttled query
    and an absent light curve are the same string.** The tell was the timing —
    failures clustered at the end of the run, which is what slow network calls
    look like beside fast cache hits. Re-running those 25 alone at two workers
    recovered **25 of 25**. The script now journals each star to `stars.jsonl`
    and resumes, treating an error as *not* done, and defaults to fewer workers
    than the machine has cores. Whatever fails twice, alone, is really missing.
    Merging the recovered 25 moves the headline from 1.2705 to 1.2529 per star
    and leaves the calibrated threshold at 15 sigma unchanged.

    **This does not stand the lane up, and 15 sigma is not yet an operating
    point.** A false-event rate is half of one: it measures what the threshold
    costs in noise, never what it buys in real single transits. Section 3.4 also
    requires single-transit injection-recovery, which has not been run, and the
    inverted rate is a *floor* on the false-alarm rate rather than the whole of
    it — a systematic that is not symmetric about the median does not invert
    into a dip and is therefore invisible to this test. Section 6.2's remaining
    prerequisites are untouched: the Seager–Mallén-Ornelas duration→period
    posterior, the mandatory pixel-vet, and the subscription re-check.

76. **Decision 2b reverted on the measurement it was approved to obtain — and
    reverting a search change does not give back the calibration that preceded
    it.** `4991367` promoted the TESS-orbit harmonic flag into the kernel and
    said so explicitly: "it is configurable … and decision 5B's re-calibration
    measures the completeness loss directly. That measurement, not this commit,
    is what should settle whether the trade is worth it." Correction 74 is that
    measurement. The owner called it on 2026-08-10.
    `veto_spacecraft_harmonic` goes **True → False**.

    The price, from the paired row-level diff: sole rejection reason for **4
    injected planets** against **1 inverted and 1 scrambled** false alarm — 4
    recoveries spent to buy 2 — with **none of the four failing release gates
    moved**.

    **Reverted at the default, not by deletion.** The flag, its tests, and
    correction 72's 1.60× enrichment stay: they are the record of why it was
    tried, `spacecraft_harmonic` still computes, and the common-mode screen
    still records the flag per target. Only the kernel veto is off. The trade
    may read differently on a cohort that is not 1,000 NCVZ M dwarfs.

    **The part worth recording is what the revert costs.** The detection
    identity moved a fourth time — config hash
    `fc1e508a…` → **`d68bccde…`**, kernel `kernel1:753fd6ef…`. It did **not**
    return to any earlier value. Decision 1's near-tie parameters and the
    alias-ladder wiring are still in place, and the boolean field itself still
    exists in `SearchConfig`, so switching it off yields a *fourth* identity
    rather than restoring the P3-certified first one. **There is no path back to
    a previous calibration, only forward through another one.** Anyone reaching
    for a revert to recover a trusted release should read that twice: the
    revert is cheap, the certification is not.

    **This does not unblock `--trusted-first-pass`, and should not be expected
    to.** Predicted from the row diff, the revert restores 4 promotions and
    re-admits 2 false alarms: inverted 3/953 → 4/953, scrambled 1/953 → 2/953,
    promotion completeness 0.07865 → 0.08090. All four gates that failed still
    fail, by the same wide margins. The false-alarm problem correction 68 found
    was never 2b's doing and is not 2b's to fix.

    A related precision, since correction 74 could be read the other way: the
    `calibration_ncvz_1000_v2_neartie` run was **already superseded before this
    revert**. It ran 2026-08-09 02:49→19:26, and the alias-ladder commits
    (`ffb9f69`, `edc10aa`) landed at 20:58 and 21:39 the same evening, moving
    the kernel again. `tests/test_config.py` already records this. The
    calibration certified nothing — 4 of 6 gates failed — so nothing was lost,
    but it describes neither the code that preceded this revert nor the code
    that follows it.

    526 passed.

77. **v3 landed. Its false-alarm prediction was exact, its completeness
    prediction was wrong, and the reason is that v3 vs v2 never isolated the 2b
    revert — the alias ladder is in between them and is destroying exact
    recoveries.** Run: `results/p5/calibration_ncvz_1000_v3_no_harmonic_veto`,
    finished 2026-08-11, certifying `sig1:ee80aa82…` / `kernel1:753fd6ef…`.
    `release_gate_passes: false`, 945 stars, 55 errors.

    | gate | v2 | v3 | predicted | passes |
    |---|---:|---:|---:|---|
    | `inverted_survivor_rate` | 0.003148 (3) | **0.004233 (4)** | 4 events | no |
    | `scrambled_survivor_rate` | 0.001049 (1) | **0.002116 (2)** | 2 events | no |
    | `t3_pass_rate` | 0.001049 | 0.001058 | — | no |
    | `epoch_enrichment` | 5.026564 | 4.911608 | — | no |
    | `median_recovered_depth_bias` | 0.037130 | 0.037118 | — | yes |
    | `edge_recovery_gap` | 0.000000 | −0.003371 | — | yes |

    The recorded prediction — 4 inverted and 2 scrambled events, all four gates
    still failing — **is exactly what happened**. The completeness half was
    wrong: predicted promotion 0.07865 → 0.08090, actual **0.07978**; predicted
    raw unchanged, actual 0.18427 → **0.18034**.

    **Why the completeness prediction failed: the baseline was confounded and
    correction 76 said so without carrying it forward.** Three kernel changes
    separate v2 from v3, not one — `ffb9f69` (the alias ladder called for the
    first time, having been written at P2 and never run), `edc10aa` (its 3×
    blind spot closed), and `f0e180f` (the 2b revert). v2 ran 08-09 02:49→19:26;
    the two ladder commits landed at 20:58 and 21:39 that evening. **No
    conclusion about the 2b revert's completeness cost can be drawn from this
    pair**, and the row-level arithmetic in correction 74 remains the only clean
    measurement of it.

    **The ladder dominates the diff, and the signature is unmistakable.** Of
    3,560 shared injection rows, 933 changed their recovered period. **792 of
    those 933 (84.9%) are an exact 3× ratio**, v2's period over v3's, with a
    further 47 at 2×. That is `edc10aa`'s 3× fix acting, not a veto that only
    ever wrote a rejection reason. Rows whose recovered period sits on a
    spacecraft harmonic barely moved at all: 44 in v2, 43 in v3.

    **The finding that matters, and it is not a good one. The alias ladder is
    converting exact recoveries into harmonic misses.** Eight injections went
    `recovered` True → False, and **all eight were `period_status: exact` in v2**:

    | injected P | v2 recovered | v3 recovered | v3 status |
    |---:|---:|---:|---|
    | 7.95271 | 7.9500 | 11.9230 (×1.5) | `miss` |
    | 5.88798 | 5.8865 | 8.8357 (×1.5) | `miss` |
    | 1.25743 | 1.2575 | 3.7723 (×3) | `harmonic_alias` |
    | 1.25743 | 1.2576 | 3.7725 (×3) | `harmonic_alias` |
    | 1.25743 | 1.2575 | 0.6288 (÷2) | `harmonic_alias` |
    | 1.25743 | 1.2573 | 0.6287 (÷2) | `harmonic_alias` |
    | 0.50000 | 0.5000 | 1.0001 (×2) | `harmonic_alias` |
    | 0.50000 | 0.5000 | 0.7500 (×1.5) | `miss` |

    A search that had the right answer was moved off it. `period_recovered` did
    improve elsewhere (8 gained against 3 lost), so the ladder is not simply
    broken — but `recovered`, which requires the correct alias *and* the depth,
    went 656 → 648 with **no row moving the other way**. **The alias ladder has
    never been calibrated**: it was called for the first time after v2 ran, and
    v3 is the first calibration that contains it. It should be priced on its own
    before it is trusted, exactly as decision 2b was.

    **Two mechanical notes.** 55 errors against v2's 47: the 47 permanently
    unavailable are all present, and the 8 extra are the throttling signature —
    they succeeded in v2, and this run used 12 workers and 4 download workers
    after the run was restarted for speed. **None of the 8 is an injection
    star**, so they cost denominator (945 vs 953) but not completeness. And
    `--trusted-first-pass` remains unsatisfiable: this signature now has a
    calibration, and it fails four gates.

78. **Alias ladder disabled pending its own calibration, and v4 is set up to be
    the clean A/B that v3 could not be.** Owner call, 2026-08-11, on correction
    77. `adjudicate_alias_ladder` goes **True → False**. Config hash
    `d68bccde…` → **`b41c1367…`**, kernel `kernel1:093e815b…` — a fifth
    identity, with no calibration at all until v4 runs.

    **Disabled, not deleted, and not judged wrong.** The ladder exists for a
    real failure: 31 of 341 known planets were recovered at exactly one third of
    their true period and none scored as recovered — 45% of all recovery
    failures, against machinery that already existed and was never called. In v3
    it also *gained* 8 `period_recovered` against 3 lost. The verdict here is
    narrower and only about evidence: **it has never been priced, and it was
    switched on inside a change that was measuring something else.**

    **What v4 buys that v3 could not.** v3 differed from v2 by three kernel
    changes at once — the ladder being called (`ffb9f69`), its 3× fix
    (`edc10aa`), and the 2b revert (`f0e180f`) — so nothing in that pair
    attributes cleanly. v4 differs from v3 by **exactly one flag**. Whatever
    moves between them is the alias ladder and nothing else. That is the
    experiment correction 77 asked for, and it costs one calibration rather than
    an argument.

    **The prediction, recorded before the fact.** If correction 77's reading is
    right, v4 should recover the 8 exact recoveries the ladder destroyed:
    `recovered` 648 → **656**, raw completeness 0.18034 → **0.18427**, and the
    933-row period churn should vanish, since with the ladder off the reported
    period is whatever the BLS grid found. The false-alarm gates should barely
    move — the ladder adjudicates periods, it does not gate survivors — so
    inverted should stay near 4 events and scrambled near 2, and **all four
    failing gates should still fail**. If completeness does *not* recover, the
    ladder was not the cause and correction 77 overread an association.

    Standing caution, now demonstrated twice: **do not batch kernel changes**.
    Decisions 1 and 2b were batched deliberately to share one re-calibration,
    and the ladder landed on top before that calibration had even been read.
    The saving was one 20-hour run; the cost was that two of the three changes
    can no longer be attributed at all.

79. **v4 landed and the A/B is clean: the alias ladder caused the entire
    completeness loss, and correction 74's arithmetic is confirmed to the row.**
    `results/p5/calibration_ncvz_1000_v4_no_alias_ladder`, finished 2026-08-11
    23:09 in 8.75 h, certifying `sig1:121fbb9a…` / `kernel1:093e815b…`. 952
    stars, 48 errors, `release_gate_passes: false`.

    **Both predictions, recorded before either run, were exact.**

    | quantity | v3 (ladder on) | v4 (ladder off) | predicted |
    |---|---:|---:|---:|
    | `recovered` | 648 | **656** | 656 |
    | `random_phase_completeness` | 0.18034 | **0.18427** | 0.18427 |
    | promotion completeness | 0.07978 | 0.08090 | — |
    | `inverted_survivor_rate` | 0.004233 (4) | 0.004202 (4) | ~4 events |
    | `scrambled_survivor_rate` | 0.002116 (2) | 0.002101 (2) | ~2 events |
    | gates passing | 2 of 6 | 2 of 6 | all four still fail |

    **The mechanism claim is settled by the churn count, not by the totals.**
    Recovered-period changes across 3,560 shared injection rows:

    | pair | changed | of which exactly 3× |
    |---|---:|---:|
    | v2 → v3 (ladder switched on) | 933 | 792 |
    | v3 → v4 (ladder switched off) | 933 | 59 |
    | **v2 → v4 (ladder never ran)** | **0** | **0** |

    Zero. With the ladder off, v4 reproduces v2's recovered period on **every one
    of 3,560 injections**. The ladder was solely responsible for the churn, and
    the search is otherwise **fully deterministic** — which also retires the
    possibility, entertained while diagnosing correction 77, that the run-to-run
    differences were non-determinism.

    **And correction 74's arithmetic is confirmed exactly.** v2 and v4 differ by
    one kernel change, the 2b veto, and across 3,560 rows the only column that
    differs is `promotion_recovered`: **4 rows, all False → True**. Not 3, not 5.
    The row-level diff that said 2b cost 4 injected planets predicted this run's
    promotion completeness a day before it existed (0.07865 → 0.08090).

    **What this does and does not license.** It licenses the ladder staying off
    until it is fixed: it is measurably destroying exact recoveries and its cost
    is now priced at 8 recoveries on this cohort. It does **not** license
    deleting it — the failure it was written for is real and unaddressed (31 of
    341 known planets recovered at exactly one third of their true period, 45% of
    all recovery failures), and it did gain 8 `period_recovered` elsewhere. The
    honest position is that the ladder solves a real problem badly, and both
    halves of that sentence are now measured.

    **`--trusted-first-pass` remains unsatisfiable.** Four gates still fail, by
    margins untouched by any of this: `epoch_enrichment` at **5.027 against a
    ceiling of 2.0** is the worst and is now the largest open scientific problem
    in the survey. Errors fell 55 → 48 against a floor of 47, so dropping
    download workers 4 → 3 recovered 7 of the 8 stars throttling had cost —
    supporting correction 77's diagnosis of that too.

## Decisions taken at the P4 close (2026-08-07)

The owner delegated the three open questions. What was decided, and what was
deliberately left alone:

1. **Status precedence: the ledger is authoritative.** Its rule is the
   principled one — the registry's stage-then-precedence fold, designed for
   exactly this — while the exporter's last-campaign-wins is an artifact of
   file write order. And in a discovery survey, keeping a survivor lead alive
   is the safer error: discarding a real signal is unrecoverable because
   nobody looks again, whereas retaining a false one costs bounded review time
   that the queue makes visible. **Decision recorded; implementation deferred**
   to a session that can verify it (correction 50).
2. **TRICERATOPS: unchanged, and the packet contract stays as it is.** The
   tempting move was to make the FPP section optional so packets could reach
   `ready`. That would manufacture a pass: §4.7 names FPP as required, and a
   packet claiming ExoFOP-grade completeness without one overclaims. The
   present behaviour — `incomplete`, with `false_positive_probability` named
   as the blocker — is already correct. Installing TRICERATOPS is the fix.
3. **The missing release row: fixed.** `scripts/finalize_p3_release.py` was
   re-run; it reads the signature from the calibration artifacts rather than
   inventing one, and `store_release_report` re-validated every gate on the
   way in. The ledger now holds `trusted
   sig1:f78342a75ab6b47d29cae14c38df62cf9a477938d1b71ab2273f26f432856017`.
4. **Signature churn: deliberately not changed.** Every detection-kernel
   module (`search`, `vetoes`, `detrend`, `detrending`, `detection`,
   `photometry`, `population`, `screening`, `campaign`, `commonmode`,
   `calibration`) is **byte-identical to the P3 calibration commit
   `36c935b`**, and `ScienceConfig` still digests to the pinned P3 value — so
   P3's calibration genuinely describes today's detection code, and correction
   39 is a defect in the *identity scheme*, not in the science. But switching
   the scheme now would change campaign signatures and immediately un-match
   the release just restored. It has to land together with a re-signed
   calibration, which is P5's entry work.

## Owner notes

- **Do not delete `data/lightkurve` yet.** Although new downloads go to
  `%LOCALAPPDATA%\exohunt\cache\lightkurve`, this 9.4 GB tree contains the
  1,902 cached SPOC Sector 100 light curves used by the offline P2 regression
  gates. It is re-downloadable, but deleting it would force a large avoidable
  MAST re-fetch before P2 exits.

- Dashboard launch path (the one true way):
  `.venv\Scripts\exohunt-dashboard.exe` — a second launch now exits
  harmlessly. Default port 8765, loopback only, unchanged.
- The ledger lives at `%LOCALAPPDATA%\exohunt\exohunt.db`; inspect with
  `exohunt ledger-status`, re-import any time with `exohunt ledger-import
  --workspace . --parity` (idempotent).
- After merging this branch, rebuild `dashboard/` once before restarting the
  installed dashboard. The live server was intentionally left on main during
  branch development.
