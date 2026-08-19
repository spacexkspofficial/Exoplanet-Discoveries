<#
.SYNOPSIS
  Keeps the EXOHUNT dashboard answering on 127.0.0.1:8765.

.DESCRIPTION
  The dashboard was started with a bare Start-Process and died silently during
  the 2026-08-15 full-pool run: its log ends at "Application startup complete"
  with no error, no traceback and no shutdown line. Correction 88 recorded the
  same signature -- a dashboard and publisher that vanished with no crash
  record -- and could not attribute it. That run reached 1.25 GB free of
  31.9 GB, so the Windows memory manager reclaiming the process is the likeliest
  explanation, and it is not one the process can log on its way out.

  Either way the fix is the same as for the coordinator: something has to notice
  and start it again. Health is measured by asking the API, not by checking
  whether a PID exists, because a wedged server that holds its port open is
  still a dashboard nobody can read.

  Relaunching while a live server is already running is safe: the dashboard
  takes a machine-wide lease (lease.py, DASHBOARD_LOCK_NAME) and a second
  instance exits without binding the port.
#>

[CmdletBinding()]
param(
  [string] $Url          = 'http://127.0.0.1:8765/api/summary',
  [string] $CacheDir     = 'E:\Agentic AI\Exoplanet Server\exohunt-cache\lightkurve',
  [int]    $PollSeconds  = 30,
  # The summary endpoint walks the results tree, so a slow answer is normal
  # under load and must not be mistaken for a dead server.
  [int]    $TimeoutSec   = 45,
  # Consecutive failed probes before restarting. One timeout during a heavy
  # export is not an outage.
  [int]    $FailuresBeforeRestart = 3
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$exe = Join-Path $repoRoot '.venv\Scripts\exohunt-dashboard.exe'
if (-not (Test-Path $exe)) { throw "Dashboard executable not found: $exe" }

$logDir = Join-Path $repoRoot 'results\dashboard'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$supervisorLog = Join-Path $logDir 'dashboard-supervisor.log'

function Write-Log {
  param([string] $Message)
  $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  $line | Tee-Object -FilePath $supervisorLog -Append
}

function Test-DashboardAlive {
  try {
    $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
    return $r.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Start-Dashboard {
  $ts = Get-Date -Format 'yyyyMMdd-HHmmss'
  $env:EXOHUNT_CACHE_DIR = $CacheDir
  $p = Start-Process -FilePath $exe -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logDir "dash-$ts.stdout.log") `
        -RedirectStandardError  (Join-Path $logDir "dash-$ts.stderr.log")
  Write-Log "launched dashboard pid=$($p.Id) logs=dash-$ts.*"
  return $p
}

Write-Log '=== dashboard supervisor start ==='
$failures = 0
$restarts = 0

while ($true) {
  if (Test-DashboardAlive) {
    if ($failures -gt 0) { Write-Log "dashboard answering again after $failures failed probe(s)" }
    $failures = 0
  } else {
    $failures++
    Write-Log "probe failed ($failures/$FailuresBeforeRestart)"
    if ($failures -ge $FailuresBeforeRestart) {
      $restarts++
      Write-Log "restarting dashboard (restart #$restarts)"
      Start-Dashboard | Out-Null
      $failures = 0
      # Give uvicorn time to bind before the next probe counts against it.
      Start-Sleep -Seconds 20
    }
  }
  Start-Sleep -Seconds $PollSeconds
}
