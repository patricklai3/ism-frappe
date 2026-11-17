#!/usr/bin/env bash
# Shared constants and helpers for Frappe bench-slot provisioning and leasing.

WSL_DEV_ROOT="/home/real2/projects/.project/frappe_docker/development"
WSL_PRIMARY_BENCH="$WSL_DEV_ROOT/frappe-bench"
WSL_SLOTS_ROOT="$WSL_DEV_ROOT/bench-slots"
CONTAINER_DEV_ROOT="/workspace/development"
CONTAINER_PRIMARY_BENCH="$CONTAINER_DEV_ROOT/frappe-bench"
CONTAINER_SLOTS_ROOT="$CONTAINER_DEV_ROOT/bench-slots"
LOCK_DIR="/tmp/ism-bench-slots"

CONTAINER_FRAPPE="devcontainer-frappe-1"
CONTAINER_DB="devcontainer-mariadb-1"
CONTAINER_REDIS_CACHE="devcontainer-redis-cache-1"
CONTAINER_REDIS_QUEUE="devcontainer-redis-queue-1"
MARIADB_ROOT_PW="123"

GOLDEN_FILE="$WSL_SLOTS_ROOT/golden/main_golden.sql.gz"
GOLDEN_FP_FILE="$WSL_SLOTS_ROOT/golden/golden_schema.fp"
HISTORY_FILE="$LOCK_DIR/lease-history.jsonl"

read_meta_value() {
  local meta_file="$1"
  local key="$2"
  if [[ -f "$meta_file" ]]; then
    awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$meta_file"
  fi
}

write_meta_file() {
  local meta_file="$1"
  local last_wt="$2"
  local last_branch="$3"
  local last_head="$4"
  local db_state="$5"
  local schema_fp="${6:-}"
  local now_iso now_epoch tmp_file
  now_iso="$(date -Iseconds)"
  now_epoch="$(date +%s)"
  tmp_file="${meta_file}.tmp.$$"

  cat > "$tmp_file" <<META
last_worktree=$last_wt
last_branch=$last_branch
last_head=$last_head
db_state=$db_state
schema_fp=$schema_fp
updated_at=$now_iso
updated_at_epoch=$now_epoch
META
  mv -f "$tmp_file" "$meta_file"
}

compute_schema_fp() {
  local app_root="$1"
  python3 - "$app_root" <<'PY'
import hashlib
import os
import sys

root = os.path.realpath(sys.argv[1])
if not os.path.isdir(root):
    print("")
    sys.exit(0)

skip_dirs = {".git", ".codegraph", "node_modules", "__pycache__", ".pytest_cache", ".venv", ".idea", ".vscode"}
targets = []

for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs)
    for fn in sorted(filenames):
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, root).replace("\\", "/")
        if not rel.startswith("ism/"):
            continue
        parts = rel.split("/")
        include = False
        if fn in ("patches.txt", "hooks.py"):
            include = True
        elif fn.endswith(".json"):
            include = True
        elif fn.endswith(".py") and ("patches" in parts or "setup_tasks" in parts):
            include = True
        if include:
            targets.append((rel, full))

targets.sort(key=lambda item: item[0])
h = hashlib.sha256()
for rel, full in targets:
    h.update(rel.encode("utf-8"))
    h.update(b"\0")
    try:
        with open(full, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        h.update(b"<missing>")
    h.update(b"\0")

print(h.hexdigest())
PY
}

now_ms() {
  python3 -c "import time; print(int(time.time() * 1000))"
}
