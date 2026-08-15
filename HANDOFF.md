# Handoff — current state

**Last updated: 2026-08-15.** This is the only handoff. When it goes stale,
**edit this file**; do not write a second one. The four handoffs that used to
compete at the repository root are in `docs/archive/`, and the rename map is in
`docs/archive/README.md`.

Three documents carry live state, and they answer different questions:

| Document | Answers |
|---|---|
| `HANDOFF.md` (this file) | What is running right now, and how do I drive it? |
| `PROGRESS.md` | What has been measured, and what did each correction find? |
| `MASTER_PLAN.md` | What is the project trying to do, and in what order? |

---

## What is running right now

| | |
|---|---|
| Dashboard | `http://127.0.0.1:8765` — up, serving `dashboard/dist/` |
| Campaign | `results/campaign/full_pool_v7_instant_wired` — 64,614 targets, running under supervision |
| Kernel identity | `kernel1:fe853e49aaf218a9446e3781bc4c51479116859fef7abda4165a0ae9fd4511ba` (v7, instant screen wired) |
| Branch | `main`. There is no other working branch. |

The campaign re-runs the full remaining pool under the **v7 kernel**. The
previous pass over this pool completed on 2026-08-06 under a kernel that has
since changed substantially — the alias ladder was disabled (correction 86),
a two-transit floor was added (correction 85), and the shared-instant screen
was wired in (correction 87). Correction 88 names the per-campaign re-run as
the work owed before any of that can be imported, and this is it.

### Driving it

The supervisor owns the campaign. Do not launch `batch-hunt` beside it.

```powershell
# status
Get-Content results\campaign\full_pool_v7_instant_wired\batch_status.json -Raw | ConvertFrom-Json | Select-Object state,completed_targets,total_targets

# what the supervisor has done, including restarts
Get-Content results\campaign\full_pool_v7_instant_wired\supervisor.log -Tail 30
```

To restart it after a reboot, from the repository root:

```powershell
Start-Process -FilePath powershell.exe -WindowStyle Hidden -ArgumentList @(
  '-NoProfile','-ExecutionPolicy','Bypass',
  '-File','scripts\run_supervised_campaign.ps1',
  '-Targets','targets\full_remaining_pool.csv',
  '-OutputDir','results\campaign\full_pool_v7_instant_wired',
  '-CacheMaxGb','250','-WorkspaceMaxGb','300')
```

It resumes: per-target reports are the durable truth and a restart re-runs only
what is missing. Relaunching while a coordinator is already alive is safe — the
lease is a named kernel mutex and the second process exits without starting a
second run.

The dashboard is separate and starts on its own:

```powershell
Start-Process -FilePath '.\.venv\Scripts\exohunt-dashboard.exe' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden
```

---

## Four traps that have each cost a session

**1. `EXOHUNT_CACHE_DIR` is not inherited.** It is set as a *User* environment
variable, and agent shells, services and scheduled tasks do not receive it.
`paths.default_cache_dir()` then falls back to `%LOCALAPPDATA%\exohunt\cache\lightkurve`,
which is not where this project's cache lives. Set it inline in the same
command as anything that reaches `photometry.py`:

```powershell
$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'
```

`scripts\run_supervised_campaign.ps1` sets it itself, which is why it is the
supported way to launch.

**2. `--cache-max-gb` defaults to 2 GB, and a run without it *evicts* down to
that.** This is how a ~90 GB warm cache became 5 GB between sessions. Always
pass it explicitly, and pass `--workspace-max-gb` too — the effective cap is
the `min()` of the two, so a large `--cache-max-gb` alone does nothing.

**3. Editing a kernel module retires the calibration.** `kernel_version()`
digests the *source text* of `DETECTION_KERNEL_MODULES` (`src/exohunt/config.py`).
A comment-only edit to `search.py` costs the same ~16–21 h re-calibration as a
real change, and there is no path back: reverting mints a *new* identity rather
than restoring the old one. This is why `MASTER_PLAN.md` and `PROGRESS.md`
cannot be moved out of the root — kernel modules name them in comments. See
`docs/README.md`.

After any edit near `src/exohunt/`, verify:

```bash
.venv/Scripts/python.exe -c "import sys; sys.path.insert(0,'src'); from exohunt.config import kernel_version; print(kernel_version())"
```

**4. `period_relation` is not what the search found.** It names the nearest
harmonic family. Read `period_status` for the actual outcome.

---

## What is blocked, honestly

### The trusted first pass is still unsatisfiable

`--trusted-first-pass` requires a passing release report for the exact
code/config/product/target-list signature. The v7 calibration
(`results/p5/calibration_ncvz_1000_v7_instant_wired`, 944 stars) does not pass:

| gate | v7 | needs |
|---|---:|---:|
| inverted control | 4 events | 0 |
| scrambled control | 3 events | 0 |
| epoch enrichment (full stack) | 6.44 | ≤ 2.0 |

The epoch gate improved a long way — 25.07 raw-after-triage down to 6.44 once
the ephemeris and instant screens are applied — but it stops there, with three
significant bins left. Correction 88 is explicit that closing the rest is not a
thresholding problem: it needs whatever produces the residual **13.02 d family**
(correction 84) to be identified, and that is not yet known.

**Do not run a v8 calibration without a change to run it against.** v7 already
demonstrated that a re-run with no detection change reproduces the previous
event counts exactly; another one costs ~9 h and would answer nothing.

And note that the epoch gate was never the binding constraint. Inverted and
scrambled fail on their own terms and were untouched by every change in
corrections 80–88. The false-alarm problem correction 68 found is where it was.

### Decision 6 is the one owner decision still open

Six of the seven decisions approved on 2026-08-08 have been executed. The open
one is **shift the science lane to §6.2 (monotransits)**. Groundwork exists —
`src/exohunt/monotransit.py`, `scripts/calibrate_monotransit_threshold.py`, and
two threshold runs under `results/p5/` — but the lane has not been shifted.
See `docs/archive/EXECUTE_DECISIONS.md` for why it was approved.

### Status precedence is decided but the exporter is still the oracle

Decision 3 landed the ledger-authoritative fold. Correction 50's finding still
stands: the exporter has outlived its role as a field-level parity oracle, and
a future session should stop treating it as one rather than editing gates until
they agree.

### TRICERATOPS is installed, isolated on purpose

`.venv-triceratops` is a *separate* interpreter, and `src/exohunt/fpp.py` shells
out to `tools/triceratops_fpp.py` inside it. This is not incidental: TRICERATOPS
pulls `pytransit` → `numba`, which caps `numpy` below 2.4, and the kernel is
calibrated on numpy 2.4.6. Installing it into `.venv` would move the kernel's
numerical floor. If FPP reports `not_run`, rebuild that venv — do not "simplify"
by merging the two.

---

## Repository layout

```
README.md          entry point
HANDOFF.md         this file — current state
PROGRESS.md        the honesty ledger; append corrections here
MASTER_PLAN.md     the plan
CONTRIBUTING.md    contribution rules
SECURITY.md        disclosure policy
docs/README.md     documentation map + the kernel-reference rule
docs/p2/           P2 measurement records
docs/science/      standing science references
docs/archive/      superseded handoffs and completed plans
src/exohunt/       the package; treat every module here as digest-sensitive
scripts/           one-purpose runners and measurement scripts
tests/             556 tests, all passing as of 2026-08-15
dashboard/         the React dashboard; dist/ is what the server serves
```

## Housekeeping that is deliberate, not neglect

- **Do not delete `data/lightkurve`.** 9.4 GB of cached SPOC Sector 100 light
  curves back the offline P2 regression gates. Re-downloadable, but deleting it
  forces a large avoidable MAST re-fetch.
- **Dependency ceilings in `pyproject.toml` are held on purpose.** The kernel
  identity does not cover installed library versions, so a numerical dependency
  could change results while `kernel_version()` still reports the calibration
  valid. `.github/dependabot.yml` records which upgrades are refused and why.
- **The dashboard needs a rebuild after a frontend change**, not after a merge:
  `cd dashboard && npm run build`. The server reads `dashboard/dist/`.
