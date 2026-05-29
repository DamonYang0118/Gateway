from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from danfoss_850a import Danfoss850AClient, DanfossApiError, DanfossPointSpec


DEFAULT_NODES = "1-8,81,82"
DEFAULT_EXPECTED_DEVICES = [
    "TP-A1-7007A-01-01",
    "TP-A1-7007A-01-02",
    "TP-A1-7007A-01-03",
    "TP-A1-7007A-01-04",
    "TP-A1-7007A-01-05",
    "TP-A1-7007A-01-06",
    "TP-A1-7007A-01-07",
    "TP-A1-7007A-01-08",
    "水冷机组/Water CHILL 01",
    "风冷机组 /Air CHILL   02",
]
STATUS_KEYWORDS = ("offline", "online", "comm", "status", "alarm", "通信", "通讯", "状态", "报警")
PARAM_KEYWORDS = (
    "temp",
    "humid",
    "rh",
    "pressure",
    "status",
    "alarm",
    "comm",
    "温度",
    "湿度",
    "压力",
    "状态",
    "报警",
    "通讯",
    "通信",
)


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


def device_search_blob(item: Dict[str, Any]) -> str:
    return " ".join(str(value) for value in item.values() if value is not None)


def check_expected_devices(devices: Iterable[Dict[str, Any]], expected: Iterable[str]) -> Dict[str, List[str]]:
    blobs = [device_search_blob(item).lower() for item in devices]
    found: List[str] = []
    missing: List[str] = []
    for name in expected:
        needle = name.lower()
        if any(needle in blob for blob in blobs):
            found.append(name)
        else:
            missing.append(name)
    return {"found": found, "missing": missing}


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
    matches = []
    for item in configs:
        blob = device_search_blob(item).lower()
        if any(keyword.lower() in blob for keyword in STATUS_KEYWORDS):
            matches.append(item)
    if matches:
        print("  status/alarm/communication candidates:")
        for item in matches[: min(5, sample_limit)]:
            name = item.get("name") or item.get("display") or item.get("tag") or ""
            cid = item.get("cid", "")
            vid = item.get("vid", "")
            hist_index = item.get("hist_index", "")
            print(f"    hist_index={hist_index!s:<5} cid={cid!s:<5} vid={vid!s:<7} name={name}")


def summarize_parameters(device_label: str, params: List[Any], sample_limit: int) -> None:
    print(f"[PARM_INFO] {device_label}: {len(params)} parameter(s)")
    candidates = []
    for param in params:
        raw = asdict(param)
        blob = device_search_blob(raw).lower()
        if any(keyword.lower() in blob for keyword in PARAM_KEYWORDS):
            candidates.append(param)
    rows = candidates or params
    for param in rows[:sample_limit]:
        print(
            f"  cid={param.cid!s:<5} vid={param.vid!s:<7} unit={param.unit!s:<8} "
            f"rw={param.rw!s:<4} name={param.name}"
        )
    if len(rows) > sample_limit:
        print(f"  ... {len(rows) - sample_limit} more candidate/item(s)")


def summarize_device_alarms(device_label: str, alarms: Any) -> None:
    counts = {
        "active": len(alarms.active),
        "acked": len(alarms.acked),
        "cleared": len(alarms.cleared),
    }
    print(
        f"[DEVICE_ALARMS] {device_label}: active={counts['active']} "
        f"acked={counts['acked']} cleared={counts['cleared']} newest={alarms.newest_time or '-'}"
    )
    for ref in (alarms.active + alarms.acked + alarms.cleared)[:8]:
        print(f"  {ref.state:<7} id={ref.alarm_id:<8} name={ref.name}")


def run_diagnostics(args: argparse.Namespace) -> int:
    cfg = load_config(args.config) if args.config else {}
    client = build_client(args, cfg)
    endpoint = client.endpoint_url
    report: Dict[str, Any] = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "auth_mode": client.auth_mode,
        "username_set": bool(client.username),
        "tls_verify": client.verify_tls,
        "timeout_sec": client.timeout_sec,
        "min_interval_sec": client.min_interval_sec,
        "checks": {},
    }

    print_title("Danfoss 850A Local API Diagnostics")
    print_kv("Started", report["started"])
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
    report["checks"]["tcp"] = {"ok": tcp_result.startswith("OK"), "message": tcp_result}
    if tcp_result.startswith("FAIL"):
        failures += 1

    print_title("2. read_date_time")
    try:
        dt = client.read_date_time()
        print(f"[DATE_TIME] OK {dt.isoformat(sep=' ')}")
        report["checks"]["read_date_time"] = {"ok": True, "value": dt.isoformat(sep=" ")}
    except Exception as exc:
        failures += 1
        print(f"[DATE_TIME][FAIL] {type(exc).__name__}: {exc}")
        report["checks"]["read_date_time"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print_title("3. read_devices")
    devices: List[Dict[str, Any]] = []
    try:
        devices = client.read_devices()
        print(f"[DEVICES] OK {len(devices)} device(s)")
        summarize_devices(devices)
        expected_check = check_expected_devices(devices, args.expected_device)
        print("[DEVICES] Expected field module names:")
        for name in expected_check["found"]:
            print(f"  OK      {name}")
        for name in expected_check["missing"]:
            print(f"  MISSING {name}")
        if expected_check["missing"]:
            print("[DEVICES][WARN] Some expected names were not found. Save this output and confirm account/API permissions.")
        report["checks"]["read_devices"] = {
            "ok": True,
            "count": len(devices),
            "devices": devices,
            "expected": expected_check,
        }
    except Exception as exc:
        failures += 1
        print(f"[DEVICES][FAIL] {type(exc).__name__}: {exc}")
        report["checks"]["read_devices"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print_title("4. read_parm_info")
    parm_report: Dict[str, Any] = {}
    if not args.discover_params:
        print("[PARM_INFO][SKIP] Use --discover-params to query read_parm_info for devices.")
        report["checks"]["read_parm_info"] = {"ok": None, "skipped": True}
    else:
        parm_success = 0
        for item in devices[: args.max_param_devices]:
            device_id = str(item.get("device_id") or "").strip()
            label = str(item.get("name") or item.get("node") or device_id or "device")
            if not device_id:
                print(f"[PARM_INFO][SKIP] {label}: no device_id in read_devices output")
                parm_report[label] = {"ok": None, "skipped": True, "reason": "missing device_id"}
                continue
            try:
                params = client.read_parm_info(device_id)
                summarize_parameters(label, params, args.param_sample_limit)
                parm_report[label] = {
                    "ok": True,
                    "device_id": device_id,
                    "count": len(params),
                    "sample": [asdict(param) for param in params[: args.param_sample_limit]],
                    "candidates": [
                        asdict(param)
                        for param in params
                        if any(keyword.lower() in device_search_blob(asdict(param)).lower() for keyword in PARAM_KEYWORDS)
                    ][: args.param_sample_limit],
                }
                parm_success += 1
            except Exception as exc:
                failures += 1
                print(f"[PARM_INFO][FAIL] {label}: {type(exc).__name__}: {exc}")
                parm_report[label] = {"ok": False, "device_id": device_id, "error": f"{type(exc).__name__}: {exc}"}
        report["checks"]["read_parm_info"] = {
            "ok": parm_success > 0 if devices else False,
            "success_count": parm_success,
            "device_count": min(len(devices), args.max_param_devices),
            "devices": parm_report,
        }

    print_title("5. read_device_history_cfg")
    nodes = parse_nodes(args.nodes or DEFAULT_NODES)
    history_success = 0
    history_report: Dict[str, Any] = {}
    for node in nodes:
        try:
            configs = client.read_device_history_cfg(node=node, nodetype=int(args.nodetype))
            summarize_history_cfg(node, configs, args.history_sample_limit)
            history_success += 1
            status_candidates = [
                item
                for item in configs
                if any(keyword.lower() in device_search_blob(item).lower() for keyword in STATUS_KEYWORDS)
            ]
            history_report[str(node)] = {
                "ok": True,
                "count": len(configs),
                "sample": configs[: args.history_sample_limit],
                "status_candidates": status_candidates[: args.history_sample_limit],
            }
        except Exception as exc:
            failures += 1
            print(f"[HISTORY_CFG][FAIL] node={node}: {type(exc).__name__}: {exc}")
            history_report[str(node)] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if nodes:
        print(f"[HISTORY_CFG] Completed {history_success}/{len(nodes)} node(s)")
    report["checks"]["read_device_history_cfg"] = {
        "ok": history_success == len(nodes) if nodes else True,
        "success_count": history_success,
        "node_count": len(nodes),
        "nodes": history_report,
    }

    print_title("6. read_val Configured Points")
    specs = configured_points(cfg)
    if not specs:
        print("[READ_VAL][SKIP] No danfoss_points found in config.")
        print("[READ_VAL][NEXT] Add one confirmed point with nodetype/node + cid/vid or tag, then rerun.")
        report["checks"]["read_val"] = {"ok": None, "skipped": True, "reason": "No danfoss_points configured."}
    else:
        try:
            values = client.read_values(specs)
            point_values = {}
            for key, value in values.items():
                print(
                    f"[READ_VAL] {key}: value={value.value!r} raw={value.raw_value!r} "
                    f"name={value.name!r} display={value.display!r} status={value.status!r} "
                    f"error={value.error!r}"
                )
                point_values[key] = {
                    "value": value.value,
                    "raw_value": value.raw_value,
                    "name": value.name,
                    "display": value.display,
                    "status": value.status,
                    "status_code": value.status_code,
                    "error": value.error,
                }
            report["checks"]["read_val"] = {"ok": True, "values": point_values}
        except Exception as exc:
            failures += 1
            print(f"[READ_VAL][FAIL] {type(exc).__name__}: {exc}")
            report["checks"]["read_val"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print_title("7. Alarm Discovery")
    alarm_report: Dict[str, Any] = {"device_alarms": {}, "generic_alarms": None}
    if not args.query_device_alarms:
        print("[DEVICE_ALARMS][SKIP] Use --query-device-alarms to query read_device_alarms for devices.")
    else:
        alarm_success = 0
        for item in devices[: args.max_alarm_devices]:
            label = str(item.get("name") or item.get("node") or "device")
            nodetype = item.get("nodetype") or args.nodetype
            node = item.get("node")
            if node in (None, ""):
                print(f"[DEVICE_ALARMS][SKIP] {label}: no node in read_devices output")
                continue
            try:
                alarms = client.read_device_alarms(
                    nodetype=int(nodetype),
                    node=int(node),
                    mod=int(item.get("mod") or 0),
                    point=int(item.get("point") or 0),
                )
                summarize_device_alarms(label, alarms)
                alarm_report["device_alarms"][label] = asdict(alarms)
                alarm_success += 1
            except Exception as exc:
                failures += 1
                print(f"[DEVICE_ALARMS][FAIL] {label}: {type(exc).__name__}: {exc}")
                alarm_report["device_alarms"][label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        alarm_report["device_alarm_success_count"] = alarm_success

    if not args.query_generic_alarms:
        print("[GENERIC_ALARMS][SKIP] Use --query-generic-alarms to query read_generic_alarms.")
    else:
        try:
            alarms = client.read_generic_alarms(count=args.generic_alarm_count)
            print(f"[GENERIC_ALARMS] {len(alarms)} alarm(s)")
            for alarm in alarms[:10]:
                print(
                    f"  state={alarm.state or '-':<8} id={alarm.alarm_id or '-':<8} "
                    f"name={alarm.name or '-'} device={alarm.device or '-'} time={alarm.time or '-'}"
                )
            alarm_report["generic_alarms"] = [asdict(alarm) for alarm in alarms]
        except Exception as exc:
            failures += 1
            print(f"[GENERIC_ALARMS][FAIL] {type(exc).__name__}: {exc}")
            alarm_report["generic_alarms"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    report["checks"]["alarms"] = alarm_report

    print_title("Summary")
    report["failures"] = failures
    if failures:
        print(f"Result: FAIL/WARN with {failures} failed check(s).")
        print("Use the first failing section above to decide whether this is network, auth, device list, or point mapping.")
        write_report_if_requested(args.report_json, report)
        return 1
    print("Result: OK. 850A API is reachable and basic diagnostics completed.")
    write_report_if_requested(args.report_json, report)
    return 0


def write_report_if_requested(path: Optional[str], report: Dict[str, Any]) -> None:
    if not path:
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[REPORT] Wrote JSON report: {output_path}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local diagnostics against a Danfoss 850A XML API endpoint without starting BACnet."
    )
    parser.add_argument("--config", default="config.850a.example.json", help="Gateway-style JSON config to read.")
    parser.add_argument("--endpoint", help="Override endpoint, e.g. http://172.28.238.109/html/xml.cgi")
    parser.add_argument("--username", help="Override Danfoss username. Defaults to config or DANFOSS_USERNAME.")
    parser.add_argument("--password", help="Override Danfoss password. Defaults to config or DANFOSS_PASSWORD.")
    parser.add_argument(
        "--auth-mode",
        choices=[
            "none",
            "disabled",
            "basic",
            "basic_header",
            "header",
            "session",
            "session_token",
            "cmd_credentials",
            "cmd",
            "xml_credentials",
            "aksm_plain_header",
            "plain_header",
            "strict_header",
        ],
        help="Override auth mode.",
    )
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
    parser.add_argument("--discover-params", action="store_true", help="Query read_parm_info for devices returned by read_devices.")
    parser.add_argument("--max-param-devices", type=int, default=12, help="Maximum devices to query with read_parm_info.")
    parser.add_argument("--param-sample-limit", type=int, default=12, help="Rows printed per device from read_parm_info.")
    parser.add_argument("--query-device-alarms", action="store_true", help="Query read_device_alarms for devices returned by read_devices.")
    parser.add_argument("--max-alarm-devices", type=int, default=12, help="Maximum devices to query with read_device_alarms.")
    parser.add_argument("--query-generic-alarms", action="store_true", help="Query read_generic_alarms once for system-level alarms.")
    parser.add_argument("--generic-alarm-count", type=int, default=100, help="Count parameter for read_generic_alarms.")
    parser.add_argument(
        "--expected-device",
        action="append",
        default=list(DEFAULT_EXPECTED_DEVICES),
        help="Expected device/module name to look for in read_devices output. Can be repeated.",
    )
    parser.add_argument("--report-json", help="Optional path to write a sanitized JSON evidence report.")
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
