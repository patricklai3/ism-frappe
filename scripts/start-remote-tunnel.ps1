<#
.SYNOPSIS
    Starts a Cloudflare Tunnel for remote testing of the ISM dev site.
.DESCRIPTION
    Exposes the local Frappe development site (port 8000) over a secure HTTPS URL
    via Cloudflare Tunnel. Supports both permanent custom domains (via Cloudflare
    Tunnel Token) and instant Quick Tunnels (trycloudflare.com).
.PARAMETER Token
    Cloudflare Tunnel Token for a named tunnel with a custom domain.
.PARAMETER Domain
    Custom domain name mapped to the tunnel (e.g. dev.yourdomain.com).
.PARAMETER Quick
    Forces an instant Quick Tunnel (temporary URL on trycloudflare.com), even if
    a token is configured.
.PARAMETER Background
    Runs the tunnel in the background and exits immediately.
.PARAMETER NoQr
    Disables printing the terminal ASCII QR code.
.PARAMETER Port
    Local webserver port to expose (defaults to 8000).
.EXAMPLE
    .\scripts\start-remote-tunnel.ps1 -Quick
.EXAMPLE
    .\scripts\start-remote-tunnel.ps1 -Token "eyJh..." -Domain "dev.yourdomain.com"
#>

[CmdletBinding()]
param(
    [string]$Token,
    [string]$Domain,
    [switch]$Quick,
    [switch]$Background,
    [switch]$NoQr,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot   = Split-Path -Parent $scriptDir
$configFile = Join-Path $scriptDir ".tunnel-config.json"
$logsDir    = Join-Path $repoRoot "logs"
$tunnelLog  = Join-Path $logsDir "tunnel.log"
$tunnelPid  = Join-Path $logsDir "tunnel.pid"

if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "         ISM Dev Site - Remote Testing Launcher           " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Locate cloudflared.exe
$cloudflaredCmd = Get-Command "cloudflared.exe" -ErrorAction SilentlyContinue
$cloudflaredPath = $null

if ($cloudflaredCmd) {
    $cloudflaredPath = $cloudflaredCmd.Source
} else {
    $candidates = @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe",
        "$env:LOCALAPPDATA\cloudflared\cloudflared.exe",
        "$env:USERPROFILE\bin\cloudflared.exe"
    )
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            $cloudflaredPath = $cand
            break
        }
    }
}

if (-not $cloudflaredPath) {
    Write-Host "[!] cloudflared.exe not found on system." -ForegroundColor Yellow
    Write-Host "[*] Attempting automatic installation via winget..." -ForegroundColor Yellow
    try {
        winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
        $cloudflaredPath = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    } catch {
        Write-Error "Failed to install cloudflared automatically. Please install it manually: winget install Cloudflare.cloudflared"
        exit 1
    }
}

Write-Host "[+] Cloudflare Tunnel Client: $cloudflaredPath" -ForegroundColor Green

# 2. Local Dev Site Health Check
Write-Host "[*] Checking local dev site on port $Port..." -ForegroundColor Gray
$siteHealthy = $false
try {
    $req = [System.Net.WebRequest]::Create("http://localhost:$Port")
    $req.Timeout = 3000
    $resp = $req.GetResponse()
    $siteHealthy = $true
    $resp.Close()
} catch {
    if ($_.Exception.Response) {
        $siteHealthy = $true
    }
}

if ($siteHealthy) {
    Write-Host "[+] Local site responding on http://localhost:$Port" -ForegroundColor Green
} else {
    Write-Host "[!] WARNING: http://localhost:$Port is not responding yet." -ForegroundColor Yellow
    Write-Host "    Make sure WSL2 Docker containers and Frappe bench are running:" -ForegroundColor Yellow
    Write-Host "    docker ps  (look for devcontainer-frappe-1)" -ForegroundColor Yellow
}

# 3. Resolve Configuration (Token vs Quick Tunnel)
if (-not $Token -and (Test-Path $configFile)) {
    try {
        $config = Get-Content $configFile -Raw | ConvertFrom-Json
        if (-not $Token -and $config.tunnel_token) {
            $Token = $config.tunnel_token.Trim()
        }
        if (-not $Domain -and $config.domain) {
            $Domain = $config.domain.Trim()
        }
    } catch {
        Write-Host "[!] Failed to parse $configFile. Ignoring." -ForegroundColor Yellow
    }
}

if (-not $Token -and $env:CLOUDFLARE_TUNNEL_TOKEN) {
    $Token = $env:CLOUDFLARE_TUNNEL_TOKEN.Trim()
}

$useToken = (-not $Quick) -and ([bool]$Token)

# Clean up stale logs and PID files
if (Test-Path $tunnelLog) { Remove-Item $tunnelLog -Force -ErrorAction SilentlyContinue }
if (Test-Path $tunnelPid) { Remove-Item $tunnelPid -Force -ErrorAction SilentlyContinue }

$tunnelProcess = $null
$remoteUrl = $null

if ($useToken) {
    Write-Host "[*] Mode: Permanent Custom Domain (Token-based)" -ForegroundColor Cyan
    if ($Domain) {
        Write-Host "[*] Target Domain: https://$Domain" -ForegroundColor Cyan
        $remoteUrl = "https://$Domain"
    } else {
        $remoteUrl = "https://<your-configured-domain>"
    }

    $argList = "tunnel --no-autoupdate --logfile `"$tunnelLog`" --pidfile `"$tunnelPid`" run --token `"$Token`""
    $tunnelProcess = Start-Process -FilePath $cloudflaredPath -ArgumentList $argList -WorkingDirectory $repoRoot -PassThru -NoNewWindow

    # Give it 3 seconds to establish and verify process is alive
    Start-Sleep -Seconds 3
    if ($tunnelProcess.HasExited) {
        $errContent = if (Test-Path $tunnelLog) { Get-Content $tunnelLog -Tail 10 } else { "" }
        Write-Error "cloudflared exited unexpectedly with code $($tunnelProcess.ExitCode).`nLogs:`n$errContent"
        exit 1
    }
} else {
    Write-Host "[*] Mode: Quick Tunnel (Instant trycloudflare.com URL)" -ForegroundColor Cyan
    Write-Host "[*] Establishing secure Cloudflare edge tunnel..." -ForegroundColor Gray

    $argList = "tunnel --url http://localhost:$Port --logfile `"$tunnelLog`" --pidfile `"$tunnelPid`" --no-autoupdate"
    $tunnelProcess = Start-Process -FilePath $cloudflaredPath -ArgumentList $argList -WorkingDirectory $repoRoot -PassThru -NoNewWindow

    # Wait for the assigned trycloudflare.com URL to appear in the logfile
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $timeoutSec = 25

    while ($timer.Elapsed.TotalSeconds -lt $timeoutSec -and -not $remoteUrl) {
        if (Test-Path $tunnelLog) {
            try {
                $content = Get-Content $tunnelLog -Raw -ErrorAction SilentlyContinue
                if ($content -and $content -match '(https://[a-zA-Z0-9-]+\.trycloudflare\.com)') {
                    $remoteUrl = $matches[1]
                    break
                }
            } catch {}
        }
        Start-Sleep -Milliseconds 400
    }

    if (-not $remoteUrl) {
        if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
            $tunnelProcess.Kill()
        }
        $errContent = if (Test-Path $tunnelLog) { Get-Content $tunnelLog -Tail 15 } else { "" }
        Write-Error "Failed to obtain Quick Tunnel URL within $timeoutSec seconds.`nLogs:`n$errContent"
        exit 1
    }

    # Wait briefly for Cloudflare edge registration to ensure URL is instantly reachable
    $regTimer = [System.Diagnostics.Stopwatch]::StartNew()
    while ($regTimer.Elapsed.TotalSeconds -lt 8) {
        if (Test-Path $tunnelLog) {
            $content = Get-Content $tunnelLog -Raw -ErrorAction SilentlyContinue
            if ($content -and $content -match 'Registered tunnel connection') {
                break
            }
        }
        Start-Sleep -Milliseconds 400
    }
}

# 4. Connection Details & Mobile QR Code
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "                REMOTE DEV SITE IS LIVE!                  " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Public HTTPS URL:  $remoteUrl" -ForegroundColor Yellow
Write-Host "  Local Endpoint:    http://localhost:$Port" -ForegroundColor Gray
Write-Host "  Process ID:        $($tunnelProcess.Id)" -ForegroundColor Gray
Write-Host "  Log File:          $tunnelLog" -ForegroundColor Gray
Write-Host ""

if (-not $NoQr -and $remoteUrl -and $remoteUrl -notlike "*<*") {
    Write-Host "[*] Scan with your phone camera to open on mobile:" -ForegroundColor Cyan
    try {
        $qr = curl.exe -s --max-time 4 "https://qrenco.de/$remoteUrl"
        if ($qr -and $qr.Length -gt 50) {
            Write-Host $qr -ForegroundColor White
        }
    } catch {}
}

Write-Host "Quick Tips for Remote Testing:" -ForegroundColor Cyan
Write-Host "  1. Desk URL: $remoteUrl/app" -ForegroundColor White
Write-Host "  2. Login with your existing dev site credentials (e.g. Administrator)." -ForegroundColor White
Write-Host "  3. Stop tunnel anytime with: powershell scripts\stop-remote-tunnel.ps1" -ForegroundColor White
Write-Host ""

# 5. Foreground vs Background Execution
if ($Background) {
    Write-Host "[+] Tunnel is running in background (PID: $($tunnelProcess.Id))." -ForegroundColor Green
    Write-Host "[+] To stop, run: powershell scripts\stop-remote-tunnel.ps1" -ForegroundColor Gray
    exit 0
}

Write-Host "Tunnel is active. Press [Ctrl+C] to stop." -ForegroundColor Magenta

try {
    while (-not $tunnelProcess.HasExited) {
        Start-Sleep -Seconds 1
    }
} finally {
    if ($tunnelProcess -and -not $tunnelProcess.HasExited) {
        Write-Host "`n[*] Stopping tunnel..." -ForegroundColor Yellow
        try {
            $tunnelProcess.Kill()
            $tunnelProcess.WaitForExit(3000)
        } catch {}
        Write-Host "[+] Tunnel stopped cleanly." -ForegroundColor Green
    }
    if (Test-Path $tunnelPid) {
        Remove-Item $tunnelPid -Force -ErrorAction SilentlyContinue
    }
}
