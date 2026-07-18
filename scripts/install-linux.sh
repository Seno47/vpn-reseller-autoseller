#!/usr/bin/env bash
set -euo pipefail

APP_NAME="xyranet-reseller-autoseller"
APP_DIR="${APP_DIR:-/opt/${APP_NAME}}"
APP_USER="${APP_USER:-xyranet-reseller}"
APP_PORT="${APP_PORT:-8095}"
APP_REPO_URL="${REPO_URL:-https://github.com/Seno47/vpn-reseller-autoseller.git}"
APP_BRANCH="${APP_BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-}"
export DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo bash scripts/install-linux.sh"
    exit 1
  fi
}

ask() {
  local prompt="$1"
  local default="${2:-}"
  local value
  if [ -n "$default" ]; then
    read -r -p "${prompt} [${default}]: " value
    echo "${value:-$default}"
  else
    read -r -p "${prompt}: " value
    echo "$value"
  fi
}

ask_secret() {
  local prompt="$1"
  local value
  read -r -s -p "${prompt}: " value
  echo
  echo "$value"
}

apt_install() {
  apt-get install -y "$@"
}

system_python_supported() {
  local python_bin="$1"
  "$python_bin" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if (3, 10) <= sys.version_info[:2] <= (3, 12) else 1)
PY
}

python_version_text() {
  local python_bin="$1"
  "$python_bin" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

ensure_python_venv() {
  local python_bin="$1"
  local package

  if "$python_bin" -m venv --help >/dev/null 2>&1; then
    return
  fi

  package="$(basename "$python_bin")-venv"
  echo "Installing venv support for $(basename "$python_bin")..."
  apt_install "$package" || apt_install python3-venv
}

find_supported_python() {
  local candidate
  for candidate in "${PYTHON_BIN:-}" python3.12 python3.11 python3.10 python3; do
    if [ -n "$candidate" ] && command -v "$candidate" >/dev/null 2>&1 && system_python_supported "$candidate"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

install_python_runtime() {
  local python_bin

  if python_bin="$(find_supported_python)"; then
    PYTHON_BIN="$python_bin"
    ensure_python_venv "$PYTHON_BIN"
    echo "Using Python $(python_version_text "$PYTHON_BIN") at ${PYTHON_BIN}"
    return
  fi

  echo "Supported Python 3.10-3.12 was not found. Installing Python packages..."
  apt_install python3 python3-venv python3-pip

  if python_bin="$(find_supported_python)"; then
    PYTHON_BIN="$python_bin"
    ensure_python_venv "$PYTHON_BIN"
    echo "Using Python $(python_version_text "$PYTHON_BIN") at ${PYTHON_BIN}"
    return
  fi

  if [ -r /etc/os-release ]; then
    . /etc/os-release
  fi

  if [ "${ID:-}" = "ubuntu" ]; then
    echo "Default Ubuntu repositories do not provide supported Python. Adding deadsnakes PPA..."
    apt_install software-properties-common ca-certificates
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
    apt_install python3.12 python3.12-venv python3.12-dev
    PYTHON_BIN="$(command -v python3.12)"
    ensure_python_venv "$PYTHON_BIN"
    echo "Using Python $(python_version_text "$PYTHON_BIN") at ${PYTHON_BIN}"
    return
  fi

  echo "Could not install supported Python automatically on this OS."
  echo "Install Python 3.10, 3.11 or 3.12, then run again with PYTHON_BIN=/path/to/python."
  exit 1
}

prepare_system_packages() {
  echo "Updating package index..."
  apt-get update

  if [ "${SKIP_SYSTEM_UPGRADE:-0}" != "1" ]; then
    echo "Upgrading installed system packages..."
    apt-get upgrade -y
  else
    echo "Skipping system upgrade because SKIP_SYSTEM_UPGRADE=1."
  fi

  apt_install ca-certificates curl git rsync openssl build-essential python3-dev util-linux
  install_python_runtime
}

dotenv_quote() {
  local name="$1"
  local value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "${name} cannot contain a line break." >&2
    return 1
  fi
  if [[ "$value" == *'${'* ]]; then
    echo "${name} cannot contain a \${...} sequence." >&2
    return 1
  fi
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

write_env() {
  local env_file="$1"
  local app_base_url_value xyranet_api_key_value digiseller_seller_id_value
  local digiseller_api_key_value ggsel_seller_id_value ggsel_api_key_value
  local telegram_bot_token_value admin_ids_value admin_username_value admin_password_value
  local panel_language_value repo_url_value branch_value

  app_base_url_value="$(dotenv_quote "APP_BASE_URL" "$APP_BASE_URL")"
  xyranet_api_key_value="$(dotenv_quote "XYRANET_API_KEY" "$XYRANET_API_KEY")"
  digiseller_seller_id_value="$(dotenv_quote "DIGISELLER_SELLER_ID" "$DIGISELLER_SELLER_ID")"
  digiseller_api_key_value="$(dotenv_quote "DIGISELLER_API_KEY" "$DIGISELLER_API_KEY")"
  ggsel_seller_id_value="$(dotenv_quote "GGSEL_SELLER_ID" "$GGSEL_SELLER_ID")"
  ggsel_api_key_value="$(dotenv_quote "GGSEL_API_KEY" "$GGSEL_API_KEY")"
  telegram_bot_token_value="$(dotenv_quote "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN")"
  admin_ids_value="$(dotenv_quote "ADMIN_IDS" "$ADMIN_IDS")"
  admin_username_value="$(dotenv_quote "ADMIN_USERNAME" "$ADMIN_USERNAME")"
  admin_password_value="$(dotenv_quote "ADMIN_PASSWORD" "$ADMIN_PASSWORD")"
  panel_language_value="$(dotenv_quote "PANEL_LANGUAGE" "$PANEL_LANGUAGE")"
  repo_url_value="$(dotenv_quote "APP_UPDATE_REPO_URL" "$APP_REPO_URL")"
  branch_value="$(dotenv_quote "APP_UPDATE_BRANCH" "$APP_BRANCH")"

  (
    umask 077
    cat > "$env_file" <<EOF_ENV
APP_HOST=127.0.0.1
APP_PORT=${APP_PORT}
APP_BASE_URL=${app_base_url_value}

XYRANET_API_BASE_URL=https://xyranet.pro/api/wholesale
XYRANET_API_KEY=${xyranet_api_key_value}
XYRANET_TIMEOUT_SECONDS=30

DIGISELLER_SELLER_ID=${digiseller_seller_id_value}
DIGISELLER_API_KEY=${digiseller_api_key_value}
GGSEL_SELLER_ID=${ggsel_seller_id_value}
GGSEL_API_KEY=${ggsel_api_key_value}

TELEGRAM_BOT_TOKEN=${telegram_bot_token_value}
ADMIN_IDS=${admin_ids_value}
ADMIN_USERNAME=${admin_username_value}
ADMIN_PASSWORD=${admin_password_value}

DATABASE_PATH=data/reseller.sqlite3
PANEL_LANGUAGE=${panel_language_value}
ENABLE_TELEGRAM=true
APP_UPDATE_REPO_URL=${repo_url_value}
APP_UPDATE_BRANCH=${branch_value}
APP_UPDATE_CHECK_INTERVAL_HOURS=12
APP_UPDATE_TRIGGER_FILE=data/update-request.json
APP_UPDATE_STATUS_FILE=/run/${APP_NAME}/update-status.json
LOG_LEVEL=INFO
EOF_ENV
  )
}

sync_source_tree() {
  local source_dir="$1"
  local source_real app_real
  claim_install_target_boundary
  reject_unsafe_preserved_paths
  source_real="$(realpath "$source_dir")"
  app_real="$(realpath "$APP_DIR")"
  if [ "$source_real" = "$app_real" ]; then
    return
  fi
  rsync -a --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude ".env" \
    --exclude ".pytest_cache" \
    --exclude "__pycache__" \
    --exclude "backups" \
    --exclude "data" \
    --exclude "output" \
    --exclude "*.log" \
    "$source_dir/" "$APP_DIR/"
}

install_source() {
  local repo_url="${APP_REPO_URL:-}"
  local temporary_dir=""
  validate_install_target
  if [ -z "$repo_url" ]; then
    repo_url="$(ask "GitHub repository URL")"
    APP_REPO_URL="$repo_url"
  fi
  temporary_dir="$(mktemp -d)"
  if ! git clone --depth 1 --branch "$APP_BRANCH" -- "$repo_url" "$temporary_dir/source"; then
    rm -rf -- "$temporary_dir"
    return 1
  fi
  if ! sync_source_tree "$temporary_dir/source"; then
    rm -rf -- "$temporary_dir"
    return 1
  fi
  rm -rf -- "$temporary_dir"
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

validate_install_target() {
  validate_app_dir_path || return 1
  if [ ! -e "$APP_DIR" ]; then
    return 0
  fi
  if [ ! -d "$APP_DIR" ]; then
    echo "Application target is not a directory: ${APP_DIR}" >&2
    return 1
  fi
  if [ -f "$APP_DIR/run.py" ] && [ ! -L "$APP_DIR/run.py" ] &&
     [ -f "$APP_DIR/requirements.txt" ] && [ ! -L "$APP_DIR/requirements.txt" ] &&
     [ -d "$APP_DIR/reseller_autoseller" ] && [ ! -L "$APP_DIR/reseller_autoseller" ]; then
    return 0
  fi
  if [ -z "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    return 0
  fi
  echo "Refusing to replace a non-empty directory without application markers: ${APP_DIR}" >&2
  return 1
}

claim_install_target_boundary() {
  validate_install_target || return 1
  mkdir -p "$APP_DIR"
  chown -h root:root "$APP_DIR"
  chmod 0755 "$APP_DIR"
  validate_install_target
}

reject_unsafe_preserved_paths() {
  local child
  for child in data .env .venv backups .git; do
    if [ -L "$APP_DIR/$child" ]; then
      echo "Refusing symlinked preserved path: ${APP_DIR}/${child}" >&2
      return 1
    fi
  done
}

remove_legacy_git_metadata() {
  reject_unsafe_preserved_paths || return 1
  rm -rf -- "$APP_DIR/.git"
}

install_runtime_artifacts() {
  local venv_new env_new old_venv old_env had_old_venv=0 had_old_env=0
  reject_unsafe_preserved_paths || return 1
  venv_new="$(mktemp -d "$APP_DIR/.venv-new.XXXXXX")"
  env_new="$(mktemp "$APP_DIR/.env-new.XXXXXX")"
  if ! "$PYTHON_BIN" -m venv --clear "$venv_new" ||
     ! "$venv_new/bin/python" -m pip install --upgrade pip ||
     ! "$venv_new/bin/python" -m pip install -r "$APP_DIR/requirements.txt" ||
     ! write_env "$env_new"; then
    rm -rf -- "$venv_new"
    rm -f -- "$env_new"
    return 1
  fi

  old_venv="$APP_DIR/.venv-old-$$"
  old_env="$APP_DIR/.env-old-$$"
  if [ -e "$old_venv" ] || [ -L "$old_venv" ] || [ -e "$old_env" ] || [ -L "$old_env" ]; then
    echo "Refusing existing runtime rollback paths in ${APP_DIR}" >&2
    rm -rf -- "$venv_new"
    rm -f -- "$env_new"
    return 1
  fi

  trap '' INT TERM
  if [ -e "$APP_DIR/.venv" ]; then
    if ! mv -T -- "$APP_DIR/.venv" "$old_venv"; then
      trap - INT TERM
      rm -rf -- "$venv_new"
      rm -f -- "$env_new"
      return 1
    fi
    had_old_venv=1
  fi
  if [ -e "$APP_DIR/.env" ]; then
    if ! mv -T -- "$APP_DIR/.env" "$old_env"; then
      [ "$had_old_venv" = "1" ] && mv -T -- "$old_venv" "$APP_DIR/.venv"
      trap - INT TERM
      rm -rf -- "$venv_new"
      rm -f -- "$env_new"
      return 1
    fi
    had_old_env=1
  fi
  if ! mv -T -- "$venv_new" "$APP_DIR/.venv"; then
    [ "$had_old_venv" = "1" ] && mv -T -- "$old_venv" "$APP_DIR/.venv"
    [ "$had_old_env" = "1" ] && mv -T -- "$old_env" "$APP_DIR/.env"
    trap - INT TERM
    rm -rf -- "$venv_new"
    rm -f -- "$env_new"
    return 1
  fi
  if ! mv -T -- "$env_new" "$APP_DIR/.env"; then
    rm -rf -- "$APP_DIR/.venv"
    [ "$had_old_venv" = "1" ] && mv -T -- "$old_venv" "$APP_DIR/.venv"
    [ "$had_old_env" = "1" ] && mv -T -- "$old_env" "$APP_DIR/.env"
    trap - INT TERM
    rm -f -- "$env_new"
    return 1
  fi
  trap - INT TERM
  [ "$had_old_venv" = "1" ] && rm -rf -- "$old_venv"
  [ "$had_old_env" = "1" ] && rm -f -- "$old_env"
  return 0
}

secure_app_permissions() {
  local app_real
  validate_app_dir_path || exit 1
  if [ -L "$APP_DIR" ] || [ ! -d "$APP_DIR" ]; then
    echo "Refusing unsafe application directory: ${APP_DIR}" >&2
    exit 1
  fi
  app_real="$(realpath -e -- "$APP_DIR")" || exit 1
  if ! { [ -f "$APP_DIR/run.py" ] && [ ! -L "$APP_DIR/run.py" ] &&
         [ -f "$APP_DIR/requirements.txt" ] && [ ! -L "$APP_DIR/requirements.txt" ] &&
         [ -d "$APP_DIR/reseller_autoseller" ] && [ ! -L "$APP_DIR/reseller_autoseller" ]; }; then
    echo "Application markers are missing in ${app_real}" >&2
    exit 1
  fi
  if [ -L "$APP_DIR/data" ]; then
    echo "Refusing symlinked data directory: ${APP_DIR}/data" >&2
    exit 1
  fi
  if [ -L "$APP_DIR/.env" ]; then
    echo "Refusing symlinked environment file: ${APP_DIR}/.env" >&2
    exit 1
  fi

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
  chown -hR "$APP_USER:$APP_USER" "$APP_DIR/data"
  chmod 0750 "$APP_DIR/data"
  chown "root:$APP_USER" "$APP_DIR/.env"
  chmod 0640 "$APP_DIR/.env"
}

install_update_service() {
  local updater="/usr/local/sbin/${APP_NAME}-update"
  install -m 0755 "$APP_DIR/scripts/update-linux.sh" "$updater"
  cat > "/etc/systemd/system/${APP_NAME}-updater.service" <<EOF_SERVICE
[Unit]
Description=XyraNet reseller autoseller updater

[Service]
Type=oneshot
User=root
Group=root
UMask=0022
RuntimeDirectory=${APP_NAME}
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
Environment=APP_NAME=${APP_NAME}
Environment=APP_DIR=${APP_DIR}
Environment=APP_USER=${APP_USER}
Environment=APP_REPO_URL=${APP_REPO_URL}
Environment=APP_BRANCH=${APP_BRANCH}
Environment=APP_SERVICE_NAME=${APP_NAME}.service
Environment=APP_REQUEST_FILE=${APP_DIR}/data/update-request.json
Environment=APP_STATUS_FILE=/run/${APP_NAME}/update-status.json
Environment=APP_UPDATER_PATH=${updater}
Environment=APP_PYTHON_BIN=${PYTHON_BIN}
ExecStart=${updater}
EOF_SERVICE

  cat > "/etc/systemd/system/${APP_NAME}-updater.path" <<EOF_PATH
[Unit]
Description=Watch XyraNet reseller autoseller update requests

[Path]
PathExists=${APP_DIR}/data/update-request.json
Unit=${APP_NAME}-updater.service

[Install]
WantedBy=multi-user.target
EOF_PATH

  systemctl daemon-reload
  systemctl enable --now "${APP_NAME}-updater.path"
}

install_systemd() {
  cat > "/etc/systemd/system/${APP_NAME}.service" <<EOF_SERVICE
[Unit]
Description=XyraNet reseller autoseller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
UMask=0077
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/.venv/bin/python run.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF_SERVICE
  systemctl daemon-reload
  systemctl enable --now "${APP_NAME}.service"
}

install_nginx_https() {
  local domain="$1"
  cat > "/etc/nginx/sites-available/${APP_NAME}" <<EOF_NGINX
server {
    listen 80;
    server_name ${domain};

    # Marketplace callback credentials are embedded in the URL path by the
    # upstream APIs. Never persist those paths in nginx access logs.
    location ~ ^/api/(digiseller/notify/(sale|message)|ggsel/notify/order)/ {
        access_log off;
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF_NGINX
  ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
  nginx -t
  systemctl reload nginx
  certbot --nginx -d "$domain" --redirect --agree-tos --no-eff-email -m "$CERTBOT_EMAIL"
}

need_root
validate_install_target
claim_install_target_boundary
reject_unsafe_preserved_paths

echo "== XyraNet Reseller Autoseller Linux installer =="
echo "The app itself will bind to 127.0.0.1:${APP_PORT}; it will not expose HTTP directly."

prepare_system_packages

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

ADMIN_USERNAME="$(ask "Web panel login" "admin")"
ADMIN_PASSWORD="$(ask_secret "Web panel password")"
while [ "${#ADMIN_PASSWORD}" -lt 8 ]; do ADMIN_PASSWORD="$(ask_secret "Web panel password must be at least 8 characters")"; done

PANEL_LANGUAGE="$(ask "Interface and Telegram bot language: ru/en" "ru")"
PANEL_LANGUAGE="$(printf '%s' "$PANEL_LANGUAGE" | tr '[:upper:]' '[:lower:]')"
while [ "$PANEL_LANGUAGE" != "ru" ] && [ "$PANEL_LANGUAGE" != "en" ]; do
  PANEL_LANGUAGE="$(ask "Language must be ru or en" "ru")"
  PANEL_LANGUAGE="$(printf '%s' "$PANEL_LANGUAGE" | tr '[:upper:]' '[:lower:]')"
done

TELEGRAM_BOT_TOKEN="$(ask_secret "Telegram bot token (can be empty)")"
ADMIN_IDS="$(ask "Telegram admin ID, comma separated" "")"
while [ -z "$ADMIN_IDS" ]; do ADMIN_IDS="$(ask "Telegram admin ID is required")"; done

XYRANET_API_KEY="$(ask_secret "XyraNet API key (can be filled later in panel)")"
DIGISELLER_SELLER_ID="$(ask "Digiseller seller ID (optional)" "")"
DIGISELLER_API_KEY="$(ask_secret "Digiseller API key (optional)")"
GGSEL_SELLER_ID="$(ask "GGsel seller ID (optional)" "")"
GGSEL_API_KEY="$(ask_secret "GGsel API key (optional)")"

USE_DOMAIN="$(ask "Use domain with HTTPS? yes/no" "no")"
DOMAIN=""
CERTBOT_EMAIL=""
if [ "$USE_DOMAIN" = "yes" ] || [ "$USE_DOMAIN" = "y" ]; then
  DOMAIN="$(ask "Domain name, DNS A/AAAA must point to this server")"
  CERTBOT_EMAIL="$(ask "Email for Let's Encrypt")"
  APP_BASE_URL="https://${DOMAIN}"
  apt_install nginx certbot python3-certbot-nginx
else
  APP_BASE_URL="http://127.0.0.1:${APP_PORT}"
fi

install_source
cd "$APP_DIR"
claim_install_target_boundary
remove_legacy_git_metadata
mkdir -p data
install_runtime_artifacts
secure_app_permissions

install_systemd
install_update_service

if [ -n "$DOMAIN" ]; then
  install_nginx_https "$DOMAIN"
fi

echo
echo "Installed."
echo "Service: systemctl status ${APP_NAME}"
echo "Logs: journalctl -u ${APP_NAME} -f"
if [ -n "$DOMAIN" ]; then
  echo "Panel: https://${DOMAIN}"
else
  echo "No public web panel was exposed."
  echo "Connect from your computer with:"
  echo "ssh -L ${APP_PORT}:127.0.0.1:${APP_PORT} user@server"
  echo "Then open: http://127.0.0.1:${APP_PORT}"
fi
