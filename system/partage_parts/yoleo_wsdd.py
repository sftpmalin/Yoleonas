#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small WSD responder for Yoleo Samba discovery.

Debian ships wsdd2, but its TCP metadata endpoint is exposed on 3702. Some
Windows clients refuse that TCP connection from Explorer. This responder keeps
the normal WSD discovery UDP port 3702 and serves metadata on TCP 5357, like
the classic wsdd daemon used by the old Samba docker.
"""

from __future__ import annotations

import argparse
import http.server
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from html import escape


MCAST_ADDR = "239.255.255.250"
WSD_PORT = 3702
WSD_ANON = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
WSD_DISCOVERY = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"
ACTION_PROBE = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe"
ACTION_PROBE_MATCHES = "http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches"
ACTION_RESOLVE = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Resolve"
ACTION_RESOLVE_MATCHES = "http://schemas.xmlsoap.org/ws/2005/04/discovery/ResolveMatches"
ACTION_HELLO = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Hello"
ACTION_BYE = "http://schemas.xmlsoap.org/ws/2005/04/discovery/Bye"
ACTION_GET_RESPONSE = "http://schemas.xmlsoap.org/ws/2004/09/transfer/GetResponse"


def run_text(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def interface_ipv4(interface: str) -> str:
    out = run_text(["ip", "-4", "-o", "addr", "show", "dev", interface])
    match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", out)
    if match:
        return match.group(1)
    return ""


def stable_uuid() -> str:
    raw = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            raw = open(path, "r", encoding="utf-8").read().strip()
        except OSError:
            raw = ""
        if raw:
            break
    if len(raw) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return str(uuid.UUID(raw))
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname()))


def xml_text(value: str) -> str:
    return escape(str(value or ""), quote=False)


def find_tag(text: str, tag: str) -> str:
    match = re.search(rf"<(?:\w+:)?{re.escape(tag)}[^>]*>(.*?)</(?:\w+:)?{re.escape(tag)}>", text, re.S)
    return match.group(1).strip() if match else ""


def find_action(text: str) -> str:
    return find_tag(text, "Action")


def find_message_id(text: str) -> str:
    return find_tag(text, "MessageID") or f"urn:uuid:{uuid.uuid4()}"


class WsddState:
    def __init__(self, args: argparse.Namespace) -> None:
        self.interface = args.interface
        self.host = args.host
        self.netbios = args.netbios
        self.workgroup = args.workgroup
        self.port = args.port
        self.ip = args.ip or interface_ipv4(args.interface)
        if not self.ip:
            raise SystemExit(f"No IPv4 address found for interface {args.interface!r}")
        self.uuid = args.uuid or stable_uuid()
        self.sequence = str(uuid.uuid4())
        self.instance = int(time.time())
        self.message_number = 0
        self.stop_event = threading.Event()
        self.udp_socket: socket.socket | None = None
        self.httpd: http.server.ThreadingHTTPServer | None = None

    @property
    def epr(self) -> str:
        return f"urn:uuid:{self.uuid}"

    @property
    def xaddr(self) -> str:
        return f"http://{self.ip}:{self.port}/{self.uuid}"

    @property
    def presentation_url(self) -> str:
        return f"http://{self.ip}:12345/"

    def next_message(self) -> int:
        self.message_number += 1
        return self.message_number

    def header(self, action: str, relates_to: str = "", to: str = WSD_ANON) -> str:
        rel = f"<wsa:RelatesTo>{xml_text(relates_to)}</wsa:RelatesTo>" if relates_to else ""
        return (
            f"<soap:Header>"
            f"<wsa:To>{xml_text(to)}</wsa:To>"
            f"<wsa:Action>{xml_text(action)}</wsa:Action>"
            f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsd:AppSequence InstanceId=\"{self.instance}\" SequenceId=\"urn:uuid:{self.sequence}\" "
            f"MessageNumber=\"{self.next_message()}\" />"
            f"{rel}</soap:Header>"
        )

    def envelope(self, header: str, body: str) -> str:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
            'xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
            'xmlns:wsx="http://schemas.xmlsoap.org/ws/2004/09/mex" '
            'xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof" '
            'xmlns:un0="http://schemas.microsoft.com/windows/pnpx/2005/10" '
            'xmlns:pub="http://schemas.microsoft.com/windows/pub/2005/07">'
            f"{header}{body}</soap:Envelope>"
        )

    def probe_match(self, relates_to: str) -> str:
        body = (
            "<soap:Body><wsd:ProbeMatches><wsd:ProbeMatch>"
            f"<wsa:EndpointReference><wsa:Address>{self.epr}</wsa:Address></wsa:EndpointReference>"
            "<wsd:Types>wsdp:Device pub:Computer</wsd:Types>"
            f"<wsd:XAddrs>{xml_text(self.xaddr)}</wsd:XAddrs>"
            "<wsd:MetadataVersion>2</wsd:MetadataVersion>"
            "</wsd:ProbeMatch></wsd:ProbeMatches></soap:Body>"
        )
        return self.envelope(self.header(ACTION_PROBE_MATCHES, relates_to), body)

    def resolve_match(self, relates_to: str) -> str:
        body = (
            "<soap:Body><wsd:ResolveMatches><wsd:ResolveMatch>"
            f"<wsa:EndpointReference><wsa:Address>{self.epr}</wsa:Address></wsa:EndpointReference>"
            "<wsd:Types>wsdp:Device pub:Computer</wsd:Types>"
            f"<wsd:XAddrs>{xml_text(self.xaddr)}</wsd:XAddrs>"
            "<wsd:MetadataVersion>2</wsd:MetadataVersion>"
            "</wsd:ResolveMatch></wsd:ResolveMatches></soap:Body>"
        )
        return self.envelope(self.header(ACTION_RESOLVE_MATCHES, relates_to), body)

    def hello(self) -> str:
        body = (
            "<soap:Body><wsd:Hello>"
            f"<wsa:EndpointReference><wsa:Address>{self.epr}</wsa:Address></wsa:EndpointReference>"
            "<wsd:Types>wsdp:Device pub:Computer</wsd:Types>"
            f"<wsd:XAddrs>{xml_text(self.xaddr)}</wsd:XAddrs>"
            "<wsd:MetadataVersion>2</wsd:MetadataVersion>"
            "</wsd:Hello></soap:Body>"
        )
        return self.envelope(self.header(ACTION_HELLO, to=WSD_DISCOVERY), body)

    def bye(self) -> str:
        body = (
            "<soap:Body><wsd:Bye>"
            f"<wsa:EndpointReference><wsa:Address>{self.epr}</wsa:Address></wsa:EndpointReference>"
            "<wsd:Types>wsdp:Device pub:Computer</wsd:Types>"
            "<wsd:MetadataVersion>2</wsd:MetadataVersion>"
            "</wsd:Bye></soap:Body>"
        )
        return self.envelope(self.header(ACTION_BYE, to=WSD_DISCOVERY), body)

    def metadata(self) -> str:
        nb = xml_text(self.netbios.upper())
        wg = xml_text(self.workgroup.upper())
        host = xml_text(self.host)
        uid = xml_text(self.uuid)
        presentation = xml_text(self.presentation_url)
        body = (
            '<soap:Body><wsx:Metadata>'
            '<wsx:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisDevice">'
            f"<wsdp:ThisDevice><wsdp:FriendlyName>{host}</wsdp:FriendlyName>"
            "<wsdp:FirmwareVersion>Yoleo NAS OS</wsdp:FirmwareVersion>"
            f"<wsdp:SerialNumber>{uid}</wsdp:SerialNumber></wsdp:ThisDevice>"
            "</wsx:MetadataSection>"
            '<wsx:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/ThisModel">'
            "<wsdp:ThisModel><wsdp:Manufacturer>Yoleo</wsdp:Manufacturer>"
            f"<wsdp:ManufacturerUrl>{presentation}</wsdp:ManufacturerUrl>"
            "<wsdp:ModelName>Yoleo NAS OS</wsdp:ModelName><wsdp:ModelNumber>1</wsdp:ModelNumber>"
            f"<wsdp:ModelUrl>{presentation}</wsdp:ModelUrl><wsdp:PresentationUrl>{presentation}</wsdp:PresentationUrl>"
            "<un0:DeviceCategory>Computers</un0:DeviceCategory></wsdp:ThisModel>"
            "</wsx:MetadataSection>"
            '<wsx:MetadataSection Dialect="http://schemas.xmlsoap.org/ws/2006/02/devprof/Relationship">'
            '<wsdp:Relationship Type="http://schemas.xmlsoap.org/ws/2006/02/devprof/host">'
            "<wsdp:Host>"
            f"<wsa:EndpointReference><wsa:Address>{self.epr}</wsa:Address></wsa:EndpointReference>"
            "<wsdp:Types>pub:Computer</wsdp:Types>"
            f"<wsdp:ServiceId>{self.epr}</wsdp:ServiceId>"
            f"<pub:Computer>{nb}/Workgroup:{wg}</pub:Computer>"
            "</wsdp:Host></wsdp:Relationship></wsx:MetadataSection>"
            "</wsx:Metadata></soap:Body>"
        )
        return self.envelope(self.header(ACTION_GET_RESPONSE), body)

    def send_multicast(self, payload: str) -> None:
        if not self.udp_socket:
            return
        self.udp_socket.sendto(payload.encode("utf-8"), (MCAST_ADDR, WSD_PORT))

    def send_hello(self) -> None:
        for _ in range(3):
            self.send_multicast(self.hello())
            time.sleep(0.2)

    def send_bye(self) -> None:
        for _ in range(2):
            self.send_multicast(self.bye())
            time.sleep(0.1)


class MetadataHandler(http.server.BaseHTTPRequestHandler):
    server_version = "YoleoWSDD/1.0"

    def do_POST(self) -> None:
        state: WsddState = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            self.rfile.read(length)
        body = state.metadata().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", 'application/soap+xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.send_error(405, "POST required")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"http {self.client_address[0]} - {fmt % args}", flush=True)


def udp_loop(state: WsddState) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    sock.bind(("", WSD_PORT))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(state.ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(MCAST_ADDR) + socket.inet_aton(state.ip))
    sock.settimeout(1.0)
    state.udp_socket = sock
    state.send_hello()

    while not state.stop_event.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode("utf-8", errors="ignore")
        action = find_action(text)
        message_id = find_message_id(text)
        if action == ACTION_PROBE:
            payload = state.probe_match(message_id).encode("utf-8")
            sock.sendto(payload, addr)
            print(f"probe {addr[0]} -> {state.xaddr}", flush=True)
        elif action == ACTION_RESOLVE and state.uuid.lower() in text.lower():
            payload = state.resolve_match(message_id).encode("utf-8")
            sock.sendto(payload, addr)
            print(f"resolve {addr[0]} -> {state.xaddr}", flush=True)

    state.send_bye()
    sock.close()


def http_loop(state: WsddState) -> None:
    httpd = http.server.ThreadingHTTPServer((state.ip, state.port), MetadataHandler)
    httpd.state = state  # type: ignore[attr-defined]
    state.httpd = httpd
    httpd.serve_forever(poll_interval=0.5)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", default="br0")
    parser.add_argument("--ip", default="")
    parser.add_argument("--host", default=socket.gethostname().split(".", 1)[0])
    parser.add_argument("--netbios", default=socket.gethostname().split(".", 1)[0].upper())
    parser.add_argument("--workgroup", default="WORKGROUP")
    parser.add_argument("--uuid", default="")
    parser.add_argument("--port", type=int, default=5357)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state = WsddState(args)
    print(
        f"yoleo-wsdd interface={state.interface} ip={state.ip} "
        f"host={state.host} netbios={state.netbios} workgroup={state.workgroup} xaddr={state.xaddr}",
        flush=True,
    )

    def stop(signum: int, frame: object) -> None:
        if signum == getattr(signal, "SIGHUP", -1):
            state.send_hello()
            return
        state.stop_event.set()
        if state.httpd:
            threading.Thread(target=state.httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop)

    http_thread = threading.Thread(target=http_loop, args=(state,), daemon=True)
    http_thread.start()
    try:
        udp_loop(state)
    finally:
        if state.httpd:
            state.httpd.shutdown()
        http_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
