<#
.SYNOPSIS
    Real-time file watcher that syncs changes from the local repository to WSL2 Frappe bench.
#>

param (
    [string]$Path = "",
    [string]$Destination = "",
    [string]$Distro = "",
    [int]$DebounceMs = 250
)

$DefaultDistro = if ($env:ISM_WSL_DISTRO) { $env:ISM_WSL_DISTRO } else { "Ubuntu" }
$TargetDistro = if ($Distro) { $Distro } else { $DefaultDistro }

$DefaultDestination = if ($env:ISM_WSL_DEST) { $env:ISM_WSL_DEST } else { "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/" }
$WslDestination = if ($Destination) { $Destination } else { $DefaultDestination }
if (-not $WslDestination.EndsWith("/")) { $WslDestination += "/" }

$LocalWatchPath = if ($Path) { (Get-Item $Path).FullName } else { (Get-Item "$PSScriptRoot\..").FullName }
$NormalizedWatchPath = $LocalWatchPath.Replace('\', '/')
$WslSource = (wsl -d $TargetDistro wslpath -u "$NormalizedWatchPath").Trim() + "/"

$IgnorePattern = '([\\/](\.git|node_modules|__pycache__|\.pytest_cache|\.venv|\.idea|\.vscode)([\\/]|$))|(\.(tmp|swp|lock)$)|(sync-(to|from)-wsl\.ps1$)'

Get-EventSubscriber -SourceIdentifier "FS_*" -ErrorAction SilentlyContinue | Unregister-Event

$Watcher = New-Object System.IO.FileSystemWatcher
$Watcher.Path = $LocalWatchPath
$Watcher.IncludeSubdirectories = $true
$Watcher.EnableRaisingEvents = $true
$Watcher.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor 
                        [System.IO.NotifyFilters]::LastWrite -bor 
                        [System.IO.NotifyFilters]::CreationTime -bor 
                        [System.IO.NotifyFilters]::DirectoryName

$SubChanged = Register-ObjectEvent $Watcher "Changed" -SourceIdentifier "FS_Changed"
$SubCreated = Register-ObjectEvent $Watcher "Created" -SourceIdentifier "FS_Created"
$SubDeleted = Register-ObjectEvent $Watcher "Deleted" -SourceIdentifier "FS_Deleted"
$SubRenamed = Register-ObjectEvent $Watcher "Renamed" -SourceIdentifier "FS_Renamed"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " Watching: $LocalWatchPath" -ForegroundColor Green
Write-Host " Target:   WSL2 ($TargetDistro`:$WslDestination)" -ForegroundColor Green
Write-Host " Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

try {
    while ($true) {
        $event = Wait-Event -SourceIdentifier "FS_*" -Timeout 1
        if ($event) {
            $changedFile = $event.SourceEventArgs.Name
            $fullPath = $event.SourceEventArgs.FullPath
            $relativePath = ""
            if ($fullPath -and $fullPath.Length -ge $LocalWatchPath.Length) {
                $relativePath = $fullPath.Substring($LocalWatchPath.Length)
            }
            
            Start-Sleep -Milliseconds $DebounceMs
            Get-Event -SourceIdentifier "FS_*" | Remove-Event

            if ($relativePath -notmatch $IgnorePattern) {
                $timestamp = (Get-Date).ToString("HH:mm:ss")
                Write-Host "[$timestamp] Change detected ($changedFile). Syncing..." -ForegroundColor Cyan
                
                $WslCommand = "rsync -rtvc --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.venv' '$WslSource' '$WslDestination'"
                wsl -d $TargetDistro bash -c $WslCommand | Out-Null
                
                Write-Host "[$timestamp] Synced to WSL2 successfully." -ForegroundColor Green
            }
        }
    }
}
finally {
    $Watcher.EnableRaisingEvents = $false
    Get-EventSubscriber -SourceIdentifier "FS_*" -ErrorAction SilentlyContinue | Unregister-Event
    $Watcher.Dispose()
    Write-Host "`nWatcher stopped." -ForegroundColor Yellow
}
