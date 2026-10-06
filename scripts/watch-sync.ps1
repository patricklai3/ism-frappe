<#
.SYNOPSIS
    Real-time file watcher that syncs changes from D:\ism to WSL2 Frappe bench.
#>

param (
    [string]$Path = "D:\ism",
    [int]$DebounceMs = 250
)

$IgnorePattern = '([\\/](\.git|node_modules|__pycache__|\.pytest_cache|\.venv|\.idea|\.vscode)([\\/]|$))|(\.(tmp|swp|lock)$)|(sync-(to|from)-wsl\.ps1$)'

Get-EventSubscriber -SourceIdentifier "FS_*" -ErrorAction SilentlyContinue | Unregister-Event

$Watcher = New-Object System.IO.FileSystemWatcher
$Watcher.Path = $Path
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
Write-Host " Watching: $Path" -ForegroundColor Green
Write-Host " Target:   WSL2 (/home/real2/projects/.../apps/ism/)" -ForegroundColor Green
Write-Host " Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

try {
    while ($true) {
        $event = Wait-Event -SourceIdentifier "FS_*" -Timeout 1
        if ($event) {
            $changedFile = $event.SourceEventArgs.Name
            $fullPath = $event.SourceEventArgs.FullPath
            $relativePath = ""
            if ($fullPath -and $fullPath.Length -ge $Path.Length) {
                $relativePath = $fullPath.Substring($Path.Length)
            }
            
            Start-Sleep -Milliseconds $DebounceMs
            Get-Event -SourceIdentifier "FS_*" | Remove-Event

            if ($relativePath -notmatch $IgnorePattern) {
                $timestamp = (Get-Date).ToString("HH:mm:ss")
                Write-Host "[$timestamp] Change detected ($changedFile). Syncing..." -ForegroundColor Cyan
                
                $WslCommand = "rsync -rtv --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.venv' '/mnt/d/ism/' '/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/'"
                wsl -d Ubuntu bash -c $WslCommand | Out-Null
                
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
