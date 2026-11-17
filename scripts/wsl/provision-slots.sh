#!/usr/bin/env bash
set -euo pipefail

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

SLOT_COUNT=4
REFRESH_GOLDEN=0
RECREATE=0
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slots)
      SLOT_COUNT="${2:-4}"
      shift 2
      ;;
    --refresh-golden)
      REFRESH_GOLDEN=1
      shift
      ;;
    --recreate)
      RECREATE=1
      shift
      ;;
    --status)
      STATUS_ONLY=1
      shift
      ;;
    *)
      echo "[provision-slots] Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! [[ "$SLOT_COUNT" =~ ^[1-5]$ ]]; then
  echo "[provision-slots] --slots must be between 1 and 5 (got '$SLOT_COUNT')." >&2
  exit 2
fi

mkdir -p "$LOCK_DIR"

read_meta_value() {
  local meta_file="$1"
  local key="$2"
  if [[ -f "$meta_file" ]]; then
    awk -F= -v k="$key" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$meta_file"
  fi
}

print_status() {
  printf "%-7s %-11s %-7s %-42s %-22s %-10s %-26s\n" "SLOT" "PROVISIONED" "LOCK" "LAST_WORKTREE" "BRANCH" "HEAD" "SERVE"
  for k in $(seq 1 "$SLOT_COUNT"); do
    local slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
    local prov="missing"
    if [[ -d "$slot_host_dir/env" && -f "$slot_host_dir/sites/wt${k}.localhost/site_config.json" ]]; then
      prov="ready"
    fi

    local lock_file="$LOCK_DIR/slot-$k.lock"
    local lock_state="free"
    if ! ( exec 9>"$lock_file"; flock -n 9 ); then
      lock_state="busy"
    fi

    local meta_file="$LOCK_DIR/slot-$k.meta"
    local last_wt last_branch last_head
    last_wt="$(read_meta_value "$meta_file" "last_worktree")"
    last_branch="$(read_meta_value "$meta_file" "last_branch")"
    last_head="$(read_meta_value "$meta_file" "last_head")"
    [[ -z "$last_wt" ]] && last_wt="-"
    [[ -z "$last_branch" ]] && last_branch="-"
    [[ -z "$last_head" ]] && last_head="-"
    if [[ ${#last_head} -gt 8 ]]; then
      last_head="${last_head:0:8}"
    fi

    local serve_info="-"
    local pid_file="$LOCK_DIR/slot-$k-serve.pid"
    if [[ -f "$pid_file" ]]; then
      local spid
      spid="$(cat "$pid_file" 2>/dev/null || true)"
      if [[ -n "$spid" ]] && kill -0 "$spid" 2>/dev/null; then
        serve_info="http://localhost:800${k} (pid $spid)"
      fi
    fi

    printf "%-7s %-11s %-7s %-42s %-22s %-10s %-26s\n" "slot-$k" "$prov" "$lock_state" "$last_wt" "$last_branch" "$last_head" "$serve_info"
  done
}

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  print_status
  exit 0
fi

if [[ ! -d "$WSL_PRIMARY_BENCH/env" || ! -f "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" ]]; then
  echo "[provision-slots] Primary bench missing at $WSL_PRIMARY_BENCH" >&2
  exit 1
fi

mkdir -p "$WSL_SLOTS_ROOT/golden"

GOLDEN_FILE="$WSL_SLOTS_ROOT/golden/main_golden.sql.gz"
if [[ ! -s "$GOLDEN_FILE" || "$REFRESH_GOLDEN" -eq 1 || "$RECREATE" -eq 1 ]]; then
  PRIMARY_DB_NAME="$(python3 -c 'import json; print(json.load(open("/home/real2/projects/.project/frappe_docker/development/frappe-bench/sites/development.localhost/site_config.json")).get("db_name", "_c6b75eebb8edc84d"))')"
  echo "[provision-slots] Dumping primary DB '$PRIMARY_DB_NAME' -> $GOLDEN_FILE..."
  TMP_GOLDEN="$GOLDEN_FILE.tmp"
  docker exec "$CONTAINER_DB" mariadb-dump -uroot -p"$MARIADB_ROOT_PW" \
    --single-transaction --routines --triggers "$PRIMARY_DB_NAME" \
    | gzip -1 > "$TMP_GOLDEN"
  if [[ ! -s "$TMP_GOLDEN" ]]; then
    rm -f "$TMP_GOLDEN"
    echo "[provision-slots] Golden dump produced an empty archive." >&2
    exit 1
  fi
  mv "$TMP_GOLDEN" "$GOLDEN_FILE"
  echo "[provision-slots] Golden DB snapshot updated ($(du -h "$GOLDEN_FILE" | cut -f1))."
fi

provision_slot_fs_and_venv() {
  local k="$1"
  local refresh_ism="${2:-0}"
  docker exec -i "$CONTAINER_FRAPPE" bash -s -- "$k" "$CONTAINER_PRIMARY_BENCH" "$CONTAINER_SLOTS_ROOT" "$refresh_ism" <<'EOF'
set -euo pipefail
k="$1"
PRIMARY_BENCH="$2"
SLOTS_ROOT="$3"
REFRESH_ISM="$4"
SLOT_DIR="$SLOTS_ROOT/slot-$k"
SITE_NAME="wt${k}.localhost"
SLOT_DB="_ism_slot_${k}"
SLOT_PW="ism_slot_${k}_pw"

mkdir -p \
  "$SLOT_DIR/apps" \
  "$SLOT_DIR/logs" \
  "$SLOT_DIR/config/pids" \
  "$SLOT_DIR/sites/assets" \
  "$SLOT_DIR/sites/$SITE_NAME/logs" \
  "$SLOT_DIR/sites/$SITE_NAME/indexes" \
  "$SLOT_DIR/sites/$SITE_NAME/private/files" \
  "$SLOT_DIR/sites/$SITE_NAME/public/files" \
  "$SLOT_DIR/sites/$SITE_NAME/locks"

for app in frappe erpnext hrms induct_shop; do
  if [[ -d "$PRIMARY_BENCH/apps/$app" ]]; then
    ln -sfn "$PRIMARY_BENCH/apps/$app" "$SLOT_DIR/apps/$app"
  fi
done

if [[ ! -f "$SLOT_DIR/apps/ism/pyproject.toml" || "$REFRESH_ISM" -eq 1 ]]; then
  rm -rf "$SLOT_DIR/apps/ism"
  cp -a "$PRIMARY_BENCH/apps/ism" "$SLOT_DIR/apps/ism"
fi

if [[ ! -d "$SLOT_DIR/env" ]]; then
  echo "[provision-slots] Hardlink-cloning env for slot-$k..."
  cp -al "$PRIMARY_BENCH/env" "$SLOT_DIR/env"

  # Break hardlinks and rewrite shebangs in slot env/bin/* so wrapper scripts stay isolated.
  for f in "$SLOT_DIR/env/bin/"*; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    first_line="$(head -n 1 "$f" 2>/dev/null || true)"
    if [[ "$first_line" == "#!${PRIMARY_BENCH}/env/bin/"* ]]; then
      python3 - "$f" "${PRIMARY_BENCH}/env/bin/" "${SLOT_DIR}/env/bin/" <<'PY'
import os, sys
path, old, new = sys.argv[1], sys.argv[2].encode("utf-8"), sys.argv[3].encode("utf-8")
data = open(path, "rb").read()
nl = data.find(b"\n")
head = data[:nl] if nl != -1 else data
rest = data[nl:] if nl != -1 else b""
if head.startswith(b"#!" + old):
    new_data = b"#!" + new + head[2 + len(old):] + rest
    tmp = path + ".tmp"
    with open(tmp, "wb") as out:
        out.write(new_data)
    st = os.stat(path)
    os.chmod(tmp, st.st_mode)
    os.replace(tmp, path)
PY
    fi
  done

  SITE_PKGS="$SLOT_DIR/env/lib/python3.14/site-packages"
  rm -rf "$SITE_PKGS/ism.pth" "$SITE_PKGS"/ism-*.dist-info
  /home/frappe/.local/bin/uv pip install \
    --python "$SLOT_DIR/env/bin/python" \
    --offline --no-deps --link-mode=copy \
    -e "$SLOT_DIR/apps/ism"
fi

# Verify Python resolves ism strictly inside this slot
"$SLOT_DIR/env/bin/python" -c "import ism, os; p=os.path.realpath(ism.__file__); expected='$SLOT_DIR/apps/ism/'; assert p.startswith(expected), f'{p} does not start with {expected}'"

cp -f "$PRIMARY_BENCH/sites/apps.txt" "$SLOT_DIR/sites/apps.txt"
cp -f "$PRIMARY_BENCH/sites/apps.json" "$SLOT_DIR/sites/apps.json"

for app in frappe erpnext hrms induct_shop; do
  if [[ -d "$PRIMARY_BENCH/apps/$app/$app/public" ]]; then
    ln -sfn "$PRIMARY_BENCH/apps/$app/$app/public" "$SLOT_DIR/sites/assets/$app"
  fi
done
ln -sfn "$SLOT_DIR/apps/ism/ism/public" "$SLOT_DIR/sites/assets/ism"

for f in assets.json assets-rtl.json; do
  if [[ -f "$PRIMARY_BENCH/sites/assets/$f" ]]; then
    cp -f "$PRIMARY_BENCH/sites/assets/$f" "$SLOT_DIR/sites/assets/$f"
  fi
done
for d in css js locale; do
  if [[ -d "$PRIMARY_BENCH/sites/assets/$d" ]]; then
    rm -rf "$SLOT_DIR/sites/assets/$d"
    cp -a "$PRIMARY_BENCH/sites/assets/$d" "$SLOT_DIR/sites/assets/$d"
  fi
done

python3 - "$PRIMARY_BENCH" "$SLOT_DIR" "$k" "$SITE_NAME" "$SLOT_DB" "$SLOT_PW" <<'PY'
import json, os, sys
primary_bench, slot_dir, k_str, site_name, slot_db, slot_pw = sys.argv[1:7]
k = int(k_str)

with open(os.path.join(primary_bench, "sites", "common_site_config.json"), "r", encoding="utf-8") as f:
    common = json.load(f)

common.update({
    "background_workers": 1,
    "default_site": site_name,
    "file_watcher_port": 6790 + k,
    "gunicorn_workers": 2,
    "live_reload": False,
    "redis_cache": f"redis://redis-cache:6379/{k}?health_check_interval=30",
    "redis_queue": f"redis://redis-queue:6379/{k}?health_check_interval=30",
    "redis_socketio": f"redis://redis-queue:6379/{k}",
    "serve_default_site": True,
    "socketio_port": 9000 + k,
    "webserver_port": 8000 + k,
})
with open(os.path.join(slot_dir, "sites", "common_site_config.json"), "w", encoding="utf-8") as f:
    json.dump(common, f, indent=1)
    f.write("\n")

with open(os.path.join(primary_bench, "sites", "development.localhost", "site_config.json"), "r", encoding="utf-8") as f:
    primary_site = json.load(f)

site_cfg = {
    "allow_cors": "*",
    "allow_tests": True,
    "db_name": slot_db,
    "db_password": slot_pw,
    "db_type": "mariadb",
    "db_user": slot_db,
    "developer_mode": 1,
}
if "encryption_key" in primary_site:
    site_cfg["encryption_key"] = primary_site["encryption_key"]
if "installed_apps" in primary_site:
    site_cfg["installed_apps"] = primary_site["installed_apps"]

with open(os.path.join(slot_dir, "sites", site_name, "site_config.json"), "w", encoding="utf-8") as f:
    json.dump(site_cfg, f, indent=1)
    f.write("\n")
PY
EOF
}

seed_slot_db_and_migrate() {
  local k="$1"
  local slot_db="_ism_slot_${k}"
  local slot_pw="ism_slot_${k}_pw"
  local site_name="wt${k}.localhost"

  echo "[provision-slots] Seeding MariaDB '$slot_db' & Redis /$k for slot-$k..."
  docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
    "DROP DATABASE IF EXISTS \`$slot_db\`; CREATE DATABASE \`$slot_db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER IF NOT EXISTS '$slot_db'@'%' IDENTIFIED BY '$slot_pw'; ALTER USER '$slot_db'@'%' IDENTIFIED BY '$slot_pw'; GRANT ALL PRIVILEGES ON \`$slot_db\`.* TO '$slot_db'@'%'; FLUSH PRIVILEGES;"

  gunzip -c "$GOLDEN_FILE" | docker exec -i "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" "$slot_db"
  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$k" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$k" FLUSHDB >/dev/null

  docker exec -w "$CONTAINER_SLOTS_ROOT/slot-$k" "$CONTAINER_FRAPPE" bash -c \
    "export PATH=\"$CONTAINER_SLOTS_ROOT/slot-$k/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:\$PATH\"; bench --site '$site_name' clear-cache && bench --site '$site_name' migrate"

  cat > "$LOCK_DIR/slot-$k.meta" <<META
last_worktree=__GOLDEN__
last_branch=main
last_head=golden
db_state=golden
updated_at=$(date -Iseconds)
META
}

for k in $(seq 1 "$SLOT_COUNT"); do
  (
    exec 200>"$LOCK_DIR/slot-$k.lock"
    flock 200

    slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
    needs_create=0
    if [[ "$RECREATE" -eq 1 || ! -d "$slot_host_dir/env" || ! -d "$slot_host_dir/sites/wt${k}.localhost/logs" || ! -f "$slot_host_dir/sites/wt${k}.localhost/site_config.json" ]]; then
      needs_create=1
    fi

    if [[ "$RECREATE" -eq 1 && -d "$slot_host_dir" ]]; then
      echo "[provision-slots] Recreating slot-$k..."
      rm -rf "$slot_host_dir"
    fi

    if [[ "$needs_create" -eq 1 || "$REFRESH_GOLDEN" -eq 1 ]]; then
      provision_slot_fs_and_venv "$k" "$REFRESH_GOLDEN"
      seed_slot_db_and_migrate "$k"
      echo "[provision-slots] slot-$k ready (wt${k}.localhost, port 800${k}, redis /$k)."
    else
      echo "[provision-slots] slot-$k already provisioned (pass --refresh-golden or --recreate to rebuild)."
    fi
  )
done

echo "[provision-slots] All $SLOT_COUNT slot(s) ready."
