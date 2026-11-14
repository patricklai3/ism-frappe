param (
    [switch]$Delete = $false,
    [string]$Source = "",
    [string]$Destination = "",
    [string]$Distro = ""
)

$ErrorActionPreference = "Stop"

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$DefaultDestination = if ($env:ISM_WSL_DEST) { $env:ISM_WSL_DEST } else { "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/" }
$WslDestination = if ($Destination) { $Destination } else { $DefaultDestination }
if (-not $WslDestination.EndsWith("/")) { $WslDestination += "/" }

$LocalSource = if ($Source) { (Get-Item $Source).FullName } else { (Get-Item "$PSScriptRoot\..").FullName }
$NormalizedSource = $LocalSource.Replace('\', '/')
$WindowsSource = (wsl -d $TargetDistro wslpath -u "$NormalizedSource").Trim() + "/"

$Excludes = @(
    "--exclude=.git",
    "--exclude=.codegraph",
    "--exclude=node_modules",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=.pytest_cache",
    "--exclude=.venv",
    "--exclude=.idea",
    "--exclude=.vscode"
)

$DeleteFlag = if ($Delete) { "--delete" } else { "" }

$Command = "rsync -rtvc $DeleteFlag $($Excludes -join ' ') '$WindowsSource' '$WslDestination'"

Write-Host "[sync-to-wsl] Syncing $NormalizedSource -> WSL2 ($TargetDistro`:$WslDestination)..." -ForegroundColor Cyan
wsl -d $TargetDistro bash -c $Command

if ($LASTEXITCODE -eq 0) {
    Write-Host "[sync-to-wsl] Complete." -ForegroundColor Green
} else {
    Write-Host "[sync-to-wsl] Error during rsync (exit code $LASTEXITCODE)." -ForegroundColor Red
}
