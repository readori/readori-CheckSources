#!/usr/bin/env bash
# Install the Readori source-validator server on a small Ubuntu/Debian VM.
#
# This script intentionally runs the FastAPI validator directly.  It no longer
# starts the legacy D1 lease executor, so task state, inputs, progress and
# results stay on this server's SQLite/filesystem instead of consuming D1/R2.
#
# The script is intentionally non-interactive: provide Cloudflare values through
# environment variables (or source an untracked env file) so credentials never
# appear in shell history or in the generated unit file's command line.
# Required variables:
#   READORI_VALIDATOR_API_KEY     token held by the Worker proxy (or clients)
#
# Backwards compatibility: READORI_AMD_EXECUTOR_TOKEN is accepted as the API
# key when READORI_VALIDATOR_API_KEY is not set.  READORI_AMD_EXECUTOR_BASE_URL
# is ignored and is no longer required.
#
# Optional variables:
#   READORI_SOURCE_REPOSITORY (default readori/readori-CheckSources)
#   READORI_SOURCE_REF (default main; used when only server/ was checked out)
#   READORI_SOURCE_TOKEN (optional GitHub token for a private source repository)
#   READORI_INSTALL_DIR (default /opt/readori-validator)
#   READORI_AMD_WORK_DIR (default /var/lib/readori-validator)
#   READORI_VALIDATOR_PORT (default 8787)
#   READORI_VALIDATOR_HOST (default 0.0.0.0)
#   READORI_SKIP_APT=1 skips apt package installation (use only on prepared hosts)
#   READORI_SKIP_SYSTEMD=1 installs files and the virtualenv without starting it

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
SOURCE_REPOSITORY="${READORI_SOURCE_REPOSITORY:-readori/readori-CheckSources}"
SOURCE_REF="${READORI_SOURCE_REF:-main}"
SOURCE_TOKEN="${READORI_SOURCE_TOKEN:-}"
INSTALL_DIR="${READORI_INSTALL_DIR:-/opt/readori-validator}"
CONFIG_DIR="/etc/readori-validator"
ENV_FILE="$CONFIG_DIR/amd-micro.env"
SERVICE_NAME="readori-source-validator"
WORK_DIR="${READORI_AMD_WORK_DIR:-/var/lib/readori-validator}"
VALIDATOR_HOST="${READORI_VALIDATOR_HOST:-0.0.0.0}"
VALIDATOR_PORT="${READORI_VALIDATOR_PORT:-8787}"
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

validate_source_reference() {
  case "$SOURCE_REPOSITORY" in
    */*) ;;
    *) die "READORI_SOURCE_REPOSITORY must use owner/repository form" ;;
  esac
  case "$SOURCE_REPOSITORY" in
    ''|/*|*/|*//*|*..*|*[^A-Za-z0-9._/-]*) die "READORI_SOURCE_REPOSITORY contains unsafe path characters" ;;
  esac
  case "$SOURCE_REF" in
    ''|/*|*/|*//*|*..*|*[^A-Za-z0-9._/-]*) die "READORI_SOURCE_REF contains unsafe path characters" ;;
  esac
  validate_single_line READORI_SOURCE_TOKEN "$SOURCE_TOKEN"
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

bootstrap_minimal_runtime_files() {
  [ -f "$SOURCE_ROOT/validator/__init__.py" ] && \
    [ -f "$SOURCE_ROOT/validator/validate_source_packages.py" ] && \
    [ -f "$SOURCE_ROOT/requirements-validate-sources.txt" ] && \
    [ -f "$SOURCE_ROOT/requirements.txt" ] && return

  command -v curl >/dev/null 2>&1 || die "curl is required to bootstrap validator files"
  validate_source_reference
  local base_url="https://raw.githubusercontent.com/$SOURCE_REPOSITORY/$SOURCE_REF"
  local bootstrap_dir
  bootstrap_dir="$(mktemp -d "$SOURCE_ROOT/.readori-source-bootstrap.XXXXXX")"
  local -a curl_auth=()
  if [ -n "$SOURCE_TOKEN" ]; then
    curl_auth=(-H "Authorization: Bearer $SOURCE_TOKEN")
  fi

  fetch_runtime_file() {
    local relative_path="$1"
    local destination="$bootstrap_dir/$relative_path"
    mkdir -p "$(dirname -- "$destination")"
    if ! curl --fail --silent --show-error --location --retry 3 --retry-delay 1 \
      "${curl_auth[@]}" "$base_url/$relative_path" --output "$destination"; then
      rm -rf -- "$bootstrap_dir"
      die "failed to download $relative_path from $SOURCE_REPOSITORY@$SOURCE_REF"
    fi
    [ -s "$destination" ] || {
      rm -rf -- "$bootstrap_dir"
      die "downloaded file is empty: $relative_path"
    }
  }

  info "Only server/ was checked out; bootstrapping validator runtime files from $SOURCE_REPOSITORY@$SOURCE_REF"
  fetch_runtime_file validator/__init__.py
  fetch_runtime_file validator/validate_source_packages.py
  fetch_runtime_file requirements-validate-sources.txt
  fetch_runtime_file requirements.txt
  mkdir -p "$SOURCE_ROOT/validator"
  cp -a "$bootstrap_dir/validator/." "$SOURCE_ROOT/validator/"
  cp -f "$bootstrap_dir/requirements-validate-sources.txt" "$SOURCE_ROOT/requirements-validate-sources.txt"
  cp -f "$bootstrap_dir/requirements.txt" "$SOURCE_ROOT/requirements.txt"
  rm -rf -- "$bootstrap_dir"
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
  # Remove the legacy D1/Queue executor when upgrading an existing install.
  # Leaving it on disk is misleading and can lead to an accidental manual
  # launch even though the systemd unit now runs the local API server.
  if [ -f "$INSTALL_DIR/server/amd_micro_executor.py" ]; then
    rm -f -- "$INSTALL_DIR/server/amd_micro_executor.py"
    info "Removed legacy server/amd_micro_executor.py from $INSTALL_DIR"
  fi
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
    "$INSTALL_DIR/server/source_validator_server.py" \
    "$INSTALL_DIR/validator/validate_source_packages.py"
}

existing_api_key_line() {
  [ -r "$ENV_FILE" ] || return 1
  local line value
  line="$(grep -m1 '^READORI_VALIDATOR_API_KEY=' "$ENV_FILE" || true)"
  case "$line" in
    READORI_VALIDATOR_API_KEY=*) ;;
    *) return 1 ;;
  esac
  value="${line#READORI_VALIDATOR_API_KEY=}"
  case "$value" in
    ''|'""') return 1 ;;
  esac
  printf '%s\n' "$line"
}

write_env_file() {
  if [ -z "${READORI_VALIDATOR_API_KEY:-}" ] && [ -n "${READORI_AMD_EXECUTOR_TOKEN:-}" ]; then
    READORI_VALIDATOR_API_KEY="$READORI_AMD_EXECUTOR_TOKEN"
    export READORI_VALIDATOR_API_KEY
  fi
  local existing_key_line=""
  if [ -z "${READORI_VALIDATOR_API_KEY:-}" ]; then
    existing_key_line="$(existing_api_key_line || true)"
  fi
  if [ -z "${READORI_VALIDATOR_API_KEY:-}" ] && [ -z "$existing_key_line" ]; then
    require_value READORI_VALIDATOR_API_KEY
  fi

  local validator_host="$VALIDATOR_HOST"
  local validator_port="$VALIDATOR_PORT"
  validate_single_line READORI_VALIDATOR_HOST "$validator_host"
  validate_single_line READORI_VALIDATOR_PORT "$validator_port"
  case "$validator_host" in
    ''|*[!A-Za-z0-9._:-]*) die "READORI_VALIDATOR_HOST contains unsafe characters" ;;
  esac
  case "$validator_port" in
    ''|*[!0-9]*) die "READORI_VALIDATOR_PORT must be an integer" ;;
  esac
  [ "$validator_port" -ge 1 ] && [ "$validator_port" -le 65535 ] || die "READORI_VALIDATOR_PORT must be between 1 and 65535"

  umask 077
  local temp_file
  temp_file="$(mktemp "$CONFIG_DIR/amd-micro.env.XXXXXX")"
  trap 'rm -f -- "${temp_file:-}"' RETURN
  {
    if [ -n "${READORI_VALIDATOR_API_KEY:-}" ]; then
      printf 'READORI_VALIDATOR_API_KEY=%s\n' "$(env_quote "$READORI_VALIDATOR_API_KEY")"
    else
      # Preserve an existing quoted EnvironmentFile value without evaluating
      # it as shell code. A new exported key still takes precedence above.
      printf '%s\n' "$existing_key_line"
    fi
    printf 'READORI_VALIDATOR_HOST=%s\n' "$(env_quote "$validator_host")"
    printf 'READORI_VALIDATOR_PORT=%s\n' "$(env_quote "$validator_port")"
    printf 'READORI_VALIDATOR_DB=%s\n' "$(env_quote "$WORK_DIR/validator.sqlite3")"
    printf 'READORI_VALIDATOR_EXECUTOR_PROFILE=amd-micro\n'
    printf 'READORI_AMD_MICRO=1\n'
    printf 'READORI_VALIDATOR_MAX_WORKERS=1\n'
    printf 'READORI_VALIDATOR_MAX_JOBS=1\n'
    printf 'READORI_VALIDATOR_DOMAIN_CONCURRENCY=1\n'
    printf 'READORI_VALIDATOR_MAX_SOURCES=20000\n'
    printf 'READORI_VALIDATOR_MAX_UPLOAD_BYTES=67108864\n'
    printf 'READORI_AMD_WORK_DIR=%s\n' "$(env_quote "$WORK_DIR")"
    printf 'PYTHONUNBUFFERED=1\n'
  } > "$temp_file"
  chown root:root "$temp_file"
  chmod 0600 "$temp_file"
  mv -f "$temp_file" "$ENV_FILE"
  trap - RETURN
}

validate_runtime_configuration() {
  # Fail before apt/pip work so a missing secret cannot waste time or leave a
  # partially prepared host after a disconnected SSH session.
  if [ -z "${READORI_VALIDATOR_API_KEY:-}" ] && [ -n "${READORI_AMD_EXECUTOR_TOKEN:-}" ]; then
    READORI_VALIDATOR_API_KEY="$READORI_AMD_EXECUTOR_TOKEN"
    export READORI_VALIDATOR_API_KEY
  fi
  if [ -z "${READORI_VALIDATOR_API_KEY:-}" ] && existing_api_key_line >/dev/null 2>&1; then
    info "READORI_VALIDATOR_API_KEY not exported; reusing the existing $ENV_FILE value"
    return
  fi
  require_value READORI_VALIDATOR_API_KEY
}

write_systemd_unit() {
  local unit_path="/etc/systemd/system/$SERVICE_NAME.service"
  cat > "$unit_path" <<EOF
[Unit]
Description=Readori AMD Micro source validation server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python -m server.source_validator_server --host $VALIDATOR_HOST --port $VALIDATOR_PORT
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
  "$INSTALL_DIR/.venv/bin/python" -c 'import requests, quickjs, fastapi; import server.source_validator_server; print("python runtime: ok")'
  command -v node >/dev/null 2>&1 || die "node is required by JavaScript source rules"
  node --version
  command -v 7z >/dev/null 2>&1 || info "7z is unavailable; RAR/7z source rules will be marked unsupported"
}

start_service() {
  if [ "${READORI_SKIP_SYSTEMD:-0}" = "1" ]; then
    info "READORI_SKIP_SYSTEMD=1; service file installed but not enabled or started"
    return
  fi
  command -v systemctl >/dev/null 2>&1 || die "systemd is unavailable; set READORI_SKIP_SYSTEMD=1 and run the validator server manually"
  systemctl daemon-reload
  # Restart on every install so upgrades cannot leave an old process running
  # with stale code, environment, or port settings.
  systemctl enable "$SERVICE_NAME.service"
  systemctl restart "$SERVICE_NAME.service"
  if ! systemctl is-active --quiet "$SERVICE_NAME.service"; then
    systemctl --no-pager --full status "$SERVICE_NAME.service" || true
    die "validator service did not stay active"
  fi
  if command -v curl >/dev/null 2>&1; then
    # ``systemctl enable --now`` returns before uvicorn has finished importing
    # the validation core.  Poll instead of treating that normal warm-up as a
    # failed installation (especially on 1 GB AMD Micro instances).
    local health_url="http://127.0.0.1:${VALIDATOR_PORT}/healthz"
    local health_attempt
    for health_attempt in {1..30}; do
      if curl --fail --silent --show-error --connect-timeout 1 --max-time 2 \
        "$health_url" >/dev/null; then
        return
      fi
      if ! systemctl is-active --quiet "$SERVICE_NAME.service"; then
        break
      fi
      sleep 1
    done
    systemctl --no-pager --full status "$SERVICE_NAME.service" || true
    journalctl -u "$SERVICE_NAME.service" -n 80 --no-pager || true
    die "validator server health check failed after waiting for $health_url"
  fi
}

main() {
  require_root
  [ -f "$SOURCE_ROOT/server/source_validator_server.py" ] || die "server/source_validator_server.py is missing; sparse checkout must include server/"
  validate_runtime_configuration
  install_os_dependencies
  bootstrap_minimal_runtime_files
  copy_project
  create_service_account
  create_virtualenv
  write_env_file
  write_systemd_unit
  verify_runtime
  start_service
  info "Installed $SERVICE_NAME at $INSTALL_DIR (server-only mode; D1/R2 not used)"
  info "Service status: systemctl status $SERVICE_NAME"
  info "Secret configuration: $ENV_FILE (mode 0600; values are not printed)"
}

main "$@"
