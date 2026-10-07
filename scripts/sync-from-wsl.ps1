param (
    [switch]$Delete = $false,
    [string]$Source = "",
    [string]$Destination = "",
    [string]$Distro = ""
)

$ErrorActionPreference = "Stop"

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$DefaultSource = if ($env:ISM_WSL_DEST) { $env:ISM_WSL_DEST } else { "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/" }
$WslSource = if ($Source) { $Source } else { $DefaultSource }
if (-not $WslSource.EndsWith("/")) { $WslSource += "/" }

$LocalDestination = if ($Destination) { (Get-Item $Destination).FullName } else { (Get-Item "$PSScriptRoot\..").FullName }
$NormalizedDestination = $LocalDestination.Replace('\', '/')
$WindowsDestination = (wsl -d $TargetDistro wslpath -u "$NormalizedDestination").Trim() + "/"

$Excludes = @(
    "--exclude=.git",
    "--exclude=node_modules",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "--exclude=.pytest_cache",
    "--exclude=.venv",
    "--exclude=.idea",
    "--exclude=.vscode"
)

$DeleteFlag = if ($Delete) { "--delete" } else { "" }

$Command = "rsync -rtuvc $DeleteFlag $($Excludes -join ' ') '$WslSource' '$WindowsDestination'"

Write-Host "[sync-from-wsl] Pulling WSL2 ($TargetDistro`:$WslSource) -> $NormalizedDestination..." -ForegroundColor Cyan
wsl -d $TargetDistro bash -c $Command

if ($LASTEXITCODE -eq 0) {
    Write-Host "[sync-from-wsl] Complete." -ForegroundColor Green
} else {
    Write-Host "[sync-from-wsl] Error during rsync (exit code $LASTEXITCODE)." -ForegroundColor Red
}
