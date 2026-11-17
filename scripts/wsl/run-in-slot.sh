#!/usr/bin/env bash
set -euo pipefail

WSL_DEV_ROOT="/home/real2/projects/.project/frappe_docker/development"
WSL_SLOTS_ROOT="$WSL_DEV_ROOT/bench-slots"
CONTAINER_DEV_ROOT="/workspace/development"
CONTAINER_SLOTS_ROOT="$CONTAINER_DEV_ROOT/bench-slots"
LOCK_DIR="/tmp/ism-bench-slots"

CONTAINER_FRAPPE="devcontainer-frappe-1"
CONTAINER_DB="devcontainer-mariadb-1"
CONTAINER_REDIS_CACHE="devcontainer-redis-cache-1"
CONTAINER_REDIS_QUEUE="devcontainer-redis-queue-1"
MARIADB_ROOT_PW="123"

WORKTREE_ID=""
SOURCE_DIR=""
BRANCH="unknown"
HEAD_SHA="unknown"
REQ_SLOT=0
SLOT_COUNT=4
FRESH_DB=0
KEEP_DB=0
SKIP_MIGRATE=0
MODE="test"
MODULE=""
DOCTYPE=""
TEST_NAME=""
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
    --keep-db)
      KEEP_DB=1
      shift
      ;;
    --skip-migrate)
      SKIP_MIGRATE=1
      shift
      ;;
    --mode)
      MODE="$2"
      shift 2
      ;;
    --module)
      MODULE="$2"
      shift 2
      ;;
    --doctype)
      DOCTYPE="$2"
      shift 2
      ;;
    --test)
      TEST_NAME="$2"
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

if ! [[ "$SLOT_COUNT" =~ ^[1-5]$ ]]; then
  echo "[run-in-slot] --slots must be 1..5 (got '$SLOT_COUNT')." >&2
  exit 2
fi
if ! [[ "$REQ_SLOT" =~ ^[0-5]$ ]] || [[ "$REQ_SLOT" -gt "$SLOT_COUNT" ]]; then
  echo "[run-in-slot] --slot must be 0..$SLOT_COUNT (got '$REQ_SLOT')." >&2
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

stop_serve_slot() {
  local target_slot="$REQ_SLOT"
  if [[ "$target_slot" -eq 0 ]]; then
    for k in $(seq 1 "$SLOT_COUNT"); do
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

  # Wait up to 5s for the detached server to release its flock.
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

if [[ ! -d "$WSL_SLOTS_ROOT/slot-1/env" || ! -s "$WSL_SLOTS_ROOT/golden/main_golden.sql.gz" ]]; then
  echo "[run-in-slot] Bench slots not provisioned at $WSL_SLOTS_ROOT. Run ism-frappe\\scripts\\provision-bench-slots.ps1 from D:\\ism on 'main' first." >&2
  exit 2
fi

# Worktree affinity + atomic flock slot leasing
hex8="$(printf '%s' "$WORKTREE_ID" | sha256sum | cut -c1-8)"
preferred=$(( (16#$hex8 % SLOT_COUNT) + 1 ))

SLOT=0
if [[ "$REQ_SLOT" -gt 0 ]]; then
  echo "[slot-lease] Waiting for explicit slot-$REQ_SLOT..."
  exec 200>"$LOCK_DIR/slot-${REQ_SLOT}.lock"
  flock 200
  SLOT="$REQ_SLOT"
else
  CANDIDATES=("$preferred")
  for k in $(seq 1 "$SLOT_COUNT"); do
    if [[ "$k" -ne "$preferred" ]]; then
      CANDIDATES+=("$k")
    fi
  done

  for cand in "${CANDIDATES[@]}"; do
    exec 200>"$LOCK_DIR/slot-${cand}.lock"
    if flock -n 200; then
      SLOT="$cand"
      break
    fi
    exec 200>&-
  done

  if [[ "$SLOT" -eq 0 ]]; then
    echo "[slot-lease] All $SLOT_COUNT slots busy; waiting on slot-$preferred..."
    exec 200>"$LOCK_DIR/slot-${preferred}.lock"
    flock 200
    SLOT="$preferred"
  fi
fi

SITE_NAME="wt${SLOT}.localhost"
SLOT_DB="_ism_slot_${SLOT}"
WSL_SLOT_DIR="$WSL_SLOTS_ROOT/slot-${SLOT}"
CONTAINER_SLOT_DIR="$CONTAINER_SLOTS_ROOT/slot-${SLOT}"
META_FILE="$LOCK_DIR/slot-${SLOT}.meta"

echo "[slot-lease] Leased slot-$SLOT ($SITE_NAME, port 800${SLOT}, redis /$SLOT) for $WORKTREE_ID ($BRANCH@${HEAD_SHA:0:8})"

LAST_WT="$(read_meta_value "$META_FILE" "last_worktree")"
DB_STATE="$(read_meta_value "$META_FILE" "db_state")"

NEED_RESET=0
RESET_REASON=""
if [[ "$FRESH_DB" -eq 1 ]]; then
  NEED_RESET=1
  RESET_REASON="--fresh-db requested"
elif [[ "$KEEP_DB" -eq 0 && "$DB_STATE" != "golden" && "$LAST_WT" != "$WORKTREE_ID" ]]; then
  NEED_RESET=1
  RESET_REASON="worktree changed ('$LAST_WT' -> '$WORKTREE_ID')"
fi

if [[ "$NEED_RESET" -eq 1 ]]; then
  echo "[slot-db] Restoring golden snapshot into $SLOT_DB and flushing Redis /$SLOT ($RESET_REASON)..."
  docker exec "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" -e \
    "DROP DATABASE IF EXISTS \`$SLOT_DB\`; CREATE DATABASE \`$SLOT_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; GRANT ALL PRIVILEGES ON \`$SLOT_DB\`.* TO '$SLOT_DB'@'%'; FLUSH PRIVILEGES;"
  gunzip -c "$WSL_SLOTS_ROOT/golden/main_golden.sql.gz" | docker exec -i "$CONTAINER_DB" mariadb -uroot -p"$MARIADB_ROOT_PW" "$SLOT_DB"
  docker exec "$CONTAINER_REDIS_CACHE" redis-cli -n "$SLOT" FLUSHDB >/dev/null
  docker exec "$CONTAINER_REDIS_QUEUE" redis-cli -n "$SLOT" FLUSHDB >/dev/null
fi

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

cat > "$META_FILE" <<META
last_worktree=$WORKTREE_ID
last_branch=$BRANCH
last_head=$HEAD_SHA
db_state=dirty
updated_at=$(date -Iseconds)
META

run_in_slot_container() {
  docker exec -w "$CONTAINER_SLOT_DIR" "$CONTAINER_FRAPPE" env \
    PATH="$CONTAINER_SLOT_DIR/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
    "$@"
}

run_in_slot_container bench --site "$SITE_NAME" clear-cache
if [[ "$SKIP_MIGRATE" -eq 0 ]]; then
  run_in_slot_container bench --site "$SITE_NAME" migrate
fi

case "$MODE" in
  test)
    CMD=(bench --site "$SITE_NAME" run-tests --app ism)
    [[ -n "$MODULE" ]] && CMD+=(--module "$MODULE")
    [[ -n "$DOCTYPE" ]] && CMD+=(--doctype "$DOCTYPE")
    [[ -n "$TEST_NAME" ]] && CMD+=(--test "$TEST_NAME")
    if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
      CMD+=("${EXTRA_ARGS[@]}")
    fi
    run_in_slot_container "${CMD[@]}"
    ;;
  execute)
    if [[ -z "$EXECUTE_FN" ]]; then
      echo "[run-in-slot] --execute-fn required for execute mode." >&2
      exit 2
    fi
    CMD=(bench --site "$SITE_NAME" execute "$EXECUTE_FN")
    if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
      CMD+=("${EXTRA_ARGS[@]}")
    fi
    run_in_slot_container "${CMD[@]}"
    ;;
  serve)
    if [[ "$SERVE_ACTION" == "foreground" ]]; then
      run_in_slot_container bench --site "$SITE_NAME" serve --port "800${SLOT}"
    elif [[ "$SERVE_ACTION" == "start" ]]; then
      SERVE_LOG="$LOCK_DIR/slot-${SLOT}-serve.log"
      SERVE_PID_FILE="$LOCK_DIR/slot-${SLOT}-serve.pid"
      CONTAINER_PATH="$CONTAINER_SLOT_DIR/env/bin:/home/frappe/.local/bin:/home/frappe/.nvm/versions/node/v24.13.0/bin:/home/frappe/.pyenv/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      nohup bash -c "
        cleanup() {
          docker exec '$CONTAINER_FRAPPE' pkill -f 'bench-slots/slot-${SLOT}/env/bin/python.*serve --port 800${SLOT}' >/dev/null 2>&1 || true
        }
        trap cleanup EXIT TERM INT
        docker exec -w '$CONTAINER_SLOT_DIR' '$CONTAINER_FRAPPE' env PATH='$CONTAINER_PATH' bench --site '$SITE_NAME' serve --port '800${SLOT}' &
        child=\$!
        wait \$child
      " </dev/null >"$SERVE_LOG" 2>&1 &
      SERVE_PID=$!
      echo "$SERVE_PID" > "$SERVE_PID_FILE"
      # Close lock fd in parent while backgrounded child retains fd 200.
      exec 200>&-

      READY=0
      for _ in $(seq 1 30); do
        if ! kill -0 "$SERVE_PID" 2>/dev/null; then
          echo "[serve-start] Server exited prematurely; see $SERVE_LOG:" >&2
          tail -n 30 "$SERVE_LOG" >&2 || true
          rm -f "$SERVE_PID_FILE"
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
