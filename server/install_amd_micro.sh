#!/usr/bin/env bash
# Install the Readori source-validator Queue executor on a small Ubuntu/Debian VM.
#
# The script is intentionally non-interactive: provide Cloudflare values through
# environment variables (or source an untracked env file) so credentials never
# appear in shell history or in the generated unit file's command line.
# Required variables:
#   READORI_AMD_EXECUTOR_BASE_URL  Cloudflare Worker URL (https://...)
#   READORI_AMD_EXECUTOR_TOKEN     same value as Worker EXECUTOR_TOKEN
#   READORI_CF_ACCOUNT_ID          Cloudflare account id
#   READORI_CF_QUEUE_ID            Queue id, not the queue name
#   READORI_CF_QUEUE_API_TOKEN     account Queue HTTP Pull read/write token
#
# Optional variables:
#   READORI_INSTALL_DIR (default /opt/readori-validator)
#   READORI_AMD_EXECUTOR_ID (default amd-micro-<hostname>)
#   READORI_AMD_WORK_DIR (default /var/lib/readori-validator)
#   READORI_AMD_POLL_SECONDS (default 3)
#   READORI_AMD_MAX_ATTEMPTS (default 3)
#   READORI_VALIDATOR_PORT / READORI_VALIDATOR_HOST are accepted for shared env
#   READORI_SKIP_APT=1 skips apt package installation (use only on prepared hosts)
#   READORI_SKIP_SYSTEMD=1 installs files and the virtualenv without starting it

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="${READORI_INSTALL_DIR:-/opt/readori-validator}"
CONFIG_DIR="/etc/readori-validator"
ENV_FILE="$CONFIG_DIR/amd-micro.env"
SERVICE_NAME="readori-source-validator"
WORK_DIR="${READORI_AMD_WORK_DIR:-/var/lib/readori-validator}"
SERVICE_USER="readori"
SERVICE_GROUP="readori"

die() {
  echo "[readori] ERROR: $*" >&2
  exit 1
}

validate_path() {
  local name="$1"
  local value="$2"
  case "$value" in
    /*) ;;
    *) die "$name must be an absolute path" ;;
  esac
  case "$value" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var)
      die "$name points to a protected system directory: $value" ;;
  esac
  case "$value" in
    *' '*|*$'\t'*) die "$name cannot contain whitespace" ;;
  esac
}

info() {
  echo "[readori] $*"
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "run this installer as root (for example: sudo bash $0)"
}

validate_single_line() {
  local name="$1"
  local value="$2"
  case "$value" in
    *$'\n'*|*$'\r'*) die "$name must be a single-line value" ;;
  esac
}

require_value() {
  local name="$1"
  local value="${!name:-}"
  [ -n "$value" ] || die "$name is required; export it before running the installer"
  validate_single_line "$name" "$value"
}

env_quote() {
  # systemd EnvironmentFile understands double-quoted values. Escape only the
  # characters that can terminate or alter that quoted value.
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/}"
  value="${value//$'\r'/}"
  printf '"%s"' "$value"
}

install_os_dependencies() {
  if [ "${READORI_SKIP_APT:-0}" = "1" ]; then
    info "READORI_SKIP_APT=1; using packages already installed on this host"
    return
  fi
  command -v apt-get >/dev/null 2>&1 || die "Ubuntu/Debian apt-get is required (or set READORI_SKIP_APT=1 on a prepared host)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    ca-certificates curl openssl \
    python3 python3-dev python3-venv python3-pip \
    build-essential nodejs p7zip-full unzip
}

copy_project() {
  validate_path READORI_INSTALL_DIR "$INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  if [ "$SOURCE_ROOT" = "$INSTALL_DIR" ]; then
    info "Source root is already $INSTALL_DIR; no copy is needed"
    return
  fi
  local item
  for item in validator server requirements-validate-sources.txt requirements.txt; do
    [ -e "$SOURCE_ROOT/$item" ] || die "source file is missing: $SOURCE_ROOT/$item"
    cp -a "$SOURCE_ROOT/$item" "$INSTALL_DIR/"
  done
  # Prevent stale bytecode and local job databases from being copied or used by
  # the service. The source repository never needs those runtime artefacts.
  find "$INSTALL_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  find "$INSTALL_DIR" -type f -name '*.pyc' -delete 2>/dev/null || true
}

create_service_account() {
  validate_path READORI_AMD_WORK_DIR "$WORK_DIR"
  if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
  fi
  if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$WORK_DIR" --create-home \
      --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  mkdir -p "$CONFIG_DIR" "$WORK_DIR" "$INSTALL_DIR"
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$WORK_DIR"
  chown -R root:root "$INSTALL_DIR"
  chmod 0755 "$INSTALL_DIR"
  chmod 0755 "$CONFIG_DIR"
  chmod 0750 "$WORK_DIR"
}

create_virtualenv() {
  local python_bin=""
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      python_bin="$(command -v "$candidate")"
      break
    fi
  done
  [ -n "$python_bin" ] || die "Python 3 is not available after dependency installation"
  "$python_bin" -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade --disable-pip-version-check --no-cache-dir pip
  "$INSTALL_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir \
    -r "$INSTALL_DIR/requirements-validate-sources.txt" \
    -r "$INSTALL_DIR/server/requirements.txt"
  "$INSTALL_DIR/.venv/bin/python" -m py_compile \
    "$INSTALL_DIR/server/amd_micro_executor.py" \
    "$INSTALL_DIR/server/source_validator_server.py" \
    "$INSTALL_DIR/validator/validate_source_packages.py"
}

write_env_file() {
  require_value READORI_AMD_EXECUTOR_BASE_URL
  require_value READORI_AMD_EXECUTOR_TOKEN
  require_value READORI_CF_ACCOUNT_ID
  require_value READORI_CF_QUEUE_ID
  require_value READORI_CF_QUEUE_API_TOKEN

  local executor_id="${READORI_AMD_EXECUTOR_ID:-amd-micro-$(hostname -s 2>/dev/null || hostname)}"
  local poll_seconds="${READORI_AMD_POLL_SECONDS:-3}"
  local max_attempts="${READORI_AMD_MAX_ATTEMPTS:-3}"
  validate_single_line READORI_AMD_EXECUTOR_ID "$executor_id"
  validate_single_line READORI_AMD_POLL_SECONDS "$poll_seconds"
  validate_single_line READORI_AMD_MAX_ATTEMPTS "$max_attempts"

  case "$poll_seconds" in
    ''|*[!0-9.]* ) die "READORI_AMD_POLL_SECONDS must be numeric" ;;
  esac
  case "$max_attempts" in
    ''|*[!0-9]* ) die "READORI_AMD_MAX_ATTEMPTS must be an integer" ;;
  esac

  umask 077
  local temp_file
  temp_file="$(mktemp "$CONFIG_DIR/amd-micro.env.XXXXXX")"
  trap 'rm -f -- "${temp_file:-}"' RETURN
  {
    printf 'READORI_AMD_EXECUTOR_BASE_URL=%s\n' "$(env_quote "$READORI_AMD_EXECUTOR_BASE_URL")"
    printf 'READORI_AMD_EXECUTOR_TOKEN=%s\n' "$(env_quote "$READORI_AMD_EXECUTOR_TOKEN")"
    printf 'READORI_CF_ACCOUNT_ID=%s\n' "$(env_quote "$READORI_CF_ACCOUNT_ID")"
    printf 'READORI_CF_QUEUE_ID=%s\n' "$(env_quote "$READORI_CF_QUEUE_ID")"
    printf 'READORI_CF_QUEUE_API_TOKEN=%s\n' "$(env_quote "$READORI_CF_QUEUE_API_TOKEN")"
    printf 'READORI_AMD_EXECUTOR_ID=%s\n' "$(env_quote "$executor_id")"
    printf 'READORI_VALIDATOR_EXECUTOR_PROFILE=amd-micro\n'
    printf 'READORI_AMD_WORK_DIR=%s\n' "$(env_quote "$WORK_DIR")"
    printf 'READORI_AMD_POLL_SECONDS=%s\n' "$(env_quote "$poll_seconds")"
    printf 'READORI_AMD_MAX_ATTEMPTS=%s\n' "$(env_quote "$max_attempts")"
    printf 'READORI_CF_QUEUE_VISIBILITY_TIMEOUT_MS=43200000\n'
    printf 'PYTHONUNBUFFERED=1\n'
  } > "$temp_file"
  chown root:root "$temp_file"
  chmod 0600 "$temp_file"
  mv -f "$temp_file" "$ENV_FILE"
  trap - RETURN
}

write_systemd_unit() {
  local unit_path="/etc/systemd/system/$SERVICE_NAME.service"
  cat > "$unit_path" <<EOF
[Unit]
Description=Readori AMD Micro source validation executor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -m server.amd_micro_executor --work-dir $WORK_DIR
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$WORK_DIR
MemoryMax=850M
TasksMax=32

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$unit_path"
}

verify_runtime() {
  "$INSTALL_DIR/.venv/bin/python" -c 'import requests, quickjs; import server.amd_micro_executor; print("python runtime: ok")'
  command -v node >/dev/null 2>&1 || die "node is required by JavaScript source rules"
  node --version
  command -v 7z >/dev/null 2>&1 || info "7z is unavailable; RAR/7z source rules will be marked unsupported"
}

start_service() {
  if [ "${READORI_SKIP_SYSTEMD:-0}" = "1" ]; then
    info "READORI_SKIP_SYSTEMD=1; service file installed but not enabled or started"
    return
  fi
  command -v systemctl >/dev/null 2>&1 || die "systemd is unavailable; set READORI_SKIP_SYSTEMD=1 and run the executor manually"
  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME.service"
  if ! systemctl is-active --quiet "$SERVICE_NAME.service"; then
    systemctl --no-pager --full status "$SERVICE_NAME.service" || true
    die "executor service did not stay active"
  fi
}

main() {
  require_root
  [ -f "$SOURCE_ROOT/validator/validate_source_packages.py" ] || die "run this script from the source-validator repository"
  install_os_dependencies
  copy_project
  create_service_account
  create_virtualenv
  write_env_file
  write_systemd_unit
  verify_runtime
  start_service
  info "Installed $SERVICE_NAME at $INSTALL_DIR"
  info "Service status: systemctl status $SERVICE_NAME"
  info "Secret configuration: $ENV_FILE (mode 0600; values are not printed)"
}

main "$@"
