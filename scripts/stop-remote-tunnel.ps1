<#
.SYNOPSIS
    Stops any active Cloudflare Tunnel processes for the ISM dev site.
.DESCRIPTION
    Finds running cloudflared.exe instances and terminates them, returning
    the development environment to local-only mode.
.EXAMPLE
    .\scripts\stop-remote-tunnel.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "SilentlyContinue"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot   = Split-Path -Parent $scriptDir
$logsDir    = Join-Path $repoRoot "logs"
$tunnelPid  = Join-Path $logsDir "tunnel.pid"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "         ISM Dev Site - Stopping Remote Tunnel            " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$stopped = 0

# Check recorded PID first
if (Test-Path $tunnelPid) {
    try {
        $pidNum = [int](Get-Content $tunnelPid -Raw).Trim()
        $proc = Get-Process -Id $pidNum -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -like "*cloudflared*") {
            Write-Host "[*] Terminating recorded cloudflared process (PID: $pidNum)..." -ForegroundColor Gray
            Stop-Process -Id $pidNum -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    } catch {}
    Remove-Item $tunnelPid -Force -ErrorAction SilentlyContinue
}

# Check any remaining cloudflared processes
$procs = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
if ($procs) {
    foreach ($p in $procs) {
        try {
            Write-Host "[*] Terminating cloudflared process (PID: $($p.Id))..." -ForegroundColor Gray
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            $stopped++
        } catch {}
    }
}

Start-Sleep -Milliseconds 500
$remaining = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue

if (-not $remaining) {
    if ($stopped -gt 0) {
        Write-Host "[+] Successfully stopped $stopped tunnel process(es)." -ForegroundColor Green
    } else {
        Write-Host "[i] No active cloudflared tunnel processes were running." -ForegroundColor Yellow
    }
    Write-Host "[+] Remote access is closed. Dev site is now local-only." -ForegroundColor Green
} else {
    Write-Host "[!] Warning: $($remaining.Count) process(es) could not be stopped." -ForegroundColor Red
}
