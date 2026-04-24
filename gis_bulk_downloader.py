#!/usr/bin/env python3
"""
NSW ArcGIS Bulk GIS Downloader
Downloads FeatureServer / MapServer layers from an ArcGIS REST endpoint as GeoJSON.

Usage:
    python gis_bulk_downloader.py --list
    python gis_bulk_downloader.py --download-all
    python gis_bulk_downloader.py --download "Planning/LEP_FSR_Map" "Planning/DCP"
    python gis_bulk_downloader.py --search "heritage"

Default base URL targets:
    https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlencode

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("requests is required: pip install requests")

# -- Config -------------------------------------------------------------------

DEFAULT_BASE = "https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/Planning"
DEFAULT_OUT_DIR = "downloads"
PAGE_SIZE = 1000          # records per request (server may cap lower)
REQUEST_TIMEOUT = 60      # seconds
RETRY_TOTAL = 5
RETRY_BACKOFF = 1.5       # seconds


# -- HTTP session with retry --------------------------------------------------

def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": "NSW-GIS-Bulk-Downloader/1.0"})
    return session


SESSION = make_session()


def get_json(url: str, params: dict | None = None) -> dict:
    p = {"f": "json"}
    if params:
        p.update(params)
    resp = SESSION.get(url, params=p, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error at {url}: {data['error']}")
    return data


# -- Service / layer discovery ------------------------------------------------

def list_services(base_url: str) -> list[dict]:
    """Return all services (FeatureServer + MapServer) under the folder URL."""
    data = get_json(base_url)
    services = data.get("services", [])
    for folder in data.get("folders", []):
        folder_url = base_url.rstrip("/") + "/" + folder
        try:
            sub = list_services(folder_url)
            services.extend(sub)
        except Exception as exc:
            print(f"  [warn] Could not read folder {folder}: {exc}", file=sys.stderr)
    return services


def get_layers(service_url: str) -> list[dict]:
    """Return layer metadata list for a FeatureServer or MapServer."""
    try:
        data = get_json(service_url)
    except Exception as exc:
        print(f"  [warn] Could not read service {service_url}: {exc}", file=sys.stderr)
        return []
    layers = data.get("layers", []) + data.get("tables", [])
    return layers


def service_url_for(base_url: str, service: dict) -> str:
    """Build the full service URL from a service record."""
    name = service["name"]          # e.g. "Planning/Heritage_LEP"
    stype = service["type"]         # "FeatureServer" | "MapServer"
    parts = name.split("/")
    folder = base_url.rstrip("/").split("/")[-1]
    if parts[0] == folder:
        parts = parts[1:]
    return base_url.rstrip("/") + "/" + "/".join(parts) + "/" + stype


# -- Downloading --------------------------------------------------------------

def max_record_count(layer_url: str) -> int:
    """Ask the server what its max record count is for this layer."""
    try:
        data = get_json(layer_url)
        return int(data.get("maxRecordCount", PAGE_SIZE))
    except Exception:
        return PAGE_SIZE


def download_layer_geojson(layer_url: str, layer_name: str, out_path: Path) -> int:
    """
    Download all features from a layer using offset pagination.
    Returns total feature count written.
    """
    page = min(PAGE_SIZE, max_record_count(layer_url))
    offset = 0
    all_features: list[dict] = []

    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page,
        }
        try:
            resp = SESSION.get(
                layer_url + "/query",
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            chunk = resp.json()
        except Exception as exc:
            print(f"  [error] page offset={offset}: {exc}", file=sys.stderr)
            break

        if "error" in chunk:
            # Some layers don't support paging - fall back to single request
            print(f"  [warn] paging error, trying single-shot: {chunk['error']}", file=sys.stderr)
            params.pop("resultOffset", None)
            params.pop("resultRecordCount", None)
            resp = SESSION.get(layer_url + "/query", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            chunk = resp.json()
            all_features.extend(chunk.get("features", []))
            break

        features = chunk.get("features", [])
        all_features.extend(features)

        exceeded = chunk.get("exceededTransferLimit", False)
        if not exceeded or len(features) == 0:
            break

        offset += len(features)
        time.sleep(0.2)

    geojson = {
        "type": "FeatureCollection",
        "name": layer_name,
        "features": all_features,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)

    return len(all_features)


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()


# -- Catalogue building -------------------------------------------------------

def build_catalogue(base_url: str) -> list[dict]:
    """
    Walk the folder and return a flat list of downloadable items:
      { service_name, service_type, service_url, layer_id, layer_name, layer_url }
    """
    print(f"Discovering services at: {base_url}")
    services = list_services(base_url)
    print(f"Found {len(services)} service(s). Fetching layer lists...")

    catalogue = []
    for svc in services:
        svc_url = service_url_for(base_url, svc)
        layers = get_layers(svc_url)
        for lyr in layers:
            catalogue.append({
                "service_name": svc["name"],
                "service_type": svc["type"],
                "service_url": svc_url,
                "layer_id": lyr["id"],
                "layer_name": lyr["name"],
                "layer_url": svc_url + "/" + str(lyr["id"]),
            })

    return catalogue


# -- CLI actions --------------------------------------------------------------

def cmd_list(args):
    catalogue = build_catalogue(args.base_url)
    print(f"\n{'#':<4}  {'Service':<50}  {'Layer'}")
    print("-" * 100)
    for i, item in enumerate(catalogue):
        print(f"{i:<4}  {item['service_name']:<50}  {item['layer_name']}")
    print(f"\nTotal: {len(catalogue)} layer(s)")


def cmd_search(args):
    catalogue = build_catalogue(args.base_url)
    term = args.term.lower()
    matches = [
        item for item in catalogue
        if term in item["service_name"].lower() or term in item["layer_name"].lower()
    ]
    if not matches:
        print(f"No layers matching '{args.term}'")
        return
    print(f"\nMatches for '{args.term}':")
    for item in matches:
        print(f"  [{item['service_name']}]  {item['layer_name']}  (id={item['layer_id']})")


def cmd_download_all(args):
    catalogue = build_catalogue(args.base_url)
    out_dir = Path(args.out_dir)
    print(f"\nDownloading {len(catalogue)} layer(s) -> {out_dir}/")

    success, failed = 0, 0
    for i, item in enumerate(catalogue, 1):
        svc_safe = safe_filename(item["service_name"].replace("/", "__"))
        lyr_safe = safe_filename(item["layer_name"])
        out_path = out_dir / svc_safe / f"{lyr_safe}__{item['layer_id']}.geojson"

        if out_path.exists() and not args.overwrite:
            print(f"[{i}/{len(catalogue)}] SKIP (exists): {out_path}")
            success += 1
            continue

        print(f"[{i}/{len(catalogue)}] {item['service_name']} / {item['layer_name']} ...", end=" ", flush=True)
        try:
            count = download_layer_geojson(item["layer_url"], item["layer_name"], out_path)
            print(f"{count} features -> {out_path}")
            success += 1
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            failed += 1

    print(f"\nDone. {success} OK, {failed} failed.")


def cmd_download_services(args):
    catalogue = build_catalogue(args.base_url)
    targets = {t.lower() for t in args.services}
    filtered = [
        item for item in catalogue
        if any(t in item["service_name"].lower() for t in targets)
    ]
    if not filtered:
        print("No matching services found. Use --list to see available services.")
        return

    out_dir = Path(args.out_dir)
    print(f"Downloading {len(filtered)} matching layer(s) -> {out_dir}/")
    for i, item in enumerate(filtered, 1):
        svc_safe = safe_filename(item["service_name"].replace("/", "__"))
        lyr_safe = safe_filename(item["layer_name"])
        out_path = out_dir / svc_safe / f"{lyr_safe}__{item['layer_id']}.geojson"

        if out_path.exists() and not args.overwrite:
            print(f"[{i}/{len(filtered)}] SKIP (exists): {out_path}")
            continue

        print(f"[{i}/{len(filtered)}] {item['service_name']} / {item['layer_name']} ...", end=" ", flush=True)
        try:
            count = download_layer_geojson(item["layer_url"], item["layer_name"], out_path)
            print(f"{count} features -> {out_path}")
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)


def cmd_download_index(args):
    catalogue = build_catalogue(args.base_url)
    out_dir = Path(args.out_dir)
    indices = [int(x) for x in args.indices]
    selected = [catalogue[i] for i in indices if i < len(catalogue)]

    if not selected:
        print("No valid indices. Use --list to see available layers.")
        return

    print(f"Downloading {len(selected)} layer(s) -> {out_dir}/")
    for i, item in enumerate(selected, 1):
        svc_safe = safe_filename(item["service_name"].replace("/", "__"))
        lyr_safe = safe_filename(item["layer_name"])
        out_path = out_dir / svc_safe / f"{lyr_safe}__{item['layer_id']}.geojson"

        if out_path.exists() and not args.overwrite:
            print(f"[{i}/{len(selected)}] SKIP (exists): {out_path}")
            continue

        print(f"[{i}/{len(selected)}] {item['service_name']} / {item['layer_name']} ...", end=" ", flush=True)
        try:
            count = download_layer_geojson(item["layer_url"], item["layer_name"], out_path)
            print(f"{count} features -> {out_path}")
        except Exception as exc:
            print(f"FAILED: {exc}", file=sys.stderr)


# -- Entry point --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk download GIS layers from an ArcGIS REST endpoint as GeoJSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all available layers
  python gis_bulk_downloader.py --list

  # Search for layers by keyword
  python gis_bulk_downloader.py --search heritage

  # Download everything (saves to ./downloads/)
  python gis_bulk_downloader.py --download-all

  # Download specific services by name fragment
  python gis_bulk_downloader.py --download "Heritage" "Flood"

  # Download specific layers by index number (from --list)
  python gis_bulk_downloader.py --download-index 0 5 12

  # Use a different ArcGIS endpoint
  python gis_bulk_downloader.py --base-url https://mapprod3.environment.nsw.gov.au/arcgis/rest/services/ePlanning --list

  # Change output directory and overwrite existing files
  python gis_bulk_downloader.py --download-all --out-dir /data/nsw_gis --overwrite
        """,
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE,
        metavar="URL",
        help=f"ArcGIS REST services folder URL (default: {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        metavar="DIR",
        help=f"Output directory for GeoJSON files (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download files that already exist locally",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List all available services and layers")
    group.add_argument("--search", metavar="TERM", help="Search layer names by keyword")
    group.add_argument("--download-all", action="store_true", help="Download every layer")
    group.add_argument(
        "--download",
        nargs="+",
        metavar="SERVICE",
        help="Download layers from services whose name contains any of these strings",
    )
    group.add_argument(
        "--download-index",
        nargs="+",
        metavar="N",
        dest="indices",
        help="Download layers by their index number shown in --list",
    )

    args = parser.parse_args()

    if args.list:
        cmd_list(args)
    elif args.search:
        args.term = args.search
        cmd_search(args)
    elif args.download_all:
        cmd_download_all(args)
    elif args.download:
        args.services = args.download
        cmd_download_services(args)
    elif args.indices:
        cmd_download_index(args)


if __name__ == "__main__":
    main()