#!/usr/bin/env bash
set -Eeuo pipefail

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
APP_UPDATER_PATH="${APP_UPDATER_PATH:-/usr/local/sbin/${APP_NAME}-update}"
APP_PYTHON_BIN="${APP_PYTHON_BIN:-}"
REQUEST_ID=""
CURRENT_STAGE="startup"
TMP_DIR=""
VENV_NEW=""
BACKUP_TMP=""

write_status() {
  local status="$1"
  local stage="$2"
  local message="${3:-}"
  python3 - "$APP_STATUS_FILE" "$status" "$stage" "$message" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

path, status, stage, message = sys.argv[1:5]
path = os.path.abspath(path)
directory = os.path.dirname(path)
os.makedirs(directory, mode=0o755, exist_ok=True)
if os.path.realpath(directory) != directory:
    raise SystemExit(f"Refusing symlinked update status directory: {directory}")
payload = {
    "status": status,
    "stage": stage,
    "message": message,
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
}
fd, temporary_path = tempfile.mkstemp(prefix=".update-status-", dir=directory, text=True)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)
except BaseException:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
    raise
PY
}

consume_request() {
  python3 - "$APP_REQUEST_FILE" "$APP_USER" <<'PY'
import errno
import json
import os
import pwd
import re
import secrets
import stat
import sys

path, app_user = sys.argv[1:3]

def discard() -> None:
    try:
        os.unlink(path)
    except IsADirectoryError:
        for _ in range(10):
            rejected_path = f"{path}.rejected-{secrets.token_hex(8)}"
            try:
                os.rename(path, rejected_path)
                return
            except FileExistsError:
                continue
        raise RuntimeError("cannot quarantine invalid update request")
    except FileNotFoundError:
        pass

try:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
except OSError as exc:
    if exc.errno in {errno.ELOOP, errno.ENOENT}:
        discard()
    raise SystemExit(f"Cannot safely open update request: {exc}")

try:
    metadata = os.fstat(fd)
    allowed_uids = {0}
    request_parent = os.path.dirname(os.path.abspath(path))
    parent_metadata = os.lstat(request_parent)
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError("update request parent is not a real directory")
    allowed_uids.add(parent_metadata.st_uid)
    try:
        allowed_uids.add(pwd.getpwnam(app_user).pw_uid)
    except KeyError:
        pass
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("update request is not a regular file")
    if metadata.st_uid not in allowed_uids:
        raise ValueError("update request has an unexpected owner")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("update request is writable by group or others")
    if metadata.st_size > 65536:
        raise ValueError("update request is too large")
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        fd = -1
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("update request must be a JSON object")
    request_id = str(payload.get("request_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
        raise ValueError("update request has an invalid request_id")
except Exception as exc:
    if fd >= 0:
        os.close(fd)
    discard()
    raise SystemExit(f"Invalid update request: {exc}")

discard()
print(request_id)
PY
}

upsert_env() {
  local env_file="$1"
  local key="$2"
  local value="$3"
  python3 - "$env_file" "$key" "$value" <<'PY'
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
if "\n" in key or "\n" in value or "\r" in key or "\r" in value:
    raise SystemExit("Refusing a multiline environment value")
if "${" in value:
    raise SystemExit("Refusing an environment value containing ${...}")
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
    raise SystemExit("Refusing an invalid environment key")
# This file is consumed both by systemd EnvironmentFile and Docker --env-file.
# Docker keeps shell-style quote characters as part of the value, so use a
# conservative unquoted alphabet that is valid in both parsers.
if not re.fullmatch(r"[A-Za-z0-9_./:@%+,=-]*", value):
    raise SystemExit("Refusing an environment value that requires quoting")
if path.is_symlink():
    raise SystemExit(f"Refusing symlinked environment file: {path}")
parent = path.parent.absolute()
if parent.resolve() != parent:
    raise SystemExit(f"Refusing symlinked environment directory: {parent}")
metadata = path.stat() if path.exists() else None
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
prefix = key + "="
encoded_value = value
done = False
result = []
for line in lines:
    if line.startswith(prefix):
        result.append(prefix + encoded_value)
        done = True
    else:
        result.append(line)
if not done:
    result.append(prefix + encoded_value)
fd, temporary_path = tempfile.mkstemp(prefix=".env-update-", dir=parent, text=True)
try:
    if metadata is not None:
        os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)
except BaseException:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
    raise
PY
}

validate_app_dir_path() {
  local app_real relative existing_parent parent_uid parent_mode next_parent
  if [ -L "$APP_DIR" ]; then
    echo "Refusing symlinked application directory: ${APP_DIR}" >&2
    return 1
  fi
  app_real="$(realpath -m -- "$APP_DIR")" || return 1
  relative="${app_real#/}"
  case "$app_real" in
    /var/lib/*)
      ;;
    /|/bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/proc|/proc/*|/root|/root/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/tmp|/tmp/*|/usr|/usr/*|/var|/var/*)
      echo "Refusing unsafe application root: ${app_real}" >&2
      return 1
      ;;
  esac
  if [[ "$relative" != */* ]]; then
    echo "Application directory is too shallow: ${app_real}" >&2
    return 1
  fi
  existing_parent="$(dirname -- "$app_real")"
  while [ ! -e "$existing_parent" ]; do
    next_parent="$(dirname -- "$existing_parent")"
    if [ "$next_parent" = "$existing_parent" ]; then
      echo "Cannot find a safe parent for ${app_real}" >&2
      return 1
    fi
    existing_parent="$next_parent"
  done
  if [ ! -d "$existing_parent" ]; then
    echo "Application parent is not a directory: ${existing_parent}" >&2
    return 1
  fi
  parent_uid="$(stat -c '%u' -- "$existing_parent")" || return 1
  parent_mode="$(stat -c '%A' -- "$existing_parent")" || return 1
  if [ "$parent_uid" != "0" ] || [ "${parent_mode:5:1}" = "w" ] || [ "${parent_mode:8:1}" = "w" ]; then
    echo "Application parent must be root-owned and not group/other-writable: ${existing_parent}" >&2
    return 1
  fi
}

reject_unsafe_runtime_paths() {
  local child
  for child in data .env .venv backups .git app/.env app/.venv app/.git; do
    if [ -L "$APP_DIR/$child" ]; then
      echo "Refusing symlinked runtime path: ${APP_DIR}/${child}" >&2
      return 1
    fi
  done
}

secure_app_permissions() {
  local app_real
  validate_app_dir_path || return 1
  if [ -L "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
    echo "Refusing unsafe application directory: ${APP_DIR}" >&2
    return 1
  fi
  app_real="$(realpath -e -- "$APP_DIR")" || return 1
  if ! { [ -f "$APP_DIR/run.py" ] && [ ! -L "$APP_DIR/run.py" ] &&
         [ -f "$APP_DIR/requirements.txt" ] && [ ! -L "$APP_DIR/requirements.txt" ] &&
         [ -d "$APP_DIR/reseller_autoseller" ] && [ ! -L "$APP_DIR/reseller_autoseller" ]; } &&
     ! { [ -f "$APP_DIR/app/run.py" ] && [ ! -L "$APP_DIR/app/run.py" ] &&
         [ -f "$APP_DIR/app/requirements.txt" ] && [ ! -L "$APP_DIR/app/requirements.txt" ] &&
         [ -d "$APP_DIR/app/reseller_autoseller" ] && [ ! -L "$APP_DIR/app/reseller_autoseller" ] &&
         [ -f "$APP_DIR/run-container.sh" ] && [ ! -L "$APP_DIR/run-container.sh" ]; }; then
    echo "Application markers are missing in ${app_real}" >&2
    return 1
  fi
  chown -h root:root "$APP_DIR"
  chmod 0755 "$APP_DIR"
  reject_unsafe_runtime_paths || return 1
  rm -rf -- "$APP_DIR/.git" "$APP_DIR/app/.git"

  mkdir -p "$APP_DIR/data" "$APP_DIR/backups"
  find "$APP_DIR" -xdev -path "$APP_DIR/data" -prune -o -exec chown -h root:root {} +
  find "$APP_DIR" \
    -xdev \
    -path "$APP_DIR/data" -prune -o \
    -path "$APP_DIR/backups" -prune -o \
    -path "$APP_DIR/.env" -prune -o \
    ! -type l -exec chmod u+rwX,go+rX,go-w {} +
  chmod 0700 "$APP_DIR/backups"
  find "$APP_DIR/backups" -xdev -type d -exec chmod 0700 {} +
  find "$APP_DIR/backups" -xdev -type f -exec chmod 0600 {} +
  if id "$APP_USER" >/dev/null 2>&1; then
    chown -hR "$APP_USER:$APP_USER" "$APP_DIR/data"
    chmod 0750 "$APP_DIR/data"
    if [ -f "$APP_DIR/.env" ]; then
      chown "root:$APP_USER" "$APP_DIR/.env"
      chmod 0640 "$APP_DIR/.env"
    fi
  fi
}

trusted_python() {
  local candidate real uid mode
  if [ -n "$APP_PYTHON_BIN" ]; then
    candidate="$APP_PYTHON_BIN"
  else
    for candidate in /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10 /usr/bin/python3; do
      [ -x "$candidate" ] && break
    done
  fi
  if [ -z "${candidate:-}" ] || [ ! -x "$candidate" ]; then
    echo "A trusted system Python 3.10-3.12 was not found." >&2
    return 1
  fi
  real="$(realpath -e -- "$candidate")" || return 1
  uid="$(stat -c '%u' -- "$real")" || return 1
  mode="$(stat -c '%A' -- "$real")" || return 1
  if [ "$uid" != "0" ] || [ "${mode:5:1}" = "w" ] || [ "${mode:8:1}" = "w" ]; then
    echo "Refusing untrusted Python interpreter: ${real}" >&2
    return 1
  fi
  if ! "$real" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)
PY
  then
    echo "Updater requires Python 3.10-3.12: ${real}" >&2
    return 1
  fi
  printf '%s\n' "$real"
}

rebuild_systemd_venv() {
  local python_bin old_venv=""
  reject_unsafe_runtime_paths || return 1
  python_bin="$(trusted_python)" || return 1
  VENV_NEW="$(mktemp -d "$APP_DIR/.venv-new.XXXXXX")"
  "$python_bin" -m venv --clear "$VENV_NEW"
  "$VENV_NEW/bin/python" -m pip install --upgrade pip
  "$VENV_NEW/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
  if [ -e "$APP_DIR/.venv" ]; then
    old_venv="$APP_DIR/.venv-old-${REQUEST_ID:-$$}"
    if [ -e "$old_venv" ] || [ -L "$old_venv" ]; then
      echo "Refusing existing venv rollback path: ${old_venv}" >&2
      return 1
    fi
    mv -T -- "$APP_DIR/.venv" "$old_venv"
  fi
  if ! mv -T -- "$VENV_NEW" "$APP_DIR/.venv"; then
    [ -n "$old_venv" ] && mv -T -- "$old_venv" "$APP_DIR/.venv"
    return 1
  fi
  VENV_NEW=""
  [ -n "$old_venv" ] && rm -rf -- "$old_venv"
  return 0
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
    --exclude "backups" \
    --exclude "output" \
    "$source_dir/" "$target_dir/"
}

refresh_host_updater() {
  local source="$1"
  local destination_parent
  destination_parent="$(dirname "$APP_UPDATER_PATH")"
  if [ ! -f "$source" ] || [ -L "$source" ]; then
    echo "Trusted update does not contain a regular updater script: ${source}" >&2
    return 1
  fi
  if [ ! -d "$destination_parent" ] || [ -L "$destination_parent" ] || [ -L "$APP_UPDATER_PATH" ]; then
    echo "Refusing unsafe host updater destination: ${APP_UPDATER_PATH}" >&2
    return 1
  fi
  install -o root -g root -m 0755 "$source" "$APP_UPDATER_PATH"
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf -- "$TMP_DIR"
  fi
  if [ -n "$VENV_NEW" ] && [ -d "$VENV_NEW" ]; then
    rm -rf -- "$VENV_NEW"
  fi
  if [ -n "$BACKUP_TMP" ] && [ -f "$BACKUP_TMP" ]; then
    rm -f -- "$BACKUP_TMP"
  fi
}

handle_error() {
  local exit_code="$1"
  local line="$2"
  trap - ERR
  write_status \
    "error" \
    "$CURRENT_STAGE" \
    "Update request ${REQUEST_ID:-unknown} failed at line ${line} (exit ${exit_code}). Retry from the panel." || true
  exit "$exit_code"
}

trap cleanup EXIT
trap 'handle_error "$?" "$LINENO"' ERR

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

CURRENT_STAGE="permissions"
secure_app_permissions

CURRENT_STAGE="request"
if ! REQUEST_ID="$(consume_request)"; then
  write_status "error" "request" "Rejected an unsafe or invalid update request. Create a new request from the panel."
  exit 1
fi

if [ -z "$APP_REPO_URL" ] || [ -z "$APP_BRANCH" ]; then
  write_status "error" "configuration" "The root-owned updater repository or branch is empty."
  exit 1
fi

CURRENT_STAGE="fetch"
write_status "running" "fetch" "Downloading the configured repository (${APP_BRANCH})..."
TMP_DIR="$(mktemp -d)"
git clone --depth 1 --branch "$APP_BRANCH" -- "$APP_REPO_URL" "$TMP_DIR/source"
COMMIT="$(git -C "$TMP_DIR/source" rev-parse --short=12 HEAD)"

mkdir -p "$APP_DIR/backups" "$APP_DIR/data"

if [ -d "$APP_DIR/app" ] && [ -f "$APP_DIR/run-container.sh" ]; then
  CURRENT_STAGE="backup"
  write_status "running" "backup" "Creating source backup before Docker update..."
  BACKUP_TMP="$(mktemp "$APP_DIR/backups/.app-before-update.XXXXXX.tar.gz")"
  tar -czf "$BACKUP_TMP" -C "$APP_DIR" app
  mv -Tf -- "$BACKUP_TMP" "$APP_DIR/backups/app-before-update-$(date +%Y%m%d%H%M%S).tar.gz"
  BACKUP_TMP=""

  CURRENT_STAGE="install"
  write_status "running" "install" "Replacing application files..."
  install_from_git "$TMP_DIR/source" "$APP_DIR/app"
  refresh_host_updater "$APP_DIR/app/scripts/update-linux.sh"

  if [ -f "$APP_DIR/.env" ]; then
    upsert_env "$APP_DIR/.env" "APP_UPDATE_CURRENT_COMMIT" "$COMMIT"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_REPO_URL" "$APP_REPO_URL"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_BRANCH" "$APP_BRANCH"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_TRIGGER_FILE" "data/update-request.json"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_STATUS_FILE" "data/update-status.json"
  fi

  CURRENT_STAGE="build"
  write_status "running" "build" "Building Docker image..."
  docker build -t "$APP_IMAGE_NAME" "$APP_DIR/app"

  CURRENT_STAGE="restart"
  write_status "running" "restart" "Restarting Docker container..."
  "$APP_DIR/run-container.sh"
else
  CURRENT_STAGE="backup"
  write_status "running" "backup" "Creating source backup before systemd update..."
  BACKUP_TMP="$(mktemp "$APP_DIR/backups/.source-before-update.XXXXXX.tar.gz")"
  tar -czf "$BACKUP_TMP" \
    --exclude ".git" --exclude ".venv" --exclude ".env" \
    --exclude "data" --exclude "backups" --exclude "output" --exclude "*.log" \
    -C "$APP_DIR" .
  mv -Tf -- "$BACKUP_TMP" "$APP_DIR/backups/source-before-update-$(date +%Y%m%d%H%M%S).tar.gz"
  BACKUP_TMP=""

  CURRENT_STAGE="install"
  write_status "running" "install" "Replacing application files..."
  install_from_git "$TMP_DIR/source" "$APP_DIR"
  refresh_host_updater "$APP_DIR/scripts/update-linux.sh"

  rebuild_systemd_venv
  if [ -f "$APP_DIR/.env" ]; then
    upsert_env "$APP_DIR/.env" "APP_UPDATE_CURRENT_COMMIT" "$COMMIT"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_REPO_URL" "$APP_REPO_URL"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_BRANCH" "$APP_BRANCH"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_TRIGGER_FILE" "data/update-request.json"
    upsert_env "$APP_DIR/.env" "APP_UPDATE_STATUS_FILE" "$APP_STATUS_FILE"
  fi
  secure_app_permissions

  CURRENT_STAGE="restart"
  write_status "running" "restart" "Restarting systemd service..."
  systemctl restart "$APP_SERVICE_NAME"
fi

CURRENT_STAGE="done"
write_status "success" "done" "Updated to ${COMMIT}."
