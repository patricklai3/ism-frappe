param (
    [switch]$AllowDirty = $false,
    [switch]$SkipSmokeTest = $false,
    [string]$SmokeModule = "ism.tests.test_simulation_foundation,ism.tests.test_estimation_math",
    [string]$Distro = ""
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

$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$Ok, $Why = Test-PrimaryTrunkCheckout -CheckPath $RepoRoot
if (-not $Ok) {
    Write-Error "Refusing promote-to-main outside canonical primary trunk ($Why)."
    exit 1
}

if (-not $AllowDirty) {
    $Dirty = (git -C $RepoRoot status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to check git status in '$RepoRoot'."
        exit 1
    }
    if ($Dirty) {
        Write-Error "Working tree '$RepoRoot' has uncommitted changes. Commit/stash first or pass -AllowDirty."
        exit 1
    }
}

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

Write-Host "[promote-to-main] 1/5 Syncing D:\ism\ism-frappe -> primary frappe-bench/apps/ism..." -ForegroundColor Cyan
& "$PSScriptRoot\sync-to-wsl.ps1" -Delete -Distro $TargetDistro
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[promote-to-main] 2/5 Running migrate + clear-cache on development.localhost..." -ForegroundColor Cyan
$ContainerPath = "PATH=/workspace/development/frappe-bench/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
wsl -d $TargetDistro --exec docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 env $ContainerPath bench --site development.localhost migrate
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
wsl -d $TargetDistro --exec docker exec -w /workspace/development/frappe-bench devcontainer-frappe-1 env $ContainerPath bench --site development.localhost clear-cache
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipSmokeTest) {
    Write-Host "[promote-to-main] 3/5 Stage A: Read-only smoke check on development.localhost..." -ForegroundColor Cyan
    $StageACode = @'
import json
import frappe

frappe.init(site="development.localhost", sites_path="/workspace/development/frappe-bench/sites")
frappe.connect()
try:
    import ism
    import ism.api.agent as agent
    settings = frappe.get_single("ISM Settings")
    status = agent.get_shop_floor_status()
    assert isinstance(status, dict), f"Unexpected shop floor status type: {type(status)}"
    print(json.dumps({"ok": True, "ism_file": ism.__file__, "company": settings.get("company"), "status_keys": sorted(status.keys())}))
finally:
    frappe.db.rollback()
    frappe.destroy()
'@
    $StageAB64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes(($StageACode -replace "`r", "")))
    wsl -d $TargetDistro --exec docker exec -w /workspace/development/frappe-bench/sites devcontainer-frappe-1 /workspace/development/frappe-bench/env/bin/python -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]))' $StageAB64
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Stage A read-only trunk smoke check failed; aborting before updating golden DB snapshot."
        exit $LASTEXITCODE
    }
} else {
    Write-Host "[promote-to-main] 3/5 Stage A skipped (-SkipSmokeTest)." -ForegroundColor Yellow
}

Write-Host "[promote-to-main] 4/5 Refreshing golden DB snapshot and bench slots..." -ForegroundColor Cyan
& "$PSScriptRoot\provision-bench-slots.ps1" -RefreshGolden -Distro $TargetDistro
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipSmokeTest) {
    Write-Host "[promote-to-main] 5/5 Stage B: Running isolated smoke suite ($SmokeModule)..." -ForegroundColor Cyan
    & "$PSScriptRoot\test-in-slot.ps1" -Module $SmokeModule -Distro $TargetDistro
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else {
    Write-Host "[promote-to-main] 5/5 Stage B skipped (-SkipSmokeTest)." -ForegroundColor Yellow
}

Write-Host "[promote-to-main] Trunk promotion complete." -ForegroundColor Green
exit 0
