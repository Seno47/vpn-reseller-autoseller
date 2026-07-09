#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-xyranet-reseller-autoseller}"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
APP_USER="${APP_USER:-xyranet-reseller}"
APP_REPO_URL="${APP_REPO_URL:-https://github.com/Seno47/vpn-reseller-autoseller.git}"
APP_BRANCH="${APP_BRANCH:-main}"
APP_SERVICE_NAME="${APP_SERVICE_NAME:-${APP_NAME}.service}"
APP_CONTAINER_NAME="${APP_CONTAINER_NAME:-${APP_NAME}}"
APP_IMAGE_NAME="${APP_IMAGE_NAME:-${APP_NAME}:local}"
APP_REQUEST_FILE="${APP_REQUEST_FILE:-${APP_DIR}/data/update-request.json}"
APP_STATUS_FILE="${APP_STATUS_FILE:-${APP_DIR}/data/update-status.json}"
LOCK_FILE="${LOCK_FILE:-/run/${APP_NAME}-update.lock}"

write_status() {
  local status="$1"
  local stage="$2"
  local message="${3:-}"
  mkdir -p "$(dirname "$APP_STATUS_FILE")"
  python3 - "$APP_STATUS_FILE" "$status" "$stage" "$message" <<'PY'
import json
import sys
from datetime import datetime, timezone

path, status, stage, message = sys.argv[1:5]
payload = {
    "status": status,
    "stage": stage,
    "message": message,
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
}
with open(path + ".tmp", "w", encoding="utf-8") as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
import os
os.replace(path + ".tmp", path)
PY
}

read_request_value() {
  local key="$1"
  if [ ! -f "$APP_REQUEST_FILE" ]; then
    return 0
  fi
  python3 - "$APP_REQUEST_FILE" "$key" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    payload = {}
print(str(payload.get(sys.argv[2]) or ""))
PY
}

upsert_env() {
  local env_file="$1"
  local key="$2"
  local value="$3"
  python3 - "$env_file" "$key" "$value" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
prefix = key + "="
done = False
result = []
for line in lines:
    if line.startswith(prefix):
        result.append(prefix + value)
        done = True
    else:
        result.append(line)
if not done:
    result.append(prefix + value)
path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY
}

install_from_git() {
  local source_dir="$1"
  local target_dir="$2"
  mkdir -p "$target_dir"
  rsync -a --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude ".env" \
    --exclude "data" \
    --exclude "output" \
    "$source_dir/" "$target_dir/"
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  write_status "running" "locked" "Another update is already running."
  exit 0
fi

REQUEST_REPO="$(read_request_value repo_url || true)"
REQUEST_BRANCH="$(read_request_value branch || true)"
[ -n "$REQUEST_REPO" ] && APP_REPO_URL="$REQUEST_REPO"
[ -n "$REQUEST_BRANCH" ] && APP_BRANCH="$REQUEST_BRANCH"

TMP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

write_status "running" "fetch" "Downloading ${APP_REPO_URL} (${APP_BRANCH})..."
git clone --depth 1 --branch "$APP_BRANCH" "$APP_REPO_URL" "$TMP_DIR/source"
COMMIT="$(git -C "$TMP_DIR/source" rev-parse --short=12 HEAD)"

mkdir -p "$APP_DIR/backups" "$APP_DIR/data"

if [ -d "$APP_DIR/app" ] && [ -f "$APP_DIR/run-container.sh" ]; then
  write_status "running" "backup" "Creating source backup before Docker update..."
  tar -czf "$APP_DIR/backups/app-before-update-$(date +%Y%m%d%H%M%S).tar.gz" -C "$APP_DIR" app

  write_status "running" "install" "Replacing application files..."
  install_from_git "$TMP_DIR/source" "$APP_DIR/app"

  if [ -f "$APP_DIR/.env" ]; then
    upsert_env "$APP_DIR/.env" "APP_UPDATE_CURRENT_COMMIT" "$COMMIT"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_REPO_URL" "$APP_REPO_URL"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_BRANCH" "$APP_BRANCH"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_TRIGGER_FILE" "data/update-request.json"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_STATUS_FILE" "data/update-status.json"
  fi

  write_status "running" "build" "Building Docker image..."
  docker build -t "$APP_IMAGE_NAME" "$APP_DIR/app"

  write_status "running" "restart" "Restarting Docker container..."
  "$APP_DIR/run-container.sh"
else
  write_status "running" "backup" "Creating source backup before systemd update..."
  tar -czf "$APP_DIR/backups/source-before-update-$(date +%Y%m%d%H%M%S).tar.gz" \
    --exclude ".venv" --exclude "data" --exclude "backups" \
    -C "$APP_DIR" .

  write_status "running" "install" "Replacing application files..."
  install_from_git "$TMP_DIR/source" "$APP_DIR"

  if [ -x "$APP_DIR/.venv/bin/python" ]; then
    "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
  fi
  if [ -f "$APP_DIR/.env" ]; then
    upsert_env "$APP_DIR/.env" "APP_UPDATE_CURRENT_COMMIT" "$COMMIT"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_REPO_URL" "$APP_REPO_URL"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_BRANCH" "$APP_BRANCH"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_TRIGGER_FILE" "data/update-request.json"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_STATUS_FILE" "data/update-status.json"
  fi
  if id "$APP_USER" >/dev/null 2>&1; then
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  fi

  write_status "running" "restart" "Restarting systemd service..."
  systemctl restart "$APP_SERVICE_NAME"
fi

rm -f "$APP_REQUEST_FILE"
write_status "success" "done" "Updated to ${COMMIT}."
