#!/usr/bin/env bash
set -euo pipefail

APP_NAME="geoip-router"
APP_DIR="/opt/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
CONFIG_FILE="/etc/geoip-router"
STATE_DIR="/var/lib/geoip-router"
STATE_FILE="${STATE_DIR}/state.json"

PURGE_CONFIG=0

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "This uninstaller must run as root." >&2
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --purge)
        PURGE_CONFIG=1
        shift
        ;;
      *)
        echo "Unknown argument: $1" >&2
        echo "Usage: $0 [--purge]" >&2
        exit 1
        ;;
    esac
  done
}

stop_and_disable_service() {
  if systemctl list-unit-files | grep -q "^${APP_NAME}\.service"; then
    systemctl stop "${APP_NAME}.service" || true
    systemctl disable "${APP_NAME}.service" || true
  fi
}

remove_routes_from_state() {
  if [[ ! -f "${STATE_FILE}" ]]; then
    echo "No state file found, skipping route cleanup."
    return
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found, cannot cleanup routes from state. Continuing..." >&2
    return
  fi

  python3 <<'PYEOF' || true
import json
import ipaddress
import os
import sys

STATE_FILE = "/var/lib/geoip-router/state.json"

try:
    from pyroute2 import IPRoute
except Exception as e:
    print(f"pyroute2 not available, skipping route cleanup: {e}", file=sys.stderr)
    sys.exit(0)

if not os.path.exists(STATE_FILE):
    sys.exit(0)

try:
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
except Exception as e:
    print(f"Failed to read state file: {e}", file=sys.stderr)
    sys.exit(0)

routes = state.get("applied_routes", [])
if not routes:
    sys.exit(0)

try:
    with IPRoute() as ipr:
        iface_cache = {}

        def link_index(iface):
            if iface in iface_cache:
                return iface_cache[iface]
            idxs = ipr.link_lookup(ifname=iface)
            if not idxs:
                raise RuntimeError(f"interface not found: {iface}")
            iface_cache[iface] = idxs[0]
            return idxs[0]

        for route in routes:
            cidr = route["cidr"]
            iface = route["iface"]
            gateway = route.get("gateway")

            net = ipaddress.ip_network(cidr, strict=False)
            kwargs = {
                "dst": str(net.network_address),
                "mask": net.prefixlen,
                "oif": link_index(iface),
            }
            if gateway:
                kwargs["gateway"] = gateway

            try:
                ipr.route("del", **kwargs)
                print(f"Removed route {cidr} via iface={iface} gateway={gateway}")
            except Exception as e:
                print(f"Skipping route {cidr}: {e}", file=sys.stderr)

except Exception as e:
    print(f"Route cleanup failed: {e}", file=sys.stderr)
PYEOF
}

remove_service_file() {
  if [[ -f "${SERVICE_FILE}" ]]; then
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload || true
  fi
}

remove_app_files() {
  if [[ -d "${APP_DIR}" ]]; then
    rm -rf "${APP_DIR}"
  fi
}

remove_state_files() {
  if [[ -d "${STATE_DIR}" ]]; then
    rm -rf "${STATE_DIR}"
  fi
}

remove_config_if_requested() {
  if [[ "${PURGE_CONFIG}" -eq 1 ]]; then
    if [[ -f "${CONFIG_FILE}" ]]; then
      rm -f "${CONFIG_FILE}"
    fi
  fi
}

show_done() {
  echo
  echo "Uninstall completed."
  echo "Removed:"
  echo "  - ${APP_DIR}"
  echo "  - ${SERVICE_FILE}"
  echo "  - ${STATE_DIR}"
  if [[ "${PURGE_CONFIG}" -eq 1 ]]; then
    echo "  - ${CONFIG_FILE}"
  else
    echo "Kept config:"
    echo "  - ${CONFIG_FILE}"
    echo
    echo "To remove config too, run:"
    echo "  bash uninstall.sh --purge"
  fi
}

main() {
  require_root
  parse_args "$@"
  stop_and_disable_service
  remove_routes_from_state
  remove_service_file
  remove_app_files
  remove_state_files
  remove_config_if_requested
  show_done
}

main "$@"
