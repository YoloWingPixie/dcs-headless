# SPDX-License-Identifier: MIT
# Copyright (c) 2026 YoloWingPixie
# Windows side of dcs-headless. Every action writes one JSON document to stdout.
# A process row is {pid, name, command, started (UTC, ISO 8601), parent}.
#   List    DCS.exe processes
#   Launch  refuse if any DCS.exe runs ({running: [rows]}); otherwise start
#           <Exe> -w <ProfileName> --server --norender through the desktop shell and
#           poll every 250 ms until a DCS.exe with a command line appears ({procs: [rows]})
#   Stop    stop <ProcessId> if it is DCS.exe started at <Started> and either its
#           parent is <ParentId> or its command line is -w <ProfileName> --server --norender
param(
    [Parameter(Mandatory = $true)][ValidateSet('List', 'Launch', 'Stop')][string]$Action,
    [string]$Exe,
    [string]$WorkingDirectory,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')][string]$ProfileName = 'unset',
    [int]$TimeoutSeconds = 15,
    [int]$ProcessId = 0,
    [int]$ParentId = 0,
    [string]$Started
)
$ErrorActionPreference = 'Stop'

function Get-Dcs([string]$Filter = "Name = 'DCS.exe'") {
    @(Get-CimInstance Win32_Process -Filter $Filter | ForEach-Object {
        $cim = $_
        $startTime = $null
        try { $startTime = (Get-Process -Id $cim.ProcessId -ErrorAction Stop).StartTime.ToUniversalTime().ToString('o') } catch { }
        [ordered]@{
            pid = [int]$cim.ProcessId; name = [string]$cim.Name; command = [string]$cim.CommandLine
            started = $startTime; parent = [int]$cim.ParentProcessId
        }
    })
}

function Write-Json($Value) { ConvertTo-Json -InputObject $Value -Compress -Depth 4 }

if ($Action -eq 'List') {
    Write-Json @(Get-Dcs)
    exit 0
}

if ($Action -eq 'Launch') {
    if ($ProfileName -eq 'unset' -or !$Exe -or !$WorkingDirectory) { throw 'Launch needs -Exe, -WorkingDirectory and -ProfileName.' }
    $running = @(Get-Dcs)
    if ($running.Count -gt 0) {
        Write-Json ([ordered]@{ running = $running; procs = @() })
        exit 0
    }
    $handle = 0
    $desktop = (New-Object -ComObject Shell.Application).Windows().FindWindowSW(0, 0, 8, [ref]$handle, 1)
    if (!$desktop) { throw 'The Windows desktop shell view is unavailable.' }
    $desktop.Document.Application.ShellExecute($Exe, "-w $ProfileName --server --norender", $WorkingDirectory, 'open', 0)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 250
        $procs = @(Get-Dcs | Where-Object { $_.command -and $_.started })
    } until ($procs.Count -gt 0 -or (Get-Date) -gt $deadline)
    Write-Json ([ordered]@{ running = @(); procs = $procs })
    exit 0
}

# Stop
$row = @(Get-Dcs "ProcessId = $ProcessId")
$process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if ($row.Count -eq 0 -or !$process) {
    Write-Json ([ordered]@{ state = 'exited' })
    exit 0
}
$row = $row[0]
if ($row.name -ne 'DCS.exe' -or $row.started -ne $Started) {
    throw "Process $ProcessId is not the recorded DCS process."
}
if ($ParentId -gt 0) {
    if ($row.parent -ne $ParentId) { throw "Process $ProcessId was not started by DCS process $ParentId." }
} else {
    $cmd = ' ' + $row.command + ' '
    $profileArg = '\s-w\s+"?' + [regex]::Escape($ProfileName) + '"?\s'
    if ($ProfileName -eq 'unset' -or $cmd -notmatch $profileArg -or $cmd -notmatch '\s--server\s' -or $cmd -notmatch '\s--norender\s') {
        throw "Process $ProcessId is not running the $ProfileName profile headless."
    }
}
Stop-Process -Id $ProcessId
$process.WaitForExit(20000) | Out-Null
Write-Json ([ordered]@{ state = $(if ($process.HasExited) { 'exited' } else { 'running' }) })
