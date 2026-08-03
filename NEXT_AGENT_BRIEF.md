# Brief for the next agent: continue the EXOHUNT overhaul

Copy everything below the line into the agent. Work happens on branch
`claude/exoplanet-discoveries-research-192dc3` from the repository root. This
is a historical handoff; use the current default branch unless reproducing that
checkpoint deliberately.

---

You are continuing a partially-executed overhaul of a TESS transit-search
pipeline. The plan is written, owner-approved, and partly implemented. Your
job is to keep executing it **in phase order, gate by gate** — not to
redesign it, and not to skip ahead.

## Read these first, in this order

1. `MASTER_PLAN.md` — the authority. Owner approved all seven §10 decisions
   on 2026-07-27. Sections are cross-referenced below as §N.
2. `PROGRESS.md` — execution state against the plan's gates, including four
   measured corrections already made to the plan's first implementations.
3. `HANDOFF.md` — the project's failure history (instrument artifacts
   confidently reported as astrophysics) and the §6 refactor mandate with
   its safety discipline. §7 lists things never to undo.
4. New modules, all tested: `src/exohunt/config.py`, `ledger.py`,
   `importer.py`, `lease.py`, `checkpoints.py`, `paths.py`, `detrend.py`,
   `search.py`, `vetoes.py`, `population.py`.
5. `RESEARCH_REVIEW.md` only if needed — literature is reliable, but its
   TOI-1431 / TIC 101319448 repository claim is **fabricated** (verified
   against the NASA archive). Never cite it.

## Verified current state (re-verify before building on it)

- **176 tests pass** with bare `pytest` from the branch checkout:
  ```powershell
  python -m pytest -q
  ```
- **P0 complete**: machine-wide coordinator mutex (auto-released on process
  death) guards `batch-hunt`, `scripts/run_science_followup.py`, and the
  dashboard server; a second instance exits 0. `exohunt repair-checkpoints`
  exists and was run: the phantom `running` state on `sector100_spoc` is
  repaired to `interrupted` with an audit block. Caches and the control
  plane live outside OneDrive (`%LOCALAPPDATA%\exohunt\`), env vars
  `EXOHUNT_CACHE_DIR` / `EXOHUNT_DB_PATH` are set (user-level).
- **P1 core complete, parity gate PASSED on real data**: the SQLite ledger
  at `%LOCALAPPDATA%\exohunt\exohunt.db` holds 43,787 imported evidence rows
  under 8 explicit legacy signatures, and its projection reproduces the
  dashboard exporter's counts exactly (12,168 stars, 16 statuses, 541
  `automated_survivor`, 5,615 `common_mode_systematic`). Re-verify any time
  (idempotent) from the repository root with the **branch** code:
  ```powershell
  $env:PYTHONPATH = ".claude\worktrees\exoplanet-discoveries-research-192dc3\src"
  python -m exohunt.cli ledger-import --workspace . --parity
  ```
  (The editable install still points at `main`'s older src; the PYTHONPATH
  prefix runs the branch code. This awkwardness disappears if the owner
  merges the branch.)
- **P2 foundations built** (synthetic gates green): support-weighted
  biweight detrending with a transit-masked second pass; density-derived
  duration grids; period overscan; alias adjudication (TOI-700 c
  half-period case is a test); T3 veto set; absolute-time dip registry;
  AST tripwire pinning the drift literals (7.1, 13.7, 21.0, 0.15) to
  `config.py` only.
- The old `data/lightkurve` cache (9.4 GB, inside OneDrive) is orphaned and
  re-downloadable; the owner may not have deleted it yet. Leave that to
  them.

## Decisions already made — do not re-ask

sector100_spoc stays un-restarted (its reports are imported under both
signatures). The Codex-side restart automation is disabled. Storage split
approved (§7.6). CPU politeness 50% idle / 25% active, 3 download slots.
Dependencies approved (wotan, TLS, numba, psutil core; batman/emcee/corner
as `[fits]`). Lanes confirmed: faint-M multi-sector primary, monotransit
riding along, EB-residual bounded to the existing 93. **Autonomous claim
ceiling is `packet_ready_for_review`; CTOI submission is a human act.**

## Your work queue, in order

1. **Dashboard reads the ledger (finishes P1, §8.1).** Add DB-backed
   endpoints to `dashboard_server.py` (`/api/summary`, paged `/api/stars`,
   `/api/star/{tic}`, `/api/ops` with heartbeat-age liveness,
   `/api/systematics`) reading `star_state`/`evidence` projections
   read-only. Parallel-run: keep the file exporter until the new endpoints
   match it on frozen inputs (extend the parity machinery), then switch the
   frontend off the 27 MB `survey.json` refetch (§6.8 of HANDOFF). The
   dashboard "live" indicator must come from lease heartbeat age, never a
   `state` string.
2. **Rewire `cli.py`'s science paths onto the kernel (the P2 switch,
   HANDOFF §6.3).** Characterization tests FIRST; the 176 existing tests
   stay green un-edited; equivalence proven on a pinned 200-target subset
   of `targets/sector100_expansion_5000.csv` run with explicit
   `--author TESScut --cadence-seconds 158` before/after, per-target JSON
   diffs explained. Structure-only commits separate from behaviour commits;
   every behaviour change carries its measurement. Extend the AST literal
   tripwire to `cli.py` when its literals migrate to `config.py`.
3. **Real-data P2 exit gates (§2.3)** — these need small downloads, which
   the approved plan authorizes at this scale: the 14-light-curve artifact
   regression (epochs BTJD 4074.4 / 4080.8 must stay dead, retention ≥85%),
   and the ~20 known-planet cohort through `batch-hunt`'s own code path
   (correct alias, depth in tolerance). No campaign-scale runs: those stay
   gated behind P3/P5 phase exits.
4. **Monotransit detector (§3.4)** on the long-window prepared flux,
   thresholds calibrated on inverted data (≤0.3 false events/star).
5. **P3 calibration (§5)** once P2 exits: injection framework + inverted/
   scrambled nulls as work items; re-derive Appendix A thresholds from the
   nulls; locked 500-target diagnostic cohort under one signature.
   **Until P3 completes, every result is labelled diagnostic.**

## Ground rules (the project's history is why)

- Scientific honesty over progress. Never let code conclude more than its
  evidence supports; "absent from a catalog" is not novelty; surviving a
  screen is not a detection. Prefer measurement to assertion — when your
  implementation disagrees with a test you wrote from theory, suspect the
  theory, measure, and document the correction in `PROGRESS.md` (four such
  corrections are already recorded there; keep that ledger honest).
- HANDOFF §7 stands: no `--author TESScut` campaign default; keep
  `requested_author` reuse semantics; sector coherence never overrides a
  common-mode verdict; automation never overrides a logged human outcome.
- Evidence is append-only; summaries never aggregate across scientific
  signatures; `resolve_status` order semantics (stage, then precedence,
  later-wins on ties) are what the importer's `affects_state` discipline
  encodes — understand `importer.py` before touching projection logic.
- One concern per commit. Commits so far follow this; keep the chain clean.

## Environment facts and traps

- Windows 11, PowerShell; the venv is `.venv` in the main project dir
  (inside OneDrive — pip operations there are slow and occasionally leave
  `~` litter dirs; harmless). Tests self-relocate their basetemp out of
  OneDrive via `tests/conftest.py`.
- OneDrive locks files mid-write. Anything high-churn goes under
  `%LOCALAPPDATA%\exohunt\` (see `paths.py`). Durable write-once evidence
  stays in `results/` deliberately.
- **Verify before destructive actions.** Precedent: what looked like two
  duplicate dashboard servers was one server's launcher→shim→interpreter
  chain; killing the "duplicate" would have killed the only server. The
  process tree, not the process list, is the truth.
- The checkpoint files are caches; per-target report JSONs are the durable
  truth (a checkpoint can lag reports written after its last publish — the
  26-vs-24 case in `PROGRESS.md`).
- `ledger.evidence_counts()` counts rows from all sources (summaries AND
  checkpoints), so it exceeds the metrics ledger's per-summary numbers;
  filter by source prefix when comparing to `metrics/current_stats.json`.
- The dashboard serves on `http://127.0.0.1:8765` from
  `.venv\Scripts\exohunt-dashboard.exe`; it currently runs `main`'s code
  and refreshes `survey.json` on poll. Restart it only after the owner
  merges or you deliberately point it at branch code.

## When you stop

Update `PROGRESS.md` against the plan's gates (status, evidence, measured
corrections), commit with the existing message style, run the full suite
last, and report: what passed which gate, what is pending, and any place
where measurement contradicted the plan. If a decision is genuinely the
owner's (merge to main, delete the old cache, authorize anything
campaign-scale), surface it — do not take it.
