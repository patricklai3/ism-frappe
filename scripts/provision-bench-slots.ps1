param (
    [int]$Slots = 5,
    [switch]$RefreshGolden = $false,
    [switch]$Recreate = $false,
    [switch]$Status = $false,
    [int]$History = 0,
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

$IsMutating = ($RefreshGolden -or $Recreate -or (-not $Status -and $History -le 0))
if ($IsMutating) {
    $RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
    $Ok, $Why = Test-PrimaryTrunkCheckout -CheckPath $RepoRoot
    if (-not $Ok) {
        Write-Error "Refusing mutating bench slot provisioning outside primary trunk ($Why). Only -Status and -History are allowed from worktrees."
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

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$WinCommonPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\slot-common.sh")).Path
Ensure-LfNoBom -FilePath $WinCommonPath
$WinScriptRealPath = (Resolve-Path (Join-Path $PSScriptRoot "wsl\provision-slots.sh")).Path
Ensure-LfNoBom -FilePath $WinScriptRealPath
$WslScriptPath = ConvertTo-WslPath -WinPath $WinScriptRealPath -DistroName $TargetDistro

$ArgList = @("--slots", "$Slots")
if ($RefreshGolden) { $ArgList += "--refresh-golden" }
if ($Recreate) { $ArgList += "--recreate" }
if ($Status) { $ArgList += "--status" }
if ($History -gt 0) { $ArgList += @("--history", "$History") }

wsl -d $TargetDistro --exec bash $WslScriptPath @ArgList
exit $LASTEXITCODE
