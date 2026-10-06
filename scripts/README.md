# Synchronization Scripts

These scripts synchronize the `ism` app repository between Windows (`D:\ism`) and WSL2 (`/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism`).

## Files

- [watch-sync.ps1](file:///D:/ism/scripts/watch-sync.ps1): Watches `D:\ism` and syncs changes to WSL2 using PowerShell.
- [watch-sync.mjs](file:///D:/ism/scripts/watch-sync.mjs): Watches `D:\ism` and syncs changes to WSL2 using Node.js.
- [sync-to-wsl.ps1](file:///D:/ism/scripts/sync-to-wsl.ps1): Pushes files from Windows to WSL2 once.
- [sync-from-wsl.ps1](file:///D:/ism/scripts/sync-from-wsl.ps1): Pulls files from WSL2 to Windows once.

## Usage

### 1. Start Live Synchronization
To start continuous synchronization, run one of these commands in a terminal:

Using PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\watch-sync.ps1
```

Using Node.js:
```powershell
node scripts/watch-sync.mjs
```

When you save a file in Windows, the script syncs the change to WSL2.
The Linux kernel emits `inotify` events to Frappe inside the Docker container.
`bench watch` and the Python server reload automatically.

### 2. Pull Changes from Frappe Desk
When you create a DocType in the Frappe Desk web interface, Frappe writes files inside WSL2.
To copy those new files into your Windows repository, run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-from-wsl.ps1
```

### 3. One-Time Push
To push all files to WSL2 without starting the watcher, run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
```
