from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from danfoss_850a import Danfoss850AClient, DanfossApiError, DanfossPointSpec


DEFAULT_NODES = "1-8,81,82"


def load_config(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return payload


def parse_nodes(text: str) -> List[int]:
    nodes: List[int] = []
    for part in (text or "").split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            left, right = item.split("-", 1)
            start = int(left.strip())
            stop = int(right.strip())
            step = 1 if stop >= start else -1
            nodes.extend(range(start, stop + step, step))
        else:
            nodes.append(int(item))
    seen = set()
    ordered = []
    for node in nodes:
        if node not in seen:
            seen.add(node)
            ordered.append(node)
    return ordered


def cfg_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def tcp_probe(endpoint_url: str, timeout_sec: float) -> str:
    parsed = urlparse(endpoint_url)
    host = parsed.hostname
    if not host:
        return "SKIP no hostname parsed from endpoint"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return f"OK TCP {host}:{port} reachable in {elapsed_ms} ms"
    except OSError as exc:
        return f"FAIL TCP {host}:{port} not reachable: {exc}"


def build_client(args: argparse.Namespace, cfg: Dict[str, Any]) -> Danfoss850AClient:
    endpoint = args.endpoint or cfg.get("danfoss_endpoint_url") or cfg.get("danfoss_xml_url")
    if not endpoint:
        raise ValueError("Missing endpoint. Use --endpoint or provide danfoss_endpoint_url in config.")

    username = args.username
    if username is None:
        username = str(cfg.get("danfoss_username") or os.getenv("DANFOSS_USERNAME", ""))
    password = args.password
    if password is None:
        password = str(cfg.get("danfoss_password") or os.getenv("DANFOSS_PASSWORD", ""))
    auth_mode = args.auth_mode or str(cfg.get("danfoss_auth_mode", "basic_header"))
    min_interval_sec = (
        args.min_interval_sec
        if args.min_interval_sec is not None
        else float(cfg.get("danfoss_min_interval_sec", 0.5) or 0)
    )

    return Danfoss850AClient(
        endpoint_url=str(endpoint),
        username=username,
        password=password,
        auth_mode=auth_mode,
        timeout_sec=int(args.timeout_sec or cfg.get("http_timeout_sec", 5)),
        units=str(args.units or cfg.get("danfoss_units", "s")),
        lang=str(args.lang or cfg.get("danfoss_lang", "e")),
        min_interval_sec=min_interval_sec,
        verify_tls=cfg_bool(args.verify_tls if args.verify_tls is not None else cfg.get("danfoss_verify_tls"), True),
        ca_cert=str(args.ca_cert if args.ca_cert is not None else cfg.get("danfoss_ca_cert", "")),
        content_type=str(args.content_type or cfg.get("danfoss_content_type", "text/xml")),
    )


def configured_points(cfg: Dict[str, Any]) -> List[DanfossPointSpec]:
    raw_points = cfg.get("danfoss_points", [])
    if not isinstance(raw_points, list):
        return []
    specs: List[DanfossPointSpec] = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            continue
        try:
            specs.append(DanfossPointSpec.from_mapping(raw))
        except Exception as exc:
            print(f"[READ_VAL][WARN] Ignoring invalid point config {raw!r}: {exc}")
    return specs


def print_title(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def print_kv(key: str, value: Any) -> None:
    print(f"{key:<24} {value}")


def summarize_devices(devices: Iterable[Dict[str, Any]]) -> None:
    devices = list(devices)
    if not devices:
        print("[DEVICES][WARN] No <device> entries returned.")
        return
    for index, item in enumerate(devices, 1):
        node = item.get("node") or item.get("addr") or item.get("address") or item.get("Address") or ""
        nodetype = item.get("nodetype") or item.get("type") or ""
        name = item.get("name") or item.get("unit_name") or item.get("device_id") or item.get("value") or ""
        online = item.get("online") or item.get("status") or item.get("stat") or ""
        model = item.get("model") or item.get("mod") or item.get("device_id") or ""
        print(f"{index:>3}. node={node!s:<4} nodetype={nodetype!s:<4} online={online!s:<6} model={model!s:<16} name={name}")


def summarize_history_cfg(node: int, configs: List[Dict[str, Any]], sample_limit: int) -> None:
    print(f"[HISTORY_CFG] node={node}: {len(configs)} item(s)")
    for item in configs[:sample_limit]:
        name = item.get("name") or item.get("display") or item.get("tag") or ""
        cid = item.get("cid", "")
        vid = item.get("vid", "")
        hist_index = item.get("hist_index", "")
        unit = item.get("unit", "")
        print(f"  hist_index={hist_index!s:<5} cid={cid!s:<5} vid={vid!s:<7} unit={unit!s:<8} name={name}")
    if len(configs) > sample_limit:
        print(f"  ... {len(configs) - sample_limit} more item(s)")


def run_diagnostics(args: argparse.Namespace) -> int:
    cfg = load_config(args.config) if args.config else {}
    client = build_client(args, cfg)
    endpoint = client.endpoint_url

    print_title("Danfoss 850A Local API Diagnostics")
    print_kv("Started", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print_kv("Endpoint", endpoint)
    print_kv("Auth mode", client.auth_mode)
    print_kv("Username", client.username or "(empty)")
    print_kv("TLS verify", client.verify_tls)
    print_kv("Timeout sec", client.timeout_sec)
    print_kv("Min interval sec", client.min_interval_sec)

    failures = 0

    print_title("1. TCP Connectivity")
    tcp_result = tcp_probe(endpoint, float(client.timeout_sec))
    print(tcp_result)
    if tcp_result.startswith("FAIL"):
        failures += 1

    print_title("2. read_date_time")
    try:
        dt = client.read_date_time()
        print(f"[DATE_TIME] OK {dt.isoformat(sep=' ')}")
    except Exception as exc:
        failures += 1
        print(f"[DATE_TIME][FAIL] {type(exc).__name__}: {exc}")

    print_title("3. read_devices")
    try:
        devices = client.read_devices()
        print(f"[DEVICES] OK {len(devices)} device(s)")
        summarize_devices(devices)
    except Exception as exc:
        failures += 1
        print(f"[DEVICES][FAIL] {type(exc).__name__}: {exc}")

    print_title("4. read_device_history_cfg")
    nodes = parse_nodes(args.nodes or DEFAULT_NODES)
    history_success = 0
    for node in nodes:
        try:
            configs = client.read_device_history_cfg(node=node, nodetype=int(args.nodetype))
            summarize_history_cfg(node, configs, args.history_sample_limit)
            history_success += 1
        except Exception as exc:
            failures += 1
            print(f"[HISTORY_CFG][FAIL] node={node}: {type(exc).__name__}: {exc}")
    if nodes:
        print(f"[HISTORY_CFG] Completed {history_success}/{len(nodes)} node(s)")

    print_title("5. read_val Configured Points")
    specs = configured_points(cfg)
    if not specs:
        print("[READ_VAL][SKIP] No danfoss_points found in config.")
        print("[READ_VAL][NEXT] Add one confirmed point with nodetype/node + cid/vid or tag, then rerun.")
    else:
        try:
            values = client.read_values(specs)
            for key, value in values.items():
                print(
                    f"[READ_VAL] {key}: value={value.value!r} raw={value.raw_value!r} "
                    f"name={value.name!r} display={value.display!r} status={value.status!r} "
                    f"error={value.error!r}"
                )
        except Exception as exc:
            failures += 1
            print(f"[READ_VAL][FAIL] {type(exc).__name__}: {exc}")

    print_title("Summary")
    if failures:
        print(f"Result: FAIL/WARN with {failures} failed check(s).")
        print("Use the first failing section above to decide whether this is network, auth, device list, or point mapping.")
        return 1
    print("Result: OK. 850A API is reachable and basic diagnostics completed.")
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local diagnostics against a Danfoss 850A XML API endpoint without starting BACnet."
    )
    parser.add_argument("--config", default="config.850a.example.json", help="Gateway-style JSON config to read.")
    parser.add_argument("--endpoint", help="Override endpoint, e.g. http://172.28.238.109/html/xml.cgi")
    parser.add_argument("--username", help="Override Danfoss username. Defaults to config or DANFOSS_USERNAME.")
    parser.add_argument("--password", help="Override Danfoss password. Defaults to config or DANFOSS_PASSWORD.")
    parser.add_argument("--auth-mode", choices=["none", "disabled", "basic", "basic_header", "header", "session", "session_token"], help="Override auth mode.")
    parser.add_argument("--timeout-sec", type=int, help="HTTP/TCP timeout seconds.")
    parser.add_argument("--units", help="Danfoss units argument, default from config or s.")
    parser.add_argument("--lang", help="Danfoss lang argument, default from config or e.")
    parser.add_argument("--min-interval-sec", type=float, help="Throttle interval between XML calls.")
    parser.add_argument("--verify-tls", dest="verify_tls", action="store_true", default=None, help="Verify HTTPS certificate.")
    parser.add_argument("--no-verify-tls", dest="verify_tls", action="store_false", help="Disable HTTPS certificate verification for field debugging.")
    parser.add_argument("--ca-cert", help="CA certificate path for HTTPS.")
    parser.add_argument("--content-type", help="Request Content-Type, default text/xml.")
    parser.add_argument("--nodes", default=DEFAULT_NODES, help="Nodes to query for history cfg, e.g. 1-8,81,82.")
    parser.add_argument("--nodetype", type=int, default=16, help="Danfoss nodetype for history cfg queries.")
    parser.add_argument("--history-sample-limit", type=int, default=8, help="Rows printed per node from read_device_history_cfg.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(run_diagnostics(parse_args()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
    except (DanfossApiError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
