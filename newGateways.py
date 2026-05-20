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
import sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

# Use bacpypes3 for manual notification sending
from bacpypes3.argparse import ArgumentParser
from bacpypes3.basetypes import (
    Destination,
    EventState,
    EventType,
    EventTransitionBits,
    NotifyType,
    Recipient,
    TimeStamp,
)
from bacpypes3.debugging import ModuleLogger, bacpypes_debugging
from bacpypes3.app import Application
from bacpypes3.apdu import (
    SimpleAckPDU,
    ConfirmedEventNotificationRequest,
    ReadPropertyACK,
    ReadPropertyRequest,
    UnconfirmedEventNotificationRequest,
)
from bacpypes3.object import NotificationClassObject
from bacpypes3.constructeddata import ArrayOf
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import CharacterString, ObjectIdentifier, Boolean
from bacpypes3.errors import ExecutionError
import logging
from argparse import Namespace

# Silence bacpypes3 verbose logging unless specifically needed
logging.getLogger("bacpypes3").setLevel(logging.WARNING)


def install_windows_reuse_port_workaround() -> None:
    """
    Some Windows/Python combinations do not support reuse_port for UDP.
    If a library passes reuse_port=True, retry without it.
    """
    if os.name != "nt":
        return

    def patch_loop_class(loop_cls) -> bool:
        if loop_cls is None:
            return False
        original_create = getattr(loop_cls, "create_datagram_endpoint", None)
        if original_create is None or getattr(loop_cls, "_gw_reuse_port_patched", False):
            return False

        async def patched_create_datagram_endpoint(self, *args, **kwargs):
            # Force-disable reuse_port on Windows to avoid unsupported socket option.
            if "reuse_port" in kwargs:
                kwargs = dict(kwargs)
                kwargs.pop("reuse_port", None)
            try:
                return await original_create(self, *args, **kwargs)
            except ValueError as e:
                msg = str(e).lower()
                if "reuse_port" in msg:
                    kwargs = dict(kwargs)
                    kwargs.pop("reuse_port", None)
                    return await original_create(self, *args, **kwargs)
                raise

        setattr(loop_cls, "create_datagram_endpoint", patched_create_datagram_endpoint)
        setattr(loop_cls, "_gw_reuse_port_patched", True)
        return True

    patched = False
    for cls_name in ("BaseEventLoop", "SelectorEventLoop", "ProactorEventLoop"):
        patched = patch_loop_class(getattr(asyncio, cls_name, None)) or patched

    # Some Python builds expose additional loop classes under asyncio.base_events.
    try:
        from asyncio import base_events  # type: ignore

        patched = patch_loop_class(getattr(base_events, "BaseEventLoop", None)) or patched
    except Exception:
        pass

    if patched:
        print("[INIT] Installed Windows UDP reuse_port workaround.")

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
    "confirmed_notify": True,

    # Simulation range (when HTTP fail)
    "sim_low": 40.0,
    "sim_high": 52.0
}

def get_config_path() -> str:
    # In PyInstaller mode, prefer config next to executable so users can edit it.
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        exe_cfg = os.path.join(exe_dir, "config.json")
        if os.path.exists(exe_cfg):
            return exe_cfg

    # Source mode fallback
    src_cfg = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(src_cfg):
        return src_cfg

    # Last fallback: current working directory
    return os.path.join(os.getcwd(), "config.json")


CONFIG_PATH = get_config_path()


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
NOTIFICATION_CLASS_INSTANCE = 1


class YabeNotificationClassObject(NotificationClassObject):
    """
    YABE Notification Editor reads recipientList using array indexing.
    Override recipientList to ArrayOf so index-based reads succeed.
    """

    recipientList: ArrayOf(Destination)

    pass


class GatewayApplication(Application):
    async def do_ReadPropertyRequest(self, apdu: ReadPropertyRequest) -> None:
        """
        YABE reads NotificationClass.recipientList with array semantics.
        bacpypes3 models it as ListOf, so index reads fail by default.
        Provide an array-compatible shim for NC_1 recipientList.
        """
        if (
            int(apdu.objectIdentifier[1]) == int(NOTIFICATION_CLASS_INSTANCE)
            and "notification" in str(apdu.objectIdentifier[0]).lower()
            and str(apdu.propertyIdentifier) == "recipientList"
        ):
            obj = self.get_object_id(apdu.objectIdentifier)
            if not obj:
                raise ExecutionError(errorClass="object", errorCode="unknownObject")

            recipient_list = getattr(obj, "recipientList", []) or []
            idx = apdu.propertyArrayIndex
            if idx is None:
                value = recipient_list
            elif idx == 0:
                value = len(recipient_list)
            elif 1 <= idx <= len(recipient_list):
                value = recipient_list[idx - 1]
            else:
                raise ExecutionError(errorClass="property", errorCode="invalidArrayIndex")

            resp = ReadPropertyACK(
                objectIdentifier=apdu.objectIdentifier,
                propertyIdentifier=apdu.propertyIdentifier,
                propertyArrayIndex=apdu.propertyArrayIndex,
                propertyValue=value,
                context=apdu,
            )
            await self.response(resp)
            return

        await super().do_ReadPropertyRequest(apdu)


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
# Manual BACnet Alarm & Notification Sender using bacpypes3
# ============================================================

class BACnetAlarmSender:
    def __init__(self, local_device_id, local_address, recipient_ip, recipient_port, recipient_process_id, confirmed_notify):
        self.local_device_id = local_device_id
        self.local_device_identifier = ("device", int(local_device_id))
        self.recipient_address = Address(f"{recipient_ip}:{recipient_port}")
        self.recipient_process_id = recipient_process_id
        self.confirmed_notify = confirmed_notify

        # Build app with SimpleArgumentParser-equivalent args.
        # vendoridentifier=999 is the built-in vendor profile available in this bacpypes3 build.
        app_args = Namespace(
            name=DEVICE_NAME,
            instance=int(local_device_id),
            network=None,
            address=local_address,
            vendoridentifier=999,
            foreign=None,
            ttl=30,
            bbmd=None,
            route_aware=None,
            color=None,
            debug=None,
            loggers=False,
        )
        self.application = GatewayApplication.from_args(app_args)
        self._ensure_notification_class()
        
        # State tracking
        self.current_alarm_state = None # Can be 'low', 'high', 'normal'
        self.last_notified_state = None
        self.alarm_timestamp = None
        print(f"[ALARM_SENDER] Initialized for sending to {recipient_ip}:{recipient_port}, PID: {recipient_process_id}")

    def _ensure_notification_class(self):
        destination = Destination(
            validDays=[1, 1, 1, 1, 1, 1, 1],
            fromTime=(0, 0, 0, 0),
            toTime=(23, 59, 59, 99),
            # Use 'device' recipient for broad client UI compatibility (YABE notification editor).
            recipient=Recipient(device=("device", int(DEVICE_ID))),
            processIdentifier=int(self.recipient_process_id),
            issueConfirmedNotifications=bool(self.confirmed_notify),
            transitions=EventTransitionBits([1, 1, 1]),
        )

        nc_obj = YabeNotificationClassObject(
            objectIdentifier=("notificationClass", NOTIFICATION_CLASS_INSTANCE),
            objectName=f"NC_{NOTIFICATION_CLASS_INSTANCE}",
            notificationClass=NOTIFICATION_CLASS_INSTANCE,
            description="Gateway intrinsic alarm notification class",
            priority=[100, 100, 100],
            ackRequired=EventTransitionBits([0, 0, 0]),
            recipientList=[destination],
        )

        existing = None
        for obj in self.application.objectIdentifier.values():
            oid = getattr(obj, "objectIdentifier", None)
            if not oid:
                continue
            if int(oid[1]) == int(NOTIFICATION_CLASS_INSTANCE):
                if "notification" in str(oid[0]).lower():
                    existing = obj
                    break
        if existing is None:
            self.application.add_object(nc_obj)
            created = self.application.get_object_name(f"NC_{NOTIFICATION_CLASS_INSTANCE}")
            rl_len = len(getattr(created, "recipientList", []) or [])
            rl_type = created.__class__._elements.get("recipientList")
            print(
                f"[NC] Created NC_{NOTIFICATION_CLASS_INSTANCE} with recipient "
                f"{RECIPIENT_IP}:{RECIPIENT_PORT} (pid={self.recipient_process_id}), "
                f"recipientList length={rl_len}, type={rl_type}."
            )
        else:
            existing.notificationClass = NOTIFICATION_CLASS_INSTANCE
            existing.recipientList = [destination]
            existing.priority = [100, 100, 100]
            existing.ackRequired = EventTransitionBits([0, 0, 0])
            rl_len = len(getattr(existing, "recipientList", []) or [])
            print(
                f"[NC] Updated NC_{NOTIFICATION_CLASS_INSTANCE} recipientList -> "
                f"{RECIPIENT_IP}:{RECIPIENT_PORT} (pid={self.recipient_process_id}), "
                f"recipientList length={rl_len}."
            )

    def determine_state(self, value):
        if value < LOW_LIMIT:
            return 'low'
        elif value > HIGH_LIMIT:
            return 'high'
        else:
            return 'normal'

    async def send_notification_async(self, obj_id, new_state, value):
        """Asynchronously sends the EventNotificationRequest."""
        # Determine event type and state
        event_type = EventType.outOfRange
        event_state = EventState.normal if new_state == 'normal' else (EventState.lowLimit if new_state == 'low' else EventState.highLimit)
        from_state = (
            EventState.normal
            if self.last_notified_state == "normal"
            else EventState.lowLimit
            if self.last_notified_state == "low"
            else EventState.highLimit
            if self.last_notified_state == "high"
            else EventState.normal
        )
        
        # Get current time for the notification
        current_time = time.time()
        timestamp = TimeStamp.as_time(current_time)
        self.alarm_timestamp = timestamp # Store for potential future use

        # Determine priority based on state (Standard BACnet priorities: 1-255, lower is higher priority)
        # Using common values: Alarm -> 50, Normal -> 255
        priority_value = 50 if new_state != 'normal' else 255

        # Create the EventNotificationRequest
        request_cls = (
            ConfirmedEventNotificationRequest
            if self.confirmed_notify
            else UnconfirmedEventNotificationRequest
        )
        request = request_cls(
            processIdentifier=self.recipient_process_id,
            initiatingDeviceIdentifier=self.local_device_identifier,
            eventObjectIdentifier=obj_id,
            timeStamp=timestamp,
            notificationClass=NOTIFICATION_CLASS_INSTANCE,
            priority=priority_value,
            eventType=event_type,
            messageText=CharacterString(f"Alarm: {new_state.upper()} - Value: {value}"),
            notifyType=NotifyType.alarm,
            ackRequired=Boolean(False), # Typically not required for alarms
            fromState=from_state,
            toState=event_state,
        )
        
        # Set destination
        request.pduDestination = self.recipient_address

        print(f"[ALARM_SENDER] Sending {new_state.upper()} alarm notification for {obj_id} (Value: {value}) to {self.recipient_address}")

        # In bacpypes3, sending a request is often done by calling a method on the application
        # that corresponds to the service type. For unsolicited notifications like this,
        # we directly call the 'request' method on the application.
        # The application handles the routing and transmission.
        try:
            await self.application.request(request)
            self.last_notified_state = new_state
            print(f"[ALARM_SENDER] Notification for {obj_id} sent successfully.")
        except Exception as e:
            print(f"[ALARM_SENDER][ERROR] Failed to send notification for {obj_id}: {e}")


    def check_and_send_alarm(self, obj_id, current_value):
        def _log_task_result(task: asyncio.Task):
            try:
                task.result()
            except Exception as e:
                print(f"[ALARM_SENDER][ERROR] Notification task failed: {e}")

        new_state = self.determine_state(current_value)

        previous_state = self.current_alarm_state

        # First sample only initializes baseline; do not send notification on startup.
        if previous_state is None:
            self.current_alarm_state = new_state
            return True

        if new_state != previous_state:
            # State changed
            if new_state != 'normal' or previous_state != 'normal':
                # Send notification if moving to/from an alarm state
                # Because send_notification_async is an async function, we need to schedule it
                # within the main asyncio event loop. We can't simply call await here.
                # Instead, we create a task.
                task = asyncio.create_task(self.send_notification_async(obj_id, new_state, current_value))
                task.add_done_callback(_log_task_result)
                
            self.current_alarm_state = new_state
            return True # Indicate a change occurred
        return False # No change


# ============================================================
# Main
# ============================================================
async def main():
    print("Starting BACnet Server (Manual High/Low Limit Alarm + NotificationSender)...")

    ip, prefix = get_bind_ip_prefix()
    bac0_ip = f"{ip}/{prefix}"
    full_local_addr = f"{ip}:{LOCAL_PORT}"

    print(f"[NET] Binding BACnet server to: {full_local_addr}")
    print(f"[CFG] Device ID: {DEVICE_ID}, Name: {DEVICE_NAME}")
    print(f"[CFG] Alarm Limits: Low={LOW_LIMIT}, High={HIGH_LIMIT}")
    print(f"[CFG] Notification Recipient: {RECIPIENT_IP or 'NOT SET (Alarms will NOT be sent!)'}")

    if not RECIPIENT_IP:
        print("[ERROR] RECIPIENT_IP is not set in config.json. Cannot send notifications. Please configure it.")
        return

    # Initialize the manual alarm sender
    alarm_sender = BACnetAlarmSender(
        local_device_id=DEVICE_ID,
        local_address=full_local_addr,
        recipient_ip=RECIPIENT_IP,
        recipient_port=RECIPIENT_PORT,
        recipient_process_id=RECIPIENT_PROCESS_ID,
        confirmed_notify=CONFIRMED_NOTIFY
    )

    print(f"[BACNET] Device {DEVICE_ID} ({DEVICE_NAME}) initialized successfully (Manual Alarm Mode).")

    last_event_state = "unknown" # Track for logging changes
    sim_use_high = True
    print("\n[LOOP] Starting main loop...")

    try:
        while True:
            # Read setpoint (for display purposes, you might want to store this in a variable if used elsewhere)
            # For now, just assume it's 50.0 as per old code
            sp_val = 50.0 

            # Fetch data from Danfoss (or use simulation)
            freq, danfoss_dt = fetch_danfoss_value_and_time()
            if freq is None or danfoss_dt is None:
                if USE_SIMULATION_IF_HTTP_FAIL:
                    if sim_use_high:
                        sim_min = 50.01
                        sim_max = max(52.0, SIM_HIGH, sim_min + 0.01)
                        freq = round(random.uniform(sim_min, sim_max), 2)
                    else:
                        sim_max = 49.99
                        sim_min = min(SIM_LOW, 49.0)
                        freq = round(random.uniform(sim_min, sim_max), 2)
                    sim_use_high = not sim_use_high
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

            # --- ALARM LOGIC ---
            alarm_obj_id = ObjectIdentifier(('analogInput', 1)) # The object triggering the alarm
            alarm_changed = alarm_sender.check_and_send_alarm(alarm_obj_id, freq)
            current_alarm_state_str = alarm_sender.current_alarm_state or 'normal'
            
            # Log value and check for event state change
            if alarm_changed or current_alarm_state_str != last_event_state:
                print(f"[EVENT_STATE_CHANGE] Alarm state changed from '{last_event_state}' to '{current_alarm_state_str}'. Notification may have been sent.")
                last_event_state = current_alarm_state_str

            print(f"[DATA] {danfoss_str} | Freq={freq}Hz | SP={sp_val}Hz | AlarmState={current_alarm_state_str} | Shadow={shadow}")
            
            await asyncio.sleep(UPDATE_SEC)

    except KeyboardInterrupt:
        print("\n[MAIN_LOOP] Shutdown requested by user.")
    except Exception as e:
        print(f"[MAIN_LOOP][ERROR] An unexpected error occurred in the main loop: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n[MAIN_LOOP] Shutting down BACnet application...")
        try:
            alarm_sender.application.close()
        except Exception as e:
            print(f"[MAIN_LOOP][WARN] Error when closing BACnet application: {e}")


if __name__ == "__main__":
    install_windows_reuse_port_workaround()
    if os.name == "nt" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
