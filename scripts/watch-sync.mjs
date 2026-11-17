import { watch } from "node:fs";
import { exec, execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DISTRO = process.env.ISM_WSL_DISTRO || "Ubuntu";
const REPO_ROOT = fileURLToPath(new URL("..", import.meta.url)).replace(/[/\\]+$/, "");
const GIT_ROOT_CANDIDATE = path.resolve(REPO_ROOT, "..");
const WATCH_DIR = process.env.ISM_WATCH_DIR || REPO_ROOT;
const DEBOUNCE_MS = 250;
const IGNORE_REGEX = /([\\/](\.git|node_modules|__pycache__|\.pytest_cache|\.venv|\.idea|\.vscode)([\\/]|$))|(\.(tmp|swp|lock)$)/;

const defaultDest = "/home/real2/projects/.project/frappe_docker/development/frappe-bench/apps/ism/";
let wslDest = process.env.ISM_WSL_DEST || defaultDest;
if (!wslDest.endsWith("/")) {
  wslDest += "/";
}

function normPath(p) {
  const resolved = path.resolve(p).replace(/[\\/]+$/, "");
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function assertPrimaryTrunkCheckout(checkPath) {
  const primaryRoot = process.env.ISM_PRIMARY_REPO || "D:\\ism";
  let toplevel = "";
  try {
    toplevel = execSync(`git -C "${checkPath}" rev-parse --show-toplevel`, { encoding: "utf-8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    throw new Error(`path '${checkPath}' is not inside a git repository`);
  }
  if (normPath(toplevel) !== normPath(primaryRoot)) {
    throw new Error(`repo root '${toplevel}' is not primary checkout '${primaryRoot}'`);
  }
  const gitDir = execSync(`git -C "${toplevel}" rev-parse --path-format=absolute --git-dir`, { encoding: "utf-8" }).trim();
  const commonDir = execSync(`git -C "${toplevel}" rev-parse --path-format=absolute --git-common-dir`, { encoding: "utf-8" }).trim();
  if (normPath(gitDir) !== normPath(commonDir)) {
    throw new Error("linked git worktree detected");
  }
  const branch = execSync(`git -C "${toplevel}" rev-parse --abbrev-ref HEAD`, { encoding: "utf-8" }).trim();
  if (branch !== "main") {
    throw new Error(`active branch is '${branch}' (expected 'main')`);
  }
}

if (wslDest === defaultDest && process.env.ISM_ALLOW_WORKTREE_MAIN !== "1") {
  for (const checkTarget of [GIT_ROOT_CANDIDATE, WATCH_DIR]) {
    try {
      assertPrimaryTrunkCheckout(checkTarget);
    } catch (err) {
      console.error(`\x1b[31mRefusing to start watch-sync targeting primary bench '${defaultDest}' (${err.message}). Run ism-frappe/scripts/test-in-slot.ps1 for isolated slot testing.\x1b[0m`);
      process.exit(1);
    }
  }
}

let wslSource = "";
try {
  const normalized = WATCH_DIR.replace(/\\/g, "/");
  wslSource = execSync(`wsl -d ${DISTRO} wslpath -u "${normalized}"`, { encoding: "utf-8" }).trim() + "/";
} catch (err) {
  console.error(`\x1b[31mFailed to resolve WSL path for ${WATCH_DIR}:\x1b[0m`, err.message);
  process.exit(1);
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
