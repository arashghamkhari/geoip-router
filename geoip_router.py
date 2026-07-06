#!/usr/bin/env python3
import csv
import hashlib
import io
import ipaddress
import json
import signal
import socket
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from pyroute2 import IPRoute
from pyroute2.netlink.exceptions import NetlinkError

CONFIG_FILE = Path("/etc/geoip-router")
STATE_DIR = Path("/var/lib/geoip-router")
DOWNLOAD_DIR = STATE_DIR / "downloads"  # You can use tmp directory for this purpose. Whe must remove zip file after converted to cidr. AI!
EXPORT_DIR = STATE_DIR / "countries"
STATE_FILE = STATE_DIR / "state.json"

IP2LOCATION_URL = "https://download.ip2location.com/lite/IP2LOCATION-LITE-DB1.CSV.ZIP"
IP2LOCATION_MD5_URL = "https://download.ip2location.com/lite/IP2LOCATION-LITE-DB1.CSV.ZIP.md5"

CHECK_INTERVAL_SECONDS = 300


@dataclass(frozen=True)
class CountryRouteConfig:
    iface: str
    gateway: Optional[str] = None


@dataclass
class Config:
    countries: Dict[str, CountryRouteConfig]


@dataclass(frozen=True)
class DesiredRoute:
    cidr: str
    iface: str
    gateway: Optional[str] = None


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"config file not found: {CONFIG_FILE}")

    countries: Dict[str, CountryRouteConfig] = {}

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                raise ValueError(f"invalid config line {line_no}: missing '='")

            country, value = line.split("=", 1)
            country = country.strip().upper()
            value = value.strip()

            if len(country) != 2:
                raise ValueError(f"invalid country code at line {line_no}: {country}")

            if not value:
                raise ValueError(f"empty route target at line {line_no}")

            if ":" in value:
                iface, gateway = value.split(":", 1)
                iface = iface.strip()
                gateway = gateway.strip()

                if not iface:
                    raise ValueError(f"empty interface at line {line_no}")

                if not gateway:
                    raise ValueError(f"empty gateway at line {line_no}")

                ipaddress.ip_address(gateway)
            else:
                iface = value.strip()
                gateway = None

                if not iface:
                    raise ValueError(f"empty interface at line {line_no}")

            countries[country] = CountryRouteConfig(
                iface=iface,
                gateway=gateway,
            )

    if not countries:
        raise ValueError("no valid country route mappings found in config")

    return Config(countries=countries)


def get_md5_from_remote() -> str:
    response = requests.get(IP2LOCATION_MD5_URL, timeout=30)
    response.raise_for_status()
    return response.text.strip().split()[0]


# Do Not need to calculate md5 hash of downloaded file. just store md5 downloaded file that downloaded before by get_md5_from_remote and check new md5 file with it. AI!
def file_md5(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def download_zip(dest: Path) -> None:
    with requests.get(IP2LOCATION_URL, stream=True, timeout=120) as response:
        response.raise_for_status()
        with dest.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


# you should compare stored md5 hash of previous downloaded file with new md4 downloaded with get_md5_from_remote. Do not need to store zip file forever. AI!
def should_update(local_zip: Path, remote_md5: str) -> bool:
    if not local_zip.exists():
        return True
    return file_md5(local_zip) != remote_md5


def extract_country_cidrs(zip_path: Path, export_dir: Path) -> Dict[str, Set[str]]:
    country_ranges: Dict[str, List[Tuple[int, int]]] = {}

    with zipfile.ZipFile(zip_path, "r") as zf:
        csv_name = next((name for name in zf.namelist() if name.lower().endswith(".csv")), None)
        if not csv_name:
            raise ValueError("no CSV found inside ZIP")

        with zf.open(csv_name, "r") as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            reader = csv.reader(text)

            for row in reader:
                if len(row) < 4:
                    continue

                try:
                    ip_from = int(row[0])
                    ip_to = int(row[1])
                    country = row[2].strip().upper()
                except Exception:
                    continue

                if not country or country == "-":
                    continue

                country_ranges.setdefault(country, []).append((ip_from, ip_to))

    country_cidrs: Dict[str, Set[str]] = {}

    for country, ranges in country_ranges.items():
        cidrs: Set[str] = set()

        for start, end in ranges:
            start_ip = ipaddress.ip_address(start)
            end_ip = ipaddress.ip_address(end)
            for net in ipaddress.summarize_address_range(start_ip, end_ip):
                cidrs.add(str(net))

        country_cidrs[country] = cidrs

        out_file = export_dir / f"{country}.cidr"
        with out_file.open("w", encoding="utf-8") as f:
            for cidr in sorted(
                    cidrs,
                    key=lambda x: (
                            ipaddress.ip_network(x).version,
                            int(ipaddress.ip_network(x).network_address),
                            ipaddress.ip_network(x).prefixlen,
                    ),
            ):
                f.write(cidr + "\n")

    return country_cidrs


def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {"applied_routes": []}
    with STATE_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


def build_desired_routes(country_cidrs: Dict[str, Set[str]], config: Config) -> Set[DesiredRoute]:
    desired: Set[DesiredRoute] = set()

    for country, route_cfg in config.countries.items():
        cidrs = country_cidrs.get(country, set())
        for cidr in cidrs:
            desired.add(
                DesiredRoute(
                    cidr=cidr,
                    iface=route_cfg.iface,
                    gateway=route_cfg.gateway,
                )
            )

    return desired


def get_link_indexes(ipr: IPRoute, desired_routes: Set[DesiredRoute]) -> Dict[str, int]:
    iface_names = {route.iface for route in desired_routes}
    links: Dict[str, int] = {}

    for iface in iface_names:
        idxs = ipr.link_lookup(ifname=iface)
        if not idxs:
            raise ValueError(f"interface not found: {iface}")
        links[iface] = idxs[0]

    return links


def route_key_from_kernel(route: dict, index_to_name: Dict[int, str]) -> Optional[Tuple[str, str, Optional[str]]]:
    attrs = dict(route.get("attrs", []))
    dst = attrs.get("RTA_DST")
    oif = route.get("oif")
    gateway = attrs.get("RTA_GATEWAY")

    if dst is None or oif is None:
        return None

    prefixlen = route.get("dst_len")
    if prefixlen is None:
        return None

    iface = index_to_name.get(oif)
    if not iface:
        return None

    cidr = f"{dst}/{prefixlen}"
    return (cidr, iface, gateway)


def get_existing_routes_snapshot(ipr: IPRoute, iface_names: Set[str]) -> Set[Tuple[str, str, Optional[str]]]:
    index_to_name: Dict[int, str] = {}
    for iface in iface_names:
        idxs = ipr.link_lookup(ifname=iface)
        if idxs:
            index_to_name[idxs[0]] = iface

    existing: Set[Tuple[str, str, Optional[str]]] = set()

    for family in (socket.AF_INET,):
        for route in ipr.get_routes(family=family):
            key = route_key_from_kernel(route, index_to_name)
            if key and key[1] in iface_names:
                existing.add(key)

    return existing


def add_route(ipr: IPRoute, route: DesiredRoute, link_indexes: Dict[str, int]) -> None:
    network = ipaddress.ip_network(route.cidr, strict=False)
    kwargs = {
        "dst": str(network.network_address),
        "mask": network.prefixlen,
        "oif": link_indexes[route.iface],
    }

    if route.gateway:
        kwargs["gateway"] = route.gateway

    try:
        ipr.route("replace", **kwargs)
    except NetlinkError as exc:
        raise RuntimeError(
            f"failed to add/replace route {route.cidr} via iface={route.iface} gateway={route.gateway}: {exc}"
        ) from exc


def delete_route(ipr: IPRoute, route: DesiredRoute, link_indexes: Dict[str, int]) -> None:
    network = ipaddress.ip_network(route.cidr, strict=False)
    kwargs = {
        "dst": str(network.network_address),
        "mask": network.prefixlen,
        "oif": link_indexes[route.iface],
    }

    if route.gateway:
        kwargs["gateway"] = route.gateway

    try:
        ipr.route("del", **kwargs)
    except NetlinkError:
        pass


def sync_routes(country_cidrs: Dict[str, Set[str]], config: Config) -> None:
    desired_routes = build_desired_routes(country_cidrs, config)
    iface_names = {route.iface for route in desired_routes} or {cfg.iface for cfg in config.countries.values()}

    with IPRoute() as ipr:
        link_indexes = get_link_indexes(ipr, desired_routes if desired_routes else set(
            DesiredRoute(cidr="0.0.0.0/32", iface=iface) for iface in iface_names
        ))

        existing_snapshot = get_existing_routes_snapshot(ipr, iface_names)
        desired_snapshot = {(r.cidr, r.iface, r.gateway) for r in desired_routes}

        to_add = desired_routes - {
            DesiredRoute(cidr=cidr, iface=iface, gateway=gateway)
            for cidr, iface, gateway in existing_snapshot
        }

        state = load_state()
        previous_applied = {
            DesiredRoute(
                cidr=item["cidr"],
                iface=item["iface"],
                gateway=item.get("gateway"),
            )
            for item in state.get("applied_routes", [])
        }

        to_remove = previous_applied - desired_routes

        for route in to_add:
            add_route(ipr, route, link_indexes)

        for route in to_remove:
            delete_route(ipr, route, link_indexes)

        state["applied_routes"] = [
            {"cidr": r.cidr, "iface": r.iface, "gateway": r.gateway}
            for r in sorted(desired_routes, key=lambda x: (x.iface, x.gateway or "", x.cidr))
        ]
        save_state(state)


def cleanup_routes() -> None:
    state = load_state()
    applied_routes = [
        DesiredRoute(
            cidr=item["cidr"],
            iface=item["iface"],
            gateway=item.get("gateway"),
        )
        for item in state.get("applied_routes", [])
    ]

    if not applied_routes:
        return

    with IPRoute() as ipr:
        link_indexes = get_link_indexes(ipr, set(applied_routes))
        for route in applied_routes:
            delete_route(ipr, route, link_indexes)

    state["applied_routes"] = []
    save_state(state)


def main() -> None:
    ensure_dirs()
    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    local_zip = DOWNLOAD_DIR / "IP2LOCATION-LITE-DB1.CSV.ZIP"

    while not stop:
        try:
            config = load_config()
            remote_md5 = get_md5_from_remote()

            if should_update(local_zip, remote_md5):
                tmp_zip = DOWNLOAD_DIR / "IP2LOCATION-LITE-DB1.CSV.ZIP.tmp"
                download_zip(tmp_zip)

                if file_md5(tmp_zip) != remote_md5:
                    raise ValueError("downloaded ZIP md5 mismatch")

                tmp_zip.replace(local_zip)

            country_cidrs = extract_country_cidrs(local_zip, EXPORT_DIR)
            sync_routes(country_cidrs, config)

        except Exception as exc:
            print(f"[geoip-router] error: {exc}", file=sys.stderr)

        for _ in range(CHECK_INTERVAL_SECONDS):
            if stop:
                break
            time.sleep(1)

    cleanup_routes()


if __name__ == "__main__":
    main()
