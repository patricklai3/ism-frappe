param (
    [switch]$Delete = $false,
    [string]$Source = "",
    [string]$Destination = "",
    [string]$Distro = "",
    [switch]$AllowWorktreeOnMain = $false
)

$ErrorActionPreference = "Stop"

function Test-PrimaryTrunkCheckout {
    param ([string]$CheckPath)
    $PrimaryRoot = if ($env:ISM_PRIMARY_REPO) { $env:ISM_PRIMARY_REPO } else { "D:\ism" }
    $ExpectedNorm = [System.IO.Path]::GetFullPath($PrimaryRoot).TrimEnd('\')

    $TopLevel = (git -C $CheckPath rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $TopLevel) { return $false, "path '$CheckPath' is not inside a git repository" }
    $TopNorm = [System.IO.Path]::GetFullPath($TopLevel.Trim().Replace('/', '\')).TrimEnd('\')
    if ($TopNorm -ine $ExpectedNorm) { return $false, "repo root '$TopNorm' is not primary checkout '$ExpectedNorm'" }

    $GitDir = (git -C $TopNorm rev-parse --path-format=absolute --git-dir 2>$null)
    $GitCommonDir = (git -C $TopNorm rev-parse --path-format=absolute --git-common-dir 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $GitDir -or -not $GitCommonDir) { return $false, "unable to inspect git worktree state" }
    $GitDirNorm = [System.IO.Path]::GetFullPath($GitDir.Trim().Replace('/', '\')).TrimEnd('\')
    $CommonDirNorm = [System.IO.Path]::GetFullPath($GitCommonDir.Trim().Replace('/', '\')).TrimEnd('\')
    if ($GitDirNorm -ine $CommonDirNorm) { return $false, "linked git worktree detected" }

    $Branch = (git -C $TopNorm rev-parse --abbrev-ref HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $Branch) { return $false, "unable to determine active git branch" }
    if ($Branch.Trim() -cne "main") { return $false, "active branch is '$($Branch.Trim())' (expected 'main')" }

    return $true, "ok"
}

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$PrimaryBenchDest = "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/"
$DefaultDestination = if ($env:ISM_WSL_DEST) { $env:ISM_WSL_DEST } else { $PrimaryBenchDest }
$WslDestination = if ($Destination) { $Destination } else { $DefaultDestination }
if (-not $WslDestination.EndsWith("/")) { $WslDestination += "/" }

$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$LocalSource = if ($Source) { (Get-Item $Source).FullName } else { (Get-Item "$PSScriptRoot\..").FullName }

if (($WslDestination -eq $PrimaryBenchDest) -and (-not $AllowWorktreeOnMain) -and ($env:ISM_ALLOW_WORKTREE_MAIN -ne "1")) {
    foreach ($CheckTarget in @($RepoRoot, $LocalSource)) {
        $Ok, $Why = Test-PrimaryTrunkCheckout -CheckPath $CheckTarget
        if (-not $Ok) {
            Write-Error "Refusing to sync to primary bench '$PrimaryBenchDest' ($Why). Run ism-frappe\scripts\test-in-slot.ps1 for isolated slot testing, or pass -AllowWorktreeOnMain outside agent sessions."
            exit 1
        }
    }
}

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
    exit $LASTEXITCODE
}
