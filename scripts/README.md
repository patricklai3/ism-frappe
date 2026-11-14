# Synchronization Scripts

These scripts synchronize the `ism` app repository between the Windows host and the WSL2 Frappe bench (`apps/ism`).

All scripts detect the repository root dynamically relative to their location. They work without configuration in the primary checkout (`D:\ism`), in Git worktrees (such as `C:\Users\...\worktrees\...`), or on alternate drives.

## Files

- [sync-to-wsl.ps1](sync-to-wsl.ps1): Pushes files from Windows to WSL2 once using checksum verification.
- [sync-from-wsl.ps1](sync-from-wsl.ps1): Pulls files from WSL2 to Windows once.
- [watch-sync.ps1](watch-sync.ps1): Watches the repository for changes and pushes them to WSL2 automatically (PowerShell).
- [watch-sync.mjs](watch-sync.mjs): Watches the repository for changes and pushes them to WSL2 automatically (Node.js).
- [start-remote-tunnel.ps1](start-remote-tunnel.ps1): Exposes dev site on port 8000 via Cloudflare Tunnel with custom domain or quick URL and mobile QR code.
- [stop-remote-tunnel.ps1](stop-remote-tunnel.ps1): Stops active Cloudflare Tunnel processes.
- [verify_mcp_agent.py](../ism/scripts/verify_mcp_agent.py): Verification suite for `ism.api.agent` via Frappe v16 native MCP protocol (`bench --site <site> execute ism.scripts.verify_mcp_agent.run`).
- [ism-agent-os](../ism-agent-os/tools/cli.py): Python package (`ism_agent_os`) and REST/CLI adapters for Frappe v16 + ISM.
- [.tunnel-config.example.json](.tunnel-config.example.json): Configuration template for your Cloudflare custom domain and tunnel token.


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
- `CLOUDFLARE_TUNNEL_TOKEN`: Optional default tunnel token for remote testing.

---

## Remote Testing Access (Cloudflare Tunnel)

These scripts allow you to test your local dev site (`development.localhost` on port 8000) from your smartphone, tablet, or external laptop when away from this PC.

### Quick Start (Instant Temporary URL)

Start an instant temporary tunnel (no Cloudflare account required):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-remote-tunnel.ps1 -Quick
```

The script will:
1. Verify the dev site is responding on port 8000.
2. Connect to Cloudflare's edge network.
3. Print your live public HTTPS URL (e.g. `https://xxx.trycloudflare.com`).
4. Display a scannable ASCII QR code to open the site directly on your phone camera.

To stop the tunnel, press `Ctrl+C` in the terminal, or run:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop-remote-tunnel.ps1
```

### Permanent Custom Domain Setup

To use a fixed, permanent URL (e.g. `https://dev.yourdomain.com`):

1. **Create Tunnel in Cloudflare**:
   - Log in to [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/).
   - Navigate to **Networks** > **Tunnels** > **Create a tunnel**.
   - Select **Cloudflared** and choose a name (e.g. `ism-dev`).
   - Under "Install and run a connector", copy the token string from the `cloudflared.exe service install <TOKEN>` command.
   - Go to the **Public Hostname** tab in Cloudflare, click **Add a public hostname**:
     - Subdomain: `dev` (or desired subdomain)
     - Domain: `yourdomain.com`
     - Service: `HTTP`
     - URL: `localhost:8000`
   - Save the hostname.

2. **Configure Locally**:
   Copy the example config:
   ```powershell
   Copy-Item scripts\.tunnel-config.example.json scripts\.tunnel-config.json
   ```
   Edit `scripts\.tunnel-config.json` with your details:
   ```json
   {
     "domain": "dev.yourdomain.com",
     "tunnel_token": "<YOUR_TUNNEL_TOKEN>"
   }
   ```
   *(Note: `scripts\.tunnel-config.json` is gitignored so your token is never committed).*

3. **Launch Custom Domain Tunnel**:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start-remote-tunnel.ps1
   ```

### Optional Parameters

| Parameter | Description |
|-----------|-------------|
| `-Quick` | Force an instant Quick Tunnel on `trycloudflare.com` even if a token is configured. |
| `-Token "<token>"` | Provide or override tunnel token directly via CLI. |
| `-Domain "<domain>"` | Provide or override target domain name via CLI. |
| `-Background` | Run the tunnel in background and return PID immediately. |
| `-NoQr` | Suppress rendering the ASCII QR code in the terminal. |
| `-Port <int>` | Custom local port (defaults to 8000). |


