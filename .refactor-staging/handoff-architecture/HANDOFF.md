# Handoff: renewed sector 100 search on mission-processed photometry

Written 2026-07-26. Covers commits `f3761c4`, `6cefe66`, `3e62187`, `586fe8f`
(all local on `main`, not pushed). Baseline was `d6177e9`.

Read this whole file before running anything. The headline is that **every
result currently in `results/campaign/` was produced through a light-curve
extraction that manufactured its own signals**, and the run described at the end
is the first search on photometry that can support a detection claim.

---

## 1. Why the previous results cannot be used

The sector 100 campaign returned 2,343 survivors from 5,000 stars — a 47 percent
survivor rate, against a real transiting-planet occurrence well under 1 percent.
The period distribution explains it:

- **43.7 percent of survivors sit within 6 percent of 6.85 days**, which is
  exactly half the 13.70-day TESS spacecraft orbital period.
- A second pile-up sits at 12–13 days, the 13.70-day peak truncated by the
  `--max-period 13` ceiling.
- **92 percent of the ~1,000 survivors near 6.85 days share just two transit
  epochs** (BTJD ≈ 4074.1 and ≈ 4080.2). Unrelated stars do not share transit
  times.
- The modal fitted duration is 6.0 h (the top of the duration grid) and the
  modal transit count is 2 (the minimum the gate allows).

Two dominant instrumental events were isolated:

| Campaign | Epoch (BTJD) | Targets sharing | Enrichment | Cameras |
|---|---:|---:|---:|---:|
| `sector105_overnight_5000` | 4206.2 | 1,292 | 35.6× | 4 |
| `sector100_expansion_5000` | 4080.8 | 712 | 41.2× | 4 |

**Root cause:** the campaigns ran `--author TESScut --cadence-seconds 158`. A
TESScut cutout summed through a local aperture carries the observatory's
scattered light, and the Savitzky-Golay flatten that follows
(`window_length ≈ 2 d`, `break_tolerance=5`) fits polynomials independently
across every perigee downlink gap. Both imprint the 13.7-day spacecraft period
on the photometry.

**This was avoidable.** A random sample of 40 targets from
`targets/sector100_expansion_5000.csv` found SPOC 120-second light curves
available for **all 40**. The mission-processed photometry existed the whole
time.

Scale of the difference, TIC 101170045, same sector:

| | TESScut (campaign) | SPOC 120 s |
|---|---:|---:|
| Period | 6.676 d | 1.009 d |
| Depth | 14,355 ppm | 37 ppm |
| S/N | 181.1 | 5.5 |

A 1.4 percent "transit" at S/N 181, centroid 2.6 pixels off target, is simply
absent from the mission reduction.

---

## 2. What changed in the code

### `src/exohunt/commonmode.py` (new) + `exohunt common-mode-screen`

Counts, for each searched target, how many **other targets in the same
campaign** carry the same ephemeris — period matching within 2 percent *and*
transits in phase — against the uniform-phase expectation for the targets
already sharing that period.

Requiring period agreement as well as phase agreement is what makes the test
sharp. An earlier version asking only "do you share an instant" flagged nothing:
with 5,000 targets that is true by chance almost always. Measured separation is
enrichment 12.3 for the suspect population versus 2.1 for everything else.

- Campaigns are screened **separately**; only stars observed together can share
  an observatory event.
- Records camera span and sky spread, separating an observatory-wide effect
  (`common_mode_systematic`) from one contaminating source
  (`localized_coincidence`).
- Also records `spacecraft_harmonic` (proximity to a simple ratio of 13.70 d),
  `duration_at_grid_rail`, and `period_at_search_ceiling` as **cautions, not
  verdicts** — a planet may legitimately sit at 6.85 days.
- Thresholds are conservative (10× enrichment, ≥10 sharing targets); it
  under-flags rather than discarding real signals.

Current output across saved campaigns: **5,615 of 12,038 screened targets
(46.6 %) flagged**; 3,806 on an orbit ratio; 4,401 at a duration rail; 2,570 at
the search ceiling.

Re-run any time — it is pure post-processing over `batch_summary.json`, under
one second for all 12k targets, no downloads.

### `src/exohunt/cli.py` — `--author auto` (now the default)

`analyze`, `hunt`, and `batch-hunt` default to `--author auto`: tries **SPOC →
TESS-SPOC → QLP** per target, falling back to a local TESScut extraction only
when no processed product exists.

Details that matter if you touch this:

- **TESScut needs exactly one sector**, so the fallback is offered only for
  single-sector requests. A multi-sector request with no processed data raises
  instead of silently changing its data source.
- **One exposure time is pinned per target** — the finest that is still ≥100 s
  (`MIN_USEFUL_CADENCE_SECONDS`). Some targets have 20 s data: six times the
  volume, zero detection gain on a 15-minute-minimum transit.
- **Reports record `requested_author` (`"auto"`) alongside the resolved
  `author` (`"SPOC"`), and reuse compares the request.** Without this a resumed
  `auto` campaign would not recognise its own earlier reports — they store the
  resolved name — and would re-download the entire list on every restart. Do not
  "simplify" this away.
- A per-collection archive outage is caught so one unavailable author cannot
  strand a target on the fallback.
- `authors_considered` is recorded per report for auditing.

Explicit `--author TESScut` still reproduces the old behaviour exactly.
`pixel-vet` legitimately keeps TESScut — it needs the pixel cutout.

### `src/exohunt/dashboard.py` / `dashboard_server.py` / `App.tsx`

- Pixel-localization and sector-coherence verdicts now reach the dashboard.
  They previously did not, so 90 science-vetted stars kept displaying their
  earlier metadata-only classification. New statuses:
  `pixel_offset_contamination`, `single_sector_unconfirmed`,
  `science_vetted_lead`.
- Common-mode verdicts **outrank** the pixel and sector gates. This is
  deliberate: an observatory event repeats identically in every sector, so
  multi-sector coherence passes it by construction and cannot clear it.
- Logged human outcomes outrank every automated classification. Previously
  `false_positive` ranked *below* `unresolved_transit_like_signal`, so a
  hand-vetted false positive on any lead was silently discarded.
- `science_products_downloaded` was read from a `runtime` sub-object; the
  single-threaded science runner writes it flat, so the panel always showed 0.
- Snapshot freshness now compares a fingerprint sampled *before* the export
  reads anything. Comparing against the snapshot's own mtime marked it fresh
  forever whenever a checkpoint landed mid-export.
- The poll path no longer parses and re-serialises the 27 MB snapshot on every
  request; it streams the file with a cached (mtime, size) freshness check.
  Idle server CPU went 18.4 % → 6.4 % of one core.

### Tests

97 pass. Run them with a basetemp outside OneDrive — OneDrive locks
`.pytest-tmp` and pytest fails with `WinError 5`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp="$env:TEMP\exohunt-pt"
```

---

## 3. Current scientific state

Both leads that survived every automated gate were vetted by hand against
mission photometry. **Both fail.** Both are logged via `log-outcome`.

- **TIC 181014443** — real signal, not a planet. SPOC reproduces the TESScut
  period to **2 seconds** (0.7708184 d) across 7 sectors spanning 2019–2026,
  209 transits, S/N 87. The combined fold shows a **215 ppm secondary eclipse at
  5.9 σ** against a 1080 ppm primary; that ratio needs a companion with a fifth
  of the star's surface brightness, impossible for the implied 0.033 radius
  ratio. Odd/even agree to 0.24 σ, so it is not a doubled alias. Eclipsing
  binary or low-mass companion. **Single-sector TESScut measured the same
  secondary at only 2.3 σ and passed it** — one sector cannot clear a signal.
- **TIC 188241769** — does not reproduce. Its 9.2822 d / 972 ppm / S/N 32
  detection is absent from 5 SPOC sectors, where a blind search returns an
  unrelated 0.911 d signal at 38 ppm that the same screen rejects on duty cycle.
- **TIC 234994474** — **inconclusive, not cleared.** No SPOC 2-min data. A QLP
  attempt downloaded only 1 of 5 requested sectors and the fit hit its rails
  (5 % depth, zero observed transits). Needs a proper multi-sector QLP run.

There is currently **no planet candidate**. That is the correct state, not a
failure.

---

## 4. The run to execute

Same 5,000 stars, real photometry, period ceiling moved off the spacecraft
period.

```powershell
.\.venv\Scripts\exohunt.exe batch-hunt `
  --targets targets\sector100_expansion_5000.csv `
  --output-dir results\campaign\sector100_spoc `
  --allow-no-known `
  --min-period 0.5 --max-period 10 `
  --workers 4 --download-workers 3 --prefetch 8 `
  --cache-max-gb 10 --workspace-max-gb 20
```

Deliberate choices:

- **No `--author`.** The default `auto` resolves per target. Do not pass
  `--author TESScut`; that reintroduces the entire problem.
- **No `--cadence-seconds`.** Auto-selection pins it per target.
- **`--max-period 10`, not 13.** Keeps the 13.70-day spacecraft period and its
  truncated peak off the edge of the grid, where BLS was piling up. It does
  sacrifice genuine 10–13 day planets; search those separately later with
  multi-sector data where the common-mode screen can police the result.
- Write to a **new** output directory. Do not overwrite
  `sector100_expansion_5000` — it is the evidence for the systematics analysis.

This downloads ~5,000 SPOC light curves. Expect an overnight run. It is
checkpointed and resumable; a restart reuses matching reports.

### Verify early, before letting it run overnight

After roughly the first 20 targets, confirm auto-selection is behaving. The
`auto` path is unit-tested against a fake archive and smoke-tested on three real
targets, but **it has not been run at campaign scale**:

```powershell
.\.venv\Scripts\python.exe scripts\check_author_selection.py results\campaign\sector100_spoc
```

It exits non-zero if more than 10 percent of reports used a local TESScut
extraction. Expect `SPOC` for nearly everything at 120 s. A high TESScut share
means the archive queries are failing and the run is silently reverting to the
bad path — stop it rather than letting it finish. For reference, the same check
against `results\campaign\sector100_expansion_5000` reports 100 percent TESScut
and fails, which is exactly the state this run exists to replace.

### After it completes

```powershell
.\.venv\Scripts\exohunt.exe common-mode-screen
.\.venv\Scripts\exohunt.exe metrics-summary
```

Then regenerate the dashboard (the server does this automatically on next poll,
or force it):

```powershell
.\.venv\Scripts\python.exe -c "from exohunt.dashboard import export_dashboard_data; export_dashboard_data('.')"
```

**The number that decides whether this worked:** the common-mode flagged
fraction. It was **46.6 %** on TESScut. On mission-processed photometry it
should fall to a few percent. If it does not, the systematics are not coming
from the extraction and the detrending needs examination next — start with the
Savitzky-Golay `break_tolerance` behaviour at downlink gaps.

Also compare the survivor rate. 47 percent was absurd; a believable blind search
of 5,000 field stars should survive well under 5 percent.

---

## 5. Open items, in priority order

1. **Re-run sector 105 the same way** once sector 100 validates the approach.
2. **TIC 234994474** needs a proper multi-sector QLP run — currently
   inconclusive, and it must not be reported as cleared.
3. **The campaign reduction path is still unvalidated end to end.**
   `VALIDATION.md` recovers five known planets, but through SPOC 120 s — which
   is now what campaigns use, so validation and production finally agree. Run
   `inject-recover` through the campaign path to measure real sensitivity and
   false-alarm rate.
4. **Under-exploited lanes**, more promising than re-searching stars SPOC and
   QLP already searched: monotransits/long-period single events (SPOC requires
   2+ transits), and EB-residual/circumbinary searches — the
   `known_eb_host_residual_review` lane already holds 93 targets.
5. `.pytest-tmp` inside OneDrive causes intermittent `WinError 5`. Consider
   moving pytest's basetemp out of the synced tree.

---

## 6. Refactor mandate — fix causes, not symptoms

The project owner's instruction is explicit: **no duct-tape patching. Real
project-wide refactors where they are warranted.** Several of the fixes in the
commits above are correct in behaviour but were applied to structures that made
those bugs easy to introduce and hard to see. Those structures are the actual
defect, and they are listed below with evidence.

### How to do this safely in a scientific codebase

A refactor that silently changes a threshold or a classification changes
published numbers. So, in this order, without exception:

1. **Characterise before changing.** For each area below, write tests that pin
   the *current* observable behaviour first — including behaviour you believe is
   wrong. Wrong behaviour gets changed deliberately, in its own commit, with the
   change stated.
2. **Refactor behaviour-preserving.** The 97 existing tests must stay green
   throughout, not be "updated to match" a refactor. If a test needs editing to
   pass, that is a behaviour change and belongs in a separate commit.
3. **Prove equivalence on real data.** Before/after, re-run a fixed subset
   (e.g. 200 targets from `targets/sector100_expansion_5000.csv` with
   `--author TESScut --cadence-seconds 158` to hold the input constant) and diff
   the per-target JSON. Any difference must be explained, not accepted.
4. One concern per commit. A refactor commit changes structure only.

### 6.1 One source of truth for the classification vocabulary — highest value

**Evidence.** A status is currently defined in seven places across two
languages: `SCREENING_LABELS`, `CONTEXT_LABELS`, `SCIENCE_LABELS`,
`COMMON_MODE_LABELS` in `src/exohunt/dashboard.py:89,98,108,114`, plus the
`Status` union, `STATUS_META`, `STATUS_HELP`, and `STATUS_SYMBOL` in
`dashboard/src/App.tsx:12,375,387,711`.

Adding the five statuses in these commits required edits in all of them. That is
why the frontend needed a defensive `statusMeta()` fallback at all — an exporter
can emit a status the bundle has never heard of.

**Fix.** One machine-readable registry defining each status once: slug, label,
short label, help text, symbol, colour, evidence stage, precedence. Python
imports it; the TypeScript tables and `Status` union are **generated** from it as
a build step. Delete the hand-maintained duplicates.

**Invariant.** Adding a classification is a one-file change, and a test fails if
the generated frontend tables drift from the registry.

### 6.2 Replace the magic-integer precedence ladder with an explicit stage model

**Evidence.** `src/exohunt/dashboard.py` resolves a star's displayed status
through one flat `dict[str, int]` mixing five unrelated kinds of evidence:
in-light-curve screening, catalog context, measured science, population screen,
and human outcomes. It is applied by four near-identical
`if priorities.get(x, -1) >= priorities.get(status, -1)` blocks at
`dashboard.py:1107-1135`.

That design caused a real defect: `false_positive` sat at 2, *below*
`unresolved_transit_like_signal` at 4, so a hand-vetted false positive on any
lead was silently discarded. It was fixed by renumbering — which is exactly the
patch this mandate forbids as a permanent answer. I then extended the same dict
twice more (science verdicts at 5, common-mode at 6), making the next collision
likelier, not less.

**Fix.** Model evidence explicitly. An ordered stage enum —
`in_light_curve < catalog_context < measured_science < population_screen <
human_outcome` — with each verdict declaring its stage in the 6.1 registry.
Resolution becomes one function: highest stage wins; ties resolve by declared
precedence within the stage. The four copy-pasted comparison blocks collapse
into one call.

**Invariant.** A test asserts that no automated classification can override a
recorded human outcome, for every pair in the registry. Correctness stops
depending on someone choosing the right integer.

### 6.3 Decompose `cli.py`

**Evidence.** 4,432 lines, 85 functions. `_run_batch_hunt` is 508 lines;
`build_parser` is 482. It holds photometry download, aperture extraction,
detrending, BLS screening, campaign orchestration, checkpointing, storage
policy, target-list construction, and argument parsing — in one module.

Concrete consequences seen today: the Savitzky-Golay detrend block is duplicated
at `cli.py:319` and `cli.py:372`, so the TESScut and processed paths can drift
apart silently; and adding author selection meant editing a 172-line function
that already did four unrelated jobs.

**Fix.** Extract by concern: `photometry.py` (search, download, extraction,
detrending — one code path parameterised by product type), `screening.py` (gates
and classification), `campaign.py` (orchestration, checkpointing, resume),
`targetlists.py`. `cli.py` keeps argument parsing and dispatch and contains no
science.

**Invariant.** No function longer than roughly 80 lines. Detrending exists once.
`cli.py` imports science; it does not implement it.

### 6.4 One checkpoint schema

**Evidence.** Three shapes describe the same concept — a resumable worker
checkpoint:

- `batch_progress.json`: `counts`, `runtime`, `settings`
- `context_vet_progress.json`: `counts`, `runtime`, no `settings`
- `science_vet_progress.json`: **flat** — `error_targets`, `remaining_targets`,
  `science_products_downloaded` at top level, no `counts`, no `runtime`

The divergence caused a live dashboard bug: `science_products_downloaded` was
read from `runtime` and therefore always displayed 0 while 359 products had been
downloaded. The current fix reads both shapes — tolerance code that exists only
because the producers disagree.

**Fix.** One `WorkerCheckpoint` dataclass with one serialiser, used by
`batch-hunt`, `context-vet-queue`, and the science runner. Migrate old files on
read, then delete the compatibility branches.

**Invariant.** The dashboard reads one shape and contains no per-producer
branching.

### 6.5 Fold `scripts/run_science_followup.py` into the CLI

**Evidence.** 890 lines outside the package, re-implementing checkpointing,
retention, publishing, and summary writing that `cli.py` already has. It is
undocumented as a command and cannot be resumed through the normal interface.

It also has a genuine performance defect: `publish()` calls `workspace_bytes()`
— a full walk of the ~14 GB workspace — and `product_count()`, which re-reads
and revalidates every report for every queue row, and `publish()` is called two
to three times per target. That is quadratic in queue length.

**Fix.** `exohunt science-vet-queue`, sharing the 6.4 checkpoint and the
existing retention machinery. Track workspace size incrementally.

### 6.6 Centralise the science thresholds

**Evidence.** `7.1` (S/N floor) appears at `cli.py:2776,3508,3549,3556,3565,3578`
as a bare literal, with arithmetic built on it. Also scattered: `0.15` duty
cycle, 5 percent depth, 3σ odd/even and secondary, `21.0` arcsec per pixel
(`cli.py:2681`), the `(0.25 … 6.0)` duration grid, and 13.70 d.

Two of those literals mattered today: the duration grid's rails produced 4,401
edge-pinned fits, and the 3σ secondary gate passed TIC 181014443 at 2.3σ on one
sector when 7 sectors showed 5.9σ.

**Fix.** One documented configuration object — every threshold named, with the
reason for its value — recorded verbatim into every report so any result can be
reproduced against the settings that produced it.

**Invariant.** No bare numeric science threshold in logic. Grep for the literals
above returns only the configuration module.

### 6.7 Remove the duplicated payload builders

`src/exohunt/dashboard.py:911` and `:962` build the same ~40-field star signal
dict twice — once from campaign summaries, once from live checkpoints. Every
field added today had to be added twice. Extract one builder.

### 6.8 The survey payload is a 27 MB monolith

`survey.json` carries all 12,168 stars with roughly 60 fields each, refetched by
the browser every five seconds. Serving it as a file rather than re-encoding it
per request cut idle server CPU from 18.4 to 6.4 percent of a core, but the
payload size is the underlying issue.

**Fix.** Separate the summary (counts, coverage, active campaigns) from per-star
detail; page or stream the star list, or encode positions as typed arrays. Keep
a full export for offline analysis.

### 6.9 Test and validation hygiene

- `pyproject.toml` sets `--basetemp=.pytest-tmp`, inside the OneDrive-synced
  tree; OneDrive locks it and pytest fails with `WinError 5`. Move it out.
- `VALIDATION.md` recovers five known planets, but no test exercises the
  **campaign** path end to end. Add a known-planet recovery through
  `batch-hunt`'s own code path so validation and production cannot diverge again
  — that divergence is precisely what hid the TESScut systematics.

### Priority order

6.1 and 6.2 first: they are small, they remove whole classes of defect, and
everything else is easier once the vocabulary is generated and precedence is
explicit. Then 6.4 and 6.6 (schema and thresholds), then 6.3 (the large
decomposition), then 6.5, 6.7, 6.8, 6.9.

Do not start the decomposition in 6.3 before the characterisation tests in step
1 exist. Moving 4,400 lines without them will change results silently.

## 7. Things not to undo

- Do not restore `--author TESScut` as a campaign default.
- Do not remove `requested_author` from reports or from the reuse comparison.
- Do not let sector coherence override a common-mode verdict.
- Do not let an automated classification override a logged human outcome.
- Do not treat surviving the common-mode screen as evidence a signal is a
  planet. It is evidence only that the observatory did not obviously produce it.
