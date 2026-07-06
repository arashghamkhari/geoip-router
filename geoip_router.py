#!/usr/bin/env python3
import csv
import io
import ipaddress
import json
import signal
import socket
import sys
import time
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from pyroute2 import IPRoute
from pyroute2.netlink.exceptions import NetlinkError

CONFIG_FILE = Path("/etc/geoip-router")
STATE_DIR = Path("/var/lib/geoip-router")

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


def download_zip(dest: Path) -> None:
    with requests.get(IP2LOCATION_URL, stream=True, timeout=120) as response:
        response.raise_for_status()
        with dest.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


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


def load_exported_cidrs(export_dir: Path, config: Config) -> Dict[str, Set[str]]:
    country_cidrs: Dict[str, Set[str]] = {}
    for country in sorted(config.countries.keys()):
        cidr_file = export_dir / f"{country}.cidr"
        if not cidr_file.exists():
            continue

        cidrs: Set[str] = set()
        with cidr_file.open("r", encoding="utf-8") as f:
            for line in f:
                cidr = line.strip()
                if cidr:
                    cidrs.add(cidr)

        if cidrs:
            country_cidrs[country] = cidrs

    return country_cidrs


def load_state() -> Dict:
    if not STATE_FILE.exists():
        return {"last_md5": None}
    with STATE_FILE.open("r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        state = {}
    return {"last_md5": state.get("last_md5")}


def save_state(state: Dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"last_md5": state.get("last_md5")}, f, indent=2, sort_keys=True)
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

        to_add = desired_routes - {
            DesiredRoute(cidr=cidr, iface=iface, gateway=gateway)
            for cidr, iface, gateway in existing_snapshot
        }

        existing_routes = {
            DesiredRoute(cidr=cidr, iface=iface, gateway=gateway)
            for cidr, iface, gateway in existing_snapshot
        }

        to_remove = existing_routes - desired_routes

        for route in to_add:
            add_route(ipr, route, link_indexes)

        for route in to_remove:
            delete_route(ipr, route, link_indexes)


def cleanup_routes() -> None:
    try:
        config = load_config()
    except Exception:
        return

    country_cidrs = load_exported_cidrs(EXPORT_DIR, config)
    desired_routes = build_desired_routes(country_cidrs, config)

    if not desired_routes:
        return

    with IPRoute() as ipr:
        link_indexes = get_link_indexes(ipr, desired_routes)
        for route in desired_routes:
            delete_route(ipr, route, link_indexes)


def main() -> None:
    ensure_dirs()
    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while not stop:
        try:
            config = load_config()
            state = load_state()
            remote_md5 = get_md5_from_remote()

            if state.get("last_md5") != remote_md5:
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp_zip = Path(tmpdir) / "IP2LOCATION-LITE-DB1.CSV.ZIP"
                    download_zip(tmp_zip)
                    extract_country_cidrs(tmp_zip, EXPORT_DIR)
                state["last_md5"] = remote_md5
                save_state(state)

            country_cidrs = load_exported_cidrs(EXPORT_DIR, config)
            if not country_cidrs:
                raise ValueError("CIDR data is not available")
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
