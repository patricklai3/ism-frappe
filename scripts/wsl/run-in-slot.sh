#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=slot-common.sh
source "$SCRIPT_DIR/slot-common.sh"

WORKTREE_ID=""
SOURCE_DIR=""
BRANCH="unknown"
HEAD_SHA="unknown"
REQ_SLOT=0
SLOT_COUNT=5
FRESH_DB=0
CLEAN_REINSTALL=0
KEEP_DB=0
SKIP_MIGRATE=0
FORCE_MIGRATE=0
FORCE_SKIP_MIGRATE=0
ALLOW_PARALLEL_SLOTS=0
MODE="test"
MODULES=()
DOCTYPE=""
TESTS=()
EXECUTE_FN=""
SERVE_ACTION=""
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worktree-id)
      WORKTREE_ID="$2"
      shift 2
      ;;
    --source-dir)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --head)
      HEAD_SHA="$2"
      shift 2
      ;;
    --slot)
      REQ_SLOT="$2"
      shift 2
      ;;
    --slots)
      SLOT_COUNT="$2"
      shift 2
      ;;
    --fresh-db)
      FRESH_DB=1
      shift
      ;;
    --clean-reinstall)
      CLEAN_REINSTALL=1
      shift
      ;;
    --keep-db)
      KEEP_DB=1
      shift
      ;;
    --skip-migrate)
      SKIP_MIGRATE=1
      shift
      ;;
    --force-migrate)
      FORCE_MIGRATE=1
      shift
      ;;
    --force-skip-migrate)
      FORCE_SKIP_MIGRATE=1
      shift
      ;;
    --allow-parallel-slots)
      ALLOW_PARALLEL_SLOTS=1
      shift
      ;;
    --mode)
      MODE="$2"
      shift 2
      ;;
    --module)
      MODULES+=("$2")
      shift 2
      ;;
    --doctype)
      DOCTYPE="$2"
      shift 2
      ;;
    --test)
      TESTS+=("$2")
      shift 2
      ;;
    --execute-fn)
      EXECUTE_FN="$2"
      shift 2
      ;;
    --serve-action)
      SERVE_ACTION="$2"
      shift 2
      ;;
    --)
      shift
      EXTRA_ARGS=("$@")
      break
      ;;
    *)
      echo "[run-in-slot] Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if ! [[ "$SLOT_COUNT" =~ ^[1-8]$ ]]; then
  echo "[run-in-slot] --slots must be 1..8 (got '$SLOT_COUNT')." >&2
  exit 2
fi
if ! [[ "$REQ_SLOT" =~ ^[0-8]$ ]] || [[ "$REQ_SLOT" -gt "$SLOT_COUNT" ]]; then
  echo "[run-in-slot] --slot must be 0..$SLOT_COUNT (got '$REQ_SLOT')." >&2
  exit 2
fi
if [[ "$MODE" == "serve" && "$REQ_SLOT" -gt 5 ]]; then
  echo "[run-in-slot] --serve is only supported on slots 1..5 (ports 8001..8005); got --slot $REQ_SLOT." >&2
  exit 2
fi

mkdir -p "$LOCK_DIR"

stop_serve_slot() {
  local target_slot="$REQ_SLOT"
  local max_serve_slot="$SLOT_COUNT"
  [[ "$max_serve_slot" -gt 5 ]] && max_serve_slot=5

  if [[ "$target_slot" -eq 0 ]]; then
    for k in $(seq 1 "$max_serve_slot"); do
      local pf="$LOCK_DIR/slot-$k-serve.pid"
      if [[ -f "$pf" ]]; then
        local pid
        pid="$(cat "$pf" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
          local wt
          wt="$(read_meta_value "$LOCK_DIR/slot-$k.meta" "last_worktree")"
          if [[ -z "$WORKTREE_ID" || "$wt" == "$WORKTREE_ID" ]]; then
            target_slot="$k"
            break
          fi
        fi
      fi
    done
  fi

  if [[ "$target_slot" -eq 0 ]]; then
    echo "[serve-stop] No active slot server found for '$WORKTREE_ID'."
    return 0
  fi

  local pid_file="$LOCK_DIR/slot-${target_slot}-serve.pid"
  if [[ -f "$pid_file" ]]; then
    local spid
    spid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$spid" ]] && kill -0 "$spid" 2>/dev/null; then
      kill "$spid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  fi

  docker exec "$CONTAINER_FRAPPE" pkill -f "bench-slots/slot-${target_slot}/env/bin/python.*serve --port 800${target_slot}" >/dev/null 2>&1 || true

  local released=0
  for _ in $(seq 1 25); do
    if ( exec 9>"$LOCK_DIR/slot-${target_slot}.lock"; flock -n 9 ); then
      released=1
      break
    fi
    sleep 0.2
  done

  if [[ "$released" -ne 1 ]]; then
    echo "[serve-stop] Warning: slot-$target_slot lock still held after 5s." >&2
    return 1
  fi
  rm -f "$LOCK_DIR/slot-${target_slot}.holder"
  echo "[serve-stop] Stopped wt${target_slot}.localhost (slot-$target_slot, port 800${target_slot})."
  return 0
}

if [[ "$MODE" == "serve" && "$SERVE_ACTION" == "stop" ]]; then
  stop_serve_slot
  exit $?
fi

if [[ -z "$SOURCE_DIR" || ! -d "$SOURCE_DIR" ]]; then
  echo "[run-in-slot] Source directory missing: '$SOURCE_DIR'." >&2
  exit 2
fi

if [[ ! -d "$WSL_SLOTS_ROOT/slot-1/env" || ! -s "$GOLDEN_FILE" ]]; then
  echo "[run-in-slot] Bench slots not provisioned at $WSL_SLOTS_ROOT. Run ism-frappe\\scripts\\provision-bench-slots.ps1 from D:\\ism on 'main' first." >&2
  exit 2
fi

# Telemetry state
SLOT=0
LEASE_TIER="explicit"
NEED_RESET=0
RUN_MIGRATE=0
WAIT_MS=0
RESTORE_MS=0
SYNC_MS=0
EXEC_MS=0
EXEC_RC=0
HISTORY_LOGGED=0
SERVE_STARTED_BG=0
HOLDER_FILE=""

record_history() {
  local rc="$1"
  [[ "$SLOT" -eq 0 || "$HISTORY_LOGGED" -eq 1 ]] && return 0
  HISTORY_LOGGED=1
  python3 - "$HISTORY_FILE" "$SLOT" "$LEASE_TIER" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "$MODE" \
    "$NEED_RESET" "$RUN_MIGRATE" "$WAIT_MS" "$RESTORE_MS" "$SYNC_MS" "$EXEC_MS" "$rc" <<'PY' >/dev/null 2>&1 || true
import datetime
import json
import sys

(
    history_path, slot, tier, wt, branch, head, mode,
    need_reset, ran_migrate, wait_ms, restore_ms, sync_ms, exec_ms, rc
) = sys.argv[1:15]

rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "slot": int(slot),
    "tier": tier,
    "worktree": wt,
    "branch": branch,
    "head": head[:8],
    "mode": mode,
    "need_reset": bool(int(need_reset)),
    "ran_migrate": bool(int(ran_migrate)),
    "wait_ms": int(wait_ms),
    "restore_ms": int(restore_ms),
    "sync_ms": int(sync_ms),
    "exec_ms": int(exec_ms),
    "rc": int(rc),
}
with open(history_path, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\n")
PY
}

on_exit() {
  local exit_rc=$?
  if [[ "$SERVE_STARTED_BG" -eq 0 && -n "$HOLDER_FILE" ]]; then
    rm -f "$HOLDER_FILE"
  fi
  if [[ "$SLOT" -gt 0 && "$HISTORY_LOGGED" -eq 0 ]]; then
    record_history "$exit_rc"
  fi
}
trap on_exit EXIT

# Lock order: FD 199 (per-worktree) -> FD 201 (queue) -> FD 200 (slot) -> FD 202 (mariadb-restore)
hex8="$(printf '%s' "$WORKTREE_ID" | sha256sum | cut -c1-8)"

if [[ "$REQ_SLOT" -eq 0 && "$ALLOW_PARALLEL_SLOTS" -eq 0 && "$MODE" != "serve" ]]; then
  WT_LOCK_FILE="$LOCK_DIR/wt-${hex8}.lock"
  exec 199>"$WT_LOCK_FILE"
  if ! flock -n 199; then
    echo "[slot-lease] Serializing same-worktree run; waiting on $WORKTREE_ID..."
    flock 199
  fi
fi

# Build ranked candidates: Tier 1 sticky -> Tier 2 golden -> Tier 3 LRU (oldest updated_at_epoch)
rank_candidates() {
  local max_slot="$SLOT_COUNT"
  if [[ "$MODE" == "serve" && "$max_slot" -gt 5 ]]; then
    max_slot=5
  fi
  local offset=$(( 16#$hex8 % max_slot ))
  local sticky=()
  local golden=()
  local lru_entries=()

  for k in $(seq 1 "$max_slot"); do
    local mf="$LOCK_DIR/slot-${k}.meta"
    local wt st ep rot
    wt="$(read_meta_value "$mf" "last_worktree")"
    st="$(read_meta_value "$mf" "db_state")"
    ep="$(read_meta_value "$mf" "updated_at_epoch")"
    [[ -z "$ep" || ! "$ep" =~ ^[0-9]+$ ]] && ep=0
    rot=$(( (k - 1 - offset + max_slot) % max_slot ))

    if [[ ( "$st" == "ready" || "$st" == "dirty" ) && "$wt" == "$WORKTREE_ID" ]]; then
      sticky+=("${k}:sticky")
    elif [[ "$st" == "golden" ]]; then
      golden+=("${k}:golden")
    else
      lru_entries+=("$(printf '%012d:%02d:%d' "$ep" "$rot" "$k")")
    fi
  done

  if [[ ${#sticky[@]} -gt 0 ]]; then
    printf '%s\n' "${sticky[@]}"
  fi
  if [[ ${#golden[@]} -gt 0 ]]; then
    printf '%s\n' "${golden[@]}"
  fi
  if [[ ${#lru_entries[@]} -gt 0 ]]; then
    printf '%s\n' "${lru_entries[@]}" | sort | while IFS=: read -r _ _ k; do
      printf '%s:lru\n' "$k"
    done
  fi
}

try_lease_ranked() {
  while IFS=: read -r cand tier; do
    [[ -z "$cand" ]] && continue
    [[ ! -d "$WSL_SLOTS_ROOT/slot-${cand}/env" ]] && continue
    exec 200>"$LOCK_DIR/slot-${cand}.lock"
    if flock -n 200; then
      SLOT="$cand"
      LEASE_TIER="$tier"
      return 0
    fi
    exec 200>&-
  done < <(rank_candidates)
  return 1
}

T_WAIT_START="$(now_ms)"
if [[ "$REQ_SLOT" -gt 0 ]]; then
  LEASE_TIER="explicit"
  exec 200>"$LOCK_DIR/slot-${REQ_SLOT}.lock"
  if ! flock -n 200; then
    echo "[slot-lease] Waiting for explicit slot-$REQ_SLOT..."
    flock 200
  fi
  SLOT="$REQ_SLOT"
else
  if ! try_lease_ranked; then
    echo "[slot-lease] All $SLOT_COUNT slots busy; joining FIFO wait queue..."
    exec 201>"$LOCK_DIR/queue.lock"
    flock 201
    while ! try_lease_ranked; do
      sleep 0.3
    done
    exec 201>&-
  fi
fi
T_WAIT_END="$(now_ms)"
WAIT_MS=$(( T_WAIT_END - T_WAIT_START ))

HOLDER_FILE="$LOCK_DIR/slot-${SLOT}.holder"
printf '%s|%s|%s|%s\n' "$WORKTREE_ID" "$BRANCH" "$$" "$(date -Iseconds)" > "$HOLDER_FILE"

SITE_NAME="wt${SLOT}.localhost"
SLOT_DB="_ism_slot_${SLOT}"
WSL_SLOT_DIR="$WSL_SLOTS_ROOT/slot-${SLOT}"
CONTAINER_SLOT_DIR="$CONTAINER_SLOTS_ROOT/slot-${SLOT}"
META_FILE="$LOCK_DIR/slot-${SLOT}.meta"

echo "[slot-lease] Leased slot-$SLOT ($SITE_NAME, port 800${SLOT}, redis /$SLOT, tier=$LEASE_TIER, wait=${WAIT_MS}ms) for $WORKTREE_ID ($BRANCH@${HEAD_SHA:0:8})"

LAST_WT="$(read_meta_value "$META_FILE" "last_worktree")"
DB_STATE="$(read_meta_value "$META_FILE" "db_state")"
SLOT_SCHEMA_FP="$(read_meta_value "$META_FILE" "schema_fp")"

RESET_REASON=""
if [[ "$CLEAN_REINSTALL" -eq 1 ]]; then
  NEED_RESET=1
  RESET_REASON="--clean-reinstall requested"
elif [[ "$FRESH_DB" -eq 1 ]]; then
  NEED_RESET=1
  RESET_REASON="--fresh-db requested"
elif [[ "$DB_STATE" == "restoring" || -z "$DB_STATE" ]]; then
  NEED_RESET=1
  RESET_REASON="recovering interrupted/uninitialized slot DB (db_state='${DB_STATE:-empty}')"
elif [[ "$KEEP_DB" -eq 0 && "$DB_STATE" != "golden" && "$LAST_WT" != "$WORKTREE_ID" ]]; then
  NEED_RESET=1
  RESET_REASON="worktree changed ('$LAST_WT' -> '$WORKTREE_ID')"
fi

if [[ "$NEED_RESET" -eq 1 && "$CLEAN_REINSTALL" -eq 0 ]]; then
  T_RESTORE_START="$(now_ms)"
  echo "[slot-db] Restoring golden snapshot into $SLOT_DB and flushing Redis /$SLOT ($RESET_REASON)..."
  write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "restoring" ""

  (
    exec 202>"$LOCK_DIR/mariadb-restore.lock"
    flock 202
    docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
      "DROP DATABASE IF EXISTS \`$SLOT_DB\`; CREATE DATABASE \`$SLOT_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; GRANT ALL PRIVILEGES ON \`$SLOT_DB\`.* TO '$SLOT_DB'@'%'; FLUSH PRIVILEGES;"
    { echo "SET SESSION sql_log_bin=0;"; gunzip -c "$GOLDEN_FILE"; } | docker exec -i "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" "$SLOT_DB"
  )

  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$SLOT" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$SLOT" FLUSHDB >/dev/null

  if [[ -s "$GOLDEN_FP_FILE" ]]; then
    SLOT_SCHEMA_FP="$(tr -d '[:space:]' < "$GOLDEN_FP_FILE")"
  else
    SLOT_SCHEMA_FP="$(compute_schema_fp "$WSL_PRIMARY_BENCH/apps/ism")"
  fi
  DB_STATE="ready"
  write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "$SLOT_SCHEMA_FP"
  T_RESTORE_END="$(now_ms)"
  RESTORE_MS=$(( T_RESTORE_END - T_RESTORE_START ))
fi

T_SYNC_START="$(now_ms)"
echo "[slot-sync] Syncing ${SOURCE_DIR%/}/ -> $WSL_SLOT_DIR/apps/ism/..."
rsync -rtvc --delete \
  --exclude='.git' \
  --exclude='.codegraph' \
  --exclude='node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.venv' \
  --exclude='.idea' \
  --exclude='.vscode' \
  "${SOURCE_DIR%/}/" "$WSL_SLOT_DIR/apps/ism/"
T_SYNC_END="$(now_ms)"
SYNC_MS=$(( T_SYNC_END - T_SYNC_START ))

CURRENT_SCHEMA_FP="$(compute_schema_fp "$WSL_SLOT_DIR/apps/ism")"

if [[ "$CLEAN_REINSTALL" -eq 1 ]]; then
  T_RESTORE_START="$(now_ms)"
  echo "[slot-db] Performing clean site reinstall & Layer 1 catalog seed on $SITE_NAME ($SLOT_DB)..."
  write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "restoring" ""

  SLOT_APPS_CSV="$(python3 - "$WSL_SLOT_DIR/sites/$SITE_NAME/site_config.json" <<'PY'
import json, os, sys
p = sys.argv[1]
apps = json.load(open(p, "r", encoding="utf-8")).get("installed_apps", []) if os.path.isfile(p) else []
apps = [a for a in apps if a and a != "frappe"]
print(",".join(apps) if apps else "erpnext,hrms,ism")
PY
)"
  (
    exec 202>"$LOCK_DIR/mariadb-restore.lock"
    flock 202
    install_clean_ism_site "$WSL_SLOT_DIR" "$CONTAINER_SLOT_DIR" "$SITE_NAME" "$SLOT_DB" "ism_slot_${SLOT}_pw" "$SLOT" "$SLOT_APPS_CSV"
  )

  SLOT_SCHEMA_FP="$CURRENT_SCHEMA_FP"
  DB_STATE="ready"
  T_RESTORE_END="$(now_ms)"
  RESTORE_MS=$(( T_RESTORE_END - T_RESTORE_START ))
fi

if [[ "$DB_STATE" == "golden" || "$DB_STATE" == "dirty" || "$LAST_WT" != "$WORKTREE_ID" ]]; then
  DB_STATE="ready"
fi
write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "$DB_STATE" "$SLOT_SCHEMA_FP"

MIGRATE_REASON=""
if [[ "$FORCE_MIGRATE" -eq 1 ]]; then
  RUN_MIGRATE=1
  MIGRATE_REASON="--force-migrate"
elif [[ "$FORCE_SKIP_MIGRATE" -eq 1 ]]; then
  RUN_MIGRATE=0
  MIGRATE_REASON="--force-skip-migrate"
elif [[ -n "$SLOT_SCHEMA_FP" && "$SLOT_SCHEMA_FP" == "$CURRENT_SCHEMA_FP" ]]; then
  RUN_MIGRATE=0
  MIGRATE_REASON="schema fingerprint unchanged (${CURRENT_SCHEMA_FP:0:8})"
elif [[ "$SKIP_MIGRATE" -eq 1 ]]; then
  if [[ "$NEED_RESET" -eq 1 && "$SLOT_SCHEMA_FP" != "$CURRENT_SCHEMA_FP" ]]; then
    RUN_MIGRATE=1
    MIGRATE_REASON="overriding -SkipMigrate: schema changed after DB reset (${SLOT_SCHEMA_FP:0:8} -> ${CURRENT_SCHEMA_FP:0:8})"
    echo "[slot-migrate] Warning: $MIGRATE_REASON" >&2
  else
    RUN_MIGRATE=0
    MIGRATE_REASON="--skip-migrate"
  fi
else
  RUN_MIGRATE=1
  MIGRATE_REASON="schema fingerprint changed (${SLOT_SCHEMA_FP:-none} -> ${CURRENT_SCHEMA_FP:0:8})"
fi

echo "[slot-migrate] migrate=$RUN_MIGRATE ($MIGRATE_REASON)"

CONTAINER_PATH="$CONTAINER_SLOT_DIR/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

run_in_slot_container_exec() {
  local inner_script='
set -euo pipefail
site="$1"
run_mig="$2"
mode="$3"
doctype="$4"
exec_fn="$5"
mod_count="$6"
test_count="$7"
shift 7

modules=()
for ((i=0; i<mod_count; i++)); do
  modules+=("$1")
  shift
done
tests=()
for ((i=0; i<test_count; i++)); do
  tests+=("$1")
  shift
done
extra=("$@")

rm -f logs/.migrate_ok
if [[ "$run_mig" -eq 1 ]]; then
  bench --site "$site" migrate
  touch logs/.migrate_ok
else
  bench --site "$site" clear-cache
fi

case "$mode" in
  test)
    common=(--app ism)
    [[ -n "$doctype" ]] && common+=(--doctype "$doctype")
    if [[ ${#tests[@]} -gt 0 ]]; then
      for t in "${tests[@]}"; do
        common+=(--test "$t")
      done
    fi
    if [[ ${#extra[@]} -gt 0 ]]; then
      common+=("${extra[@]}")
    fi
    if [[ ${#modules[@]} -eq 0 ]]; then
      bench --site "$site" run-tests "${common[@]}"
    else
      for m in "${modules[@]}"; do
        echo "[slot-test] Running module: $m"
        bench --site "$site" run-tests "${common[@]}" --module "$m"
      done
    fi
    ;;
  execute)
    if [[ ${#extra[@]} -gt 0 ]]; then
      bench --site "$site" execute "$exec_fn" "${extra[@]}"
    else
      bench --site "$site" execute "$exec_fn"
    fi
    ;;
  prep-only)
    ;;
  *)
    echo "[run-in-slot] Invalid inner mode: $mode" >&2
    exit 2
    ;;
esac
'
  local pass_args=(
    "$SITE_NAME" "$RUN_MIGRATE" "$MODE" "$DOCTYPE" "$EXECUTE_FN"
    "${#MODULES[@]}" "${#TESTS[@]}"
  )
  [[ ${#MODULES[@]} -gt 0 ]] && pass_args+=("${MODULES[@]}")
  [[ ${#TESTS[@]} -gt 0 ]] && pass_args+=("${TESTS[@]}")
  [[ ${#EXTRA_ARGS[@]} -gt 0 ]] && pass_args+=("${EXTRA_ARGS[@]}")

  docker exec -w "$CONTAINER_SLOT_DIR" "$CONTAINER_FRAPPE" env PATH="$CONTAINER_PATH" \
    bash -c "$inner_script" _ "${pass_args[@]}"
}

T_EXEC_START="$(now_ms)"
case "$MODE" in
  test)
    if [[ "$RUN_MIGRATE" -eq 1 ]]; then
      write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "migrating"
    fi
    set +e
    run_in_slot_container_exec
    EXEC_RC=$?
    set -e
    if [[ "$RUN_MIGRATE" -eq 1 && -f "$WSL_SLOT_DIR/logs/.migrate_ok" ]]; then
      SLOT_SCHEMA_FP="$CURRENT_SCHEMA_FP"
      write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "$SLOT_SCHEMA_FP"
    fi
    T_EXEC_END="$(now_ms)"
    EXEC_MS=$(( T_EXEC_END - T_EXEC_START ))
    record_history "$EXEC_RC"
    exit "$EXEC_RC"
    ;;
  execute)
    if [[ -z "$EXECUTE_FN" ]]; then
      echo "[run-in-slot] --execute-fn required for execute mode." >&2
      exit 2
    fi
    if [[ "$RUN_MIGRATE" -eq 1 ]]; then
      write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "migrating"
    fi
    set +e
    run_in_slot_container_exec
    EXEC_RC=$?
    set -e
    if [[ "$RUN_MIGRATE" -eq 1 && -f "$WSL_SLOT_DIR/logs/.migrate_ok" ]]; then
      SLOT_SCHEMA_FP="$CURRENT_SCHEMA_FP"
      write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "$SLOT_SCHEMA_FP"
    fi
    T_EXEC_END="$(now_ms)"
    EXEC_MS=$(( T_EXEC_END - T_EXEC_START ))
    record_history "$EXEC_RC"
    exit "$EXEC_RC"
    ;;
  serve)
    if [[ "$RUN_MIGRATE" -eq 1 ]]; then
      write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "migrating"
      ORIG_MODE="$MODE"
      MODE="prep-only"
      run_in_slot_container_exec
      MODE="$ORIG_MODE"
      if [[ -f "$WSL_SLOT_DIR/logs/.migrate_ok" ]]; then
        SLOT_SCHEMA_FP="$CURRENT_SCHEMA_FP"
        write_meta_file "$META_FILE" "$WORKTREE_ID" "$BRANCH" "$HEAD_SHA" "ready" "$SLOT_SCHEMA_FP"
      fi
    else
      docker exec -w "$CONTAINER_SLOT_DIR" "$CONTAINER_FRAPPE" env PATH="$CONTAINER_PATH" bench --site "$SITE_NAME" clear-cache
    fi

    if [[ "$SERVE_ACTION" == "foreground" ]]; then
      set +e
      docker exec -w "$CONTAINER_SLOT_DIR" "$CONTAINER_FRAPPE" env PATH="$CONTAINER_PATH" bench --site "$SITE_NAME" serve --port "800${SLOT}"
      EXEC_RC=$?
      set -e
      T_EXEC_END="$(now_ms)"
      EXEC_MS=$(( T_EXEC_END - T_EXEC_START ))
      record_history "$EXEC_RC"
      exit "$EXEC_RC"
    elif [[ "$SERVE_ACTION" == "start" ]]; then
      SERVE_LOG="$LOCK_DIR/slot-${SLOT}-serve.log"
      SERVE_PID_FILE="$LOCK_DIR/slot-${SLOT}-serve.pid"
      nohup bash -c "
        cleanup() {
          docker exec '$CONTAINER_FRAPPE' pkill -f 'bench-slots/slot-${SLOT}/env/bin/python.*serve --port 800${SLOT}' >/dev/null 2>&1 || true
          rm -f '$HOLDER_FILE'
        }
        trap cleanup EXIT TERM INT
        docker exec -w '$CONTAINER_SLOT_DIR' '$CONTAINER_FRAPPE' env PATH='$CONTAINER_PATH' bench --site '$SITE_NAME' serve --port '800${SLOT}' &
        child=\$!
        wait \$child
      " </dev/null >"$SERVE_LOG" 2>&1 &
      SERVE_PID=$!
      echo "$SERVE_PID" > "$SERVE_PID_FILE"
      SERVE_STARTED_BG=1
      exec 200>&-

      READY=0
      for _ in $(seq 1 30); do
        if ! kill -0 "$SERVE_PID" 2>/dev/null; then
          echo "[serve-start] Server exited prematurely; see $SERVE_LOG:" >&2
          tail -n 30 "$SERVE_LOG" >&2 || true
          rm -f "$SERVE_PID_FILE" "$HOLDER_FILE"
          SERVE_STARTED_BG=0
          exit 1
        fi
        code="$(python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:800${SLOT}/api/method/ping', timeout=1).status)" 2>/dev/null || true)"
        if [[ "$code" == "200" ]]; then
          READY=1
          break
        fi
        sleep 0.5
      done
      if [[ "$READY" -ne 1 ]]; then
        echo "[serve-start] Server did not answer HTTP 200 on port 800${SLOT} within 15s (pid $SERVE_PID)." >&2
        tail -n 30 "$SERVE_LOG" >&2 || true
        exit 1
      fi
      T_EXEC_END="$(now_ms)"
      EXEC_MS=$(( T_EXEC_END - T_EXEC_START ))
      record_history 0
      echo "[serve-start] Serving $SITE_NAME at http://localhost:800${SLOT} (slot-$SLOT, pid $SERVE_PID). Stop with -Slot $SLOT -Serve stop."
    else
      echo "[run-in-slot] Invalid --serve-action: '$SERVE_ACTION' (expected start|stop|foreground)." >&2
      exit 2
    fi
    ;;
  *)
    echo "[run-in-slot] Invalid --mode: '$MODE'." >&2
    exit 2
    ;;
esac
