param (
    [switch]$Delete = $false
)

$ErrorActionPreference = "Stop"

$WslSource = "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/"
$WindowsDestination = "/mnt/d/ism/"

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

$Command = "rsync -rtuv $DeleteFlag $($Excludes -join ' ') '$WslSource' '$WindowsDestination'"

Write-Host "[sync-from-wsl] Pulling WSL2 -> Windows..." -ForegroundColor Cyan
wsl -d Ubuntu bash -c $Command

if ($LASTEXITCODE -eq 0) {
    Write-Host "[sync-from-wsl] Complete." -ForegroundColor Green
} else {
    Write-Host "[sync-from-wsl] Error during rsync (exit code $LASTEXITCODE)." -ForegroundColor Red
}
