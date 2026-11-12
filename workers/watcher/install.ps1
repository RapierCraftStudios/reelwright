# SPDX-License-Identifier: AGPL-3.0-or-later
<#
.SYNOPSIS
  Install the Reelwright footage watcher for the current user on the capture PC (MrDubey).

.DESCRIPTION
  Checks uv and ffprobe, stores the worker token in the user environment (never in a file in the
  repo) and registers a per-user Scheduled Task that starts workers/watcher/watch.py at logon at
  BelowNormal priority. The watcher only posts metadata (checksum, size, kind, duration): footage
  stays on this PC, and proxy encodes go to whichever GPU worker the picker chooses.

.EXAMPLE
  .\install.ps1 -Server https://devbox.example.ts.net -DjiDir D:\DJI -RecordlyDir D:\Recordly
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string[]]$DjiDir = @(),
  [string[]]$RecordlyDir = @(),
  [string]$Name = $env:COMPUTERNAME,
  [string]$Source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)
$ErrorActionPreference = 'Stop'

if (-not $Server.StartsWith('https://')) {
  throw 'The watcher posts manifests over HTTPS on the tailnet: -Server must start with https://'
}
if ($DjiDir.Count -eq 0 -and $RecordlyDir.Count -eq 0) {
  throw 'Give at least one -DjiDir or -RecordlyDir folder to watch'
}
foreach ($dir in $DjiDir + $RecordlyDir) {
  if (-not (Test-Path -LiteralPath $dir -PathType Container)) { throw "Folder not found: $dir" }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw 'uv is required: https://docs.astral.sh/uv/'
}
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
  Write-Warning 'ffprobe not found on PATH: assets will be reported with duration 0 until it is installed'
}

uv sync --project $Source --no-dev

if (-not [Environment]::GetEnvironmentVariable('REELWRIGHT_WORKER_TOKEN', 'User')) {
  $secure = Read-Host -AsSecureString 'Worker token (REELWRIGHT_WORKER_TOKEN)'
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
  [Environment]::SetEnvironmentVariable('REELWRIGHT_WORKER_TOKEN', $plain, 'User')
}

$watch = Join-Path $Source 'workers\watcher\watch.py'
$argList = @('run', '--project', "`"$Source`"", '--no-dev', 'python', "`"$watch`"",
  '--server', $Server, '--host', $Name)
foreach ($dir in $DjiDir) { $argList += @('--dji', "`"$dir`"") }
foreach ($dir in $RecordlyDir) { $argList += @('--recordly', "`"$dir`"") }

$uv = (Get-Command uv -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $uv -Argument ($argList -join ' ')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Priority 7 = BelowNormal: MrDubey is the owner's work PC.
$settings = New-ScheduledTaskSettingsSet -Priority 7 -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "Reelwright watcher $Name" -Action $action -Trigger $trigger `
  -Settings $settings -Description 'Reelwright footage watcher (metadata only, low priority)' `
  -Force | Out-Null

Write-Host "Start now: Start-ScheduledTask -TaskName 'Reelwright watcher $Name'"
Write-Host 'Then copy a clip into a watched folder and confirm on the dev server: GET /api/assets'
