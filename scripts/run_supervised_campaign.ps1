<#
.SYNOPSIS
  Keeps a long batch-hunt campaign alive across unexplained coordinator deaths.

.DESCRIPTION
  Correction 88 recorded a v7 calibration that died 33 minutes in with no
  reboot, no sleep event, no crash record and healthy resources, and concluded
  that "a nine-hour run on this machine needs something sturdier than
  Start-Process". This is that sturdier thing.

  Restarting is safe because the campaign's per-target reports are the durable
  truth: a resume rediscovers them and re-runs only what is missing. The
  coordinator lease is a named kernel mutex released automatically on process
  death (src/exohunt/lease.py), so a crashed run can never wedge the lock, and
  a relaunch that races a still-live coordinator exits successfully without
  starting a second one.

  That last property is why this script decides completion from
  batch_status.json rather than from the child's exit code -- exit 0 means
  either "the cohort finished" or "another coordinator already owns it", and
  those need opposite responses.

  Environment is set here rather than inherited: EXOHUNT_CACHE_DIR is a User
  environment variable that agent and service shells do not receive, and
  without it paths.default_cache_dir() silently falls back to a LOCALAPPDATA
  directory that does not hold this project's cache.

.EXAMPLE
  pwsh -File scripts\run_supervised_campaign.ps1 -Targets targets\full_remaining_pool.csv -OutputDir results\campaign\my_run
#>

[CmdletBinding()]
param(
  [string] $Targets           = 'targets\full_remaining_pool.csv',
  [string] $OutputDir         = 'results\campaign\supervised_run',
  [string] $CacheDir          = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve',
  [string] $Author            = 'SPOC',
  [int]    $CadenceSeconds    = 120,
  [int]    $Workers           = 4,
  [int]    $AnalysisProcesses = 16,
  [int]    $DownloadWorkers   = 3,
  [int]    $Prefetch          = 64,
  [int]    $CacheMaxGb        = 250,
  [int]    $WorkspaceMaxGb    = 300,
  [switch] $AllowNoKnown      = $true,
  # A restart budget, not a retry-forever loop. Exhausting it is a real signal.
  [int]    $MaxRestarts       = 200,
  # Below this, an exit counts as "immediate" and feeds the backoff. Above it,
  # the run did real work and the backoff resets.
  [int]    $HealthyRunSeconds = 300,
  # Consecutive immediate exits that made no progress before giving up.
  [int]    $MaxStalledStarts  = 5
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve to the repository root regardless of where this was invoked from.
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "Interpreter not found: $python" }
if (-not (Test-Path $Targets)) { throw "Target list not found: $Targets" }

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$supervisorLog = Join-Path $OutputDir 'supervisor.log'
$statusPath    = Join-Path $OutputDir 'batch_status.json'

function Write-Log {
  param([string] $Message)
  $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  $line | Tee-Object -FilePath $supervisorLog -Append
}

# Returns a hashtable describing the cohort, or $null when no status exists yet.
function Get-CampaignStatus {
  if (-not (Test-Path $statusPath)) { return $null }
  try {
    $raw = Get-Content $statusPath -Raw -ErrorAction Stop
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    $j = $raw | ConvertFrom-Json -ErrorAction Stop
    return @{
      State     = [string] $j.state
      Completed = [int]    $j.completed_targets
      Total     = [int]    $j.total_targets
    }
  } catch {
    # A status read racing the coordinator's atomic rewrite is not an error.
    return $null
  }
}

function Test-CampaignComplete {
  param($Status)
  if ($null -eq $Status) { return $false }
  return ($Status.State -eq 'completed' -and $Status.Total -gt 0 -and $Status.Completed -ge $Status.Total)
}

# The campaign is CPU-bound Python across many processes. Leaving BLAS free to
# spawn its own pool inside each child oversubscribes the machine badly.
$env:EXOHUNT_CACHE_DIR    = $CacheDir
$env:OMP_NUM_THREADS      = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS      = '1'
$env:NUMEXPR_NUM_THREADS  = '1'

$cliArgs = @(
  '-m', 'exohunt.cli', 'batch-hunt',
  '--targets', $Targets,
  '--output-dir', $OutputDir,
  '--author', $Author,
  '--cadence-seconds', $CadenceSeconds,
  '--workers', $Workers,
  '--analysis-processes', $AnalysisProcesses,
  '--download-workers', $DownloadWorkers,
  '--prefetch', $Prefetch,
  '--cache-max-gb', $CacheMaxGb,
  '--workspace-max-gb', $WorkspaceMaxGb
)
if ($AllowNoKnown) { $cliArgs += '--allow-no-known' }

Write-Log "=== supervisor start ==="
Write-Log "repo=$repoRoot"
Write-Log "targets=$Targets output=$OutputDir"
Write-Log "cache=$CacheDir cache_max=${CacheMaxGb}GB workspace_max=${WorkspaceMaxGb}GB"
Write-Log "processes=$AnalysisProcesses workers=$Workers downloads=$DownloadWorkers prefetch=$Prefetch"

$attempt        = 0
$stalledStarts  = 0
$lastCompleted  = -1

while ($true) {
  $status = Get-CampaignStatus
  if (Test-CampaignComplete $status) {
    Write-Log "campaign complete: $($status.Completed)/$($status.Total). Supervisor exiting."
    exit 0
  }

  if ($attempt -ge $MaxRestarts) {
    Write-Log "restart budget of $MaxRestarts exhausted; stopping. Investigate before raising it."
    exit 1
  }

  $attempt++
  $before = if ($null -ne $status) { $status.Completed } else { 0 }
  Write-Log "launch attempt #$attempt (completed=$before)"

  $ts     = Get-Date -Format 'yyyyMMdd-HHmmss'
  $outLog = Join-Path $OutputDir "run-$ts.stdout.log"
  $errLog = Join-Path $OutputDir "run-$ts.stderr.log"

  $started = Get-Date
  $proc = Start-Process -FilePath $python -ArgumentList $cliArgs `
            -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog
  Write-Log "coordinator pid=$($proc.Id) stdout=$outLog"

  $proc.WaitForExit()
  # Refresh before reading ExitCode: a process object obtained from
  # Start-Process -PassThru caches its state, and the first run logged an empty
  # exit code for a coordinator that had definitely exited non-zero.
  try { $proc.Refresh() } catch { }
  $code = if ($null -ne $proc.ExitCode) { $proc.ExitCode } else { 'unknown' }
  $elapsed = (Get-Date) - $started
  # `hh` silently drops whole days -- a 52-hour run logged as "04:27:15" -- so
  # total hours are formatted explicitly.
  Write-Log ("coordinator pid={0} exited code={1} after {2:N0}h{3:mm\:ss}" -f `
    $proc.Id, $code, [Math]::Floor($elapsed.TotalHours), $elapsed)

  $status = Get-CampaignStatus
  if (Test-CampaignComplete $status) {
    Write-Log "campaign complete: $($status.Completed)/$($status.Total). Supervisor exiting."
    exit 0
  }

  $after = if ($null -ne $status) { $status.Completed } else { 0 }

  # An exit that was both immediate and fruitless is the signature of a
  # duplicate coordinator or a launch-time failure -- neither is fixed by
  # trying again straight away.
  if ($elapsed.TotalSeconds -lt $HealthyRunSeconds -and $after -le $lastCompleted) {
    $stalledStarts++
    Write-Log "no progress on a short run ($stalledStarts/$MaxStalledStarts). Last stderr:"
    Get-Content $errLog -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Write-Log "  | $_" }
    if ($stalledStarts -ge $MaxStalledStarts) {
      Write-Log "giving up after $stalledStarts starts that neither ran nor advanced the cohort."
      exit 1
    }
  } else {
    $stalledStarts = 0
  }
  $lastCompleted = $after

  $backoff = [Math]::Min(300, 15 * [Math]::Pow(2, $stalledStarts))
  Write-Log "resuming in $backoff s (completed=$after of $(if ($null -ne $status) { $status.Total } else { '?' }))"
  Start-Sleep -Seconds $backoff
}
