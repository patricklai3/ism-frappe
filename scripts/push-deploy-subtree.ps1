param (
    [string]$Remote = "frappe-deploy",
    [string]$Branch = "staging",
    [switch]$Force = $false,
    [switch]$DryRun = $false
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path

$DirtyIsmFrappe = git -C $RepoRoot status --porcelain -- ism-frappe
if ($LASTEXITCODE -ne 0) {
    throw "Failed to inspect git status in $RepoRoot"
}
if ($DirtyIsmFrappe) {
    Write-Warning "Uncommitted changes exist under ism-frappe/. 'git subtree split' only includes committed HEAD:"
    $DirtyIsmFrappe | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
}

Write-Host "[push-deploy-subtree] Splitting ism-frappe from HEAD in $RepoRoot..." -ForegroundColor Cyan
$SplitSha = (git -C $RepoRoot subtree split --prefix=ism-frappe HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $SplitSha) {
    throw "git subtree split --prefix=ism-frappe HEAD failed."
}

git -C $RepoRoot cat-file -e "${SplitSha}:pyproject.toml"
if ($LASTEXITCODE -ne 0) {
    throw "Split commit $SplitSha is missing pyproject.toml at root."
}

git -C $RepoRoot cat-file -e "${SplitSha}:ism/hooks.py"
if ($LASTEXITCODE -ne 0) {
    throw "Split commit $SplitSha is missing ism/hooks.py at root."
}

if ($DryRun) {
    Write-Host "[push-deploy-subtree] DryRun OK. Split commit: $SplitSha" -ForegroundColor Green
    Write-Host "[push-deploy-subtree] Root tree entries for ${SplitSha}:" -ForegroundColor Cyan
    git -C $RepoRoot ls-tree --name-only $SplitSha
    if ($LASTEXITCODE -ne 0) {
        throw "git ls-tree failed for $SplitSha."
    }
    exit 0
}

$RemoteUrl = git -C $RepoRoot remote get-url $Remote 2>$null
if ($LASTEXITCODE -ne 0 -or -not $RemoteUrl) {
    throw "Git remote '$Remote' is not configured. Add it first, e.g.: git remote add $Remote git@github.com:<owner>/ism-frappe.git"
}

$PushArgs = @("-C", $RepoRoot, "push")
if ($Force) {
    $PushArgs += "--force-with-lease"
}
$PushArgs += @($Remote, "${SplitSha}:refs/heads/$Branch")

Write-Host "[push-deploy-subtree] Pushing $SplitSha -> $Remote ($Branch)..." -ForegroundColor Cyan
& git @PushArgs
if ($LASTEXITCODE -ne 0) {
    throw "git push to $Remote ($Branch) failed with exit code $LASTEXITCODE."
}

Write-Host "[push-deploy-subtree] Complete." -ForegroundColor Green
