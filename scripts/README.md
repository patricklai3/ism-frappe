# Synchronization Scripts

These scripts synchronize the `ism` app repository between the Windows host and the WSL2 Frappe bench (`apps/ism`).

All scripts detect the repository root dynamically relative to their location. They work without configuration in the primary checkout (`D:\ism`), in Git worktrees (such as `C:\Users\...\worktrees\...`), or on alternate drives.

## Files

- [sync-to-wsl.ps1](file:///d:/ism/scripts/sync-to-wsl.ps1): Pushes files from Windows to WSL2 once using checksum verification.
- [sync-from-wsl.ps1](file:///d:/ism/scripts/sync-from-wsl.ps1): Pulls files from WSL2 to Windows once.
- [watch-sync.ps1](file:///d:/ism/scripts/watch-sync.ps1): Watches the repository for changes and pushes them to WSL2 automatically (PowerShell).
- [watch-sync.mjs](file:///d:/ism/scripts/watch-sync.mjs): Watches the repository for changes and pushes them to WSL2 automatically (Node.js).

## Usage

### 1. One-Time Push (Windows to WSL2)
Push all modified files to WSL2:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-to-wsl.ps1
```

Optional parameters:
- `-Source "<path>"`: Custom local directory to push (defaults to repo root).
- `-Destination "<wsl-path>"`: Custom target directory in WSL2.
- `-Distro "<distro>"`: Target WSL distribution (defaults to `Ubuntu`).
- `-Delete`: Delete remote files not present in the local source.

### 2. Pull Changes from Frappe Desk (WSL2 to Windows)
When you create a DocType in Frappe Desk, Frappe writes files inside WSL2. Pull them back to Windows:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\sync-from-wsl.ps1
```

Optional parameters:
- `-Source "<wsl-path>"`: Custom WSL source directory.
- `-Destination "<path>"`: Custom local destination directory (defaults to repo root).
- `-Distro "<distro>"`: Target WSL distribution (defaults to `Ubuntu`).
- `-Delete`: Delete local files not present in WSL2.

### 3. Live File Watcher
Start continuous synchronization on file save:

Using PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\watch-sync.ps1
```

Using Node.js:
```powershell
node scripts/watch-sync.mjs
```

### 4. Environment Variables
You can configure defaults across all scripts using environment variables:
- `ISM_WSL_DEST`: Target path in WSL2 (default: `/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/`).
- `ISM_WSL_DISTRO`: Target WSL distribution (default: `Ubuntu`).

