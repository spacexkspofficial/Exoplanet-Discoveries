# EXOHUNT master overhaul plan

Written 2026-07-27 (UTC) against branch `claude/exoplanet-discoveries-research-192dc3`
at commit `7a6f2cc`, after reading the repository, the six mandated documents, the
science modules, the campaign outputs, and the live state of the machine. This is
a plan, not an implementation. Nothing was run, changed, or downloaded beyond
read-only inspection.

**The verdict in five sentences.** The pipeline's machinery is better than its
results: the status registry, the common-mode screen, the evidence-stage model,
and the honest labelling are genuinely good and survive this overhaul. The
science strategy does not: re-running single-sector BLS over stars SPOC and QLP
already searched with better tooling is a lane in which our best possible outcome
is rediscovery, and our typical outcome — demonstrated at 12,168 stars — is
artifacts. The overhaul therefore changes *what* is searched (under-searched
faint M dwarfs with multi-sector coverage, single-transit events, residuals in
known systems) at least as much as *how*. Everything runs on one durable spine:
an append-only evidence ledger with a single-writer lease, scientific-signature
partitioning, and calibration (injection + inverted-data nulls) as a standing
gate rather than an occasional exercise. Until Phase 3 of the roadmap completes,
every result the system produces — including the current 541 "automated
survivors" — remains labelled diagnostic, because we cannot yet state a false-alarm
rate for any of it.

---

## 0. Current state, verified

Claims below were checked directly, this session, against the repository and the
running machine. Where the research review or the brief asserted something, I
re-verified rather than inherited.

### 0.1 Live observations (2026-07-27 ~01:06 UTC)

1. **The phantom-liveness problem is live right now.**
   `results/campaign/sector100_spoc/batch_progress.json` says `state: "running"`,
   24/5,000 targets complete, last updated 2026-07-27T00:19:14Z — 47 minutes
   before I looked — and **no coordinator process exists**. The only project
   processes running are two dashboard servers (one launched from `.venv`, one
   from a Codex runtime Python — itself a small demonstration that more than one
   actor starts things on this machine). The dashboard is presenting a live
   campaign panel for a process that is dead.

2. **The restart automation could not be located.** It is not among Windows
   scheduled tasks and not among Claude scheduled tasks (which contain only
   unrelated bookkeeping jobs). It has fired at least once (it produced the
   current v3 re-run inside `sector100_spoc`), so it presumably lives in the
   Codex agent's own automation, invisible from here. Consequence for design:
   we do not get to assume we know all the actors. Single-writer safety must be
   enforced by the system itself (§7.2), not by an inventory of schedulers.

3. **The mixed-version split is exactly as the brief states**: 1,864 per-target
   reports at `processed-lc-v2` and 26 at `processed-lc-v3-edge-safe` in
   `sector100_spoc`. The live checkpoint counts only 24 completed — two v3
   reports exist on disk that the last checkpoint publish never recorded. That
   is not data loss (reports are durable and rediscovered on resume) but it is
   a clean demonstration that *files are the truth and the checkpoint is a
   cache*, which is the storage model §7.3 formalizes.

4. **Storage**: 9.4 GB in `data/lightkurve` (dominated by TESScut cutouts at
   tens of MB each), 12 MB in `data/catalogs`, 2.0 GB in `results/` across 17
   campaign directories. Real headroom under the 20 GB ceiling is ~8 GB.

5. **Ledger vs dashboard**: `metrics/current_stats.json` records 12,168 unique
   targets searched, 3,939 survivor *conclusions*, 8,170 rejections, 14
   hand-vetted false positives, 0 candidates. The dashboard's 541 "automated
   survivor" count is the current-best per-star state after later tiers
   overrode earlier conclusions. Both readings are correct and both are kept
   (§1.2).

6. **Git hygiene**: the `.refactor-staging` accidental tracking flagged in
   `REFACTOR_REVIEW.md` is already resolved on this branch (zero tracked files);
   verify the same on `main` before merging anything.

### 0.2 How the research review was used

Per the brief: its literature-grounded material (product survey, evidence
taxonomy, tier ladder, completeness-and-reliability discipline) is adopted where
it survives contact with the repository. Its repository-specific claims were
re-verified: the mixed-version claim is right (and its partition-by-signature
rule is adopted, §7.4); the TOI-1431/TIC 101319448 example is fabricated and is
not used anywhere in this plan. Two of its architecture recommendations are
deliberately overridden with reasons stated: PostgreSQL (§7.1) and
25%-survivor-rate framing (the measured worst case was 47% on TESScut, 24.8% on
the partial v2 SPOC re-run; the target numbers in §1.4 are derived from our own
measurements, not its estimate).

### 0.3 What already works and is kept

To be explicit that this is an overhaul, not a rewrite-everything:

- **`status_registry.json` + `statuses.py`** — one vocabulary, explicit evidence
  stages, generated TypeScript. Kept and extended (§1.2, Appendix C).
- **`commonmode.py`** — the shared-ephemeris screen with period+phase matching
  and uniform-phase enrichment is sound and empirically sharp (12.3× vs 2.1×
  separation). Kept; generalized with an absolute-time dip registry (§3.6).
- **The evidence-stage precedence rules** — population screen outranks
  per-star science; human outcomes outrank everything. Kept verbatim.
- **`requested_author` reuse semantics, per-target durable reports, atomic
  writes, TAP semaphore, storage retention safety rails.** Kept.
- **The honest-labelling culture** (`warning` fields, "not a planet candidate"
  everywhere). Kept and enforced by claim ceilings (§4.7).
- **HANDOFF §7 "things not to undo"** — all six are preserved by this plan.

---

## 1. Scientific architecture — the tiered funnel

### 1.1 Reframe: the unit of work is (star, data-state), not star

The current model runs campaigns over target lists and is finished when the list
is finished. A 24/7 system needs a different shape: a **queue of (star,
data-state) work items**, where data-state = the set of sectors/products
available for that star at a given time. TESS adds a sector roughly every 27
days, so every star in the sample re-enters the cheap tiers when its data-state
changes; monotransit leads re-enter automatically when the sector that could
confirm them lands. "Campaign" survives only as a reporting label (a batch of
work items sharing one scientific signature), not as the unit of execution.

Two consequences worth stating plainly:

- The **first pass** over a new sample is the only time the system runs at
  thousands of stars/day. Steady state is event-driven and much cheaper:
  ~1/13 of the sky refreshes per month, plus promoted-tier work.
- **Rejection is per (signal, data-state)**, so most rejections are naturally
  reversible: a new sector triggers re-evaluation without any human deciding to
  "reopen" anything.

### 1.2 The two ledgers, formalized

The brief requires both readings preserved. The design:

- **Evidence ledger (append-only)**: every tier execution writes an immutable
  evidence record — target, tier, inputs (product IDs + versions), scientific
  signature, verdict, numeric payload, artifact hashes, timestamps. Records are
  never edited or deleted; supersession is expressed by later records. This is
  the successor of `metrics/events.jsonl` and the per-target JSONs.
- **State projection (derived)**: current-best status per star = fold of that
  star's records through the existing `resolve_status()` stage/precedence
  rules. Rebuildable from the ledger at any time; stored as a table for query
  speed. The dashboard reads only projections.

"3,939 survivor conclusions logged" (ledger count) and "541 stars currently in
survivor state" (projection count) are then two labelled queries over one store,
and can never silently diverge, because one is defined as a function of the
other.

### 1.3 The tiers

Cost figures are per star·sector unless noted; they assume the compact FFI
products of §2.1 (0.3–2 MB per star·sector), not TESScut cutouts (tens of MB —
banned outside T6). "Kill" lists what the tier may conclude; "may never
conclude" is enforced by the claim ceilings in §4.7.

| Tier | Name | Cost/star | Data needed | May decide | May never conclude | Reversible? |
|---|---|---|---|---|---|---|
| T0 | Identity & preflight | ~0.1 s CPU, no network (bulk snapshots) | Catalog snapshots (§4.1) | Route to lane; `no_usable_data`; record identity + stellar params + crowding prior | Anything astrophysical | Yes (re-run on snapshot refresh) |
| T1 | Photometry & preparation | 0.3–2 MB download + ~1–2 s CPU | One preferred product (§2.1) | `no_usable_data` (quality); produce 2 prepared fluxes (§2.2) | Anything about planets | Yes (products re-downloadable) |
| T2 | Search | 2 s (BLS) – ~2 min (TLS multi-sector) CPU | Prepared flux | `no_significant_signal`; emit top-k signals with diagnostics | That a star is planet-free; that a signal is a planet | Yes — auto re-queued on new sector or new signature |
| T3 | Cheap vetoes | <0.5 s CPU | Signal + prepared flux + stellar params | Kill a *signal* as artifact-shaped / EB-shaped / non-physical, with named gate | That the *star* has no planet; that a survivor is a candidate | Yes — signal recorded in full; gates re-run under new config |
| T4 | Population screens | <1 s per 10k stars (batch) | All fitted ephemerides in the cohort + dip registry (§3.6) | `common_mode_systematic`, `localized_coincidence` | That an unshared signal is astrophysical | Yes — pure post-processing, re-run any time |
| T5 | Catalog adjudication | ~0 (snapshots) + seconds live for survivors only | Identity graph + snapshots + live SIMBAD/ExoFOP for survivors | `known_planet/TOI/TCE/EB rediscovery`, `known_host_new_signal`, variability/crowding review lanes, `unresolved` | Novelty. Absence from catalogs changes nothing upward | Yes — re-run on every snapshot refresh (statuses can *improve or worsen*) |
| T6 | Pixel localization | 1 TPF/cutout download (tens of MB, fetch→measure→delete) + ~1 min CPU | Pixels for in/out difference imaging | `pixel_offset_contamination`; host reassignment to a neighbour; on-target within tolerance | That on-target = planet | Partially — verdict re-derivable while pixels are re-fetchable |
| T7 | Independent reduction & coherence | 2–4 extra product downloads + minutes CPU | ≥2 independent reductions, all sectors | `single_sector_unconfirmed`, `reduction_dependent_artifact`, `science_vetted_lead` | Confirmation of any kind | Yes |
| T8 | Candidate packet & FPP | ~10–60 min CPU (fits, TRICERATOPS) | Everything above + Gaia scene | `packet_ready_for_review` or named deficiency | "Validated", "confirmed", or any public claim | Yes |
| T9 | Human review & submission queue | Human | Rendered evidence bundle | `vetted_candidate` (human), `false_positive` (human), CTOI submission (human) | — | Human outcomes outrank all automation, as today |

Per-tier notes a literal implementer needs:

- **T0** routes; it never kills for science reasons. Its outputs (canonical
  identity, stellar radius/mass/density, contamination ratio, variability
  prior, sector coverage, product availability) are stamped into the work item
  so later tiers never re-query.
- **T2 emits top-k (k=3) phase-separated peaks** plus the harmonic ladder
  diagnostics, not only argmax. Rationale: the strongest peak is frequently the
  systematic; killing it must not cost the second peak. This directly addresses
  "a stronger false signal hides a weaker planet" from DETECTION_LIMITS.md at
  screening cost, not full-residual-search cost.
- **T3's gate list** (all thresholds named in Appendix A, all recorded in the
  evidence record): red-noise-adjusted S/N; ≥3 transits multi-sector (2 in
  single-sector routes to `needs_additional_sector`, not survivor); per-event
  support (each counted transit needs ≥N in-transit cadences and two-sided
  local baseline — the existing single-event checks, applied per event);
  odd/even (model-fit version, §3.5); secondary scan over *all* phases, not
  just 0.5; duration-vs-stellar-density consistency (§3.5 — the cheapest
  powerful test we currently don't run); depth physicality vs stellar radius
  (implied companion > 2 R_Jup → EB lane, not survivor); duty cycle; grid-rail
  demotion (any parameter at a grid boundary); gap/dump adjacency of events;
  quality-flag coincidence fraction.
- **T5 has a lane the current system lacks**: `known_host_new_signal`. Today a
  period match to a catalogued planet ends the story as rediscovery. The right
  behaviour: mask the catalogued ephemeris (the `mask_periodic_events`
  machinery already exists) and re-search the residual. Known transiting-planet
  hosts are the single most planet-rich population we have access to, and TTV
  systems aside, official pipelines have already skimmed them — but the masked
  re-search across *all* sectors with our multi-sector stitching is cheap and
  occasionally finds the second planet. Bounded: only exact-host matches, only
  after the primary lane is healthy.
- **T6** must do the **neighbour-transit test**, not only the centroid: extract
  per-neighbour light curves from the same cutout (Gaia positions), fit the
  candidate ephemeris in each, and reassign the host if a neighbour's depth is
  greater. Centroids alone at Tmag 13–15 have arcsecond-level noise; the
  neighbour test is decisive more often.
- **T8's ceiling claim** is `packet_ready_for_review`. The system never
  autonomously produces the words "candidate", "validated", or "discovered".

### 1.4 Funnel arithmetic — is "5,000/day then narrower" the right shape?

The shape is right; the number is the wrong thing to fix first, and it is not
CPU-bound. Measured/derived budget on this machine:

- 5,000 stars/day = 17.3 s/star serial budget; with 10–15 analysis workers the
  CPU budget is minutes/star — TLS multi-sector fits comfortably.
- The binding constraints are **archive politeness** (2–3 concurrent MAST
  downloads, which at 0.3–2 MB/star·sector supports roughly 3,000–8,000
  star·sectors/day) and **cache churn** (6 GB rolling cache outside OneDrive,
  §7.6, turns over ~1–2×/day at that rate — fine).
- So: **first-pass bursts of 3,000–5,000 star·sectors/day are realistic;
  steady state is ~500–1,500/day** of new-sector re-checks plus promoted work.
  Size the queues for the burst, the storage for the steady state.

Design attrition targets (these are *targets to calibrate against*, per §5 —
not claims):

| Transition | Target | Grounding |
|---|---|---|
| T2 flags a signal | 5–15% of star·sectors | Current strongest-signal-always model replaced by SDE threshold; measured on nulls |
| Past T3 | **≤1–2%** of searched | 47% (TESScut) and 24.8% (v2 SPOC partial) were instrument-dominated; a believable blind rate is well under 5% (HANDOFF), and injection-calibrated gates should land ≤2% |
| T4 kills | <2% of fitted ephemerides flagged | 46.6% on TESScut, 15.1% on the partial SPOC re-run, target after §2.2 detrending: low single digits |
| Past T5 (unresolved) | ~0.3–0.5% of searched | Today's catalog stage resolves ~⅔ of survivors into known/variable/crowding lanes |
| Past T6+T7 | ~0.05–0.1% of searched | Kepler/TESS experience: background EBs dominate this stage |
| Reach T8 packet | O(1–10) per 10,000 stars | Depends on lane quality (§6.4); measured, not promised |
| Human queue | ≤5 items/week sustained | A queue growing faster than review capacity is an alarm (§8.4) |

If T3 passes more than ~2% for a week, or the inverted-data run (§5.2) yields
more than ~0.1% survivors, the funnel is broken and the scheduler automatically
suspends promotions (§8.4) — the numeric embodiment of "this pipeline's failure
mode is confidently reporting artifacts."

---

## 2. Photometry and detrending

### 2.1 What to search, per star — the product matrix

Decision rule (first match wins), recorded per target in the evidence record:

| Priority | Product | Use when | Why | Cautions |
|---|---|---|---|---|
| 1 | **SPOC 2-min PDCSAP** | Target has 2-min coverage | Best systematics correction (CBV/MAP), 100% availability on our old samples was already demonstrated | Bright-lane only by sample construction; PDC can clip deep/long events (rare at our depths) |
| 2 | **TESS-SPOC (FFI) PDCSAP** | FFI target in the TESS-SPOC target list | SPOC-grade calibration at 10-min/200-s cadence; DV products exist for cross-check | Covers ~160k selected stars/sector, *not* everything — absence is a selection fact, not a data fact |
| 3 | **QLP SAP (searched with our detrend), KSPSAP as cross-check** | Everything else to Tmag ≈ 15 | Broadest FFI coverage; small files | QLP's own detrending (KSPSAP) can erode long/shallow events — search our detrend of their SAP, use theirs as the T7 cross-check |
| 4 | **TGLC (PSF flux)** | Crowded fields (contamination ratio or Gaia neighbour density above threshold), faint targets where QLP quality is poor | Gaia-prior forward modelling is the only real answer to blends at TESS pixel scale | Slower to fetch; treat PSF and aperture flux as two related-but-distinct reductions |
| — | **eleanor / GSFC-eleanor-lite, CDIPS, PATHOS, TASOC** | T7 comparisons and special populations (clusters, young stars, oscillators) only | Independent methodologies useful as disagreement probes | Never the discovery product |
| — | **Own TESScut extraction** | **T6 pixel work only, and forensics** | We need cutouts for difference imaging anyway | **Banned as a search input.** This is the reduction that manufactured the 2023–26 artifact catalog; `--author TESScut` searches remain possible only behind an explicit `--forensic` flag |

Multi-sector rule: stitch per-product across sectors (never across products);
per-sector medians normalized independently before stitching (current
behaviour, kept); sector boundaries are always segment boundaries.

### 2.2 Detrending: replace the Savitzky–Golay default

The SavGol flatten was the proximate source of the residual shared epochs, and
the edge-safe guard that fixed it costs 33% of cadences. Replace the default;
keep the machinery.

**New default: time-windowed biweight (wotan), two prepared fluxes per star:**

- **Short-search flux**: biweight, window = 3× the longest searched duration
  (duration grid from stellar density, §3.2 — typically window ≈ 0.75–1.0 d),
  segmented at gaps ≥ 0.1 d exactly as `detrending.py` does today. Input to the
  periodic search (T2).
- **Long-event flux**: biweight, window = 3.0 d. Input to the monotransit
  detector (§3.4) and to duration-suspicious re-checks. Costs one extra pass
  over the same array — negligible.

**Edge handling — recovering the 33% honestly.** Replace the hard half-window
guard with **support-weighted de-weighting**: for each cadence, compute the
fraction *f* of its trend window actually populated with samples (f = 1 deep
inside a segment, → 0.5 at a clean edge, lower beside ragged gaps). Cadences
with f below a floor (0.4) are dropped; cadences between floor and 1 are kept
with their BLS/TLS uncertainty inflated by 1/f^α (α calibrated on the
regression set, start at 1). The search then *sees* edge cadences but cannot be
driven by them. Expected retention ≥ 85% (from ~67% today).

This is a hypothesis with a measurement, per the ground rules — acceptance
criteria in §2.3. If de-weighting fails the artifact regression, the fallback
is a narrower hard guard (0.25 window instead of 0.5), which alone returns
roughly half the loss.

**Escalation for active stars** (T1 flags from Gaia variability class, measured
rotation < 3 d, or photometric RMS above threshold): notch/biweight with
shorter window plus an explicit transit-masked second pass — i.e. after T2
finds a signal, mask it, re-detrend, re-measure depth; a depth that moves >20%
between passes is flagged `detrend_sensitive`. Full GP detrending (celerite2
SHO) is reserved for T7/T8 on promoted signals only; it is not a first-pass
tool on this hardware.

**Not doing, with reasons:** re-deriving our own CBVs (that is SPOC/QLP's job;
we consume their corrected products); PLD at first-pass scale (needs pixels —
banned at scale); "detrend once with the single best method" (the T7
requirement is that a promoted signal survives ≥2 independent preparations,
because *no single detrender may define the candidate population* — the one
research-review principle this plan adopts word-for-word).

### 2.3 Acceptance measurements for the detrending change

> **Amended 2026-08-05, owner-delegated.** The four numbers below were written
> before any of them had been measured directly. Three campaigns and the
> edge-bias instrument have since measured all of them, and two of the four are
> now known to be unusable as written: criterion 1 cannot discriminate, and
> criteria 2 and 3 are *jointly unsatisfiable* for shallow transits at any
> guard width. The amended set is below; the original is kept beneath it
> because the reasoning only makes sense against what it replaced. See
> PROGRESS corrections 23–29 and `P2_EDGE_BIAS.md`.

**Amended acceptance set.**

1. **Artifact regression — demoted to a diagnostic, not a gate.** Enrichment
   is a 0.41σ statistic between arms (correction 24), and the historical
   epochs it was measured at return 1.011 (p=0.37) on a neutral cohort — they
   measure nothing (correction 25). It is still computed and reported; it no
   longer blocks. *Survivors sitting on derived artifact epochs* is the number
   with resolving power and is what a reviewer should read.
2. **Retention and depth bias are one criterion, stated per depth regime.**
   Measured directly (`P2_EDGE_BIAS.md`): edge trend bias is ~89% non-variance
   at every offset, and the guard width needed follows from the shallowest
   depth a lane claims:

   | shallowest claimed depth | required guard | retention |
   |---:|---:|---:|
   | ≥ 4,000 ppm | ~300 cadences | ~0.86 |
   | ~2,000 ppm | ~626 cadences | ~0.71 |

   A lane passes when its guard is sized for its own shallowest claim. The
   flat ≥85% retention target applies only to the deep lane; requiring it of a
   shallow lane demands trend bias below what the estimator can deliver.
3. **Injection recovery** — unchanged, and still unspent. Recovered-depth bias
   ≤ 5% median, recovery rate within 3% of the interior rate at equal depth.
   This moves to P3, where the injection framework is built anyway.
4. **Population check** — unchanged: locked 500-target diagnostic re-run,
   per-epoch histogram enrichment < 2× everywhere, T3-pass rate in the §1.4
   band.

**Consequence for §2.2.** No detrending replacement ships. Support-weighted
biweight (correction 10) and the quarter-window/event-support lane (correction
13) are rejected on measurement, and the direct bias measurement explains why
no third parameterisation of the same idea would work either. Production keeps
Savitzky–Golay with the half-window guard, and the measurement now *justifies*
that choice rather than merely inheriting it: 720 cadences holds edge bias to
~102 ppm, which is 5% of ~2,000 ppm — close to the shallowest depth this
pipeline claims. The 33% cadence cost is earned, not wasted.

<details><summary>Original criteria, superseded</summary>

The change is a behaviour change and ships only when all four numbers pass, on
the pinned sets, in one report:

1. **Artifact regression**: the 14 real light curves from the edge-safe work —
   0 of 14 may detect at the artifact epochs (BTJD 4074.4, 4080.8) above gate.
2. **Retention**: ≥ 85% of cadences kept (vs 67% today), measured over the
   same 14 plus a 100-star random subset.
3. **Injection recovery**: box + limb-darkened injections at P ∈ {1, 3, 7, 12} d
   spanning segment edges: recovered-depth bias ≤ 5% median, and recovery rate
   within 3% of the interior-transit rate at equal depth (i.e., edges no longer
   destroy sensitivity — this is the number that proves we got the 33% back
   rather than just re-admitting noise).
4. **Population check**: a locked 500-target diagnostic re-run (same list, new
   signature): per-epoch histogram enrichment < 2× everywhere; T3-pass rate
   within the §1.4 band.

</details>

---

## 3. Detection

### 3.1 BLS stays the screen; TLS becomes the decider

- **BLS** (existing, with §3.2 grid fixes) runs on every (star, data-state):
  cheap, well-understood, produces the top-k peaks and the SDE-like statistic.
- **TLS** runs when BLS shows anything (best SDE ≥ 6) *and* always in the
  faint-M lane (its limb-darkened template is worth ~5–10% S/N exactly in the
  shallow regime that lane lives in). TLS's SDE and its shape diagnostics
  (transit vs. V-shape, per-transit depths) feed T3.
- The current `depth/depth_err` white-noise S/N is demoted to a diagnostic. The
  promotion statistic is **SDE plus the red-noise-adjusted S/N** already
  computed in `signal_vetting_diagnostics` (that machinery is good; it becomes
  gating rather than advisory).

### 3.2 Grid design — killing the rails

Evidence of the problem: 4,401 duration-rail fits; modal duration exactly 6.0 h
(grid top); pile-up at the period ceiling; `--max-period 13` truncating the
13.70 d peak into a false 12–13 d population.

- **Duration grid from stellar density**, per star: expected b=0 duration
  T₀(P) from Gaia/TIC density; grid spans [0.3, 1.5]×T₀ in ~8 log steps,
  clamped to [0.5 h, 12 h]. No star searches durations it cannot physically
  produce; the 6-hour rail disappears as a *class*.
- **Period grid**: min 0.5 d; max = baseline/3 (≥3 transits required for a
  periodic claim; the 2-transit case is handled as `needs_additional_sector`,
  §1.3). Optimal frequency sampling (A_ofac from the shortest duration), which
  `search_transits` already approximates with its bounded `frequency_factor`.
- **Overscan trick**: the grid extends 8% past the *reporting* ceiling; any
  best fit in the overscan region is reported as `ceiling_zone_diagnostic`,
  never as a survivor. Rails cannot accumulate at the reporting boundary
  because the reporting boundary is not a grid boundary.
- **Rail demotion is a hard gate**: best period or duration on the first/last
  grid point → automatic T3 kill with reason `grid_rail` (reversible, like all
  T3 kills). Today this is a caution; the 4,401 measured cases say it should
  be a verdict on the *fit* (not the star).

### 3.3 Alias and harmonic policy

TOI-700 c (recovered at exactly P/2) is the standing lesson. For every T2
signal: evaluate the fold at {P/3, P/2, 2P/3, P, 3P/2, 2P, 3P} within the data
span; pick the ephemeris by folded-model χ² *plus* odd/even consistency at each
alias (a true P reported at P/2 shows alternating depths); record the decision
and the runner-up. When a later sector arrives, the alias decision re-runs
automatically (it is part of T2, which re-runs on data-state change). The
existing `harmonic_diagnostics` is 80% of the code needed.

### 3.4 The monotransit detector (separate detector, separate statistics)

SPOC requires ≥2 transits; QLP likewise. Single events in FFI data are a real
structural gap and the strongest scientific argument for a 24/7 system: leads
are re-checked automatically as sectors land.

- Detector: matched filter bank of limb-darkened single-transit templates,
  durations 1.5–24 h, on the long-event flux; per-point uncertainty from local
  robust scatter; detection = peak significance ≥ 8σ (calibrate on inverted
  data, target ≤ 0.3 false events/star at first pass).
- Immediate cheap vetoes (all exist today in the single-event checks, kept):
  two-sided local baseline; not adjacent to a gap/dump/quality-flag block;
  event shape not V-shaped beyond tolerance; depth physical for the star.
- Every survivor gets: stellar-density duration→period posterior
  (Seager–Mallén-Ornelas inversion) with the predicted next-transit windows,
  pixel-level neighbour test (T6 is mandatory before human review for
  monotransits — asteroid crossings and background EBs dominate), and a
  standing re-check subscription on future sectors.
- Claim ceiling: `single_event_lead`, never a period, never a candidate, until
  a second event or an external ephemeris lands.

### 3.5 Specific upgrades to existing vetoes

- **Odd/even**: replace median-of-per-event-depths (needs ≥2+≥2 events, often
  returns None exactly when it matters) with a two-depth model fit on the
  folded curve (all odd points vs all even points, one depth each, shared
  shape). Works at 3+1 events; propagates uncertainties.
- **Secondary**: scan the *full* out-of-transit phase for the strongest dip
  (eccentric EBs put secondaries far from phase 0.5, which is the only place
  the current screen looks), report its phase, depth, and significance. The
  TIC 181014443 lesson — a 2.3σ single-sector secondary that was 5.9σ in the
  stack — becomes a stacked test at T7: secondary significance is re-measured
  on the all-sector fold before any promotion.
- **Duration–density consistency** (new, cheap, powerful): compare fitted
  duration against T₀(P, ρ*) from Gaia density; outside [0.4, 1.5] → flag
  (giant host, blend, or junk fit); outside [0.25, 2.5] → kill. Catches giants
  and blends single-sector, before any pixel download.
- **Depth physicality**: depth → implied companion radius via stellar radius;
  > 2 R_Jup → EB lane (not "survivor with a big planet").

### 3.6 Generalizing the population screen: the absolute-time dip registry

`commonmode.py` catches shared *ephemerides*. Add its absolute-time
counterpart, built during every campaign: per sector-camera-CCD, aggregate
robust per-cadence z-scores across all searched stars into a shared-dip
time series; any absolute-time bin where >X% of stars dip together is a
registered systematic window. T3 then vetoes any *individual transit event*
whose center falls in a registered window (with the window list versioned like
any catalog snapshot). This catches single-epoch artifacts *before* they alias
into periods — upstream of where the common-mode screen catches them today —
and produces, as a free by-product, the empirical momentum-dump/scattered-light
map per sector that the research review wanted from external documentation.

### 3.7 Explicitly not doing (with reasons)

- **TTV-tolerant periodic searches (QATS-family)** at survey scale: cost and
  false-alarm behaviour don't fit a first-pass funnel; revisit inside the EB
  lane (§6.3) only.
- **ML triage (Astronet/ExoMiner-style)**: not until the physics funnel is
  calibrated; when added, its score is a *ranking* input to the human queue and
  never a gate (risk register: "ML score abused as proof").
- **GPU/deep-learning detrending**: no.

---

## 4. Vetting and catalog cross-matching

The design principle: **every catalog claim is (source, version, match-basis,
confidence), and absence is only ever "no match in checked sources at these
versions".** Concretely, evidence records store the snapshot hash they were
adjudicated against, so "re-vet the world against new catalogs" is a routine
batch job (T5 re-runs), not an event.

### 4.1 Identity graph and snapshot infrastructure

- **Canonical node**: (TIC ID, Gaia DR3 source_id) pair resolved once at T0 via
  the TIC's own cross-match, stored with provenance. TIC duplicate/artifact
  flags respected. Every other identifier (2MASS, KIC/EPIC, TOI, common name)
  is an edge with source + confidence + retrieved-at.
- **Proper-motion-aware positional matching**: all cone matches against
  epoch-specific catalogs propagate Gaia PM to the catalog's epoch. At our
  magnitudes PM errors are negligible; the win is against 2MASS/older-survey
  mismatches for high-PM M dwarfs — precisely our primary lane.
- **Ambiguity is preserved**: when >1 plausible counterpart exists inside a
  TESS pixel, the node records all of them ranked; T6's neighbour test consumes
  that list. No forced unique identity.
- **Snapshots, not firehoses**: nightly/weekly bulk snapshots (versioned,
  hashed, pruned to last 3) of: TOI+CTOI table, NASA confirmed planets (`ps`
  default rows), per-sector SPOC TCE and TESS-SPOC FFI TCE bulk CSVs, Villanova
  TESS EB catalog, Gaia DR3 per-sample extracts (variability summary, EB table,
  NSS two-body, RUWE/astrometric quality — one bulk TAP job per target list,
  not per star), VSX for the sample footprint, ASAS-SN variables catalog,
  Kepler/K2 candidate+EB lists for overlap regions. Live per-target queries
  remain only for T5 survivors: SIMBAD, ExoFOP target detail, ZTF/ASAS-SN
  light-curve fetches.
- **Known-object regression suite** (the research review's soundest
  recommendation, adopted): ~500 curated objects — TOIs across
  magnitude/period/sector, confirmed planets, VSX and Villanova EBs, Gaia NSS
  binaries, known blends, plus deliberate near-miss impostors (neighbours of
  TOIs, aliased periods). Every identity/catalog code change must resolve the
  whole suite correctly before merge. This is how we find real resolution
  failures instead of citing fabricated ones.

### 4.2 Source matrix — what each source can and cannot settle

| Source | Settles | Can never settle | Cadence | Failure semantics |
|---|---|---|---|---|
| NASA Exoplanet Archive `ps` (confirmed) | "This ephemeris is a known planet" (with epoch+period match) | Anything about *new* signals on the same star | Weekly snapshot | Living table; dispositions change — snapshot hash recorded per adjudication |
| TOI/CTOI table | "Already a community/project candidate"; TFOPWG `FP` disposition kills the matching ephemeris | `PC` ≠ planet; absence ≠ novel | Daily snapshot | Same |
| SPOC / TESS-SPOC TCE bulk lists | "The official pipeline saw this threshold crossing" — powerful *deprioritizer* | A TCE is not an astrophysical classification | Per data release | Absence for FFI-only faint stars is expected, means nothing |
| SPOC DV reports (per-TCE) | Independent centroid/odd-even/secondary measurements for the same signal | — | On demand for matches | If SPOC's DV already localized the signal off-target, we inherit that as `measured_science` evidence rather than re-deriving |
| Villanova TESS EB | Host is a catalogued EB; period/alias match kills as EB rediscovery | Host EB-ness does not explain a *different* residual period (→ residual lane, exists today) | Monthly mirror | Incompleteness outside covered sectors |
| Gaia DR3: NSS, `vari_*`, RUWE/AEN | Binarity/variability *priors*; an NSS orbit matching the candidate period is a kill; RUWE>1.4 raises the blend prior | High RUWE alone never kills (planet hosts have binaries) | Static release; per-sample extract at list build | Versioned; the planetary-transit table's known correction history is exactly why snapshot versions are recorded |
| Gaia parallax+photometry | Stellar radius/density for §3.5 gates; giant-host detection | — | Static | — |
| SIMBAD | Identifier resolution, object-type context, literature pointers | Population truth (it is a bibliography, not a catalog) | Live, survivors only | Miss ≠ absence of literature |
| VSX | Known variable classifications incl. many ground EBs | Absence (heterogeneous coverage) | Footprint snapshot | — |
| ExoFOP-TESS | Existing follow-up state: someone already observing this ⇒ deprioritize; imaging/spectroscopy uploads inform T8 | Community content is uneven; presence of files ≠ conclusions | Live for survivors; TOI/CTOI CSVs daily | — |
| ZTF light curves (dec ≳ −28°) | EB unmasking at 1″ resolution — the *neighbour* eclipsing at 2× or 1× the period; rotation periods | Shallow-transit confirmation (precision insufficient) | Live fetch, survivors only | Southern targets: not covered — recorded as `not_applicable`, never as a pass |
| ASAS-SN | All-sky bright-variable context to g≈18; deep EB eclipses | Anything at mmag depth | Catalog snapshot + live fetch | Cadence too sparse to be decisive alone |
| ATLAS forced photometry | Special-case long-baseline checks | — | Manual, rare | Explicitly rate-limited use |
| Kepler/K2 archives | Decisive history where fields overlap | Nothing outside their footprints | On demand | — |
| HST/JWST program metadata | "Someone considered this target worth telescope time" — context only | **Never evidence of a planet** (current context.py already says this; kept) | On demand | — |

### 4.3 Matching semantics

- **Ephemeris matching requires period AND epoch.** The current TCE and TOI
  matching (`tce.py:104`, `evidence.py:384`) accepts a period-ratio match at
  1% with no epoch test; at common EB periods (0.5–3 d) that produces false
  "known" verdicts, and a false *known* is as damaging as a false *novel* (it
  silently discards a genuine new signal). Fix: project the catalog epoch to
  the observed window; require phase agreement within max(0.5 duration, period
  ×1%·N_cycles drift allowance); alias families {⅓, ½, 1, 2, 3} checked with
  the same rule; report `period_only_match` as a distinct, weaker relation.
- **Host match ≠ signal match.** Every catalog verdict states which it matched.
  Host-level matches route (EB-host residual lane, known-planet-host
  new-signal lane); only signal-level matches kill.
- **Disagreement policy**: sources are ranked per claim type (planet-ness:
  ps > TOI > TCE; EB-ness: Villanova/Gaia NSS/VSX + our own secondary
  measurement). A conflict (e.g., TOI says PC, Gaia NSS says SB1 at the same
  period) never auto-resolves: the record carries both, status takes the more
  conservative lane, and the conflict surfaces in the review queue.
- **Absence handling**: `catalog_coverage_gap` (a checked source does not cover
  this sector/star) stays distinct from `no_match` (covered and absent) —
  the registry already distinguishes these; T5 must populate them from the
  per-source `status` fields it already receives.

### 4.4 Pixel vetting v2 (T6)

Current: threshold-mask difference imaging + centroid against 21″. Upgrades,
in order of value:

1. **Neighbour-transit extraction** (§1.3 note) — decisive host reassignment.
2. **Per-sector difference images with consistency**: centroids that wander
   sector-to-sector are a blend signature even when each is within tolerance.
3. **Aperture-growth depth curve**: depth rising with aperture radius ⇒
   contaminating neighbour; falling ⇒ on-target. Three apertures suffice.
4. Localization verdicts carry uncertainties (bootstrap over in/out cadence
   selection), not just a pixel distance.

PRF-fit centroiding is a stretch goal; the three above are cheap and cover most
of its value at our magnitudes.

### 4.5 Independent reductions (T7)

Promotion to `science_vetted_lead` requires, on the fixed ephemeris:

- Depth consistent (within 3σ) in ≥2 independent reductions (product matrix
  §2.1, e.g. our-detrend-of-QLP-SAP vs TESS-SPOC PDCSAP vs TGLC), and
- present in the *undetrended* SAP fold (guards against any detrender creating
  it — the direct lesson of this project's history), and
- supported in every sector where injection says it should be detectable
  (a sector where completeness at that depth is <50% cannot vote against), and
- stacked secondary/odd-even re-measured on the all-sector fold (TIC 181014443
  rule).

Common-mode verdicts still outrank all of this, as today (HANDOFF §7 preserved:
sector coherence cannot clear a shared-ephemeris kill).

### 4.6 Statistical context (T8)

- Full transit fit (batman + emcee; lightweight, Windows-friendly) with limb
  darkening from stellar parameters; physical posterior sanity (ρ* from fit vs
  Gaia).
- **TRICERATOPS FPP** with the Gaia scene, run per candidate: FPP and NFPP
  recorded with priors and version. Gate for packet-readiness: FPP < 0.015 and
  NFPP < 10⁻³ *as a routing threshold* (matching its published usage), with the
  honest caveat recorded in the packet that single-instrument FPPs are
  assumptions-laden. High NFPP routes to "needs ground imaging" rather than
  killing.
- SED/isochrone sanity via Gaia+2MASS photometry (giant impostor check).

### 4.7 Claim ceilings — what a "validated candidate" would require, and why we stop below it

The packet the system is allowed to assemble autonomously — `packet_ready_for_
review` — contains: ephemeris + fit posteriors; all T3 gate values; population
screen results; catalog adjudication with snapshot hashes; pixel localization
with images; multi-reduction depth table; completeness at that (P, depth) for
this star; FPP; and the full provenance chain (signatures, product versions).
That is an ExoFOP-grade CTOI submission package.

The words **"vetted candidate"** require a human decision (as today —
`human_outcome` stage). The words **"validated planet"** require what the
literature requires — typically FPP < 1% *plus* ground-based imaging excluding
NFPP scenarios *plus* published-quality stellar characterization — and this
system should **never** emit them autonomously. "Confirmed" requires external
mass/dynamics evidence and is likewise human-only. These ceilings go into the
status registry as literal absence: there is no automated path that writes
those statuses (the registry already stages them as `human_outcome`; the
overhaul keeps that and adds a test asserting no automated writer can emit
human-stage statuses).

---

## 5. Completeness and false-alarm calibration

Neither exists today (`fixed_ephemeris_injection_sensitivity` is a per-star
probe, honest about not being completeness). Both become standing
infrastructure with their own tier (runs as batch work items like everything
else).

### 5.1 Injection–recovery (completeness)

- **Where injected**: into the normalized, stitched, *pre-detrending* flux, so
  the measured completeness includes detrending erosion — the place this
  pipeline has actually been hurt. Pixel-level injection is out of scope
  (needs cutouts at scale; noted as a known limitation in every completeness
  report).
- **What**: limb-darkened models (batman), P ∈ log-grid 0.5 d–P_max(baseline),
  depth ∈ {0.5, 1, 2, 4, 8}× the star's photon-noise depth at 3 h, b ∈
  {0, 0.5, 0.8}, random phase; ≥20 injections per sampled star.
- **Whom**: 5% random sample of every first-pass cohort + a dense fixed grid on
  ~50 archetype stars per lane (spanning Tmag, Teff, sector count, crowding).
- **Output**: completeness surfaces C(P, depth | archetype), attached to the
  campaign report; every candidate packet quotes C at its own (P, depth).
  **A campaign without its completeness surface cannot leave "diagnostic"
  status** — enforced in the reporting code, not in prose.
- Cost: ≈ 20 extra searches on 5% of stars ⇒ ~1× the cohort's base search cost
  spread over its runtime. Affordable; runs at low scheduler priority.

### 5.2 Null tests (false-alarm rate)

- **Inverted flux** (sign-flip after preparation) through the identical T2–T3
  path: any "survivor" is by construction a false alarm. Target: **≤0.1%
  inverted-survivor rate**; T3 thresholds are *tuned to this*, not to taste.
- **Sector-shift scrambles** (circular time shifts per sector, preserving
  red noise within sectors while destroying coherent ephemerides) as the
  second null, sensitive to a different artifact family than inversion.
- Run on every cohort at 10% sampling, and at 100% after any change to
  detrending/search/gates (release gate, §7.7).
- **Reliability reporting** (DR25 discipline): every cohort report states
  E[false survivors] from the nulls next to the observed survivor count, per
  period/depth bin. When observed ≈ expected-false, the report itself says the
  cohort's survivors are consistent with noise — the sentence that would have
  saved months of TESScut work.

### 5.3 Known-recovery through the campaign path

`VALIDATION.md`'s five planets ran through a bespoke path; production ran
elsewhere — that divergence hid the systematics. A pinned ~20-planet set
(spanning depth 200 ppm–2%, P 0.5–15 d, Tmag 8–14, incl. one deliberate
half-period-alias case like TOI-700 c) runs **through `batch-hunt`'s own code
path** as a standing regression cohort under every new signature. All must be
recovered at the correct alias with depth within tolerance; any miss blocks the
release. (HANDOFF §6.9's exact request; it becomes CI-adjacent, not
documentation.)

---

## 6. Target selection — where new discoveries can actually come from

The uncomfortable, load-bearing fact: **for bright stars with existing 2-min or
TESS-SPOC coverage, SPOC's TPS+DV and QLP's multi-sector searches are strictly
stronger than this pipeline**, run by teams with pixel-level calibration
knowledge and ExoMiner-scale vetting behind them. Re-searching that population
is a validation activity, not a discovery lane. Zero yield from 12,168 such
stars is roughly what a calibrated expectation would have predicted even with a
perfect pipeline.

Lanes, with honest verdicts:

### 6.1 Primary lane: faint M dwarfs with multi-sector FFI coverage

**Rationale**: transit depth scales as R*⁻²; a 2 R⊕ planet on an M4 dwarf is a
~0.3–0.5% event — detectable at Tmag 13.5–15 in stacked FFI sectors, where
QLP's *searched* sample historically thins out (their light curves extend
fainter than their vetted search set) and TESS-SPOC's FFI target list is
selective. Multi-sector stitching (which we now do routinely) pushes
sensitivity precisely where single-sector official searches stop. This is a
genuine, populated niche between "SPOC did it" and "nobody can".

**Sample definition (operational, all criteria measurable at T0):**
TIC × Gaia: Teff < 4,000 K, R* < 0.6 R☉, Tmag 12.5–15.0, ≥3 sectors of FFI
coverage, contamination ratio below threshold OR TGLC available, dwarf by
Gaia absolute magnitude. Subtract: stars whose *matching ephemerides* already
exist (TOI/TCE at any alias) — as signal-kills at T5, not as sample exclusions
(a known TOI host may still yield a second signal).

**Size and yield arithmetic** (assumptions stated, wide bars, replaced by
measurement in the first 1,000 stars): geometric transit probability at
P < 10 d around M dwarfs ≈ 3–5%; occurrence of ≥1 planet with P < 10 d,
R > 1.5 R⊕ ≈ 20–40%; detectable fraction at our completeness (to be measured,
assume 30–60% for R > 2 R⊕) ⇒ ~0.3–0.8% of sample stars show a detectable
transiter; fraction already catalogued 50–80% ⇒ **new-candidate yield ~5–40 per
10,000 stars searched**. The rediscovery rate in the first cohort measures the
"already catalogued" fraction directly and will tell us within weeks whether
the lane is as under-searched as claimed — that is the lane's kill criterion:
if >95% of detectable signals are rediscoveries *and* completeness is healthy,
the niche is thinner than believed and effort shifts to §6.2.

**Northern preference**: where coverage quality is comparable, prefer fields
with ZTF overlap (dec ≳ −28°) — ground EB-unmasking at 1″ doubles the vetting
power for the brighter half of the lane. Southern CVZ targets accept
ASAS-SN-only context (thin at these magnitudes) and lean harder on T6 pixels;
also note the southern CVZ borders the LMC — crowding screens matter there.

### 6.2 Secondary lane: monotransits / long-period single events

§3.4. Structurally unsearched by the official pipelines (≥2-transit
requirement); scientifically valuable (long-period planets); uniquely suited to
an always-on system that re-checks leads as sectors arrive. False-alarm control
is the whole game; the lane rides the same sample as 6.1 (no separate
downloads) plus brighter multi-sector stars where a single event is
higher-S/N. Yield honestly unknown; cost marginal since it reuses T1 outputs.
Run it as a detector on everything the primary lane touches; queue discipline
(≤0.3 false events/star, hard pixel-vet requirement) keeps it from flooding
humans.

### 6.3 Bounded research lane: EB residuals and circumbinary

93 `known_eb_host_residual_review` targets exist; the Villanova catalog
supplies thousands more hosts. The structural gap is real (official linear-
ephemeris searches are blind to quasi-periodic circumbinary transits), but the
methods are specialist (eclipse masking + QATS-like relaxed searches; eclipse
timing) and occurrence is low. Verdict: **keep bounded** — the 93 existing
targets get proper eclipse-masked residual searches (the masking machinery
exists) as a T5 by-product; a real circumbinary search program is out of scope
until the primary lane is producing calibrated results. Do not let this lane
consume more than ~5% of compute.

### 6.4 Validation lane (not a discovery lane, but always on)

Known-planet regression cohort (§5.3) + a rotating 1–2% "bright control"
sample re-searched each month purely to track pipeline health against stars
where the truth is known. This is what the 12,168-star bright search *becomes*:
its scientific value was always calibration, so keep a small standing version
of it and say so.

### 6.5 Lanes rejected, so nobody re-litigates them silently

- **Re-searching SPOC 2-min stars for new short-period planets**: rejected as
  a discovery lane (see above). Exception: `known_host_new_signal` masked
  re-searches (§1.3), which are cheap and targeted.
- **Ultra-short-period searches (< 0.5 d) at scale**: real niche, but alias/
  systematics-dense and already actively mined by specialist groups; revisit
  only with a dedicated proposal after the funnel is calibrated.
- **Faint-star searches without multi-sector coverage** (single-sector,
  Tmag > 14): completeness too poor; anything found is unvettable.
- **Asteroseismology, flares, general variability catalogs**: out of scope —
  different science, different pipeline.

---

## 7. Software architecture

### 7.1 Control plane: SQLite (WAL) — deliberately not PostgreSQL

The research review recommends PostgreSQL + possibly Temporal. On a
single-user Windows daily-driver, that is the wrong trade: a service to
install, patch, and keep alive through reboots and OneDrive migrations, to
serve write rates of a few rows/second. **SQLite in WAL mode, one writer**,
is sufficient and radically more operable here, *provided the design honors
its constraint*: exactly one process writes (§7.2), workers return results to
the writer over local IPC (stdout/pipe or loopback HTTP), and readers
(dashboard) open read-only connections. WAL gives concurrent readers during
writes. If the project ever outgrows a desktop, the schema (§7.3) is plain
relational and ports to Postgres mechanically. The database lives **outside
OneDrive** (`%LOCALAPPDATA%\exohunt\exohunt.db`) with a nightly integrity-
checked backup copied *into* the project tree (so OneDrive becomes the
backup transport instead of a lock hazard).

### 7.2 One writer, enforced by the system: lease + heartbeat

The unknown restart automation (§0.1) and the two live dashboard processes
make this non-negotiable, exactly as the brief anticipates:

- The scheduler acquires a **Windows named mutex** (`Global\exohunt-scheduler`)
  at startup *and* a lease row in the DB (holder id, hostname/pid,
  `heartbeat_at` refreshed every 15 s). Either being held ⇒ a second scheduler
  **exits 0 immediately with a clear message** ("scheduler already running,
  pid N since T"). Restart automations then become harmless: whatever fires
  them, the extra instance no-ops.
- **Liveness is defined as heartbeat age**, not as a `state` string: the
  dashboard shows "live" iff `now − heartbeat_at < 45 s`. A crashed scheduler
  shows "stale (last seen T)" within a minute — the phantom-running panel
  becomes structurally impossible. `state: "running"` disappears from
  checkpoint files entirely.
- A stale lease (> 5 min) is claimable by a new scheduler after writing a
  `lease_takeover` event recording the dead holder — restarts are self-healing
  and audited.
- CLI one-shot commands that mutate campaign state check the same lease.

### 7.3 Data model (the tables an implementer should build)

`star` (canonical node: tic_id, gaia_source_id, stellar params, crowding
prior, lane) · `identity_edge` (star → external id, source, confidence,
retrieved_at) · `data_state` (star, sectors/products available, hash) ·
`work_item` (star, tier, data_state hash, signature, status:
queued/claimed/done/failed, claim_owner, idempotency key = hash(star, tier,
data_state, signature)) · `evidence` (append-only: work_item, verdict, numeric
payload JSON, artifact hashes, signature, created_at) · `signal` (fitted
ephemerides with provenance; signals are first-class so kills attach to
signals, not stars) · `star_state` (projection: current status per star +
per-signal, rebuildable) · `snapshot` (catalog snapshots: source, version,
hash, path) · `lease`, `event_log` (operational events: takeovers, alarms,
config changes).

Artifacts (per-target JSON, plots, packets) stay as files under `results/`,
**content-addressed** (name contains hash), written once, indexed from
`evidence`. OneDrive syncing write-once files is safe; the DB it must never
touch.

Historical import: existing `batch_summary.json`s, per-target reports,
`events.jsonl`, and human outcomes import as evidence records under their
legacy signatures (`processed-lc-v2`, `tesscut-bgsub-…v4`, `legacy_unversioned`)
so the 12,168-star history remains queryable under exactly the same rules as
new data. Nothing is rewritten; the import is additive.

### 7.4 Scientific signatures

`signature = sha256(code_version, config_bundle_hash, product_family+versions,
target_list_hash)` stamped on every work item and evidence record.
**Summaries, dashboards, and completeness surfaces group by signature and
never aggregate across signatures** (the research review's rule, verified
correct against the v2/v3 mix). The config bundle is one frozen, documented
dataclass module (HANDOFF §6.6): every threshold named, valued, and reasoned,
serialized verbatim into every evidence record. A grep-test asserts the known
magic literals (`7.1`, `0.15`, `21.0`, `13.70`, grid arrays…) appear only in
that module.

### 7.5 Package decomposition (executes HANDOFF §6.3–6.7)

```
src/exohunt/
  config.py        # frozen science config (6.6) — the only home of thresholds
  identity.py      # T0: canonical nodes, PM-aware matching, snapshots (part of 6.3)
  photometry.py    # T1: product matrix, download, stitch, quality masks (6.3)
  detrend.py       # biweight/notch/GP + support-weighting (successor of detrending.py)
  search.py        # T2: BLS/TLS, grids, aliases, monotransit detector
  vetoes.py        # T3 gates (pure functions over signal+flux+star)
  population.py    # T4: commonmode.py + dip registry
  adjudicate.py    # T5: catalog matching semantics (evidence.py successor)
  pixelvet.py      # T6 (pixel.py successor + neighbour test)
  reductions.py    # T7 cross-reduction machinery
  packets.py       # T8 fits, FPP, packet assembly
  calibrate.py     # §5 injection + nulls
  ledger.py        # DB, evidence records, projections, signatures, lease
  scheduler.py     # 24/7 loop, queues, budgets, politeness (replaces _run_batch_hunt)
  cli.py           # parsing + dispatch only; no science
```

`scripts/run_science_followup.py` (890-line parallel implementation) dissolves
into `scheduler.py` + `pixelvet.py`/`reductions.py` work items (HANDOFF §6.5),
and the three checkpoint schemas collapse into `work_item` rows + evidence
(§6.4) — the dashboard's per-producer tolerance code is deleted, not extended.

**Refactor discipline** (unchanged from HANDOFF, restated because it is the
contract): characterization tests first; behaviour-preserving moves with the
114 tests green un-edited; equivalence proven on a pinned 200-target TESScut
set (`--author TESScut --cadence-seconds 158` held constant) with per-target
JSON diffs explained; behaviour changes each in their own commit with their
§2.3/§5-style measurement attached. The already-integrated statuses/detrending
slice followed this pattern; keep following it.

### 7.6 Scheduler, politeness, storage steady state

- **Queues per tier with budgets**: download slots (2–3, token-bucket per
  archive host, circuit breaker + exponential backoff per service), CPU slots
  (default: 50% of logical cores when the console is idle > 10 min, 25%
  otherwise — psutil-based; both numbers in config), pixel-tier slots (1).
  Priorities: human-queue prep > promoted tiers > calibration > first-pass >
  backlog re-adjudication.
- **Idempotency**: work claims carry the idempotency key; a crashed claim
  older than the lease timeout reverts to `queued`; re-execution is safe
  because evidence is append-only and artifacts are content-addressed
  (duplicate work converges on identical hashes).
- **Storage steady state** (the ceiling becomes a budget, per the brief):
  - rolling FITS cache **6 GB, outside OneDrive**, LRU as today;
  - TESScut cutouts: fetch → measure → **delete in the same work item**
    (never cached — this is what turned 9.4 GB into a surprise);
  - durable evidence: rejected signals ≤ 4 KB JSON, no plots (regenerable);
    survivors keep full bundles (~200 KB + plots);
  - cohort compaction: after a cohort closes, per-star rejected JSONs roll
    into one `evidence.jsonl.gz` per cohort (~10× smaller), individual files
    deleted — the DB keeps every row either way;
  - catalog snapshots: last 3 versions (~1–2 GB);
  - DB + indexes < 500 MB with nightly backup into the tree.
  Projected steady state: **≤ 12 GB total project footprint at 5,000
  star·sectors/day first-pass rates**, leaving real headroom under 20 GB. The
  existing 9.4 GB cache shrinks to 6 GB on the first prune after the cache
  moves; the 2.0 GB of results is kept (it is evidence — including the TESScut
  campaigns, which are the systematics record).
- **Disk politeness**: prunes and compactions run only in the idle window.

### 7.7 Release gates (what "done" means for any science-touching change)

A change to photometry/detrend/search/vetoes/config ships only with: new
signature; unit + characterization tests green; known-planet cohort (§5.3)
recovered; artifact regression (§2.3) clean; inverted-run FA rate within
budget on the 500-star locked subset; a one-page delta report (what changed,
what moved, why that is acceptable). The scheduler refuses to enqueue
first-pass work under a signature lacking a stored release report — enforced,
not procedural.

---

## 8. Dashboard and observability

Keep the visual identity; replace the data flow.

### 8.1 Data flow

FastAPI reads **the projection tables read-only** (never files, never
recomputation per request):

- `GET /api/summary` — funnel counts by lane/status/signature, throughput,
  health flags. A few KB, poll-friendly.
- `GET /api/stars?lane=&status=&page=` — paged star lists (the 27 MB
  `survey.json` refetch dies; HANDOFF §6.8).
- `GET /api/star/{tic}` — full evidence chain: every record, every artifact
  link, both ledger history and current state.
- `GET /api/positions?cohort=` — one-time typed-array blob (Float32: ra, dec,
  dist, status-code) for the 3D map; status deltas ride `/summary`.
- `GET /api/ops` — scheduler liveness (heartbeat age — the *only* source of
  "running"), queue depths per tier, per-archive breaker states, storage
  budgets, error tails.
- `GET /api/systematics` — per sector-camera-CCD epoch histograms, dip
  registry, survivor-rate control chart vs the §1.4 bands, common-mode counts.
- `GET /api/review` — the human queue with rendered packets, and POST of human
  outcomes (which are evidence records like everything else).

Full offline export remains as a CLI command producing the old-style JSON for
analysis, explicitly not consumed by the browser.

### 8.2 The operator's questions, answered on one screen

Is it alive (heartbeat age, current tier mix)? · What did it do while I was
away (per-tier throughput, 24 h/7 d)? · Is it healthy (survivor-rate control
chart within bands; epoch-histogram flatness; null-rate tracking; archive
error rates; storage vs budget)? · What needs me (review queue, conflicts from
§4.3, alarms)? · What is it working from (signature, config hash, snapshot
versions — displayed, always).

### 8.3 The two-readings display

Per §1.2: the funnel view shows current-best statuses (the 541-style reading);
a "history" toggle per star and a ledger panel show tier conclusions over time
(the 3,939-style reading), each labelled with exactly those words ("current
best state" / "conclusions logged"), so the two numbers stop looking like a
contradiction to anyone.

### 8.4 Alarms (local-only: dashboard banner + Windows toast + status file)

Heartbeat stale > 2 min · T3 pass rate outside band for > 24 h → **automatic
promotion freeze** (first-pass continues, nothing advances past T3 until an
operator clears it) · any epoch bin > K× expectation · inverted-run rate above
budget · storage > 85% of budget · archive breaker open > 1 h · review queue
> 20 · lease takeover occurred · **any evidence written under an unknown or
report-less signature** (the mixed-version tripwire).

---

## 9. Sequenced roadmap

Every phase has entry criteria, exit criteria, and the measurement that says
it worked. **Trust is restored at the end of P3** — no output before that
point may be represented as anything but diagnostic. Effort estimates are in
focused engineer-days (calendar time on this project compresses with
agent-assisted implementation, but the gates are the gates).

### P0 — Fence the machine (1–2 days). *Entry: plan approved.*

1. Resolve the stray automation with you (find it in the Codex agent's config;
   disable or re-point it). Until found, P0's mutex makes it harmless.
2. Minimal-diff lease: named mutex + heartbeat file check in `_run_batch_hunt`
   and the science runner (a ~50-line guard, not the full §7.2), so no second
   coordinator can start.
3. Truth-repair tool: `exohunt repair-checkpoints` rewrites stale
   `state: "running"` checkpoints to `interrupted` (with audit note). Run it.
4. Kill/consolidate the duplicate dashboard process; document the one true
   launch path.
5. Move pytest basetemp + `data/lightkurve` + future DB out of OneDrive
   (config only; copy nothing).
6. Decide sector100_spoc disposition (§10, decision 1).
   **Exit**: no path to concurrent coordinators; no phantom "running" anywhere;
   tests green from a clean checkout. **Measurement**: start two coordinators
   deliberately; the second exits 0 with the message.

### P1 — Control plane (5–10 days). *Entry: P0 exit.*

Schema §7.3; ledger + projection code; signature stamping; historical import
of all 17 campaigns, ledgers, and human outcomes under legacy signatures;
lease v2 (§7.2); dashboard reads DB behind the existing API shape
(parallel-run against the JSON exporter until counts match exactly).
**Exit/measurement**: DB projection reproduces the current dashboard counts
(5,615/3,837/1,145/541/472/169/99/93/14/0/0 across 12,168 stars) exactly on
frozen inputs; kill -9 during import/write leaves DB consistent (WAL);
double-scheduler chaos test passes; dashboard latency < 100 ms on
`/api/summary`.

### P2 — Science kernel (10–20 days). *Entry: P1 exit.*

Decomposition §7.5 with characterization-first discipline; wotan detrending +
support weighting (§2.2); grid/alias/statistic fixes (§3.1–3.3); T3 veto set
incl. duration-density and full-phase secondary (§3.5); dip registry (§3.6);
ephemeris-with-epoch catalog matching (§4.3).
**Exit/measurements**: the four §2.3 numbers pass; 200-target pinned TESScut
equivalence diff — refactor commits produce byte-identical science rows,
behaviour commits produce explained diffs only; known-planet cohort recovered
through the campaign path at correct aliases; magic-literal grep test passes.

### P3 — Calibration, and the return of trust (5–8 days). *Entry: P2 exit.*

Injection framework + inverted/scrambled nulls (§5) wired as work items;
thresholds re-derived from the nulls (Appendix A updated from measurement);
locked 500-target diagnostic cohort re-run end-to-end under one signature.
**Exit/measurements**: completeness surface + FA estimate exist for the
cohort; inverted-survivor rate ≤ 0.1%; T3 pass rate in band; epoch histograms
flat; release-gate machinery (§7.7) blocks an intentionally broken test
change. **After this gate, and only after, results may be labelled better
than diagnostic.**

### P4 — Vetting depth + backlog adjudication (10–15 days). *Entry: P3 exit.*

Snapshot infra + identity graph + 500-object regression suite (§4.1);
pixel-vet v2 (§4.4); T7 cross-reduction machinery; TRICERATOPS + fit + packet
assembly (§4.6); review queue UI (§8).
Then the immediate scientific payoff: **re-adjudicate the existing backlog**
under the calibrated pipeline — the 541 current survivors, the 93 EB-residual
targets (proper eclipse-masked re-search), TIC 234994474 (the promised
multi-sector QLP run, currently mislabeled-risk), and the 169
catalog-coverage-gap stars.
**Exit/measurements**: regression suite green; backlog: ≥80% of the 541
resolve into a terminal or review lane with full evidence chains (prediction —
if far fewer resolve, the vetting stack is weaker than designed and P5 waits);
TIC 234994474 carries a real verdict.

### P5 — The new survey (ongoing). *Entry: P4 exit + owner sign-off on lanes.*

Build the §6.1 sample (list construction is itself signature-stamped);
first-pass at ~1,000/day for one week — hold; review control charts; scale to
3,000–5,000/day; enable the monotransit detector on the flowing sample; Sector
105 re-run and the legacy bright sample continue only as the §6.4 validation
lane. Steady state thereafter: event-driven re-checks, calibration cohorts
monthly, snapshots refreshing, humans reviewing a bounded queue.
**Measurements that matter forever**: rediscovery rate vs prediction (lane
health); completeness-weighted yield; null-rate stability; zero
mixed-signature aggregations (alarmed); review-queue latency.

### Silent-change risk register (things that could alter science without

anyone noticing, and the tripwire for each):

| Risk | Tripwire |
|---|---|
| Detrend/search/gate change alters results | Signature partitioning + release gates (§7.7); summaries cannot mix |
| Product version drift (SPOC reprocessing, HLSP updates) | Product version in signature; T5 re-adjudication on snapshot refresh writes *new* records |
| Catalog snapshot drift changes adjudications | Snapshot hash on every T5 record; diffs between snapshot generations reported |
| Threshold edited casually | Config module is the only home; grep test; config hash in signature |
| A second writer corrupts state | Mutex + lease + WAL single-writer; alarm on takeover |
| Dashboard shows derived numbers that drift from truth | Dashboard reads projections only; projection rebuild is a test |
| Nulls quietly stop running | Cohort reports refuse "non-diagnostic" status without attached calibration artifacts |

---

## 10. Decisions I need from you

1. **sector100_spoc**: it is not running (despite its checkpoint) and its v2
   majority is diagnostic-only by the review's stop condition. Recommend: do
   **not** restart it; keep all reports on disk; import both signatures into
   the ledger in P1; fold the 5,000-star list into the P3 locked-cohort pool.
   Alternative if you want the v3 baseline now: restart *after P0's lease*,
   accepting ~2 days of machine time for a cohort we will re-run anyway.
2. **The restart automation**: I could not find it from here (not Windows
   Task Scheduler, not Claude scheduled tasks — likely Codex-side). P0.1 needs
   you to locate/disable it, or explicitly bless "P0 mutex makes it inert" and
   we leave it firing harmlessly.
3. **Storage budget**: approve the §7.6 split (6 GB cache **outside OneDrive**
   + ≤12 GB steady-state total), or state a different ceiling. This moves
   ~9 GB of churn out of your OneDrive quota and sync path.
4. **CPU politeness envelope**: proposed 50% of cores when idle / 25% when
   you're active, downloads capped at 3. Adjust to taste; they're config.
5. **Dependency additions** (Appendix B): notably `transitleastsquares`,
   `wotan`, `numba`, `batman-package`, `emcee`, `triceratops` (heavier;
   optional extra), `psutil`. Any objections to the heavier ones?
6. **Lane priorities**: primary = faint-M multi-sector (§6.1) with monotransit
   detector riding along (§6.2), EB-residual bounded to the existing 93
   (§6.3). Confirm or reorder.
7. **Claim ceiling**: confirm the system's autonomous ceiling is
   `packet_ready_for_review` and that CTOI submission remains a human act with
   your name on it (I consider this non-negotiable for scientific honesty, but
   it is your name).

---

## Appendix A — Initial thresholds (all provisional until P3 replaces them with calibrated values)

| Name | Initial | Rationale | Calibration |
|---|---|---|---|
| `sde_min_multisector` | 8.0 | TLS literature FAP~1% is SDE≈7 in white noise; TESS red noise pushes higher | Inverted+scrambled nulls to FA ≤ 0.1% |
| `sde_min_single_sector` | 9.0 | Single sector is alias-richer | Same |
| `red_noise_snr_min` | 7.1 | Continuity with existing gate; already red-noise adjusted | Nulls |
| `min_transits_multisector` | 3 | 2-transit periodic claims are the alias factory (TOI-700 c) | — (structural) |
| `odd_even_kill_sigma` | 3.0 (model-fit version) | Continuity; better estimator §3.5 | Injection of EBs |
| `secondary_kill_sigma` | 3.0 on **stacked all-sector fold**, full-phase scan | TIC 181014443: 2.3σ single-sector, 5.9σ stacked | EB injections |
| `duration_density_flag / kill` | outside [0.4,1.5] / [0.25,2.5] × T₀(P,ρ*) | Physicality; giants/blends | Known-planet cohort must sit inside |
| `depth_eb_lane_ppm` | implied R > 2 R_Jup | Astrophysics | — |
| `duty_cycle_max` | 0.15 | Continuity | Nulls |
| `edge_support_floor f` | 0.4; weight 1/f^α, α=1 | §2.2 | Artifact regression + injections |
| `segment_gap_days` | 0.10 | Measured (detrending.py history) | Keep; re-verify on new detrend |
| `common-mode: enrichment/sharers/fraction` | 10× / 10 / 0.5% | Measured separation 12.3 vs 2.1 | Keep; re-measure per cohort |
| `dip_registry_bin / fraction` | 30 min / >5% of stars dipping | New (§3.6) | Tune on sector 100/105 data where truth is known |
| `mono_sigma_min` | 8.0 | §3.4 | Inverted nulls to ≤0.3 events/star |
| `fpp_packet / nfpp_packet` | <0.015 / <10⁻³ | Published TRICERATOPS practice | Reported per packet, never auto-“validated” |
| `t3_pass_band` | 0.2–2% of searched | §1.4 | Control chart bands from first calibrated cohorts |
| `inverted_survivor_budget` | ≤0.1% | §5.2 | Definitional |

## Appendix B — New dependencies

`transitleastsquares` + `wotan` (+`numba`) — search/detrend core;
`batman-package` — injection + fits; `emcee`+`corner` — posterior fits
(deliberately not PyMC/exoplanet: heavy on Windows for marginal gain here);
`celerite2` — GP escalation (T7+ only); `triceratops` — FPP (optional extra
`[fpp]`, degrade gracefully to "FPP unavailable" in packets); `psutil` —
politeness governor. All wheel-installable on Windows/py3.12. SQLite is
stdlib. No Temporal, no Postgres, no Docker.

## Appendix C — Status registry evolution (additive; existing 23 statuses keep their slugs)

New slugs: `needs_additional_sector` (in_light_curve), `ceiling_zone_diagnostic`
(in_light_curve), `detrend_sensitive` (measured_science flag),
`reduction_dependent_artifact` (measured_science), `known_host_new_signal`
(catalog_context), `single_event_lead` gains monotransit substates via payload
(not new slugs), `packet_ready_for_review` (measured_science, precedence above
`science_vetted_lead`). Human-outcome stage unchanged and remains unreachable
by automation (asserted by test). The registry stays the single source;
TypeScript stays generated.

## Appendix D — Standing test inventory (beyond unit tests)

Characterization goldens (200-target pinned TESScut) · artifact regression
(14 curves) · known-planet campaign-path cohort (~20) · known-object identity
suite (~500) · inverted + scrambled nulls (per release) · chaos: double-start,
kill -9 per tier, OneDrive-lock simulation, disk-full, archive-outage
(breaker) · projection-rebuild equivalence · registry↔frontend drift ·
magic-literal grep · mixed-signature aggregation refusal.
