# Exoplanet campaign reboot handoff — 2026-08-05 22:21 PDT

## Shutdown state

- The campaign, dashboard, and prefetch process trees were explicitly stopped for a computer reboot.
- 28 verified processes were terminated. No orphan multiprocessing workers remained.
- Dashboard port `127.0.0.1:8765` is intentionally down.
- Repository: `E:\Agentic AI\Exoplanet Server\Exoplanet-Discoveries`
- Branch: `codex/p2-catalog-matching`
- Worktree is clean and three commits ahead of origin; nothing from this session was pushed.

## Last durable campaign state

- Campaign/output: `results\campaign\full_remaining_pool`
- Target list: `targets\full_remaining_pool.csv`
- Last dashboard checkpoint: **25,927 / 64,614** completed.
- Counts at shutdown: 192 survivors, 25,733 rejected, 2 errors.
- Last state used 16 analysis processes, 3 download workers, and a prefetch depth of 64.
- The checkpoint can lag the durable per-target reports. Let the normal resume scan rediscover reports; do not delete or rebuild the output directory.
- `batch_progress.json` is about 46.5 MB. A cold post-reboot resume scan may take 15–25 minutes before analysis child processes appear.

## Cache/prefetch state

- Always set this inline in every launch shell:

```powershell
$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'
```

- Sector 97 prefetch completed: 1,119/1,119 newly fetched in the last pass, zero failures, about 13,878/hour.
- The campaign was still in Sector 97, so it has a warm reserve after restart.
- Prefetch had advanced to Sector 98. Its startup inventory found 1,985/9,502 already cached and last reported 276/7,517 additional fetches before shutdown.
- Cache size at the Sector 98 start was about 94.69 GB of the 130 GB prefetch budget.
- Resume the coordinator at `--start-sector 98`; it will inventory existing files and skip them.

## Recommended post-reboot launch order

Run from the repository root.

### 1. Dashboard

```powershell
$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'
Start-Process -FilePath '.\.venv\Scripts\exohunt-dashboard.exe' `
  -WorkingDirectory (Get-Location).Path -WindowStyle Hidden
```

Confirm `http://127.0.0.1:8765/api/summary` returns HTTP 200. The user can then open/refresh the dashboard. Use `Ctrl+Shift+R` once to guarantee the new CSS is loaded.

### 2. Campaign resume

```powershell
$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:NUMEXPR_NUM_THREADS = '1'

Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList @(
  '-m','exohunt.cli','batch-hunt',
  '--targets','targets\full_remaining_pool.csv',
  '--output-dir','results\campaign\full_remaining_pool',
  '--author','SPOC','--cadence-seconds','120',
  '--workers','4','--analysis-processes','16',
  '--download-workers','3','--prefetch','64',
  '--cache-max-gb','150','--workspace-max-gb','95',
  '--allow-no-known'
) -WorkingDirectory (Get-Location).Path -WindowStyle Hidden
```

Wait for the durable resume validation to finish and confirm 16 child processes appear. Do not mistake the validation interval for a hang when the main process is alive and accumulating CPU time.

After the children spawn, giving only the analysis children `AboveNormal` priority produced the best measured rate while the dashboard and prefetch stayed low priority. Do not use Realtime priority.

### 3. Sector 98+ prefetch

```powershell
$env:EXOHUNT_CACHE_DIR = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve'
$prefetch = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList @(
  'scripts\prefetch_all_sectors.py',
  '--targets','targets\full_remaining_pool.csv',
  '--workers','28','--max-gb','130',
  '--skip-catalogs','--start-sector','98'
) -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
$prefetch.PriorityClass = 'BelowNormal'
```

Let the one-time Sector 98 inventory complete. Once transfer progress lines such as `[100/...]` appear, lower the whole prefetch Python tree to `Idle` priority so analysis owns CPU scheduling. The coordinator will continue automatically through Sectors 99–104.

## What was fixed

1. `10d7b12 Fix campaign progress layout in constrained windows`
   - The dashboard page now scrolls instead of clipping the bottom metrics/campaign panels.
   - The bottom grid has intrinsic height, and in-flight rows have their own bounded scroller.
   - Browser-verified at the user's exact 1920×1050 viewport with no horizontal overflow.
2. `4cffb56 Support resuming multi-sector prefetch`
   - Adds `--start-sector`, so a prefetch resume can begin at the current/future sector.
3. `c53972b Avoid repeated full cache scans during prefetch`
   - Replaces a recursive 70–95 GB cache scan on every progress report with an in-memory byte counter.
   - Regression test added in `tests/test_prefetch_scripts.py`.

Verification already completed:

- Dashboard `npm.cmd run build` passed.
- Exact-size browser layout check passed.
- `pytest -q tests\test_prefetch_scripts.py`: 1 passed.
- Python compilation and `git diff --check` passed.

## Throughput diagnosis

- The original ~2k/hour collapse involved 70 orphaned analysis workers from older force-stopped coordinators plus an orphaned prefetch process. Those were removed, and careful tree shutdown now leaves zero orphans.
- With a clean machine, 16 analysis processes, all BLAS thread counts at one, High Performance power mode, and no prefetch CPU competition, short live samples were approximately 4,705, 4,792, and 4,862 stars/hour.
- This Sector 97 stretch uses about 10.5–12.5 seconds of wall service per target under full 16-thread saturation on the Ryzen 7 1700X (8 physical / 16 logical CPUs). The requested 5k–10k/hour was not honestly reached; the observed hardware/workload ceiling was about 4.8k/hour.
- Dashboard rolling throughput includes restart/validation downtime for 15 minutes and will initially under-report the live rate.
- Do not undo scientific behavior merely to inflate throughput. Any further attempt to exceed the current ceiling should profile and equivalence-test the analysis path first.

## Operational cautions

- Before force-stopping any coordinator, enumerate and stop its full descendant tree. Killing only the parent caused the prior orphan-worker leak.
- Direct prefetch writes `.part` files and atomically renames, but lightkurve's built-in download path can leave a truncated final FITS file if killed mid-transfer. If a specific target reports a corrupt FITS after reboot, remove only that target's cache namespace and re-fetch it; do not delete the entire cache.
- The two campaign errors at shutdown predated the final tuning and did not increase during the verified samples.
- Monitoring commands may use short `Start-Sleep` intervals solely to separate throughput snapshots. They do not pause or run inside the campaign.

## Latest logs

- Campaign: `results\campaign\full_remaining_pool\restart-16proc-clean-20260805-214727.stdout.log`
- Campaign stderr: `results\campaign\full_remaining_pool\restart-16proc-clean-20260805-214727.stderr.log`
- Prefetch: `results\campaign\full_remaining_pool\prefetch-s97-idle-20260805-220248.stdout.log`
- Dashboard restart logs: `results\dashboard\restart-20260805-215725.*.log`
