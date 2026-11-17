#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=slot-common.sh
source "$SCRIPT_DIR/slot-common.sh"

SLOT_COUNT=5
REFRESH_GOLDEN=0
RECREATE=0
STATUS_ONLY=0
HISTORY_LIMIT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slots)
      SLOT_COUNT="${2:-5}"
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
    --history)
      if [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]]; then
        HISTORY_LIMIT="$2"
        shift 2
      else
        HISTORY_LIMIT=20
        shift
      fi
      ;;
    *)
      echo "[provision-slots] Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! [[ "$SLOT_COUNT" =~ ^[1-8]$ ]]; then
  echo "[provision-slots] --slots must be between 1 and 8 (got '$SLOT_COUNT')." >&2
  exit 2
fi

mkdir -p "$LOCK_DIR"

print_status() {
  printf "%-7s %-11s %-7s %-10s %-10s %-42s %-24s %-10s %-26s\n" \
    "SLOT" "PROVISIONED" "LOCK" "DB_STATE" "SCHEMA_FP" "LAST_WORKTREE" "BRANCH" "HEAD" "SERVE"
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
    local last_wt last_branch last_head db_state schema_fp
    last_wt="$(read_meta_value "$meta_file" "last_worktree")"
    last_branch="$(read_meta_value "$meta_file" "last_branch")"
    last_head="$(read_meta_value "$meta_file" "last_head")"
    db_state="$(read_meta_value "$meta_file" "db_state")"
    schema_fp="$(read_meta_value "$meta_file" "schema_fp")"

    if [[ "$lock_state" == "busy" && -f "$LOCK_DIR/slot-$k.holder" ]]; then
      local holder_line holder_wt holder_branch
      holder_line="$(cat "$LOCK_DIR/slot-$k.holder" 2>/dev/null || true)"
      IFS='|' read -r holder_wt holder_branch _ _ <<< "$holder_line"
      [[ -n "$holder_wt" ]] && last_wt="$holder_wt"
      [[ -n "$holder_branch" ]] && last_branch="$holder_branch"
    fi

    [[ -z "$last_wt" ]] && last_wt="-"
    [[ -z "$last_branch" ]] && last_branch="-"
    [[ -z "$last_head" ]] && last_head="-"
    [[ -z "$db_state" ]] && db_state="-"
    if [[ -z "$schema_fp" ]]; then
      schema_fp="-"
    elif [[ "$schema_fp" != "migrating" && ${#schema_fp} -gt 8 ]]; then
      schema_fp="${schema_fp:0:8}"
    fi
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

    printf "%-7s %-11s %-7s %-10s %-10s %-42s %-24s %-10s %-26s\n" \
      "slot-$k" "$prov" "$lock_state" "$db_state" "$schema_fp" "$last_wt" "$last_branch" "$last_head" "$serve_info"
  done
}

print_history() {
  local limit="$1"
  if [[ ! -s "$HISTORY_FILE" ]]; then
    echo "[provision-slots] No slot lease history recorded yet at $HISTORY_FILE."
    return 0
  fi

  python3 - "$HISTORY_FILE" "$limit" <<'PY'
import json
import sys
from collections import Counter

history_path, limit_str = sys.argv[1], sys.argv[2]
limit = max(1, int(limit_str))

rows = []
with open(history_path, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

if not rows:
    print("[provision-slots] History file is empty or unreadable.")
    sys.exit(0)

total = len(rows)
tiers = Counter(r.get("tier", "?") for r in rows)
restores = sum(1 for r in rows if r.get("need_reset"))
migrates = sum(1 for r in rows if r.get("ran_migrate"))
failures = sum(1 for r in rows if r.get("rc", 0) != 0)

def avg(key):
    vals = [r.get(key, 0) for r in rows if isinstance(r.get(key), (int, float))]
    return (sum(vals) / len(vals)) if vals else 0.0

print(
    f"[slot-history] total={total} restores={restores} ({100*restores/total:.1f}%) "
    f"migrates={migrates} ({100*migrates/total:.1f}%) failures={failures} "
    f"tiers={dict(sorted(tiers.items()))}"
)
print(
    f"[slot-history] avg_ms: wait={avg('wait_ms'):.0f} restore={avg('restore_ms'):.0f} "
    f"sync={avg('sync_ms'):.0f} exec={avg('exec_ms'):.0f}"
)
print(
    f"{'TIMESTAMP':<20} {'SLOT':<6} {'TIER':<9} {'RST':<4} {'MIG':<4} "
    f"{'WAIT':>6} {'REST':>6} {'EXEC':>6} {'RC':>3} {'MODE':<8} {'BRANCH':<26} {'WORKTREE'}"
)
for r in rows[-limit:]:
    ts = str(r.get("ts", ""))[:19]
    slot = f"slot-{r.get('slot', '?')}"
    tier = str(r.get("tier", "?"))[:9]
    rst = "yes" if r.get("need_reset") else "no"
    mig = "yes" if r.get("ran_migrate") else "no"
    wait_s = f"{r.get('wait_ms', 0)/1000:.1f}s"
    rest_s = f"{r.get('restore_ms', 0)/1000:.1f}s"
    exec_s = f"{r.get('exec_ms', 0)/1000:.1f}s"
    rc = r.get("rc", "?")
    mode = str(r.get("mode", "?"))[:8]
    branch = str(r.get("branch", "?"))[:26]
    wt = str(r.get("worktree", "?")).rstrip("/").split("/")[-1]
    print(
        f"{ts:<20} {slot:<6} {tier:<9} {rst:<4} {mig:<4} "
        f"{wait_s:>6} {rest_s:>6} {exec_s:>6} {str(rc):>3} {mode:<8} {branch:<26} {wt}"
    )
PY
}

if [[ "$STATUS_ONLY" -eq 1 || "$HISTORY_LIMIT" -gt 0 ]]; then
  if [[ "$STATUS_ONLY" -eq 1 ]]; then
    print_status
  fi
  if [[ "$HISTORY_LIMIT" -gt 0 ]]; then
    [[ "$STATUS_ONLY" -eq 1 ]] && echo ""
    print_history "$HISTORY_LIMIT"
  fi
  exit 0
fi

if [[ ! -d "$WSL_PRIMARY_BENCH/env" || ! -f "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" ]]; then
  echo "[provision-slots] Primary bench missing at $WSL_PRIMARY_BENCH" >&2
  exit 1
fi

mkdir -p "$WSL_SLOTS_ROOT/golden"

ensure_golden_fp() {
  local fp
  fp="$(compute_schema_fp "$WSL_PRIMARY_BENCH/apps/ism")"
  local tmp_fp="${GOLDEN_FP_FILE}.tmp.$$"
  printf '%s\n' "$fp" > "$tmp_fp"
  mv -f "$tmp_fp" "$GOLDEN_FP_FILE"
  printf '%s' "$fp"
}

provision_bench_fs_and_venv() {
  local bench_dir_name="$1"
  local site_name="$2"
  local db_name="$3"
  local db_pw="$4"
  local redis_idx="$5"
  local refresh_ism="${6:-0}"
  docker exec -i "$CONTAINER_FRAPPE" bash -s -- \
    "$bench_dir_name" "$site_name" "$db_name" "$db_pw" "$redis_idx" \
    "$CONTAINER_PRIMARY_BENCH" "$CONTAINER_SLOTS_ROOT" "$refresh_ism" <<'EOF'
set -euo pipefail
BENCH_DIR_NAME="$1"
SITE_NAME="$2"
SLOT_DB="$3"
SLOT_PW="$4"
REDIS_IDX="$5"
PRIMARY_BENCH="$6"
SLOTS_ROOT="$7"
REFRESH_ISM="$8"
SLOT_DIR="$SLOTS_ROOT/$BENCH_DIR_NAME"

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
  echo "[provision-slots] Hardlink-cloning env for $BENCH_DIR_NAME..."
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

# Verify Python resolves ism strictly inside this bench directory
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

python3 - "$PRIMARY_BENCH" "$SLOT_DIR" "$REDIS_IDX" "$SITE_NAME" "$SLOT_DB" "$SLOT_PW" <<'PY'
import json, os, sys
primary_bench, slot_dir, idx_str, site_name, slot_db, slot_pw = sys.argv[1:7]
idx = int(idx_str)

with open(os.path.join(primary_bench, "sites", "common_site_config.json"), "r", encoding="utf-8") as f:
    common = json.load(f)

common.update({
    "background_workers": 1,
    "default_site": site_name,
    "file_watcher_port": 6790 + idx,
    "gunicorn_workers": 2,
    "live_reload": False,
    "mariadb_user_host_login_scope": "%",
    "redis_cache": f"redis://redis-cache:6379/{idx}?health_check_interval=30",
    "redis_queue": f"redis://redis-queue:6379/{idx}?health_check_interval=30",
    "redis_socketio": f"redis://redis-queue:6379/{idx}",
    "serve_default_site": True,
    "socketio_port": 9000 + idx,
    "webserver_port": 8000 + idx,
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

provision_slot_fs_and_venv() {
  local k="$1"
  local refresh_ism="${2:-0}"
  provision_bench_fs_and_venv "slot-${k}" "wt${k}.localhost" "_ism_slot_${k}" "ism_slot_${k}_pw" "$k" "$refresh_ism"
}

build_clean_golden_snapshot() {
  echo "[provision-slots] Building clean Golden DB snapshot from fresh site + ism.testing.catalog.seed_all..."
  (
    exec 203>"$LOCK_DIR/golden-build.lock"
    flock 203

    local golden_wsl="$WSL_SLOTS_ROOT/golden-site"
    local golden_ctr="$CONTAINER_SLOTS_ROOT/golden-site"
    local golden_site="golden.localhost"
    local golden_db="_ism_golden_build"
    local golden_pw="ism_golden_pw"

    provision_bench_fs_and_venv "golden-site" "$golden_site" "$golden_db" "$golden_pw" "9" "1"

    local apps_csv
    apps_csv="$(python3 - "$WSL_PRIMARY_BENCH/sites/development.localhost/site_config.json" <<'PY'
import json, os, sys
p = sys.argv[1]
apps = json.load(open(p, "r", encoding="utf-8")).get("installed_apps", []) if os.path.isfile(p) else []
apps = [a for a in apps if a and a != "frappe"]
print(",".join(apps) if apps else "erpnext,hrms,ism")
PY
)"

    install_clean_ism_site "$golden_wsl" "$golden_ctr" "$golden_site" "$golden_db" "$golden_pw" "9" "$apps_csv"

    docker exec -w "$golden_ctr/sites" "$CONTAINER_FRAPPE" "$golden_ctr/env/bin/python" - <<'PY'
import frappe
frappe.init(site="golden.localhost", sites_path=".")
frappe.connect()
try:
    assert frappe.db.exists("Company", "Lightspeed EV"), "Lightspeed EV missing from clean golden site"
    assert frappe.db.count("Repair Order") == 0, "Expected 0 Repair Orders in clean golden site"
    bad_bays = frappe.get_all("Service Bay", filters={"name": ["like", "Test %"]}, pluck="name")
    assert not bad_bays, f"Unexpected legacy Service Bays: {bad_bays}"
finally:
    frappe.destroy()
PY

    local tmp_golden="${GOLDEN_FILE}.tmp.$$"
    docker exec "$CONTAINER_DB" mariadb-dump -uroot -p"$MARIADB_ROOT_PW" \
      --single-transaction --routines --triggers "$golden_db" \
      | gzip -1 > "$tmp_golden"
    if [[ ! -s "$tmp_golden" ]]; then
      rm -f "$tmp_golden"
      echo "[provision-slots] Clean Golden dump produced an empty archive." >&2
      exit 1
    fi
    mv -f "$tmp_golden" "$GOLDEN_FILE"
  )
}

if [[ ! -s "$GOLDEN_FILE" || "$REFRESH_GOLDEN" -eq 1 || "$RECREATE" -eq 1 ]]; then
  build_clean_golden_snapshot
  GOLDEN_FP="$(ensure_golden_fp)"
  echo "[provision-slots] Clean Golden DB snapshot updated ($(du -h "$GOLDEN_FILE" | cut -f1), schema_fp=${GOLDEN_FP:0:8})."
elif [[ ! -s "$GOLDEN_FP_FILE" ]]; then
  GOLDEN_FP="$(ensure_golden_fp)"
else
  GOLDEN_FP="$(tr -d '[:space:]' < "$GOLDEN_FP_FILE")"
fi

seed_slot_db_and_migrate() {
  local k="$1"
  local slot_db="_ism_slot_${k}"
  local slot_pw="ism_slot_${k}_pw"
  local site_name="wt${k}.localhost"
  local meta_file="$LOCK_DIR/slot-$k.meta"

  echo "[provision-slots] Seeding MariaDB '$slot_db' & Redis /$k for slot-$k..."
  write_meta_file "$meta_file" "__GOLDEN__" "main" "golden" "restoring" ""

  (
    exec 202>"$LOCK_DIR/mariadb-restore.lock"
    flock 202
    docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
      "DROP DATABASE IF EXISTS \`$slot_db\`; CREATE DATABASE \`$slot_db\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER IF NOT EXISTS '$slot_db'@'%' IDENTIFIED BY '$slot_pw'; ALTER USER '$slot_db'@'%' IDENTIFIED BY '$slot_pw'; GRANT ALL PRIVILEGES ON \`$slot_db\`.* TO '$slot_db'@'%'; FLUSH PRIVILEGES;"
    { echo "SET SESSION sql_log_bin=0;"; gunzip -c "$GOLDEN_FILE"; } | docker exec -i "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" "$slot_db"
  )

  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$k" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$k" FLUSHDB >/dev/null

  docker exec -w "$CONTAINER_SLOTS_ROOT/slot-$k" "$CONTAINER_FRAPPE" bash -c \
    "export PATH=\"$CONTAINER_SLOTS_ROOT/slot-$k/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:\$PATH\"; bench --site '$site_name' clear-cache && bench --site '$site_name' migrate"

  local slot_fp
  slot_fp="$(compute_schema_fp "$WSL_SLOTS_ROOT/slot-$k/apps/ism")"
  write_meta_file "$meta_file" "__GOLDEN__" "main" "golden" "golden" "$slot_fp"
}

for k in $(seq 1 "$SLOT_COUNT"); do
  (
    exec 200>"$LOCK_DIR/slot-$k.lock"
    flock 200

    slot_host_dir="$WSL_SLOTS_ROOT/slot-$k"
    meta_file="$LOCK_DIR/slot-$k.meta"
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
      # Backfill schema_fp / updated_at_epoch on legacy golden/ready slots without rebuilding.
      cur_db_state="$(read_meta_value "$meta_file" "db_state")"
      cur_fp="$(read_meta_value "$meta_file" "schema_fp")"
      cur_epoch="$(read_meta_value "$meta_file" "updated_at_epoch")"
      if [[ -z "$cur_fp" || -z "$cur_epoch" ]] && [[ "$cur_db_state" == "golden" || "$cur_db_state" == "dirty" || "$cur_db_state" == "ready" ]]; then
        cur_wt="$(read_meta_value "$meta_file" "last_worktree")"
        cur_br="$(read_meta_value "$meta_file" "last_branch")"
        cur_hd="$(read_meta_value "$meta_file" "last_head")"
        [[ "$cur_db_state" == "dirty" ]] && cur_db_state="ready"
        if [[ -z "$cur_fp" ]]; then
          if [[ "$cur_db_state" == "golden" ]]; then
            cur_fp="$GOLDEN_FP"
          else
            cur_fp="$(compute_schema_fp "$slot_host_dir/apps/ism")"
          fi
        fi
        write_meta_file "$meta_file" "${cur_wt:-__GOLDEN__}" "${cur_br:-main}" "${cur_hd:-golden}" "$cur_db_state" "$cur_fp"
      fi
      echo "[provision-slots] slot-$k already provisioned (pass --refresh-golden or --recreate to rebuild)."
    fi
  )
done

echo "[provision-slots] All $SLOT_COUNT slot(s) ready."
