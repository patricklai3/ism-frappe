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

install_clean_ism_site() {
  local bench_wsl_dir="$1"
  local bench_container_dir="$2"
  local site_name="$3"
  local db_name="$4"
  local db_pw="$5"
  local redis_idx="$6"
  local apps_csv="${7:-erpnext,hrms,ism}"

  local site_dir="$bench_wsl_dir/sites/$site_name"
  local site_cfg="$site_dir/site_config.json"
  local common_cfg="$bench_wsl_dir/sites/common_site_config.json"

  mkdir -p "$site_dir"
  local enc_key=""
  if [[ -f "$site_cfg" ]]; then
    enc_key="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("encryption_key",""))' "$site_cfg" 2>/dev/null || true)"
  fi
  if [[ -z "$enc_key" && -f "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" ]]; then
    enc_key="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("encryption_key",""))' "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" 2>/dev/null || true)"
  fi

  python3 - "$common_cfg" <<'PY'
import json, os, sys
path = sys.argv[1]
cfg = json.load(open(path, "r", encoding="utf-8")) if os.path.isfile(path) else {}
cfg["mariadb_user_host_login_scope"] = "%"
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=1)
    f.write("\n")
PY

  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$redis_idx" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$redis_idx" FLUSHDB >/dev/null

  local container_path="$bench_container_dir/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

  # Create fresh Frappe site (base frappe app only), checking CLI root-password flag compatibility.
  docker exec -i -w "$bench_container_dir" "$CONTAINER_FRAPPE" env PATH="$container_path" bash -s -- \
    "$site_name" "$db_name" "$MARIADB_ROOT_PW" <<'EOF'
set -euo pipefail
site="$1"
db_name="$2"
root_pw="$3"

help_txt="$(bench new-site --help 2>&1 || true)"
cmd=(bench new-site "$site" --force --db-name "$db_name" --admin-password admin)
if printf '%s' "$help_txt" | grep -q -- '--mariadb-root-username'; then
  cmd+=(--mariadb-root-username root)
fi
if printf '%s' "$help_txt" | grep -q -- '--mariadb-root-password'; then
  cmd+=(--mariadb-root-password "$root_pw")
elif printf '%s' "$help_txt" | grep -q -- '--db-root-password'; then
  cmd+=(--db-root-password "$root_pw")
fi
if printf '%s' "$help_txt" | grep -q -- '--mariadb-user-host-login-scope'; then
  cmd+=(--mariadb-user-host-login-scope '%')
fi
if printf '%s' "$help_txt" | grep -q -- '--no-mariadb-socket'; then
  cmd+=(--no-mariadb-socket)
fi
echo "[clean-install] Running: ${cmd[*]}"
"${cmd[@]}"
EOF

  # Normalize MariaDB '%' grants and site_config.json before installing erpnext/hrms/ism.
  docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
    "CREATE USER IF NOT EXISTS '$db_name'@'%' IDENTIFIED BY '$db_pw'; ALTER USER '$db_name'@'%' IDENTIFIED BY '$db_pw'; GRANT ALL PRIVILEGES ON \`$db_name\`.* TO '$db_name'@'%'; FLUSH PRIVILEGES;"

  python3 - "$site_cfg" "$db_name" "$db_pw" "$enc_key" <<'PY'
import json, os, sys
site_cfg, db_name, db_pw, enc_key = sys.argv[1:5]
cur = json.load(open(site_cfg, "r", encoding="utf-8")) if os.path.isfile(site_cfg) else {}
if enc_key:
    cur["encryption_key"] = enc_key
cur.update({
    "allow_cors": "*",
    "allow_tests": True,
    "db_name": db_name,
    "db_password": db_pw,
    "db_type": "mariadb",
    "db_user": db_name,
    "developer_mode": 1,
})
with open(site_cfg, "w", encoding="utf-8") as f:
    json.dump(cur, f, indent=1)
    f.write("\n")
PY

  docker exec -i -w "$bench_container_dir" "$CONTAINER_FRAPPE" env PATH="$container_path" bash -s -- \
    "$site_name" "$apps_csv" <<'EOF'
set -euo pipefail
site="$1"
apps_csv="$2"
IFS=',' read -ra apps <<< "$apps_csv"
for app in "${apps[@]}"; do
  app="${app// /}"
  [[ -z "$app" || "$app" == "frappe" ]] && continue
  echo "[clean-install] Installing app '$app' on $site..."
  bench --site "$site" install-app "$app"
done
bench --site "$site" clear-cache
echo "[clean-install] Seeding Layer 1 canonical catalog on $site..."
bench --site "$site" execute ism.testing.catalog.seed_all
EOF

  python3 - "$site_cfg" "$apps_csv" <<'PY'
import json, os, sys
site_cfg, apps_csv = sys.argv[1:3]
cur = json.load(open(site_cfg, "r", encoding="utf-8")) if os.path.isfile(site_cfg) else {}
extra_apps = [a.strip() for a in apps_csv.split(",") if a.strip() and a.strip() != "frappe"]
cur["installed_apps"] = ["frappe", *extra_apps]
with open(site_cfg, "w", encoding="utf-8") as f:
    json.dump(cur, f, indent=1)
    f.write("\n")
PY
}
