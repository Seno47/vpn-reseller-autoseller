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

write_env() {
  local env_file="$1"
  umask 077
  cat > "$env_file" <<EOF_ENV
APP_HOST=127.0.0.1
APP_PORT=${APP_PORT}
APP_BASE_URL=${APP_BASE_URL}

XYRANET_API_BASE_URL=https://xyranet.pro/api/wholesale
XYRANET_API_KEY=${XYRANET_API_KEY}
XYRANET_TIMEOUT_SECONDS=30

DIGISELLER_SELLER_ID=${DIGISELLER_SELLER_ID}
DIGISELLER_API_KEY=${DIGISELLER_API_KEY}
GGSEL_SELLER_ID=${GGSEL_SELLER_ID}
GGSEL_API_KEY=${GGSEL_API_KEY}

TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
ADMIN_USERNAME=${ADMIN_USERNAME}
ADMIN_PASSWORD=${ADMIN_PASSWORD}

DATABASE_PATH=data/reseller.sqlite3
PANEL_LANGUAGE=${PANEL_LANGUAGE}
ENABLE_TELEGRAM=true
APP_UPDATE_REPO_URL=${APP_REPO_URL}
APP_UPDATE_BRANCH=${APP_BRANCH}
APP_UPDATE_CHECK_INTERVAL_HOURS=12
APP_UPDATE_TRIGGER_FILE=data/update-request.json
APP_UPDATE_STATUS_FILE=data/update-status.json
LOG_LEVEL=INFO
EOF_ENV
}

install_source() {
  local repo_url="${APP_REPO_URL:-}"
  if [ -f "run.py" ] && [ -d "reseller_autoseller" ]; then
    mkdir -p "$APP_DIR"
    rsync -a --delete \
      --exclude ".venv" \
      --exclude ".env" \
      --exclude "data/*.sqlite3-wal" \
      --exclude "data/*.sqlite3-shm" \
      ./ "$APP_DIR/"
    return
  fi

  if [ -z "$repo_url" ]; then
    repo_url="$(ask "GitHub repository URL")"
    APP_REPO_URL="$repo_url"
  fi
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
  else
    rm -rf "$APP_DIR"
    git clone "$repo_url" "$APP_DIR"
  fi
}

install_update_service() {
  local updater="/usr/local/sbin/${APP_NAME}-update"
  install -m 0755 "$APP_DIR/scripts/update-linux.sh" "$updater"
  cat > "/etc/systemd/system/${APP_NAME}-updater.service" <<EOF_SERVICE
[Unit]
Description=XyraNet reseller autoseller updater

[Service]
Type=oneshot
Environment=APP_NAME=${APP_NAME}
Environment=APP_DIR=${APP_DIR}
Environment=APP_USER=${APP_USER}
Environment=APP_REPO_URL=${APP_REPO_URL}
Environment=APP_BRANCH=${APP_BRANCH}
Environment=APP_SERVICE_NAME=${APP_NAME}.service
Environment=APP_REQUEST_FILE=${APP_DIR}/data/update-request.json
Environment=APP_STATUS_FILE=${APP_DIR}/data/update-status.json
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
"$PYTHON_BIN" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data
write_env "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

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
