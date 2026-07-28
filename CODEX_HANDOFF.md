# Codex handoff: P2 CLI rewiring and the road to first science

> **Superseded for state and work queue by [CODEX_HANDOFF_2.md](CODEX_HANDOFF_2.md) (2026-07-27).** Step 2 of §1 (structure-only
> extraction) is complete; step 3 has been attempted twice and both
> attempts were measured and rejected. The P2 exit gates, P3 plan,
> monotransit detector, and claim ceiling below remain current.

Supersedes `NEXT_AGENT_BRIEF.md` (its item 1, the dashboard-ledger switch, is
done, verified, merged to `main`, and deployed). Work in
`C:\Users\alexa\OneDrive\Desktop\Codex\Exoplanet Discoveries` — **`main` now
carries all overhaul code**, so the PYTHONPATH workaround is dead; the
editable install serves current code directly. The worktree branch
`claude/exoplanet-discoveries-research-192dc3` is identical to `main`; use
either.

---

## Are we ready to start science?

**No — by the plan's own gates, and this is the point of the plan.** What
stands between here and the first believable science run, in order:

1. the science code `batch-hunt` executes is still the *old* kernel
   (Savitzky–Golay path, fixed grids, period-only catalog matching);
2. the new kernel has passed synthetic gates only — its real-data gates
   (artifact regression, known-planet cohort) have not run;
3. no thresholds are calibrated: until Phase 3's injection + inverted-data
   nulls exist, there is no measured false-alarm rate, so nothing can be
   labelled better than **diagnostic**.

The first sanctioned science-shaped run is the **locked 500-target
diagnostic cohort at P3 exit**. Your job is to get there. Do not start
campaign-scale searches before that gate; the plan (§9) and the owner have
already decided this.

## Verified state you inherit (re-verify, don't trust)

```powershell
cd "C:\Users\alexa\OneDrive\Desktop\Codex\Exoplanet Discoveries"
& ".venv\Scripts\python.exe" -m pytest -q          # expect: 180 passed
& ".venv\Scripts\python.exe" -m exohunt.cli ledger-import --workspace . --parity
# expect: parity_match true, zero differences at count, star-status, and field level
```

- P0/P1 complete and **deployed**: ledger at `%LOCALAPPDATA%\exohunt\exohunt.db`
  (43,790 evidence rows, 8 legacy signatures), dashboard serving read-only
  ledger projections on `http://127.0.0.1:8765` (summary 3.4 KB; liveness =
  lease heartbeat age only; `survey.json` polling gone; the file exporter
  survives as the offline parity oracle).
- P2 kernel modules exist and pass synthetic gates: `config.py` (every
  threshold named; AST tripwire), `detrend.py` (support-weighted biweight,
  two-pass masking), `search.py` (density grids, overscan, alias
  adjudication), `vetoes.py`, `population.py`. **None of it is wired into
  `batch-hunt` yet.**
- `PROGRESS.md` is the honesty ledger: five measured corrections so far.
  When your measurement contradicts the plan or your first implementation,
  fix it, measure, and record it there. That is the culture.

## Work queue

### 1. Rewire `cli.py`'s science paths onto the kernel (the riskiest step)

Target structure: MASTER_PLAN.md §7.5; mandate and discipline: HANDOFF.md §6.3.
The order is the safety mechanism:

1. **Golden run first.** Pick the pinned equivalence subset — the first 150
   rows of `targets/sector100_expansion_5000.csv` — and run it through the
   *current* code with input held constant:
   `batch-hunt --author TESScut --cadence-seconds 158 --max-targets 150
   --cache-max-gb 8` into a dedicated `results/equivalence/golden_v0`
   directory. (150 × ~35 MB cutouts ≈ 5–6 GB through the relocated cache;
   the temporary `--cache-max-gb 8` is per-run and within the approved
   envelope. The downloads are the sanctioned, bounded kind.) These reports
   are the characterization baseline; never regenerate them.
2. **Structure-only extraction**, one concern per commit: photometry
   acquisition, screening, campaign orchestration, target lists move out of
   `cli.py`; after every commit, re-run the subset **from cache** and diff
   per-target JSON against golden — byte-identical science fields or the
   commit is wrong. The 180 tests stay green un-edited throughout.
3. **Behaviour changes, each in its own commit with its measurement**:
   switch detrending to `detrend.prepare_fluxes` + the two-pass mask;
   duration/period grids from `search.py` (grid-rail kill + overscan
   diagnostics); alias adjudication on every reported ephemeris; T3 vetoes
   from `vetoes.py` (duration-density, full-phase secondary, folded
   odd/even, per-event support, depth→EB-lane); dip-registry construction
   during campaigns and the per-event window veto; epoch-aware catalog
   matching (`tce.py` and `evidence.py` currently match period only — a
   false "known" silently discards a real signal). Every evidence row the
   new path writes carries the real `scientific_signature` from
   `config.py`, and results land in the ledger as first-class evidence
   (the importer's `affects_state` discipline shows how winners vs history
   work).
4. **Extend the AST literal tripwire to `cli.py`** once its literals migrate
   to `config.py`, and delete the migrated duplicates.

### 2. Real-data P2 exit gates (§2.3) — the first new measurements

- **Artifact regression**: reconstruct the artifact set from the ledger —
  targets whose common-mode evidence carries `shared_epoch_btjd` at 4074.4
  or 4080.8 (`/api/systematics` or SQL on `evidence.kind='common_mode'`);
  take ≥14 with SPOC 120 s coverage. Gate: with the new kernel, **0 of 14
  detect at the artifact epochs** above threshold, and cadence retention is
  **≥85%** (vs 67% under the old hard guard).
- **Known-planet cohort through `batch-hunt`'s own path** (not the separate
  `validate` path — that divergence is what hid the TESScut disaster):
  ~20 planets spanning depth 200 ppm–2%, P 0.5–15 d, Tmag 8–14, including
  TOI-700 c as the deliberate alias stress case. Gate: all recovered at the
  correct alias, depths within tolerance. Wire it as a standing regression
  cohort that any future signature change must re-pass.

### 3. Monotransit detector (§3.4)

Matched-filter bank over durations 1.5–24 h on the **long-window** prepared
flux; per-event vetoes reuse `vetoes.per_event_support` and the dip-window
veto. Calibrate the threshold on inverted data to ≤0.3 false events/star.
Claim ceiling: `single_event_lead`, never a period.

### 4. P3 — calibration, where trust returns (§5)

Injection–recovery (limb-darkened via the `[fits]` extra, injected into
pre-detrending flux so detrending erosion is measured) on 5% of the cohort +
archetype grid; inverted-flux and sector-shift nulls through the identical
path; re-derive every Appendix A threshold from the nulls and land the
calibration as one signature-bumping commit; then the **locked 500-target
diagnostic cohort** under one signature, with its completeness surface and
null-rate in the campaign report. That report is the ready-for-science
milestone: after it, and only after it, propose the faint-M lane list
build (§6.1) to the owner for P5 sign-off.

## Ground rules (unchanged, non-negotiable)

- HANDOFF.md §7 in full; plus: evidence is append-only; nothing aggregates
  across scientific signatures; automation never writes human-stage
  statuses; the autonomous ceiling is `packet_ready_for_review`.
- Campaign-scale anything beyond the bounded gates above needs explicit
  owner approval. The coordinator mutex will let exactly one of you run;
  a second instance exiting 0 with a message is correct behaviour, not a
  bug.
- Prefer measurement to assertion; record corrections in `PROGRESS.md`.

## Environment notes and traps

- Windows 11; venv at `.venv` (inside OneDrive; pip there is slow and may
  leave `~`-prefixed litter dirs — harmless). Tests relocate their own
  basetemp. High-churn state lives under `%LOCALAPPDATA%\exohunt\`.
- **Your sandbox could not write `.git\worktrees\...\index.lock` last
  session.** If that recurs: do the work, run the full suite, write your
  report, and leave the commit to the owner's agent — exactly as last time.
  State clearly in the report that nothing is committed.
- The dashboard server now holds a **read-only** ledger connection; your
  campaign runs write via the normal `ledger.connect()` path — never give
  request handlers a writable connection.
- Checkpoint `state` strings are display caches; liveness is heartbeat age;
  per-target reports are the durable truth.
- Verify before destructive actions; the process *tree* is the truth (a
  three-process chain here is one server).

## When you stop

Full suite last; update `PROGRESS.md` against the plan's gates (status,
evidence, corrections); report what passed which gate, what is pending, and
every place measurement contradicted expectation. Surface owner decisions
(P5 lane sign-off, anything campaign-scale) — never take them.
