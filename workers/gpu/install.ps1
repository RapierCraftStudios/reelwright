# SPDX-License-Identifier: AGPL-3.0-or-later
<#
.SYNOPSIS
  Install the Reelwright GPU worker for the current user (MrDubey, Legion).

.DESCRIPTION
  Installs the reelwright package with uv, checks nvidia-smi and ffmpeg, stores the worker token
  in the user environment (never in a file in the repo) and registers a per-user Scheduled Task
  that starts `rw worker run` at logon at BelowNormal priority with a RAM cap per job.
  The real enrolment (token issue, tailnet URL) is tracked in issue #17.

.EXAMPLE
  .\install.ps1 -Server https://devbox.example.ts.net -Name mrdubey-4070ti -MemCapMB 4096
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$Name = $env:COMPUTERNAME,
  [string]$Source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
  [int]$MemCapMB = 4096,
  [switch]$Cpu
)
$ErrorActionPreference = 'Stop'

if (-not $Server.StartsWith('https://')) {
  throw 'The worker pulls jobs over HTTPS on the tailnet: -Server must start with https://'
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw 'uv is required: https://docs.astral.sh/uv/'
}
if (-not $Cpu -and -not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
  throw 'nvidia-smi not found: install the NVIDIA driver or pass -Cpu for a CPU fallback worker'
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Warning 'ffmpeg not found on PATH: encode jobs will fail until it is installed'
}

uv tool install --force $Source
$rw = (Get-Command rw -ErrorAction Stop).Source

if (-not [Environment]::GetEnvironmentVariable('REELWRIGHT_WORKER_TOKEN', 'User')) {
  $secure = Read-Host -AsSecureString 'Worker token (REELWRIGHT_WORKER_TOKEN)'
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  [Environment]::SetEnvironmentVariable('REELWRIGHT_WORKER_TOKEN', $plain, 'User')
}

$argList = @('worker', 'run', '--server', $Server, '--name', $Name, '--mem-cap-mb', $MemCapMB)
if ($Cpu) { $argList += '--cpu' }

$action = New-ScheduledTaskAction -Execute $rw -Argument ($argList -join ' ')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Priority 7 = BelowNormal; the agent also starts every job at BelowNormal with a Job Object cap.
$settings = New-ScheduledTaskSettingsSet -Priority 7 -AllowStartIfOnBatteries:$false `
  -DontStopIfGoingOnBatteries:$false -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Reelwright worker $Name" -Action $action -Trigger $trigger `
  -Settings $settings -Description 'Reelwright render worker (low priority, RAM cap)' -Force | Out-Null

Write-Host "Installed. Check what the scheduler will see: python $PSScriptRoot\probe.py"
Write-Host "Start now: Start-ScheduledTask -TaskName 'Reelwright worker $Name'"
Write-Host 'Then confirm on the dev server: rw workers'
