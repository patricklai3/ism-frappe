param (
    [string[]]$Module = @(),
    [string]$DocType = "",
    [string[]]$Test = @(),
    [string]$Execute = "",
    [ValidateSet("", "start", "stop", "foreground")]
    [string]$Serve = "",
    [int]$Slot = 0,
    [int]$Slots = 5,
    [switch]$FreshDb = $false,
    [switch]$CleanReinstall = $false,
    [switch]$KeepDb = $false,
    [switch]$SkipMigrate = $false,
    [switch]$ForceMigrate = $false,
    [switch]$ForceSkipMigrate = $false,
    [switch]$AllowParallelSlots = $false,
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
    if (-not (Test-Path $FilePath)) { return }
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

function ConvertTo-WslPath {
    param ([string]$WinPath, [string]$DistroName)
    $norm = $WinPath.Replace('\', '/')
    if ($norm -match '^([A-Za-z]):/(.*)$') {
        return "/mnt/$($Matches[1].ToLowerInvariant())/$($Matches[2].TrimEnd('/'))"
    }
    $resolved = (wsl -d $DistroName --exec wslpath -u "$norm").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $resolved) {
        Write-Error "Failed to convert '$norm' to WSL path."
        exit 1
    }
    return $resolved
}

function Expand-CsvArgs {
    param ([string[]]$Values)
    $out = @()
    foreach ($v in $Values) {
        if (-not $v) { continue }
        foreach ($part in ($v -split ',')) {
            $trimmed = $part.Trim()
            if ($trimmed) { $out += $trimmed }
        }
    }
    return $out
}

$WslSourceDir = ConvertTo-WslPath -WinPath $IsmFrappeDir -DistroName $TargetDistro

$WinCommonPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\slot-common.sh")).Path
Ensure-LfNoBom -FilePath $WinCommonPath
$WinRunnerRealPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\run-in-slot.sh")).Path
Ensure-LfNoBom -FilePath $WinRunnerRealPath
$WslRunnerPath = ConvertTo-WslPath -WinPath $WinRunnerRealPath -DistroName $TargetDistro

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
if ($CleanReinstall) { $ArgList += "--clean-reinstall" }
if ($KeepDb) { $ArgList += "--keep-db" }
if ($SkipMigrate) { $ArgList += "--skip-migrate" }
if ($ForceMigrate) { $ArgList += "--force-migrate" }
if ($ForceSkipMigrate) { $ArgList += "--force-skip-migrate" }
if ($AllowParallelSlots) { $ArgList += "--allow-parallel-slots" }

foreach ($m in (Expand-CsvArgs -Values $Module)) {
    $ArgList += @("--module", $m)
}
if ($DocType) { $ArgList += @("--doctype", $DocType) }
foreach ($t in (Expand-CsvArgs -Values $Test)) {
    $ArgList += @("--test", $t)
}
if ($Execute) { $ArgList += @("--execute-fn", $Execute) }
if ($Serve) { $ArgList += @("--serve-action", $Serve) }

if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
    $ArgList += "--"
    $ArgList += $ExtraArgs
}

wsl -d $TargetDistro --exec bash $WslRunnerPath @ArgList
exit $LASTEXITCODE
