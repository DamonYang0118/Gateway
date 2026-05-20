import asyncio
import time
import random
import re
import socket
import subprocess
import ipaddress
import platform
import json
import os
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

import BAC0
from BAC0.core.devices.local import factory as F

# ============================================================
# Defaults (can be overridden by config.json)
# ============================================================
DEFAULTS = {
    "device_id": 2002,
    "device_name": "Danfoss_Gateway",
    "update_sec": 5,

    # Danfoss 850A HTTP XML
    "danfoss_xml_url": "http://127.0.0.1/status.xml",
    "http_timeout_sec": 3,
    "use_simulation_if_http_fail": True,

    # Intrinsic Alarm limits
    "low_limit": 41.0,
    "high_limit": 49.0,
    "deadband": 0.5,
    "time_delay_sec": 0,   # 0 = trigger immediately

    # BACnet local bind
    # bind_ip = "auto" means auto detect; can also set explicit ip e.g. "192.168.2.188"
    "bind_ip": "auto",
    "bind_prefix": 24,
    "local_port": 47808,   # BACnet/IP UDP port

    # NotificationClass / recipient
    # recipient_ip: where to send event notifications (optional); if empty -> do not set recipientList
    "recipient_ip": "", # <--- MUST BE SET IN config.json FOR ALARMS TO WORK WITH DESIGOCC
    "recipient_port": 47808,
    "recipient_process_id": 600,
    "confirmed_notify": False,

    # Simulation range (when HTTP fail)
    "sim_low": 40.0,
    "sim_high": 52.0
}

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except Exception as e:
            print(f"[CFG][WARN] Failed to read config.json: {e} (using defaults)")
    return cfg


CFG = load_config()

DEVICE_ID = int(CFG["device_id"])
DEVICE_NAME = str(CFG["device_name"])
UPDATE_SEC = int(CFG["update_sec"])

DANFOSS_XML_URL = str(CFG["danfoss_xml_url"])
HTTP_TIMEOUT_SEC = int(CFG["http_timeout_sec"])
USE_SIMULATION_IF_HTTP_FAIL = bool(CFG["use_simulation_if_http_fail"])

LOW_LIMIT = float(CFG["low_limit"])
HIGH_LIMIT = float(CFG["high_limit"])
DEADBAND = float(CFG["deadband"])
TIME_DELAY_SEC = int(CFG["time_delay_sec"])

LOCAL_PORT = int(CFG["local_port"])

RECIPIENT_IP = str(CFG["recipient_ip"]).strip()
RECIPIENT_PORT = int(CFG["recipient_port"])
RECIPIENT_PROCESS_ID = int(CFG["recipient_process_id"])
CONFIRMED_NOTIFY = bool(CFG["confirmed_notify"])

SIM_LOW = float(CFG["sim_low"])
SIM_HIGH = float(CFG["sim_high"])


# ============================================================
# BAC0 factory pick
# ============================================================
def pick(*names):
    for n in names:
        if hasattr(F, n):
            return getattr(F, n)
    raise ImportError(f"factory missing any of {names}")

analog_input = pick("analog_input")
analog_value = pick("analog_value")
character_string = pick("character_string")


def safe_add(obj, dev):
    """Attempt to add an object to the device. Return success status."""
    try:
        obj.add_objects_to_application(dev)
        return True
    except Exception as e:
        print(f"[OBJ][WARN] add_objects_to_application failed for {getattr(obj, 'name', obj)}: {e}")
        return False


# ============================================================
# Cross-platform bind IP/prefix
# ============================================================
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

def _win_ip_and_prefix_prefer_ethernet(preferred_keywords=("Ethernet", "以太网")):
    out = _run(["ipconfig", "/all"])
    blocks = re.split(r"\r?\n\r?\n", out)

    candidates = []
    for b in blocks:
        m_ip = re.search(r"IPv4 Address[.\s]*:\s*([0-9.]+)", b)
        if not m_ip:
            m_ip = re.search(r"IPv4 地址[.\s]*:\s*([0-9.]+)", b)
        if not m_ip:
            continue

        m_mask = re.search(r"Subnet Mask[.\s]*:\s*([0-9.]+)", b)
        if not m_mask:
            m_mask = re.search(r"子网掩码[.\s]*:\s*([0-9.]+)", b)

        ip = m_ip.group(1)
        mask = m_mask.group(1) if m_mask else "255.255.255.0"
        prefix = _prefix_from_netmask(mask)

        header = b.splitlines()[0] if b.splitlines() else ""
        candidates.append((header, ip, prefix))

    if not candidates:
        return _ip_by_socket(), 24

    for header, ip, prefix in candidates:
        if any(k.lower() in header.lower() for k in preferred_keywords):
            return ip, prefix

    return candidates[0][1], candidates[0][2]

def _mac_ip_and_prefix_prefer_en0():
    # prefer en0 (Wi-Fi)
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

    prefix = None
    try:
        mask = _run(["ipconfig", "getoption", iface, "subnet_mask"])
        if mask:
            prefix = _prefix_from_netmask(mask)
    except Exception:
        pass

    if prefix is None:
        # fallback from ifconfig hex netmask
        out = _run(["ifconfig", iface])
        m = re.search(r"netmask\s+0x([0-9a-fA-F]+)", out)
        if m:
            hexmask = int(m.group(1), 16)
            mask = ipaddress.IPv4Address(hexmask).__str__()
            prefix = _prefix_from_netmask(mask)
        else:
            prefix = 24

    return ip, prefix

def get_bind_ip_prefix():
    # 1) config override
    bind_ip = str(CFG.get("bind_ip", "auto")).strip()
    bind_prefix = int(CFG.get("bind_prefix", 24))

    if bind_ip and bind_ip.lower() != "auto":
        return bind_ip, bind_prefix

    # 2) environment override (optional)
    env_ip = os.getenv("BIND_IP")
    env_prefix = os.getenv("BIND_PREFIX")
    if env_ip:
        return env_ip, int(env_prefix or bind_prefix)

    sysname = platform.system().lower()
    if "windows" in sysname:
        return _win_ip_and_prefix_prefer_ethernet()
    if "darwin" in sysname:
        return _mac_ip_and_prefix_prefer_en0()

    return _ip_by_socket(), bind_prefix


# ============================================================
# Danfoss HTTP XML → (freq_value, danfoss_dt)
# ============================================================
def parse_danfoss_xml(xml_text: str):
    root = ET.fromstring(xml_text)

    def find_text_by_keywords(keywords):
        for elem in root.iter():
            tag = (elem.tag or "").lower()
            if any(k in tag for k in keywords) and (elem.text and elem.text.strip()):
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

    # keep as device time; if tz exists, convert to UTC then drop tzinfo for epoch convenience
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    freq = float(re.findall(r"[-+]?\d*\.?\d+", freq_text)[0])
    return freq, dt


def fetch_danfoss_value_and_time():
    try:
        req = Request(DANFOSS_XML_URL, headers={"User-Agent": "DanfossGateway/1.0"})
        with urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            xml_text = resp.read().decode("utf-8", errors="ignore")
        return parse_danfoss_xml(xml_text)
    except (HTTPError, URLError, TimeoutError, ValueError) as e:
        return None, None


# ============================================================
# Intrinsic Alarm setup
# ============================================================

def add_notification_class_to_bac0(dev, nc_obj):
    """
    Try multiple internal handles to add bacpypes3 object into the underlying BACnet app.

    BAC0 versions differ; we try:
      dev.this_application / dev.app / dev._app / dev.application ...
    and inside those: .app / .application / ._app ...
    then try: add_object / addObject / add / AddObject
    """
    holders = []
    for k in ("this_application", "app", "_app", "application", "_application"):
        v = getattr(dev, k, None)
        if v is not None:
            holders.append(v)

    candidates = []
    seen = set()

    def _push(x):
        if x is None:
            return
        if id(x) in seen:
            return
        seen.add(id(x))
        candidates.append(x)

    for h in holders:
        _push(h)
        for k in ("app", "application", "_app", "_application", "bacnet_app", "_bacnet_app"):
            _push(getattr(h, k, None))

    add_method_names = ("add_object", "addObject", "AddObject", "add")

    last_err = None
    for app in candidates:
        for m in add_method_names:
            fn = getattr(app, m, None)
            if callable(fn):
                try:
                    fn(nc_obj)
                    print(f"[ALARM] Successfully added NotificationClass using {type(app).__name__}.{m}")
                    return True # Success
                except Exception as e:
                    last_err = (type(app).__name__, m, repr(e))
                    print(f"[ALARM][DEBUG] Attempt {type(app).__name__}.{m} failed: {e}")

    raise RuntimeError(f"Cannot add NotificationClassObject into BAC0 application. Candidates tried: {len(candidates)}, Last error: {last_err}")

def setup_intrinsic_limit_alarm(dev, ai_name: str, notif_class_instance: int = 1):
    """
    Enable intrinsic High/Low limit alarm on AI object.

    - Create NotificationClassObject (optional recipientList)
    - Set AI properties: highLimit/lowLimit/deadband/limitEnable/eventEnable/notificationClass/notifyType
    """
    print(f"[ALARM] Enabling intrinsic limits on {ai_name} ...")

    # 1) Create NotificationClass: only if recipient_ip configured
    notification_class_created = False
    if RECIPIENT_IP:
        try:
            from bacpypes3.object import NotificationClassObject
            from bacpypes3.basetypes import (
                Destination,
                RecipientProcess,
                EventTransitionBits,
            )
            from bacpypes3.primitivedata import Unsigned, Boolean
            from bacpypes3.pdu import Address as PDUAddress

            print(f"[ALARM] Attempting to create NotificationClass={notif_class_instance} for recipient={RECIPIENT_IP}:{RECIPIENT_PORT}")

            # Note: bacpypes3 wants specific datatypes; keep it minimal
            nc_obj = NotificationClassObject(
                objectIdentifier=("notificationClass", int(notif_class_instance)),
                objectName=f"NC_{notif_class_instance}",
                description="Gateway Intrinsic Alarm Notification Class",
                priority=[Unsigned(100), Unsigned(100), Unsigned(100)], # Priorities for OffNormal, Fault, Normal
                ackRequired=EventTransitionBits([0, 0, 0]), # No need for ack for these events typically
            )

            # Destination (minimal)
            # recipient: Address must be bacpypes3.pdu.Address or basetypes.Address depending on build
            # safest: use PDUAddress("ip:port")
            rp = RecipientProcess(
                recipient=PDUAddress(f"{RECIPIENT_IP}:{RECIPIENT_PORT}"),
                processIdentifier=Unsigned(int(RECIPIENT_PROCESS_ID)),
            )

            dest = Destination(
                recipient=rp,
                processIdentifier=Unsigned(int(RECIPIENT_PROCESS_ID)),
                issueConfirmedNotifications=Boolean(bool(CONFIRMED_NOTIFY)),
                transitions=EventTransitionBits([1, 1, 1]), # All transitions (offnormal, fault, normal) trigger notification
            )

            # assign recipient list (field name differs across versions)
            if hasattr(nc_obj, "recipientList"):
                nc_obj.recipientList = [dest]
            elif hasattr(nc_obj, "recipient_list"):
                nc_obj.recipient_list = [dest]
            else:
                print("[ALARM][WARN] NotificationClassObject has no recipientList field in this bacpypes3 build")

            # Add NotificationClass object into BAC0 application FIRST
            add_notification_class_to_bac0(dev, nc_obj)
            
            # --- ADD A DELAY HERE ---
            print("[ALARM] Waiting briefly for NotificationClass registration...")
            time.sleep(0.5) # Wait half a second
            
            notification_class_created = True
            print(f"[ALARM] ✓ NotificationClass={notif_class_instance} created successfully. recipient={RECIPIENT_IP}:{RECIPIENT_PORT} pid={RECIPIENT_PROCESS_ID} confirmed={CONFIRMED_NOTIFY}")

        except Exception as e:
            # CRITICAL: Do NOT crash the gateway if NC fails; intrinsic alarm still works for polling/event-state
            print(f"[ALARM][ERROR] Failed to create NotificationClass! Alarms will not be sent to DesigoCC. Error: {e}")
            print(f"[ALARM][INFO] Intrinsic alarm logic on AI object will still work for local monitoring (eventState changes).")
            # If creation fails, we definitely didn't create it
            notification_class_created = False

    else:
        print("[ALARM][INFO] No recipient_ip specified in config. NotificationClass will NOT be created. Alarms will not be sent.")

    # 2) Enable intrinsic limits on AI local object
    ai = dev[ai_name]
    loc = getattr(ai, "_local", ai)

    def _set_if_has(obj, k, v):
        if hasattr(obj, k):
            try:
                setattr(obj, k, v)
                print(f"[ALARM] Set {k} = {v} on {ai_name}")
                return True
            except Exception as e:
                print(f"[ALARM][WARN] Failed to set {k} on {ai_name}: {e}")
        return False

    _set_if_has(loc, "highLimit", float(HIGH_LIMIT))
    _set_if_has(loc, "lowLimit", float(LOW_LIMIT))
    _set_if_has(loc, "deadband", float(DEADBAND))
    _set_if_has(loc, "timeDelay", int(TIME_DELAY_SEC))

    # limitEnable: [low, high]
    _set_if_has(loc, "limitEnable", [True, True])

    # eventEnable: [toOffnormal, toFault, toNormal] - Enable all transitions for limit alarms
    _set_if_has(loc, "eventEnable", [True, True, True]) # Changed from [True, False, True] to enable fault->normal transition too

    # --- FIX: Only set notificationClass if it was actually created ---
    if notification_class_created:
        try:
            _set_if_has(loc, "notificationClass", int(notif_class_instance))
            print(f"[ALARM] Set notificationClass to {int(notif_class_instance)} for sending alarms.")
        except RuntimeError as e:
            if "notification class object" in str(e) and "not found" in str(e):
                 print(f"[ALARM][ERROR] Setting notificationClass failed due to: {e}. This indicates the NotificationClass object might not be fully registered within the BACnet stack at this point. Local eventState will still update, but external notifications will likely fail.")
                 print(f"[ALARM][INFO] Continuing without active notificationClass link. Consider if target system ({RECIPIENT_IP}) is reachable and configured correctly.")
            else:
                print(f"[ALARM][ERROR] Unexpected error while setting notificationClass: {e}")
    else:
        print("[ALARM] Skipped setting notificationClass because NotificationClass was not created (no recipient or creation failed). Alarms will not be sent, but local eventState will update.")
    # --- END FIX ---

    # notifyType can be enum or string depending on build; try string then fallback
    if not _set_if_has(loc, "notifyType", "alarm"):
        _set_if_has(loc, "notifyType", 0)

    if notification_class_created:
        print(f"[ALARM] ✓ Intrinsic limits enabled on {ai_name}. Notifications SHOULD be sent to DesigoCC when triggered.")
    else:
        print(f"[ALARM] ! Intrinsic limits enabled on {ai_name}. However, notifications will NOT be sent (NotificationClass inactive). Local eventState will still update.")
    print(f"[ALARM] Limits: Low={LOW_LIMIT}, High={HIGH_LIMIT}, Deadband={DEADBAND}, TimeDelay={TIME_DELAY_SEC}")


# ============================================================
# Main
# ============================================================
async def main():
    print("Starting BACnet Server (Intrinsic High/Low Limit Alarm + NotificationClass)...")
    BAC0.log_level("error") # Reduce BAC0 verbose logging, keep critical errors

    ip, prefix = get_bind_ip_prefix()
    bac0_ip = f"{ip}/{prefix}"

    print(f"[NET] Binding BACnet server to: {bac0_ip} UDP Port: {LOCAL_PORT}")
    print(f"[CFG] Device ID: {DEVICE_ID}, Name: {DEVICE_NAME}")
    print(f"[CFG] Alarm Limits: Low={LOW_LIMIT}, High={HIGH_LIMIT}")
    print(f"[CFG] Notification Recipient: {RECIPIENT_IP or 'NOT SET (Alarms will NOT be sent!)'}")

    # Attempt to start BAC0 with the specified port
    bac0_instance = None
    try:
        bac0_instance = BAC0.lite(ip=bac0_ip, deviceId=DEVICE_ID, localObjName=DEVICE_NAME, port=LOCAL_PORT)
    except TypeError:
        print(f"[NET][WARN] Port argument not supported by this BAC0 version, trying default port...")
        try:
            bac0_instance = BAC0.lite(ip=bac0_ip, deviceId=DEVICE_ID, localObjName=DEVICE_NAME)
        except Exception as e:
            print(f"[NET][ERROR] Failed to initialize BAC0 even with default port: {e}")
            return
    except Exception as e:
        print(f"[NET][ERROR] Failed to initialize BAC0 with {bac0_ip}:{LOCAL_PORT}: {e}")
        return

    try:
        async with bac0_instance as dev:
            print(f"[BAC0] BACnet device {DEVICE_ID} ({DEVICE_NAME}) started successfully.")

            # Define object definitions (name and type) for logging
            obj_definitions = [
                # Add NotificationClass FIRST if needed
                {
                    "factory": analog_input,
                    "args": {
                        "instance": 1,
                        "name": "MotorFreq",
                        "description": "Danfoss actual frequency (Intrinsic Alarm enabled)",
                        # Avoid early lookup of NC=1 before NotificationClass object is created.
                        "properties": {
                            "units": "hertz",
                            "covIncrement": 0.15,
                            "notificationClass": 0,
                        },
                    },
                },
                {"factory": analog_value, "args": {"instance": 1, "name": "Setpoint", "description": "BMS setpoint (writable)", "units": "hertz", "cov_increment": 0.1, "initial_value": 50.0}},
                {"factory": analog_value, "args": {"instance": 2, "name": "LastEventState", "description": "shadow state (0 normal / 1 high / 2 low)", "units": "noUnits", "cov_increment": 1, "initial_value": 0}},
                {"factory": analog_value, "args": {"instance": 3, "name": "LastLogTs", "description": "epoch seconds from Danfoss time", "units": "noUnits", "cov_increment": 1, "initial_value": int(time.time())}},
                {"factory": character_string, "args": {"instance": 1, "name": "LastLogText", "description": "shadow text", "initial_value": "(init)"}},
            ]

            # Add objects to device
            for obj_def in obj_definitions:
                obj_factory = obj_def["factory"]
                obj_args = obj_def["args"]
                obj_name = obj_args["name"]

                obj_instance = obj_factory(**obj_args)
                
                if safe_add(obj_instance, dev):
                    print(f"[OBJ] Added object: {obj_name}")
                else:
                    print(f"[OBJ][INFO] Object {obj_name} already existed or failed to add.")

            print("\n[BAC0] Objects ready:")
            print("  - AI:1 MotorFreq (with intrinsic alarm)")
            print("  - AV:1 Setpoint")
            print("  - AV:2 LastEventState")
            print("  - AV:3 LastLogTs")
            print("  - CSV:1 LastLogText\n")

            # Enable intrinsic alarm - This is the critical step for alarms
            # The NotificationClass will be created inside this function, potentially before MotorFreq init completes fully
            setup_intrinsic_limit_alarm(dev, "MotorFreq", notif_class_instance=1)

            last_event_state = "unknown" # Track for logging changes
            print("\n[LOOP] Starting main loop...")

            while True:
                # Read setpoint (for display purposes)
                try:
                    sp_val = float(dev["Setpoint"].presentValue)
                except Exception:
                    sp_val = 50.0

                # Fetch data from Danfoss (or use simulation)
                freq, danfoss_dt = fetch_danfoss_value_and_time()
                if freq is None or danfoss_dt is None:
                    if USE_SIMULATION_IF_HTTP_FAIL:
                        freq = round(random.uniform(SIM_LOW, SIM_HIGH), 2)
                        danfoss_dt = datetime.now()
                        print(f"[HTTP][WARN] Fetch failed, using simulation: {freq} Hz")
                    else:
                        print("[HTTP][ERROR] Danfoss HTTP XML fetch failed; skipping update this cycle.")
                        await asyncio.sleep(UPDATE_SEC)
                        continue

                # Determine shadow state based on limits
                shadow = 1 if freq > HIGH_LIMIT else (2 if freq < LOW_LIMIT else 0)

                # Calculate epoch timestamp
                try:
                    epoch = int(danfoss_dt.timestamp())
                except Exception:
                    epoch = int(time.time())

                danfoss_str = danfoss_dt.strftime("%Y-%m-%d %H:%M:%S")

                # Update BACnet objects
                dev["MotorFreq"].presentValue = float(freq)
                dev["LastEventState"].presentValue = int(shadow)
                dev["LastLogTs"].presentValue = int(epoch)
                dev["LastLogText"].presentValue = f"{danfoss_str} | Freq={freq} | SP={sp_val} | shadow={shadow} | epoch={epoch}"

                # Log value and check for event state change
                current_event_state = dev["MotorFreq"].eventState
                if current_event_state != last_event_state:
                    print(f"[EVENT_STATE_CHANGE] MotorFreq.eventState changed from '{last_event_state}' to '{current_event_state}'. Alarm may have triggered.")
                    last_event_state = current_event_state

                print(f"[DATA] {danfoss_str} | Freq={freq}Hz | SP={sp_val}Hz | State={current_event_state} | Shadow={shadow}")
                
                await asyncio.sleep(UPDATE_SEC)

    except Exception as e:
        print(f"[MAIN_LOOP][ERROR] An unexpected error occurred in the main loop: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
