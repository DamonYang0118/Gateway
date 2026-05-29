from __future__ import annotations

import argparse
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Tuple


DEFAULT_POINTS_FILE = "mock_850a_points.json"


class Mock850AState:
    def __init__(self, points_file: str) -> None:
        self.points_file = points_file
        self.points = self._load_points(points_file)
        self.by_tag = {point["tag"]: point for point in self.points if point.get("tag")}
        self.by_address = {str(point["address"]): point for point in self.points}
        self.by_cid_vid = {
            (str(point.get("cid", "")), str(point.get("vid", ""))): point
            for point in self.points
            if point.get("cid") is not None and point.get("vid") is not None
        }
        self.request_count = 0

    @staticmethod
    def _load_points(points_file: str):
        path = Path(points_file)
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload["points"] if isinstance(payload, dict) else payload

    def next_value(self, point: dict) -> float:
        base = float(point.get("mock_value", 0))
        key = str(point.get("tag") or point.get("key") or "")
        if point.get("unit") in ("", None):
            return float(int(base))
        # Small deterministic movement helps prove each poll is fresh without randomness.
        wave = ((self.request_count + sum(ord(ch) for ch in key)) % 7 - 3) * 0.1
        return round(base + wave, 2)


def make_handler(state: Mock850AState):
    class Mock850AHandler(BaseHTTPRequestHandler):
        server_version = "MockDanfoss850A/1.0"

        def do_POST(self) -> None:
            if self.path != "/html/xml.cgi":
                self.send_error(404, "Only /html/xml.cgi is implemented")
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8", errors="ignore")
            state.request_count += 1
            try:
                response, content_type = self.handle_xml(body)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except Exception as exc:
                payload = (
                    f'<resp action="unknown" error="500"><message>{escape(str(exc))}</message></resp>'
                ).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def handle_xml(self, body: str) -> Tuple[bytes, str]:
            root = ET.fromstring(body)
            action = root.attrib.get("action", "")
            if action == "getauth":
                return self.xml_bytes(
                    '<resp action="getauth" error="0"><session_token>mock-session-token</session_token></resp>'
                )
            if action == "read_date_time":
                return self.xml_bytes(build_read_date_time_response())
            if action == "read_val":
                return self.xml_bytes(build_read_val_response(root, state))
            if action == "read_devices":
                return self.xml_bytes(build_read_devices_response(state))
            if action == "read_parm_info":
                return self.xml_bytes(build_read_parm_info_response(root, state))
            if action == "read_device_history_cfg":
                return self.xml_bytes(build_history_cfg_response(state))
            if action == "read_history":
                return self.xml_bytes(build_read_history_response(root))
            if action == "read_device_alarms":
                return self.xml_bytes(build_device_alarms_response(root))
            if action == "read_generic_alarms":
                return self.xml_bytes(build_generic_alarms_response())
            if action == "start_history_query":
                return self.xml_bytes(
                    '<resp action="start_history_query" error="0"><query_id>1</query_id></resp>'
                )
            if action == "read_query_status":
                return self.xml_bytes(build_query_status_response())
            if action == "read_query_data":
                return build_query_data_response(), "application/octet-stream"
            return self.xml_bytes(f'<resp action="{escape(action)}" error="404">Unsupported action</resp>')

        @staticmethod
        def xml_bytes(xml_text: str) -> Tuple[bytes, str]:
            return xml_text.encode("utf-8"), "text/xml"

        def log_message(self, fmt, *args) -> None:
            print(f"[MOCK850A] {self.address_string()} {fmt % args}")

    return Mock850AHandler


def build_read_date_time_response() -> str:
    now = datetime.now()
    return (
        '<resp action="read_date_time" error="0">'
        f"<year>{now.year}</year><month>{now.month}</month><day>{now.day}</day>"
        f"<hour>{now.hour}</hour><minute>{now.minute}</minute><second>{now.second}</second>"
        f"<epoch>{int(time.time())}</epoch><timezone>800</timezone><daylightsavings>0</daylightsavings>"
        "</resp>"
    )


def build_read_val_response(root: ET.Element, state: Mock850AState) -> str:
    vals = []
    for val in root.findall("val"):
        point = find_point(val, state)
        if point is None:
            vals.append(build_val_error(val))
            continue
        value = state.next_value(point)
        unit = point.get("unit") or ""
        value_text = f"{value:g} {unit}".strip()
        attrs = {
            "nodetype": val.attrib.get("nodetype", str(point.get("nodetype", 16))),
            "node": val.attrib.get("node", str(point.get("node", 1))),
            "tag": val.attrib.get("tag", point.get("tag", "")),
            "cid": val.attrib.get("cid", str(point.get("cid", 0))),
            "vid": val.attrib.get("vid", str(point.get("vid", point.get("address", 0)))),
            "display": point.get("display", point.get("tag", "")),
            "name": point.get("name", point.get("tag", "")),
            "stat": "Online",
            "statcode": "2",
        }
        vals.append(f"<val {format_attrs(attrs)}>{escape(value_text)}</val>")
    return f'<resp action="read_val" error="0">{"".join(vals)}</resp>'


def find_point(val: ET.Element, state: Mock850AState):
    tag = val.attrib.get("tag")
    if tag and tag in state.by_tag:
        return state.by_tag[tag]
    cid = val.attrib.get("cid")
    vid = val.attrib.get("vid")
    if (cid, vid) in state.by_cid_vid:
        return state.by_cid_vid[(cid, vid)]
    if vid and vid in state.by_address:
        return state.by_address[vid]
    return None


def build_val_error(val: ET.Element) -> str:
    attrs = dict(val.attrib)
    attrs["error"] = "404"
    return f"<val {format_attrs(attrs)}>N/A</val>"


def build_read_devices_response(state: Mock850AState) -> str:
    return (
        '<resp action="read_devices" error="0">'
        "<unit_name>Mock AK-SM800A</unit_name><software>G08.047</software>"
        '<device nodetype="16" node="1" mod="0" point="0" online="1" value="Mock">'
        "<name>Cold Staging Mock Device</name><device_id>MOCK_850A</device_id><type>PACK</type>"
        "</device>"
        f"<total>{len(state.points)}</total>"
        "</resp>"
    )


def build_history_cfg_response(state: Mock850AState) -> str:
    items = []
    for index, point in enumerate(state.points[:100], 1):
        items.append(
            "<history_cfg>"
            f"<name>{escape(point.get('name', point.get('tag', '')))}</name>"
            f"<cid>{point.get('cid', 0)}</cid>"
            f"<vid>{point.get('vid', point.get('address', index))}</vid>"
            f"<hist_index>{index}</hist_index>"
            "</history_cfg>"
        )
    return (
        '<resp action="read_device_history_cfg" nodetype="16" node="1" error="0">'
        + "".join(items)
        + f"<timezone>800</timezone><daylightsavings>0</daylightsavings>"
        f"<current_secs>{int(time.time())}</current_secs><total_points>{len(items)}</total_points></resp>"
    )


def build_read_parm_info_response(root: ET.Element, state: Mock850AState) -> str:
    parms = []
    for point in state.points[:100]:
        attrs = {
            "cid": point.get("cid", 0),
            "vid": point.get("vid", point.get("address", 0)),
            "name": point.get("name", point.get("tag", "")),
            "unit": point.get("unit", ""),
            "rw": "R",
        }
        parms.append(f"<parm {format_attrs(attrs)} />")
    device_id = root.attrib.get("device_id", "MOCK_850A")
    return f'<resp action="read_parm_info" device_id="{escape(device_id)}" error="0"><parms>{"".join(parms)}</parms></resp>'


def build_read_history_response(root: ET.Element) -> str:
    now = int(time.time()) - 300
    unit = root.attrib.get("units", "s")
    return (
        '<resp action="read_history" error="0" real_sample_rate="60">'
        f"<unit>{escape(unit)}</unit><starttime><epoch>{now}</epoch></starttime>"
        "<data><y>48</y><y>49.5</y><y>-----</y><y>51</y><y>50</y></data>"
        "</resp>"
    )


def build_device_alarms_response(root: ET.Element) -> str:
    return (
        '<resp action="read_device_alarms" error="0">'
        "<newest><time>05:17PM 05/04/25</time></newest>"
        "<oldest><time>05:10PM 05/04/25</time></oldest>"
        '<active><ref name="Mock active alarm">1001</ref></active>'
        '<acked><ref name="Mock acknowledged alarm">1002</ref></acked>'
        '<cleared><ref name="Mock cleared alarm">1003</ref></cleared>'
        "</resp>"
    )


def build_generic_alarms_response() -> str:
    return (
        '<resp action="read_generic_alarms" error="0">'
        '<alarm id="2001" name="NTPfailure" state="active">'
        "<device>Mock AK-SM800A</device><time>05:17PM 05/04/25</time>"
        "<description>NTPfailure mock alarm</description>"
        "</alarm></resp>"
    )


def build_query_status_response() -> str:
    now = int(time.time()) - 600
    stop = now + 300
    return (
        '<resp action="read_query_status" query_id="1" error="0">'
        "<status>complete</status><field_count>6</field_count><field_size>3</field_size>"
        "<exp>-1</exp><unit>degc</unit><actual_sample_rate>60</actual_sample_rate><offset>800</offset>"
        f"<starttime><epoch>{now}</epoch></starttime><stoptime><epoch>{stop}</epoch></stoptime>"
        "</resp>"
    )


def build_query_data_response() -> bytes:
    payload = (1).to_bytes(4, "big")
    for value in (48, 50, 51, 49, 47, 46):
        payload += bytes([0]) + int(value).to_bytes(2, "big", signed=True)
    return payload


def format_attrs(attrs: Dict[str, object]) -> str:
    return " ".join(
        f'{key}="{escape(str(value))}"' for key, value in attrs.items() if value not in (None, "")
    )


def escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Local mock server for Danfoss 850A XML API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--points", default=DEFAULT_POINTS_FILE)
    args = parser.parse_args()

    state = Mock850AState(args.points)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"[MOCK850A] Serving {len(state.points)} points at http://{args.host}:{args.port}/html/xml.cgi")
    server.serve_forever()


if __name__ == "__main__":
    main()
