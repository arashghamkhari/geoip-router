#!/usr/bin/env bash
set -euo pipefail

APP_NAME="geoip-router"
APP_DIR="/opt/${APP_NAME}"
VENV_DIR="${APP_DIR}/.venv"
RELEASE_API="https://api.github.com/repos/arashghamkhari/geoip-router/releases/latest"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
CONFIG_FILE="/etc/geoip-router"

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "This installer must run as root." >&2
    exit 1
  fi
}

detect_pkg_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
    return
  fi
  if command -v dnf >/dev/null 2>&1; then
    echo "dnf"
    return
  fi
  if command -v yum >/dev/null 2>&1; then
    echo "yum"
    return
  fi
  if command -v pacman >/dev/null 2>&1; then
    echo "pacman"
    return
  fi
  echo "unsupported"
}

install_packages() {
  local pm
  pm="$(detect_pkg_manager)"

  case "${pm}" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y python3 python3-pip python3-venv curl ca-certificates unzip
      ;;
    dnf)
      dnf install -y python3 python3-pip python3-venv curl ca-certificates unzip
      ;;
    yum)
      yum install -y python3 python3-pip python3-venv curl ca-certificates unzip
      ;;
    pacman)
      pacman -Sy --noconfirm python python-pip curl ca-certificates unzip
      ;;
    *)
      echo "Unsupported package manager. Please install python3, pip, venv, curl, ca-certificates, and unzip manually." >&2
      exit 1
      ;;
  esac
}

get_latest_release_tarball_url() {
  local release_json tarball_url
  release_json="$(curl -fsSL "${RELEASE_API}")"
  tarball_url="$(printf '%s' "${release_json}" | python3 - <<'PY'
import json
import sys

data = json.load(sys.stdin)
print(data.get("tarball_url") or "")
PY
)"
  if [[ -z "${tarball_url}" ]]; then
    echo "Unable to determine latest release tarball URL" >&2
    exit 1
  fi
  printf '%s' "${tarball_url}"
}

install_app_files() {
  local tmpdir release_archive extracted_dir tarball_url
  rm -rf "${APP_DIR}"
  tarball_url="$(get_latest_release_tarball_url)"
  tmpdir="$(mktemp -d)"
  release_archive="${tmpdir}/release.tar.gz"

  echo "Downloading latest release from GitHub..."
  curl -fsSL "${tarball_url}" -o "${release_archive}"

  tar -xzf "${release_archive}" -C "${tmpdir}"

  extracted_dir="$(find "${tmpdir}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "${extracted_dir}" ]]; then
    echo "Failed to extract release archive" >&2
    exit 1
  fi

  mkdir -p "${APP_DIR}"
  (
    cd "${extracted_dir}"
    cp -a . "${APP_DIR}/"
  )

  chmod 0755 "${APP_DIR}/geoip_router.py"

  rm -rf "${tmpdir}"
}

install_python_deps() {
  echo "Installing pipenv via pip..."
  python3 -m pip install --upgrade pip
  python3 -m pip install pipenv

  echo "Creating Pipfile and installing dependencies via pipenv..."
  cd "${APP_DIR}"

  export PIPENV_VENV_IN_PROJECT=1

  pipenv install requests pyroute2
}

install_config() {
  if [[ ! -f "${CONFIG_FILE}" ]]; then
    cat > "${CONFIG_FILE}" <<'CFGEOF'
# COUNTRY=interface:gateway
# Example:
# IR=eth0:192.168.1.1
# FR=eth1:10.10.10.1
# DE=eth2

IR=eth0:192.168.1.1
FR=eth5:10.10.10.1
CFGEOF
    chmod 0644 "${CONFIG_FILE}"
  fi
}

install_service() {
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=GeoIP Router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/geoip_router.py
Restart=always
RestartSec=5
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

  chmod 0644 "${SERVICE_FILE}"
  systemctl daemon-reload
  systemctl enable --now "${APP_NAME}.service"
}

show_status() {
  echo
  echo "Installed successfully."
  echo "Config file: ${CONFIG_FILE}"
  echo "Service file: ${SERVICE_FILE}"
  echo
  systemctl --no-pager --full status "${APP_NAME}.service" || true
}

main() {
  require_root
  install_packages
  install_app_files
  install_python_deps
  install_config
  install_service
  show_status
}

main "$@"
