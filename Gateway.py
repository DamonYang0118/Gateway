import asyncio
import ipaddress
import json
import os
import platform
import random
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import BAC0
from BAC0.core.devices.local import factory as F
from bacpypes3.basetypes import EventState, EventTransitionBits, LimitEnable, NotifyType, TimeStamp
from bacpypes3.local.analog import AnalogInputObject, AnalogInputObjectIR

from danfoss_850a import Danfoss850AClient, DanfossApiError, DanfossPointSpec


DEFAULTS = {
    "device_id": 2002,
    "device_name": "Danfoss_Gateway",
    "update_sec": 5,
    "danfoss_api_mode": "legacy_status_xml",
    "danfoss_xml_url": "http://127.0.0.1/status.xml",
    "danfoss_endpoint_url": "http://127.0.0.1/html/xml.cgi",
    "danfoss_auth_mode": "basic_header",
    "danfoss_username": "",
    "danfoss_password": "",
    "danfoss_units": "s",
    "danfoss_lang": "e",
    "danfoss_min_interval_sec": 4,
    "danfoss_verify_tls": True,
    "danfoss_ca_cert": "",
    "danfoss_content_type": "text/xml",
    "danfoss_primary_point": "MotorFreq",
    "danfoss_points": [],
    "danfoss_cache_file": "danfoss_last_good.json",
    "use_cached_value_if_http_fail": True,
    "http_timeout_sec": 3,
    "use_simulation_if_http_fail": True,
    "sim_low": 40.0,
    "sim_high": 52.0,
    "low_limit": 41.0,
    "high_limit": 49.0,
    "deadband": 0.5,
    "time_delay_sec": 0,
    "bind_ip": "auto",
    "bind_prefix": 24,
    "local_port": 47808,
    "nc_high_instance": 1,
    "nc_low_instance": 2,
    "recipient_ip": "",
    "recipient_port": 47808,
    "recipient_process_id": 600,
    "confirmed_notify": True,
    # NotificationClass priority mapping for High/Low alarm channels:
    # [toOffNormal, toFault, toNormal]
    "priority_high_to_offnormal": 64,
    "priority_high_to_fault": 64,
    "priority_high_to_normal": 100,
    "priority_low_to_offnormal": 180,
    "priority_low_to_fault": 64,
    "priority_low_to_normal": 100,
    # legacy shared keys (still supported as fallback)
    "priority_to_fault": 64,
    "priority_to_normal": 100,
}


def install_windows_reuse_port_workaround() -> None:
    if os.name != "nt":
        return

    def patch_loop_class(loop_cls) -> bool:
        if loop_cls is None:
            return False
        original_create = getattr(loop_cls, "create_datagram_endpoint", None)
        if original_create is None or getattr(loop_cls, "_gw_reuse_port_patched", False):
            return False

        async def patched_create_datagram_endpoint(self, *args, **kwargs):
            if "reuse_port" in kwargs:
                kwargs = dict(kwargs)
                kwargs.pop("reuse_port", None)
            return await original_create(self, *args, **kwargs)

        setattr(loop_cls, "create_datagram_endpoint", patched_create_datagram_endpoint)
        setattr(loop_cls, "_gw_reuse_port_patched", True)
        return True

    patched = False
    for cls_name in ("BaseEventLoop", "SelectorEventLoop", "ProactorEventLoop"):
        patched = patch_loop_class(getattr(asyncio, cls_name, None)) or patched

    try:
        from asyncio import base_events  # type: ignore

        patched = patch_loop_class(getattr(base_events, "BaseEventLoop", None)) or patched
    except Exception:
        pass

    if patched:
        print("[INIT] Installed Windows UDP reuse_port workaround.")


def get_config_path() -> str:
    env_cfg = os.getenv("GATEWAY_CONFIG_PATH", "").strip()
    if env_cfg:
        return env_cfg
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        exe_cfg = os.path.join(exe_dir, "config.json")
        if os.path.exists(exe_cfg):
            return exe_cfg
    src_cfg = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(src_cfg):
        return src_cfg
    return os.path.join(os.getcwd(), "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg_path = get_config_path()
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
                # Backward compatibility for older config keys.
                if "priority_high_to_fault" not in user_cfg:
                    cfg["priority_high_to_fault"] = cfg.get("priority_to_fault", DEFAULTS["priority_to_fault"])
                if "priority_low_to_fault" not in user_cfg:
                    cfg["priority_low_to_fault"] = cfg.get("priority_to_fault", DEFAULTS["priority_to_fault"])
                if "priority_high_to_normal" not in user_cfg:
                    cfg["priority_high_to_normal"] = cfg.get("priority_to_normal", DEFAULTS["priority_to_normal"])
                if "priority_low_to_normal" not in user_cfg:
                    cfg["priority_low_to_normal"] = cfg.get("priority_to_normal", DEFAULTS["priority_to_normal"])
                # Optional alias for single offnormal key.
                if "priority_high_to_offnormal" not in user_cfg and "priority_to_offnormal" in user_cfg:
                    cfg["priority_high_to_offnormal"] = user_cfg["priority_to_offnormal"]
        except Exception as e:
            print(f"[CFG][WARN] Failed to read config.json: {e} (using defaults)")
    return cfg


CFG = load_config()


def cfg_bool(name: str, default: bool = False) -> bool:
    value = CFG.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def resolve_runtime_path(path: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(get_config_path()), path)


DEVICE_ID = int(CFG["device_id"])
DEVICE_NAME = str(CFG["device_name"])
UPDATE_SEC = int(CFG["update_sec"])
DANFOSS_API_MODE = str(CFG.get("danfoss_api_mode", "legacy_status_xml")).strip().lower()
DANFOSS_XML_URL = str(CFG["danfoss_xml_url"])
DANFOSS_ENDPOINT_URL = str(CFG.get("danfoss_endpoint_url", "")).strip()
DANFOSS_AUTH_MODE = str(CFG.get("danfoss_auth_mode", "basic_header")).strip().lower()
DANFOSS_USERNAME = str(CFG.get("danfoss_username") or os.getenv("DANFOSS_USERNAME", "")).strip()
DANFOSS_PASSWORD = str(CFG.get("danfoss_password") or os.getenv("DANFOSS_PASSWORD", "")).strip()
DANFOSS_UNITS = str(CFG.get("danfoss_units", "s")).strip() or "s"
DANFOSS_LANG = str(CFG.get("danfoss_lang", "e")).strip()
DANFOSS_MIN_INTERVAL_SEC = float(CFG.get("danfoss_min_interval_sec", 4))
DANFOSS_VERIFY_TLS = cfg_bool("danfoss_verify_tls", True)
DANFOSS_CA_CERT = str(CFG.get("danfoss_ca_cert", "")).strip()
DANFOSS_CONTENT_TYPE = str(CFG.get("danfoss_content_type", "text/xml")).strip() or "text/xml"
DANFOSS_PRIMARY_POINT = str(CFG.get("danfoss_primary_point", "MotorFreq")).strip() or "MotorFreq"
DANFOSS_POINTS_CFG = CFG.get("danfoss_points", [])
DANFOSS_CACHE_FILE = resolve_runtime_path(str(CFG.get("danfoss_cache_file", "danfoss_last_good.json")))
USE_CACHED_VALUE_IF_HTTP_FAIL = cfg_bool("use_cached_value_if_http_fail", True)
HTTP_TIMEOUT_SEC = int(CFG["http_timeout_sec"])
USE_SIMULATION_IF_HTTP_FAIL = cfg_bool("use_simulation_if_http_fail", True)
SIM_LOW = float(CFG["sim_low"])
SIM_HIGH = float(CFG["sim_high"])
LOW_LIMIT = float(CFG["low_limit"])
HIGH_LIMIT = float(CFG["high_limit"])
DEADBAND = float(CFG["deadband"])
TIME_DELAY_SEC = int(CFG["time_delay_sec"])
LOCAL_PORT = int(CFG["local_port"])
NC_HIGH_INSTANCE = int(CFG["nc_high_instance"])
NC_LOW_INSTANCE = int(CFG["nc_low_instance"])
RECIPIENT_IP = str(CFG["recipient_ip"]).strip()
RECIPIENT_PORT = int(CFG["recipient_port"])
RECIPIENT_PROCESS_ID = int(CFG["recipient_process_id"])
CONFIRMED_NOTIFY = bool(CFG["confirmed_notify"])
PRIORITY_HIGH_TO_OFFNORMAL = int(CFG["priority_high_to_offnormal"])
PRIORITY_LOW_TO_OFFNORMAL = int(CFG["priority_low_to_offnormal"])
PRIORITY_HIGH_TO_FAULT = int(CFG["priority_high_to_fault"])
PRIORITY_LOW_TO_FAULT = int(CFG["priority_low_to_fault"])
PRIORITY_HIGH_TO_NORMAL = int(CFG["priority_high_to_normal"])
PRIORITY_LOW_TO_NORMAL = int(CFG["priority_low_to_normal"])

TIMESTAMP_SOURCE_EPOCH = None
_TIMESTAMP_OVERRIDE_INSTALLED = False
_ORIGINAL_TIMESTAMP_AS_TIME = TimeStamp.as_time


def install_timestamp_source_override():
    global _TIMESTAMP_OVERRIDE_INSTALLED
    if _TIMESTAMP_OVERRIDE_INSTALLED:
        return

    def _patched_as_time(cls, *args, **kwargs):
        # Keep explicit caller-provided timestamp behavior unchanged.
        if args or kwargs:
            return _ORIGINAL_TIMESTAMP_AS_TIME(*args, **kwargs)
        epoch = TIMESTAMP_SOURCE_EPOCH
        if epoch is None:
            return _ORIGINAL_TIMESTAMP_AS_TIME()
        return _ORIGINAL_TIMESTAMP_AS_TIME(epoch)

    TimeStamp.as_time = classmethod(_patched_as_time)
    _TIMESTAMP_OVERRIDE_INSTALLED = True
    print("[INIT] Installed TimeStamp override (prefer Danfoss XML time).")


def set_notification_timestamp_source(epoch_seconds):
    global TIMESTAMP_SOURCE_EPOCH
    try:
        TIMESTAMP_SOURCE_EPOCH = float(epoch_seconds)
    except Exception:
        TIMESTAMP_SOURCE_EPOCH = None


def pick(*names):
    for n in names:
        if hasattr(F, n):
            return getattr(F, n)
    raise ImportError(f"factory missing any of {names}")


analog_input = pick("analog_input")
analog_value = pick("analog_value")
character_string = pick("character_string")


def safe_add(obj, dev):
    try:
        obj.add_objects_to_application(dev)
        return True
    except Exception as e:
        print(f"[OBJ][WARN] add failed for {getattr(obj, 'name', obj)}: {e}")
        return False


def _run(cmd):
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()


def _ip_by_socket() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _prefix_from_netmask(mask: str) -> int:
    return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def _win_ip_and_prefix():
    out = _run(["ipconfig", "/all"])
    blocks = re.split(r"\r?\n\r?\n", out)
    for b in blocks:
        m_ip = re.search(r"IPv4 Address[.\s]*:\s*([0-9.]+)", b) or re.search(
            r"IPv4 地址[.\s]*:\s*([0-9.]+)", b
        )
        if not m_ip:
            continue
        m_mask = re.search(r"Subnet Mask[.\s]*:\s*([0-9.]+)", b) or re.search(
            r"子网掩码[.\s]*:\s*([0-9.]+)", b
        )
        ip = m_ip.group(1)
        mask = m_mask.group(1) if m_mask else "255.255.255.0"
        return ip, _prefix_from_netmask(mask)
    return _ip_by_socket(), 24


def _mac_ip_and_prefix():
    iface = "en0"
    try:
        out = _run(["route", "-n", "get", "default"])
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                iface = line.split(":")[1].strip()
                break
    except Exception:
        pass
    try:
        ip = _run(["ipconfig", "getifaddr", iface])
    except Exception:
        ip = _ip_by_socket()
    try:
        mask = _run(["ipconfig", "getoption", iface, "subnet_mask"])
        if mask:
            return ip, _prefix_from_netmask(mask)
    except Exception:
        pass
    return ip, 24


def get_bind_ip_prefix():
    bind_ip = str(CFG.get("bind_ip", "auto")).strip()
    bind_prefix = int(CFG.get("bind_prefix", 24))
    if bind_ip and bind_ip.lower() != "auto":
        return bind_ip, bind_prefix
    env_ip = os.getenv("BIND_IP")
    env_prefix = os.getenv("BIND_PREFIX")
    if env_ip:
        return env_ip, int(env_prefix or bind_prefix)
    sysname = platform.system().lower()
    if "windows" in sysname:
        return _win_ip_and_prefix()
    if "darwin" in sysname:
        return _mac_ip_and_prefix()
    return _ip_by_socket(), bind_prefix


_DANFOSS_CLIENT = None
_DANFOSS_POINT_SPECS = None


def get_danfoss_point_specs():
    global _DANFOSS_POINT_SPECS
    if _DANFOSS_POINT_SPECS is not None:
        return _DANFOSS_POINT_SPECS
    specs = []
    if not isinstance(DANFOSS_POINTS_CFG, list):
        print("[CFG][WARN] danfoss_points must be a list; ignoring configured value.")
        _DANFOSS_POINT_SPECS = specs
        return specs
    for raw in DANFOSS_POINTS_CFG:
        if not isinstance(raw, dict):
            print(f"[CFG][WARN] Ignoring invalid Danfoss point config: {raw!r}")
            continue
        try:
            specs.append(DanfossPointSpec.from_mapping(raw))
        except Exception as e:
            print(f"[CFG][WARN] Ignoring Danfoss point config {raw!r}: {e}")
    _DANFOSS_POINT_SPECS = specs
    return specs


def get_danfoss_client():
    global _DANFOSS_CLIENT
    if _DANFOSS_CLIENT is not None:
        return _DANFOSS_CLIENT
    endpoint_url = DANFOSS_ENDPOINT_URL or DANFOSS_XML_URL
    _DANFOSS_CLIENT = Danfoss850AClient(
        endpoint_url=endpoint_url,
        username=DANFOSS_USERNAME,
        password=DANFOSS_PASSWORD,
        auth_mode=DANFOSS_AUTH_MODE,
        timeout_sec=HTTP_TIMEOUT_SEC,
        units=DANFOSS_UNITS,
        lang=DANFOSS_LANG,
        min_interval_sec=DANFOSS_MIN_INTERVAL_SEC,
        verify_tls=DANFOSS_VERIFY_TLS,
        ca_cert=DANFOSS_CA_CERT,
        content_type=DANFOSS_CONTENT_TYPE,
    )
    return _DANFOSS_CLIENT


def danfoss_850a_enabled():
    if DANFOSS_API_MODE in ("850a_xml", "danfoss_850a", "xml_post", "read_val"):
        return True
    if DANFOSS_API_MODE == "auto" and get_danfoss_point_specs():
        return True
    return False


def save_cached_value(point_key: str, value: float, dt: datetime) -> None:
    if not DANFOSS_CACHE_FILE:
        return
    try:
        payload = {
            "point_key": point_key,
            "value": float(value),
            "timestamp": dt.isoformat(),
            "epoch": int(dt.timestamp()),
        }
        with open(DANFOSS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, indent=2)
    except Exception as e:
        print(f"[CACHE][WARN] Failed to write last-good Danfoss value: {e}")


def load_cached_value():
    if not USE_CACHED_VALUE_IF_HTTP_FAIL or not DANFOSS_CACHE_FILE or not os.path.exists(DANFOSS_CACHE_FILE):
        return None, None
    try:
        with open(DANFOSS_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        value = float(payload["value"])
        ts_text = str(payload.get("timestamp", "")).strip()
        if ts_text:
            dt = datetime.fromisoformat(ts_text)
        else:
            dt = datetime.fromtimestamp(float(payload.get("epoch", time.time())))
        return value, dt
    except Exception as e:
        print(f"[CACHE][WARN] Failed to read last-good Danfoss value: {e}")
        return None, None


def fetch_danfoss_850a_value_and_time():
    specs = get_danfoss_point_specs()
    if not specs:
        raise DanfossApiError("danfoss_points is empty; configure at least one read_val point.")
    client = get_danfoss_client()
    values = client.read_values(specs)
    point = values.get(DANFOSS_PRIMARY_POINT)
    if point is None:
        point = next(iter(values.values()), None)
    if point is None:
        raise DanfossApiError("read_val response did not contain any <val> elements.")
    if point.error not in (None, 0):
        raise DanfossApiError(f"Point {point.key} returned error={point.error}.")
    if point.value is None:
        raise DanfossApiError(f"Point {point.key} has non-numeric value: {point.raw_value!r}")
    return float(point.value), point.timestamp


def parse_danfoss_xml(xml_text: str):
    root = ET.fromstring(xml_text)

    def find_text_by_keywords(keywords):
        for elem in root.iter():
            tag = (elem.tag or "").lower()
            if any(k in tag for k in keywords) and elem.text and elem.text.strip():
                return elem.text.strip()
        return None

    ts_text = find_text_by_keywords(["timestamp", "time", "datetime", "date"])
    freq_text = find_text_by_keywords(["freq", "frequency", "motorfreq", "speed"])
    if not ts_text or not freq_text:
        raise ValueError(f"XML parse failed (ts={ts_text}, freq={freq_text})")
    ts_text_norm = ts_text.replace("Z", "+00:00").replace("/", "-").strip()
    if "T" not in ts_text_norm and " " in ts_text_norm:
        ts_text_norm = ts_text_norm.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(ts_text_norm)
    except Exception:
        dt = datetime.fromisoformat(ts_text_norm[:19])
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    freq = float(re.findall(r"[-+]?\d*\.?\d+", freq_text)[0])
    return freq, dt


def fetch_danfoss_value_and_time():
    if danfoss_850a_enabled():
        try:
            value, dt = fetch_danfoss_850a_value_and_time()
            save_cached_value(DANFOSS_PRIMARY_POINT, value, dt)
            return value, dt
        except Exception as e:
            print(f"[DANFOSS][WARN] 850A XML read_val failed: {e}")
            cached_value, cached_dt = load_cached_value()
            if cached_value is not None and cached_dt is not None:
                print(f"[CACHE][INFO] Using last-good {DANFOSS_PRIMARY_POINT}={cached_value}")
                return cached_value, cached_dt
            return None, None

    try:
        req = Request(DANFOSS_XML_URL, headers={"User-Agent": "DanfossGateway/1.0"})
        with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            xml_text = resp.read().decode("utf-8", errors="ignore")
        value, dt = parse_danfoss_xml(xml_text)
        save_cached_value(DANFOSS_PRIMARY_POINT, value, dt)
        return value, dt
    except (HTTPError, URLError, TimeoutError, ValueError) as e:
        print(f"[HTTP][WARN] Legacy Danfoss XML fetch failed: {e}")
        cached_value, cached_dt = load_cached_value()
        if cached_value is not None and cached_dt is not None:
            print(f"[CACHE][INFO] Using last-good {DANFOSS_PRIMARY_POINT}={cached_value}")
            return cached_value, cached_dt
        return None, None


def add_bacpypes_object_to_bac0(dev, obj):
    holders = []
    for k in ("this_application", "app", "_app", "application", "_application"):
        v = getattr(dev, k, None)
        if v is not None:
            holders.append(v)

    candidates = []
    seen = set()

    def _push(x):
        if x is None or id(x) in seen:
            return
        seen.add(id(x))
        candidates.append(x)

    for h in holders:
        _push(h)
        for k in ("app", "application", "_app", "_application", "bacnet_app", "_bacnet_app"):
            _push(getattr(h, k, None))

    for app in candidates:
        for m in ("add_object", "addObject", "AddObject", "add"):
            fn = getattr(app, m, None)
            if callable(fn):
                try:
                    fn(obj)
                    print(f"[BACNET] Added {getattr(obj, 'objectName', obj)} via {type(app).__name__}.{m}")
                    return True
                except Exception:
                    pass
    return False


def create_notification_class(
    dev,
    nc_instance: int,
    object_name: str,
    priority_offnormal: int,
    priority_fault: int,
    priority_normal: int,
) -> bool:
    try:
        from bacpypes3.object import NotificationClassObject
        from bacpypes3.basetypes import Destination, Recipient, EventTransitionBits
        from bacpypes3.pdu import Address

        recipient_list = []
        if RECIPIENT_IP:
            recipient_list.append(
                Destination(
                    validDays=[1, 1, 1, 1, 1, 1, 1],
                    fromTime=(0, 0, 0, 0),
                    toTime=(23, 59, 59, 99),
                    recipient=Recipient(address=Address(f"{RECIPIENT_IP}:{RECIPIENT_PORT}")),
                    processIdentifier=int(RECIPIENT_PROCESS_ID),
                    issueConfirmedNotifications=bool(CONFIRMED_NOTIFY),
                    transitions=EventTransitionBits([1, 1, 1]),
                )
            )

        nc_obj = NotificationClassObject(
            objectIdentifier=("notificationClass", int(nc_instance)),
            objectName=object_name,
            notificationClass=int(nc_instance),
            description="Gateway Intrinsic Alarm Notification Class",
            priority=[int(priority_offnormal), int(priority_fault), int(priority_normal)],
            ackRequired=EventTransitionBits([0, 0, 0]),
            recipientList=recipient_list,
        )

        ok = add_bacpypes_object_to_bac0(dev, nc_obj)
        if ok:
            if RECIPIENT_IP:
                print(
                    f"[ALARM] {object_name} ready, recipient={RECIPIENT_IP}:{RECIPIENT_PORT}, "
                    f"pid={RECIPIENT_PROCESS_ID}, confirmed={CONFIRMED_NOTIFY}, "
                    f"priority=[{priority_offnormal},{priority_fault},{priority_normal}]"
                )
            else:
                print(
                    f"[ALARM] {object_name} ready for local intrinsic alarm evaluation "
                    f"(recipientList empty), priority=[{priority_offnormal},{priority_fault},{priority_normal}]"
                )
        else:
            print("[ALARM][ERROR] Failed to attach NotificationClass into BAC0 application.")
        return ok
    except Exception as e:
        print(f"[ALARM][ERROR] NotificationClass creation failed: {e}")
        return False


def setup_intrinsic_limit_alarm(
    ai_obj,
    ai_name: str,
    notification_class_created: bool,
    nc_instance: int,
    enable_low_limit: bool,
    enable_high_limit: bool,
):
    loc = getattr(ai_obj, "_local", ai_obj)

    def _set_if_has(k, v):
        if hasattr(loc, k):
            try:
                setattr(loc, k, v)
                return True
            except Exception as e:
                print(f"[ALARM][WARN] failed set {k}: {e}")
        return False

    _set_if_has("highLimit", float(HIGH_LIMIT))
    _set_if_has("lowLimit", float(LOW_LIMIT))
    _set_if_has("deadband", float(DEADBAND))
    _set_if_has("timeDelay", int(TIME_DELAY_SEC))
    _set_if_has("timeDelayNormal", 0)
    _set_if_has("limitEnable", [enable_low_limit, enable_high_limit])
    _set_if_has("ackedTransitions", [True, True, True])
    if notification_class_created:
        _set_if_has("eventDetectionEnable", True)
        _set_if_has("eventEnable", [True, True, True])
        _set_if_has("notificationClass", int(nc_instance))
    else:
        _set_if_has("eventDetectionEnable", False)
        _set_if_has("eventEnable", [False, False, False])
        _set_if_has("notificationClass", 0)
    if not _set_if_has("notifyType", "alarm"):
        _set_if_has("notifyType", 0)
    print(
        f"[ALARM] Intrinsic limits set on {ai_name}: "
        f"low={LOW_LIMIT}, high={HIGH_LIMIT}, deadband={DEADBAND}, timeDelay={TIME_DELAY_SEC}, "
        f"limitEnable={[enable_low_limit, enable_high_limit]}, notificationClass={nc_instance if notification_class_created else 0}"
    )
    try:
        print(
            "[ALARM] Snapshot "
            f"eventDetectionEnable={getattr(loc, 'eventDetectionEnable', None)}, "
            f"eventEnable={getattr(loc, 'eventEnable', None)}, "
            f"limitEnable={getattr(loc, 'limitEnable', None)}, "
            f"notificationClass={getattr(loc, 'notificationClass', None)}, "
            f"notifyType={getattr(loc, 'notifyType', None)}, "
            f"eventState={getattr(loc, 'eventState', None)}"
        )
    except Exception as e:
        print(f"[ALARM][WARN] snapshot read failed: {e}")


async def main():
    print("Starting BACnet Server (Intrinsic Alarm + NotificationClass Distribution)...")
    BAC0.log_level("error")

    ip, prefix = get_bind_ip_prefix()
    bac0_ip = f"{ip}/{prefix}"
    print(f"[NET] Binding BACnet server to: {bac0_ip} UDP Port: {LOCAL_PORT}")
    print(f"[CFG] Device ID: {DEVICE_ID}, Name: {DEVICE_NAME}")
    print(f"[CFG] Alarm Limits: Low={LOW_LIMIT}, High={HIGH_LIMIT}")
    print(f"[CFG] Notification Recipient: {RECIPIENT_IP or 'NOT SET'}")
    if danfoss_850a_enabled():
        print(
            f"[CFG] Danfoss southbound: 850A XML POST endpoint={DANFOSS_ENDPOINT_URL or DANFOSS_XML_URL}, "
            f"auth={DANFOSS_AUTH_MODE}, primary_point={DANFOSS_PRIMARY_POINT}, "
            f"points={len(get_danfoss_point_specs())}"
        )
    else:
        print(f"[CFG] Danfoss southbound: legacy GET {DANFOSS_XML_URL}")

    try:
        bac0_instance = BAC0.lite(
            ip=bac0_ip,
            deviceId=DEVICE_ID,
            localObjName=DEVICE_NAME,
            port=LOCAL_PORT,
        )
    except TypeError:
        bac0_instance = BAC0.lite(ip=bac0_ip, deviceId=DEVICE_ID, localObjName=DEVICE_NAME)
    except Exception as e:
        print(f"[NET][ERROR] Failed to initialize BAC0: {e}")
        return

    try:
        async with bac0_instance as dev:
            print(f"[BAC0] Device started: {DEVICE_NAME} ({DEVICE_ID})")

            motor_freq_value_obj = AnalogInputObject(
                objectIdentifier=("analogInput", 1),
                objectName="MotorFreq",
                description="Danfoss actual frequency (display)",
                presentValue=0.0,
                units="hertz",
                covIncrement=0.15,
                eventState=EventState.normal,
                outOfService=False,
            )
            if not add_bacpypes_object_to_bac0(dev, motor_freq_value_obj):
                print("[OBJ][ERROR] Failed to add native MotorFreq AI object.")
                return

            nc_high_created = create_notification_class(
                dev,
                NC_HIGH_INSTANCE,
                f"NC_HIGH_{NC_HIGH_INSTANCE}",
                PRIORITY_HIGH_TO_OFFNORMAL,
                PRIORITY_HIGH_TO_FAULT,
                PRIORITY_HIGH_TO_NORMAL,
            )
            nc_low_created = create_notification_class(
                dev,
                NC_LOW_INSTANCE,
                f"NC_LOW_{NC_LOW_INSTANCE}",
                PRIORITY_LOW_TO_OFFNORMAL,
                PRIORITY_LOW_TO_FAULT,
                PRIORITY_LOW_TO_NORMAL,
            )

            motor_freq_high_obj = AnalogInputObjectIR(
                objectIdentifier=("analogInput", 11),
                objectName="MotorFreqHighAlarm",
                description="Danfoss frequency high-limit alarm source",
                presentValue=0.0,
                units="hertz",
                covIncrement=0.15,
                eventState=EventState.normal,
                outOfService=False,
                timeDelay=int(TIME_DELAY_SEC),
                notificationClass=int(NC_HIGH_INSTANCE) if nc_high_created else 0,
                highLimit=float(HIGH_LIMIT),
                lowLimit=float(LOW_LIMIT),
                deadband=float(DEADBAND),
                limitEnable=LimitEnable([0, 1]),
                eventEnable=EventTransitionBits([1, 1, 1] if nc_high_created else [0, 0, 0]),
                ackedTransitions=EventTransitionBits([1, 1, 1]),
                notifyType=NotifyType.alarm,
                eventTimeStamps=[TimeStamp.as_time(), TimeStamp.as_time(), TimeStamp.as_time()],
                eventDetectionEnable=bool(nc_high_created),
                timeDelayNormal=0,
            )
            if not add_bacpypes_object_to_bac0(dev, motor_freq_high_obj):
                print("[OBJ][ERROR] Failed to add MotorFreqHighAlarm AI object.")
                return

            motor_freq_low_obj = AnalogInputObjectIR(
                objectIdentifier=("analogInput", 12),
                objectName="MotorFreqLowAlarm",
                description="Danfoss frequency low-limit alarm source",
                presentValue=0.0,
                units="hertz",
                covIncrement=0.15,
                eventState=EventState.normal,
                outOfService=False,
                timeDelay=int(TIME_DELAY_SEC),
                notificationClass=int(NC_LOW_INSTANCE) if nc_low_created else 0,
                highLimit=float(HIGH_LIMIT),
                lowLimit=float(LOW_LIMIT),
                deadband=float(DEADBAND),
                limitEnable=LimitEnable([1, 0]),
                eventEnable=EventTransitionBits([1, 1, 1] if nc_low_created else [0, 0, 0]),
                ackedTransitions=EventTransitionBits([1, 1, 1]),
                notifyType=NotifyType.alarm,
                eventTimeStamps=[TimeStamp.as_time(), TimeStamp.as_time(), TimeStamp.as_time()],
                eventDetectionEnable=bool(nc_low_created),
                timeDelayNormal=0,
            )
            if not add_bacpypes_object_to_bac0(dev, motor_freq_low_obj):
                print("[OBJ][ERROR] Failed to add MotorFreqLowAlarm AI object.")
                return

            obj_definitions = [
                {
                    "factory": analog_value,
                    "args": {
                        "instance": 1,
                        "name": "Setpoint",
                        "description": "BMS setpoint (writable)",
                        "units": "hertz",
                        "cov_increment": 0.1,
                        "initial_value": 50.0,
                    },
                },
                {
                    "factory": analog_value,
                    "args": {
                        "instance": 2,
                        "name": "LastEventState",
                        "description": "shadow state (0 normal / 1 high / 2 low)",
                        "units": "noUnits",
                        "cov_increment": 1,
                        "initial_value": 0,
                    },
                },
                {
                    "factory": analog_value,
                    "args": {
                        "instance": 3,
                        "name": "LastLogTs",
                        "description": "epoch seconds from Danfoss time",
                        "units": "noUnits",
                        "cov_increment": 1,
                        "initial_value": int(time.time()),
                    },
                },
                {
                    "factory": character_string,
                    "args": {
                        "instance": 1,
                        "name": "LastLogText",
                        "description": "shadow text",
                        "initial_value": "(init)",
                    },
                },
            ]

            for obj_def in obj_definitions:
                obj_inst = obj_def["factory"](**obj_def["args"])
                safe_add(obj_inst, dev)

            setup_intrinsic_limit_alarm(
                motor_freq_high_obj,
                "MotorFreqHighAlarm",
                nc_high_created,
                NC_HIGH_INSTANCE,
                enable_low_limit=False,
                enable_high_limit=True,
            )
            setup_intrinsic_limit_alarm(
                motor_freq_low_obj,
                "MotorFreqLowAlarm",
                nc_low_created,
                NC_LOW_INSTANCE,
                enable_low_limit=True,
                enable_high_limit=False,
            )

            last_event_state = "unknown"
            sim_cycle = [
                round(HIGH_LIMIT + 1.0, 2),  # high alarm
                round((LOW_LIMIT + HIGH_LIMIT) / 2.0, 2),  # normal
                round(LOW_LIMIT - 1.0, 2),  # low alarm
            ]
            sim_index = 0
            print("[LOOP] Running...")
            while True:
                try:
                    sp_val = float(dev["Setpoint"].presentValue)
                except Exception:
                    sp_val = 50.0

                freq, danfoss_dt = fetch_danfoss_value_and_time()
                if freq is None or danfoss_dt is None:
                    if USE_SIMULATION_IF_HTTP_FAIL:
                        freq = sim_cycle[sim_index]
                        sim_index = (sim_index + 1) % len(sim_cycle)
                        danfoss_dt = datetime.now()
                        print(f"[HTTP][WARN] Fetch failed, simulation={freq} Hz")
                    else:
                        await asyncio.sleep(UPDATE_SEC)
                        continue

                shadow = 1 if freq > HIGH_LIMIT else (2 if freq < LOW_LIMIT else 0)
                try:
                    epoch = int(danfoss_dt.timestamp())
                except Exception:
                    epoch = int(time.time())

                danfoss_str = danfoss_dt.strftime("%Y-%m-%d %H:%M:%S")
                set_notification_timestamp_source(epoch)

                motor_freq_value_obj.presentValue = float(freq)
                motor_freq_high_obj.presentValue = float(freq)
                motor_freq_low_obj.presentValue = float(freq)
                dev["LastEventState"].presentValue = int(shadow)
                dev["LastLogTs"].presentValue = int(epoch)
                dev["LastLogText"].presentValue = (
                    f"{danfoss_str} | Freq={freq} | SP={sp_val} | shadow={shadow} | epoch={epoch}"
                )

                # Let intrinsic event algorithm process presentValue change before reading eventState.
                await asyncio.sleep(0)
                high_loc = getattr(motor_freq_high_obj, "_local", motor_freq_high_obj)
                low_loc = getattr(motor_freq_low_obj, "_local", motor_freq_low_obj)
                high_state = getattr(high_loc, "eventState", None)
                low_state = getattr(low_loc, "eventState", None)

                if high_state == EventState.highLimit:
                    current_event_state = "high-limit"
                elif low_state == EventState.lowLimit:
                    current_event_state = "low-limit"
                else:
                    current_event_state = "normal"

                if current_event_state != last_event_state:
                    print(
                        f"[EVENT_STATE_CHANGE] Combined state: {last_event_state} -> {current_event_state} "
                        f"(high_obj={high_state}, low_obj={low_state})"
                    )
                    last_event_state = current_event_state

                print(
                    f"[DATA] {danfoss_str} | Freq={freq}Hz | SP={sp_val}Hz | "
                    f"State={current_event_state} | Shadow={shadow}"
                )
                await asyncio.sleep(UPDATE_SEC)
    except Exception as e:
        print(f"[MAIN][ERROR] {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    install_windows_reuse_port_workaround()
    install_timestamp_source_override()
    if os.name == "nt" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
