# Geoip Router

A lightweight daemon that routes traffic per-country using GeoIP CIDR data from IP2Location and the Linux routing table.

## Features

- Automatically downloads the latest IP2Location CIDR dataset.
- Summarizes IP ranges per country.
- Applies per-country routes defined.
- Cleans up routes on shutdown and supports optional purge of configuration via uninstall.

## Prerequisites

- Linux system with `systemd`, iptables/iproute2, and root privileges.
- Python 3.8+ with `requests` and `pyroute2`.

## Installation

```bash
curl -LsSf https://raw.githubusercontent.com/arashghamkhari/geoip-router/refs/heads/main/install.sh | sh
```

After installation the service is enabled and started automatically.

## Configuration

Edit `/etc/geoip-router` to map country codes to network interfaces and optional gateways:

```
# COUNTRY=interface[:gateway]
IR=eth0:192.168.1.1
FR=eth5:10.10.10.1
DE=eth2
```

Reload or restart the `geoip-router` service after changes:
```bash
systemctl restart geoip-router.service
```

## Uninstallation

To remove the application while keeping your configuration:
```bash
bash uninstall.sh
```

To remove everything, including `/etc/geoip-router`:
```bash
bash uninstall.sh --purge
```

If route cleanup fails on shutdown, rerun `bash uninstall.sh` after ensuring `pyroute2` is available.