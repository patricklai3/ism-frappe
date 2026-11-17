param (
    [string]$Module = "",
    [string]$DocType = "",
    [string]$Test = "",
    [string]$Execute = "",
    [ValidateSet("", "start", "stop", "foreground")]
    [string]$Serve = "",
    [int]$Slot = 0,
    [int]$Slots = 4,
    [switch]$FreshDb = $false,
    [switch]$KeepDb = $false,
    [switch]$SkipMigrate = $false,
    [string]$Worktree = "",
    [string[]]$ExtraArgs = @(),
    [string]$Distro = ""
)

$ErrorActionPreference = "Stop"

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

if ($Worktree) {
    $ResolvedRepo = (Resolve-Path $Worktree).Path
} else {
    $ResolvedRepo = (Resolve-Path "$PSScriptRoot\..\..").Path
}

$GitTop = (git -C $ResolvedRepo rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -eq 0 -and $GitTop) {
    $WorktreeRoot = [System.IO.Path]::GetFullPath($GitTop.Trim().Replace('/', '\')).TrimEnd('\')
    $Branch = (git -C $WorktreeRoot rev-parse --abbrev-ref HEAD 2>$null)
    $HeadSha = (git -C $WorktreeRoot rev-parse HEAD 2>$null)
} else {
    $WorktreeRoot = [System.IO.Path]::GetFullPath($ResolvedRepo).TrimEnd('\')
    $Branch = "nogit"
    $HeadSha = "nogit"
}

$IsmFrappeDir = Join-Path $WorktreeRoot "ism-frappe"
if (-not (Test-Path (Join-Path $IsmFrappeDir "pyproject.toml"))) {
    if (Test-Path (Join-Path $ResolvedRepo "pyproject.toml")) {
        $IsmFrappeDir = $ResolvedRepo
    } else {
        Write-Error "Could not locate ism-frappe/pyproject.toml under '$WorktreeRoot'."
        exit 1
    }
}

function Ensure-LfNoBom {
    param ([string]$FilePath)
    $bytes = [System.IO.File]::ReadAllBytes($FilePath)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $hasCr = ($bytes -contains 0x0D)
    if ($hasBom -or $hasCr) {
        $text = [System.Text.Encoding]::UTF8.GetString($bytes)
        if ($hasBom) { $text = $text.TrimStart([char]0xFEFF) }
        $text = $text.Replace("`r`n", "`n").Replace("`r", "`n")
        [System.IO.File]::WriteAllBytes($FilePath, ([System.Text.UTF8Encoding]::new($false)).GetBytes($text))
    }
}

$NormalizedSource = $IsmFrappeDir.Replace('\', '/')
$WslSourceDir = (wsl -d $TargetDistro --exec wslpath -u "$NormalizedSource").Trim()
if ($LASTEXITCODE -ne 0 -or -not $WslSourceDir) {
    Write-Error "Failed to convert '$NormalizedSource' to WSL path."
    exit 1
}

$WinRunnerRealPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\run-in-slot.sh")).Path
Ensure-LfNoBom -FilePath $WinRunnerRealPath
$WinRunnerPath = $WinRunnerRealPath.Replace('\', '/')
$WslRunnerPath = (wsl -d $TargetDistro --exec wslpath -u "$WinRunnerPath").Trim()
if ($LASTEXITCODE -ne 0 -or -not $WslRunnerPath) {
    Write-Error "Failed to convert '$WinRunnerPath' to WSL path."
    exit 1
}

$Mode = "test"
if ($Serve) {
    $Mode = "serve"
} elseif ($Execute) {
    $Mode = "execute"
}

$WorktreeId = $WorktreeRoot.ToLowerInvariant().Replace('\', '/')
$BranchVal = if ($Branch) { $Branch.Trim() } else { "detached" }
$HeadVal = if ($HeadSha) { $HeadSha.Trim() } else { "unknown" }

$ArgList = @(
    "--worktree-id", $WorktreeId,
    "--source-dir", $WslSourceDir,
    "--branch", $BranchVal,
    "--head", $HeadVal,
    "--slot", "$Slot",
    "--slots", "$Slots",
    "--mode", $Mode
)

if ($FreshDb) { $ArgList += "--fresh-db" }
if ($KeepDb) { $ArgList += "--keep-db" }
if ($SkipMigrate) { $ArgList += "--skip-migrate" }
if ($Module) { $ArgList += @("--module", $Module) }
if ($DocType) { $ArgList += @("--doctype", $DocType) }
if ($Test) { $ArgList += @("--test", $Test) }
if ($Execute) { $ArgList += @("--execute-fn", $Execute) }
if ($Serve) { $ArgList += @("--serve-action", $Serve) }

if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
    $ArgList += "--"
    $ArgList += $ExtraArgs
}

wsl -d $TargetDistro --exec bash $WslRunnerPath @ArgList
exit $LASTEXITCODE
