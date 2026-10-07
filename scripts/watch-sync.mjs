import { watch } from "node:fs";
import { exec, execSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const DISTRO = process.env.ISM_WSL_DISTRO || "Ubuntu";
const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url)).replace(/[/\\]+$/, "");
const WATCH_DIR = process.env.ISM_WATCH_DIR || REPO_ROOT;
const DEBOUNCE_MS = 250;
const IGNORE_REGEX = /([\\/](\.git|node_modules|__pycache__|\.pytest_cache|\.venv|\.idea|\.vscode)([\\/]|$))|(\.(tmp|swp|lock)$)/;

let wslSource = "";
try {
  const normalized = WATCH_DIR.replace(/\\/g, "/");
  wslSource = execSync(`wsl -d ${DISTRO} wslpath -u "${normalized}"`, { encoding: "utf-8" }).trim() + "/";
} catch (err) {
  console.error(`\x1b[31mFailed to resolve WSL path for ${WATCH_DIR}:\x1b[0m`, err.message);
  process.exit(1);
}

const defaultDest = "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/";
let wslDest = process.env.ISM_WSL_DEST || defaultDest;
if (!wslDest.endsWith("/")) {
  wslDest += "/";
}

const WSL_COMMAND = `rsync -rtvc --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='.venv' '${wslSource}' '${wslDest}'`;

let debounceTimer = null;
let lastChangedFile = "";
let isSyncing = false;

function triggerSync() {
  if (isSyncing) {
    debounceTimer = setTimeout(triggerSync, DEBOUNCE_MS);
    return;
  }

  isSyncing = true;
  const time = new Date().toLocaleTimeString();
  console.log(`\x1b[36m[${time}] Change detected (${lastChangedFile}). Syncing to WSL2...\x1b[0m`);

  exec(`wsl -d ${DISTRO} bash -c "${WSL_COMMAND}"`, (error) => {
    isSyncing = false;
    const finishTime = new Date().toLocaleTimeString();
    if (error) {
      console.error(`\x1b[31m[${finishTime}] Sync failed:\x1b[0m`, error.message);
    } else {
      console.log(`\x1b[32m[${finishTime}] Synced to WSL2 successfully.\x1b[0m`);
    }
  });
}

console.log("\x1b[36m==========================================================");
console.log(` Watching: ${WATCH_DIR}`);
console.log(` Target:   WSL2 (${DISTRO}:${wslDest})`);
console.log(" Press Ctrl+C to stop.");
console.log("==========================================================\x1b[0m");

watch(WATCH_DIR, { recursive: true }, (eventType, filename) => {
  if (!filename) return;
  if (IGNORE_REGEX.test(filename)) return;

  lastChangedFile = filename;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(triggerSync, DEBOUNCE_MS);
});
