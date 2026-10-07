param (
    [switch]$Delete = $false
)

$ErrorActionPreference = "Stop"

$RootDir = (Get-Item "$PSScriptRoot\..").FullName.Replace('\', '/')
$WindowsSource = (wsl wslpath -u $RootDir).Trim() + "/"
$WslDestination = "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/"

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

$Command = "rsync -rtvc $DeleteFlag $($Excludes -join ' ') '$WindowsSource' '$WslDestination'"

Write-Host "[sync-to-wsl] Syncing Windows -> WSL2..." -ForegroundColor Cyan
wsl -d Ubuntu bash -c $Command

if ($LASTEXITCODE -eq 0) {
    Write-Host "[sync-to-wsl] Complete." -ForegroundColor Green
} else {
    Write-Host "[sync-to-wsl] Error during rsync (exit code $LASTEXITCODE)." -ForegroundColor Red
}
