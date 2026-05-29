from __future__ import annotations

import base64
import gzip
import re
import ssl
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class DanfossApiError(RuntimeError):
    """Raised when the 850A XML endpoint cannot return usable data."""


@dataclass(frozen=True)
class DanfossPointSpec:
    key: str
    nodetype: int = 16
    node: int = 0
    cid: Optional[int] = None
    vid: Optional[int] = None
    tag: Optional[str] = None
    field: Optional[str] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "DanfossPointSpec":
        tag = _optional_str(raw.get("tag"))
        cid = _optional_int(raw.get("cid"))
        vid = _optional_int(raw.get("vid"))
        key = _optional_str(raw.get("key")) or _optional_str(raw.get("name")) or tag
        if not key:
            key = f"node{raw.get('node', 0)}_{tag or f'cid{cid}_vid{vid}'}"
        return cls(
            key=key,
            nodetype=int(raw.get("nodetype", 16)),
            node=int(raw.get("node", 0)),
            cid=cid,
            vid=vid,
            tag=tag,
            field=_optional_str(raw.get("field")),
        )

    def validate(self) -> None:
        if not self.tag and (self.cid is None or self.vid is None):
            raise DanfossApiError(
                f"Point '{self.key}' must define either tag or both cid and vid."
            )


@dataclass
class DanfossPointValue:
    key: str
    value: Optional[float]
    raw_value: str
    timestamp: datetime
    display: str = ""
    name: str = ""
    status: str = ""
    status_code: Optional[int] = None
    pending: bool = False
    error: Optional[int] = None


@dataclass
class DanfossParameterInfo:
    cid: Optional[int]
    vid: Optional[int]
    name: str = ""
    unit: str = ""
    rw: str = ""
    raw: Optional[dict] = None


@dataclass
class DanfossHistoryRecord:
    timestamp: Optional[datetime]
    value: Optional[float]
    raw_value: str
    unit: str = ""


@dataclass
class DanfossAlarmRef:
    state: str
    alarm_id: str
    name: str = ""
    time: str = ""
    device: str = ""
    description: str = ""


@dataclass
class DanfossDeviceAlarms:
    active: List[DanfossAlarmRef]
    acked: List[DanfossAlarmRef]
    cleared: List[DanfossAlarmRef]
    newest_time: str = ""
    oldest_time: str = ""


@dataclass
class DanfossHistoryStatus:
    query_id: int
    status: str
    field_count: int = 0
    field_size: int = 0
    exp: int = 0
    unit: str = ""
    actual_sample_rate: int = 0
    start_epoch: Optional[int] = None
    stop_epoch: Optional[int] = None


@dataclass
class DanfossHistorySample:
    index: int
    status_code: int
    value: Optional[float]
    epoch: Optional[int] = None


class Danfoss850AClient:
    def __init__(
        self,
        endpoint_url: str,
        username: str = "",
        password: str = "",
        auth_mode: str = "basic_header",
        timeout_sec: int = 3,
        units: str = "s",
        lang: str = "e",
        min_interval_sec: float = 4.0,
        verify_tls: bool = True,
        ca_cert: str = "",
        content_type: str = "text/xml",
    ) -> None:
        if not endpoint_url:
            raise DanfossApiError("Danfoss endpoint_url is empty.")
        self.endpoint_url = endpoint_url
        self.username = username
        self.password = password
        self.auth_mode = (auth_mode or "none").lower()
        self.timeout_sec = int(timeout_sec)
        self.units = units
        self.lang = lang
        self.min_interval_sec = float(min_interval_sec or 0)
        self.verify_tls = bool(verify_tls)
        self.ca_cert = ca_cert
        self.content_type = content_type or "text/xml"
        self.session_token: Optional[str] = None
        self._last_request_at = 0.0
        self._ssl_context = self._build_ssl_context()

    def read_values(
        self, points: Iterable[DanfossPointSpec]
    ) -> Dict[str, DanfossPointValue]:
        point_list = list(points)
        for point in point_list:
            point.validate()
        xml_text = build_read_val_command(
            point_list,
            units=self.units,
            lang=self.lang,
            num_only=True,
            valid_only=True,
            compress=False,
        )
        response_text = self.post_xml(xml_text)
        return parse_read_val_response(response_text, point_list, timestamp=datetime.now())

    def read_date_time(self) -> datetime:
        attrs = {"action": "read_date_time", "compress": "0"}
        if self.lang:
            attrs["lang"] = self.lang
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "read_date_time")
        epoch = _optional_int(_find_child_text(root, "epoch"))
        if epoch is not None:
            return datetime.fromtimestamp(epoch)
        parts = {
            "year": _optional_int(_find_child_text(root, "year")),
            "month": _optional_int(_find_child_text(root, "month")),
            "day": _optional_int(_find_child_text(root, "day")),
            "hour": _optional_int(_find_child_text(root, "hour")) or 0,
            "minute": _optional_int(_find_child_text(root, "minute")) or 0,
            "second": _optional_int(_find_child_text(root, "second")) or 0,
        }
        if parts["year"] is not None and parts["year"] < 100:
            parts["year"] += 2000
        return datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts["hour"]),
            int(parts["minute"]),
            int(parts["second"]),
        )

    def read_devices(self) -> List[dict]:
        response_text = self.post_xml(_xml_empty("cmd", _common_attrs("read_devices", self.lang)))
        root = _parse_xml_response(response_text, "read_devices")
        devices = []
        for elem in root.findall("device"):
            item = dict(elem.attrib)
            for child in list(elem):
                item[_local_name(child.tag)] = (child.text or "").strip()
            devices.append(item)
        return devices

    def read_parm_info(self, device_id: str) -> List[DanfossParameterInfo]:
        attrs = _common_attrs("read_parm_info", self.lang)
        attrs["device_id"] = device_id
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "read_parm_info")
        parms_root = root.find("parms")
        parms = parms_root.findall("parm") if parms_root is not None else root.findall("parm")
        result = []
        for parm in parms:
            raw = dict(parm.attrib)
            result.append(
                DanfossParameterInfo(
                    cid=_optional_int(raw.get("cid")),
                    vid=_optional_int(raw.get("vid")),
                    name=raw.get("name", ""),
                    unit=raw.get("unit", ""),
                    rw=raw.get("rw", ""),
                    raw=raw,
                )
            )
        return result

    def read_device_history_cfg(self, node: int, nodetype: int = 16) -> List[dict]:
        attrs = _common_attrs("read_device_history_cfg", self.lang)
        attrs.update({"nodetype": str(nodetype), "node": str(node)})
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "read_device_history_cfg")
        configs = []
        for elem in root.findall("history_cfg"):
            item = {}
            for child in list(elem):
                item[_local_name(child.tag)] = (child.text or "").strip()
            configs.append(item)
        return configs

    def read_history(
        self,
        nodetype: int,
        node: int,
        cid: Optional[int] = None,
        vid: Optional[int] = None,
        mod: Optional[int] = None,
        point: Optional[int] = None,
        start: Optional[Union[datetime, int, float]] = None,
        stop: Optional[Union[datetime, int, float]] = None,
        sample_rate: int = 30,
        units: Optional[str] = None,
    ) -> List[DanfossHistoryRecord]:
        attrs = _common_attrs("read_history", self.lang)
        attrs.update(
            {
                "nodetype": str(nodetype),
                "node": str(node),
                "sample_rate": str(sample_rate),
                "units": units or self.units,
            }
        )
        if cid is not None:
            attrs["cid"] = str(cid)
        if vid is not None:
            attrs["vid"] = str(vid)
        if mod is not None:
            attrs["mod"] = str(mod)
        if point is not None:
            attrs["point"] = str(point)

        cmd = ET.Element("cmd", attrs)
        if start is not None:
            _append_time_element(cmd, "starttime", start)
        if stop is not None:
            _append_time_element(cmd, "stoptime", stop)

        response_text = self.post_xml(ET.tostring(cmd, encoding="unicode", short_empty_elements=True))
        return parse_read_history_response(response_text)

    def read_device_alarms(
        self,
        nodetype: int,
        node: int,
        mod: int = 0,
        point: int = 0,
    ) -> DanfossDeviceAlarms:
        attrs = _common_attrs("read_device_alarms", self.lang)
        attrs.update(
            {
                "nodetype": str(nodetype),
                "node": str(node),
                "mod": str(mod),
                "point": str(point),
            }
        )
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "read_device_alarms")
        return parse_device_alarms_response(root)

    def read_generic_alarms(
        self,
        nodetype: int = 16,
        node: int = 0,
        index: int = 0,
        count: int = 100,
    ) -> List[DanfossAlarmRef]:
        attrs = _common_attrs("read_generic_alarms", self.lang)
        attrs.update(
            {
                "nodetype": str(nodetype),
                "node": str(node),
                "index": str(index),
                "count": str(count),
            }
        )
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "read_generic_alarms")
        alarms = []
        for alarm in root.findall(".//alarm"):
            alarms.append(
                DanfossAlarmRef(
                    state=alarm.get("state", ""),
                    alarm_id=alarm.get("id", "") or (alarm.text or "").strip(),
                    name=alarm.get("name", ""),
                    time=_find_child_text(alarm, "time"),
                    device=_find_child_text(alarm, "device"),
                    description=_find_child_text(alarm, "description"),
                )
            )
        return alarms

    def start_history_query(
        self,
        hist_index: int,
        sample_rate: int,
        start_epoch: int,
        stop_epoch: int,
        units: Optional[str] = None,
        averaged_over: Optional[int] = None,
    ) -> int:
        attrs = _common_attrs("start_history_query", self.lang)
        attrs.update(
            {
                "hist_index": str(hist_index),
                "sample_rate": str(sample_rate),
                "units": units or self.units,
                "start_epoch": str(start_epoch),
                "stop_epoch": str(stop_epoch),
            }
        )
        if averaged_over is not None:
            attrs["averaged_over"] = str(averaged_over)
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "start_history_query")
        query_id = _optional_int(_find_child_text(root, "query_id"))
        if query_id is None:
            raise DanfossApiError("start_history_query response did not include query_id.")
        return query_id

    def read_query_status(self, query_id: int) -> DanfossHistoryStatus:
        attrs = _common_attrs("read_query_status", self.lang)
        attrs["query_id"] = str(query_id)
        response_text = self.post_xml(_xml_empty("cmd", attrs))
        root = _parse_xml_response(response_text, "read_query_status")
        start_epoch = _optional_int(_find_nested_text(root, "starttime", "epoch"))
        stop_epoch = _optional_int(_find_nested_text(root, "stoptime", "epoch"))
        return DanfossHistoryStatus(
            query_id=query_id,
            status=_find_child_text(root, "status") or "",
            field_count=_optional_int(_find_child_text(root, "field_count")) or 0,
            field_size=_optional_int(_find_child_text(root, "field_size")) or 0,
            exp=_optional_int(_find_child_text(root, "exp")) or 0,
            unit=_find_child_text(root, "unit") or "",
            actual_sample_rate=_optional_int(_find_child_text(root, "actual_sample_rate")) or 0,
            start_epoch=start_epoch,
            stop_epoch=stop_epoch,
        )

    def read_query_data(
        self,
        status: DanfossHistoryStatus,
    ) -> List[DanfossHistorySample]:
        attrs = _common_attrs("read_query_data", self.lang)
        attrs["query_id"] = str(status.query_id)
        raw_data = self.post_xml(_xml_empty("cmd", attrs), binary=True)
        return decode_query_data(
            raw_data,
            status.query_id,
            status.field_count,
            status.field_size,
            status.exp,
            start_epoch=status.start_epoch,
            sample_rate=status.actual_sample_rate,
        )

    def post_xml(self, xml_text: str, binary: bool = False):
        if self.auth_mode in ("session", "session_token") and not self.session_token:
            self.authenticate()
        xml_text = self._with_cmd_credentials(xml_text)
        try:
            return self._post(xml_text, include_auth=True, binary=binary)
        except (HTTPError, DanfossApiError) as exc:
            if (
                self.auth_mode in ("session", "session_token")
                and isinstance(exc, HTTPError)
                and exc.code in (401, 403)
            ):
                self.session_token = None
                self.authenticate()
                return self._post(xml_text, include_auth=True, binary=binary)
            raise

    def authenticate(self) -> None:
        if not self.username or not self.password:
            raise DanfossApiError("Session authentication requires username and password.")
        attrs = {
            "action": "getauth",
            "user": self.username,
            "password": self.password,
            "compress": "0",
        }
        response_text = self._post(_xml_empty("cmd", attrs), include_auth=False, binary=False)
        root = _parse_xml_response(response_text, "getauth")
        token = _find_child_text(root, "session_token")
        if not token:
            raise DanfossApiError("getauth succeeded but no session_token was returned.")
        self.session_token = token

    def _post(self, xml_text: str, include_auth: bool, binary: bool):
        self._throttle()
        headers = {
            "Content-Type": self.content_type,
            "Accept": "text/xml, */*",
            "Accept-Encoding": "identity",
            "User-Agent": "DanfossGateway/1.0",
            "Connection": "close",
        }
        if include_auth:
            auth_header = self._auth_header()
            if auth_header:
                headers["AKSM-Auth"] = auth_header
        payload = xml_text.encode("utf-8")
        req = Request(self.endpoint_url, data=payload, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self.timeout_sec, context=self._ssl_context) as resp:
                raw = resp.read()
                raw = _maybe_decompress(raw, resp.headers.get("Content-Encoding", ""))
                return raw if binary else raw.decode("utf-8", errors="ignore")
        except HTTPError:
            raise
        except URLError as exc:
            raise DanfossApiError(f"Danfoss XML request failed: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()

    def _auth_header(self) -> str:
        if self.auth_mode in ("none", "disabled", ""):
            return ""
        if self.auth_mode in ("cmd_credentials", "cmd", "xml_credentials"):
            return ""
        if self.auth_mode in ("basic", "basic_header", "header"):
            if not self.username or not self.password:
                return ""
            token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode(
                "ascii"
            )
            return f"Basic {token}"
        if self.auth_mode in ("aksm_plain_header", "plain_header", "strict_header"):
            if not self.username or not self.password:
                return ""
            return f"{self.username}:{self.password}"
        if self.auth_mode in ("session", "session_token"):
            return self.session_token or ""
        raise DanfossApiError(f"Unsupported Danfoss auth_mode: {self.auth_mode}")

    def _with_cmd_credentials(self, xml_text: str) -> str:
        if self.auth_mode not in ("cmd_credentials", "cmd", "xml_credentials"):
            return xml_text
        if not self.username or not self.password:
            return xml_text
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return xml_text
        if _local_name(root.tag) != "cmd":
            return xml_text
        root.set("user", self.username)
        root.set("password", self.password)
        return ET.tostring(root, encoding="unicode", short_empty_elements=True)

    def _build_ssl_context(self):
        if not self.endpoint_url.lower().startswith("https://"):
            return None
        if not self.verify_tls:
            return ssl._create_unverified_context()
        if self.ca_cert:
            return ssl.create_default_context(cafile=self.ca_cert)
        return ssl.create_default_context()

    def _throttle(self) -> None:
        if self.min_interval_sec <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait_sec = self.min_interval_sec - elapsed
        if wait_sec > 0:
            time.sleep(wait_sec)


def build_read_val_command(
    points: Iterable[DanfossPointSpec],
    units: str = "s",
    lang: str = "e",
    num_only: bool = True,
    valid_only: bool = True,
    compress: bool = False,
) -> str:
    attrs = {
        "action": "read_val",
        "num_only": "1" if num_only else "0",
        "valid_only": "1" if valid_only else "0",
        "units": units,
        "compress": "1" if compress else "0",
    }
    if lang:
        attrs["lang"] = lang
    cmd = ET.Element("cmd", attrs)
    for point in points:
        point.validate()
        val_attrs = {
            "nodetype": str(point.nodetype),
            "node": str(point.node),
        }
        if point.tag:
            val_attrs["tag"] = point.tag
        else:
            val_attrs["cid"] = str(point.cid)
            val_attrs["vid"] = str(point.vid)
        if point.field:
            val_attrs["field"] = point.field
        ET.SubElement(cmd, "val", val_attrs)
    return ET.tostring(cmd, encoding="unicode", short_empty_elements=True)


def parse_read_val_response(
    xml_text: str,
    points: Optional[Iterable[DanfossPointSpec]] = None,
    timestamp: Optional[datetime] = None,
) -> Dict[str, DanfossPointValue]:
    root = _parse_xml_response(xml_text, "read_val")
    point_list = list(points or [])
    timestamp = timestamp or datetime.now()
    values: Dict[str, DanfossPointValue] = {}
    for index, elem in enumerate(root.findall("val")):
        spec = _match_spec(elem, point_list, index)
        key = spec.key if spec else _response_key(elem, index)
        raw_value = _value_text(elem)
        point_error = _optional_int(elem.attrib.get("error"))
        values[key] = DanfossPointValue(
            key=key,
            value=_numeric_value(raw_value),
            raw_value=raw_value,
            timestamp=timestamp,
            display=elem.attrib.get("display", ""),
            name=elem.attrib.get("name", ""),
            status=elem.attrib.get("stat", ""),
            status_code=_optional_int(elem.attrib.get("statcode")),
            pending=str(elem.attrib.get("pending", "")).lower() == "true",
            error=point_error,
        )
    return values


def parse_read_history_response(xml_text: str) -> List[DanfossHistoryRecord]:
    root = _parse_xml_response(xml_text, "read_history")
    unit = _find_child_text(root, "unit")
    records: List[DanfossHistoryRecord] = []

    start_epoch = _optional_int(_find_nested_text(root, "starttime", "epoch"))
    sample_rate = _optional_int(root.attrib.get("real_sample_rate")) or _optional_int(
        root.attrib.get("sample_rate")
    )
    for index, elem in enumerate(root.findall("data/y")):
        raw_value = (elem.text or "").strip()
        if raw_value in ("", "-----", "*****", "----", "*"):
            continue
        timestamp = None
        if start_epoch is not None and sample_rate:
            timestamp = datetime.fromtimestamp(start_epoch + index * sample_rate)
        records.append(
            DanfossHistoryRecord(
                timestamp=timestamp,
                value=_numeric_value(raw_value),
                raw_value=raw_value,
                unit=unit,
            )
        )

    for elem in root.findall("record"):
        raw_value = _find_child_text(elem, "value")
        if raw_value in ("", "-----", "*****", "----", "*"):
            continue
        timestamp = _parse_timestamp_text(_find_child_text(elem, "time"))
        records.append(
            DanfossHistoryRecord(
                timestamp=timestamp,
                value=_numeric_value(raw_value),
                raw_value=raw_value,
                unit=_find_child_text(elem, "unit") or unit,
            )
        )
    return records


def parse_device_alarms_response(root: ET.Element) -> DanfossDeviceAlarms:
    return DanfossDeviceAlarms(
        active=_parse_alarm_refs(root, "active"),
        acked=_parse_alarm_refs(root, "acked"),
        cleared=_parse_alarm_refs(root, "cleared"),
        newest_time=_find_nested_text(root, "newest", "time"),
        oldest_time=_find_nested_text(root, "oldest", "time"),
    )


def decode_query_data(
    raw_data: bytes,
    query_id: int,
    field_count: int,
    field_size: int,
    exp: int,
    start_epoch: Optional[int] = None,
    sample_rate: Optional[int] = None,
) -> List[DanfossHistorySample]:
    if field_count <= 0 or field_size <= 1:
        return []
    if len(raw_data) < 4:
        raise DanfossApiError("History binary payload is shorter than the query id header.")
    returned_query_id = int.from_bytes(raw_data[:4], "big", signed=False)
    if returned_query_id != int(query_id):
        raise DanfossApiError(
            f"History payload query_id mismatch: expected {query_id}, got {returned_query_id}."
        )
    samples = []
    offset = 4
    multiplier = 10 ** int(exp)
    for index in range(field_count):
        field = raw_data[offset : offset + field_size]
        if len(field) < field_size:
            break
        status_code = field[0]
        raw_int = int.from_bytes(field[1:], "big", signed=True)
        value = raw_int * multiplier if status_code in (0x00, 0x06) else None
        epoch = None
        if start_epoch is not None and sample_rate:
            epoch = int(start_epoch) + index * int(sample_rate)
        samples.append(
            DanfossHistorySample(
                index=index,
                status_code=status_code,
                value=value,
                epoch=epoch,
            )
        )
        offset += field_size
    return samples


def _parse_xml_response(xml_text: str, expected_action: str) -> ET.Element:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise DanfossApiError(f"Invalid XML response for {expected_action}: {exc}") from exc
    error = root.attrib.get("error")
    if error not in (None, "", "0"):
        raise DanfossApiError(f"{expected_action} returned error={error}: {_short_text(root)}")
    return root


def _common_attrs(action: str, lang: str = "e") -> Dict[str, str]:
    attrs = {"action": action, "compress": "0"}
    if lang:
        attrs["lang"] = lang
    return attrs


def _append_time_element(
    parent: ET.Element,
    tag: str,
    value: Union[datetime, int, float],
) -> None:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromtimestamp(float(value))
    child = ET.SubElement(parent, tag)
    ET.SubElement(child, "year").text = dt.strftime("%y")
    ET.SubElement(child, "month").text = dt.strftime("%m")
    ET.SubElement(child, "day").text = dt.strftime("%d")
    ET.SubElement(child, "hour").text = dt.strftime("%H")
    ET.SubElement(child, "minute").text = dt.strftime("%M")
    ET.SubElement(child, "second").text = dt.strftime("%S")


def _xml_empty(tag: str, attrs: Mapping[str, object]) -> str:
    elem = ET.Element(tag, {str(k): str(v) for k, v in attrs.items() if v is not None})
    return ET.tostring(elem, encoding="unicode", short_empty_elements=True)


def _match_spec(
    elem: ET.Element, specs: List[DanfossPointSpec], index: int
) -> Optional[DanfossPointSpec]:
    for spec in specs:
        if spec.tag and elem.attrib.get("tag") == spec.tag:
            return spec
        if (
            not spec.tag
            and _optional_int(elem.attrib.get("nodetype")) == spec.nodetype
            and _optional_int(elem.attrib.get("node")) == spec.node
            and _optional_int(elem.attrib.get("cid")) == spec.cid
            and _optional_int(elem.attrib.get("vid")) == spec.vid
        ):
            return spec
    if index < len(specs):
        return specs[index]
    return None


def _response_key(elem: ET.Element, index: int) -> str:
    if elem.attrib.get("tag"):
        return elem.attrib["tag"]
    parts = [
        f"node{elem.attrib.get('node', 'x')}",
        f"cid{elem.attrib.get('cid', 'x')}",
        f"vid{elem.attrib.get('vid', 'x')}",
    ]
    if elem.attrib.get("field"):
        parts.append(f"field{elem.attrib['field']}")
    return "_".join(parts) if any("x" not in part for part in parts) else f"value_{index}"


def _value_text(elem: ET.Element) -> str:
    value_child = elem.find("value")
    if value_child is not None and value_child.text:
        return value_child.text.strip()
    return (elem.text or "").strip()


def _numeric_value(raw_value: str) -> Optional[float]:
    if not raw_value:
        return None
    if raw_value.strip() in ("*", "----"):
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", raw_value.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _parse_alarm_refs(root: ET.Element, state: str) -> List[DanfossAlarmRef]:
    parent = root.find(state)
    if parent is None:
        return []
    refs = []
    for ref in parent.findall("ref"):
        refs.append(
            DanfossAlarmRef(
                state=state,
                alarm_id=(ref.text or "").strip(),
                name=ref.attrib.get("name", ""),
            )
        )
    return refs


def _parse_timestamp_text(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%y %H:%M:%S", "%I:%M%p %m/%d/%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _find_child_text(root: ET.Element, child_name: str) -> str:
    elem = root.find(child_name)
    return (elem.text or "").strip() if elem is not None and elem.text else ""


def _find_nested_text(root: ET.Element, parent_name: str, child_name: str) -> str:
    parent = root.find(parent_name)
    if parent is None:
        return ""
    return _find_child_text(parent, child_name)


def _maybe_decompress(raw: bytes, content_encoding: str) -> bytes:
    encoding = (content_encoding or "").lower()
    if "gzip" in encoding or raw.startswith(b"\x1f\x8b"):
        return gzip.decompress(raw)
    return raw


def _optional_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _optional_str(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _short_text(root: ET.Element) -> str:
    text = " ".join(part.strip() for part in root.itertext() if part and part.strip())
    return text[:200]
