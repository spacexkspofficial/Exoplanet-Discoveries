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
fetches). The push to `origin` awaits the owner completing Git Credential
Manager's interactive sign-in — no credential was stored for the git CLI.
Next work: `CODEX_HANDOFF.md`.

### P2 — Science kernel: **foundations built and tested; cli.py rewiring pending**

| Plan item | Status | Evidence |
|---|---|---|
| Config module (HANDOFF 6.6) | Done | Every Appendix-A threshold named with rationale in `config.py`; AST tripwire test proves the drift literals (7.1, 13.7, 21.0, 0.15) exist only there (kernel modules; cli.py joins at rewiring) |
| Detrending v2 | Done (synthetic gates) | `detrend.py`: biweight, two prepared fluxes, support-weighted edges (floor 0.4, α=1), transit-masked second pass. Measured in tests: retention ≥85% on the Sector-100 gap anatomy vs 67% under the hard guard; edge transits recoverable with honest uncertainty inflation; quiet-star depth to 10% in one pass; under 2% variability blind erosion 30–50% and masked recovery to ~10% at the 0.4 d active-star window — the residual P3 will measure at scale |
| Search grids | Done | `search.py`: density-derived duration grids (M dwarf max <3 h — the 6 h rail cannot exist), period overscan so the reporting ceiling is not a grid boundary |
| Alias adjudication | Done | Ratio-ladder scoring with significance-gated event fractions and a 1.1× change margin; TOI-700 c half-period case recovered in tests; measured corrections documented in the commit |
| T3 vetoes | Done | `vetoes.py`: duration-density (pass/flag/kill), depth physicality → EB lane, folded odd/even at 3+1 events, full-phase secondary scan (finds a phase-0.3 secondary the old screen missed), per-event support, dip-window veto |
| Dip registry | Done | `population.py`; noise calibration measured in tests moved σ 2→3 and cohort floor 5%→10% (at σ=2, ~5% of pure-noise star-bins tripped) |
| New dependencies | Installed + pinned | wotan, transitleastsquares (+numba), psutil core; batman/emcee/corner as `[fits]` extra; setuptools pinned for batman's distutils import on py3.12 |
| **Not yet done** | — | Rewiring cli.py's science paths onto the kernel (characterization-first, 200-target pinned TESScut equivalence), the real-data artifact regression (the 14 curves — needs downloads), known-planet cohort through the campaign path, monotransit detector, TLS integration into T2 |

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
