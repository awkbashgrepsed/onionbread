"""Experimental Windows full-tunnel support for Onionbread.

Requires an elevated process. This module is intentionally isolated from the Tor
GUI so its routing lifecycle can be tested and revised independently.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path


TUN_ADDRESS = "198.18.0.1"
DNS_HOST = "cloudflare-dns.com"
DNS_PATH = "/dns-query"


def _powershell(script: str, *, check: bool = True) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "PowerShell command failed").strip())
    return result.stdout.strip()


def _admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class DnsOverTor:
    """Minimal UDP DNS forwarder: DNS wire messages over DoH through Tor SOCKS."""

    def __init__(self, socks_port: int) -> None:
        self.socks_port = socks_port
        self.sock: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 53))
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._serve, daemon=True, name="Onionbread-DNS")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.sock:
            self.sock.close()
            self.sock = None
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def _doh(self, packet: bytes) -> bytes:
        raw = socket.create_connection(("127.0.0.1", self.socks_port), timeout=12)
        try:
            # SOCKS5 CONNECT to the DoH hostname; Tor resolves the hostname remotely.
            raw.sendall(b"\x05\x01\x00")
            response = self._read_exact(raw, 2)
            if response != b"\x05\x00":
                raise OSError("Tor SOCKS5 proxy rejected the DNS forwarder")
            host = DNS_HOST.encode("idna")
            raw.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
            head = self._read_exact(raw, 4)
            if head[1] != 0:
                raise OSError("Tor could not open a DoH connection")
            atyp = head[3]
            if atyp == 1:
                self._read_exact(raw, 4)
            elif atyp == 3:
                self._read_exact(raw, self._read_exact(raw, 1)[0])
            elif atyp == 4:
                self._read_exact(raw, 16)
            self._read_exact(raw, 2)
            tls = ssl.create_default_context().wrap_socket(raw, server_hostname=DNS_HOST)
            raw = None
            try:
                request = (
                    f"POST {DNS_PATH} HTTP/1.1\r\nHost: {DNS_HOST}\r\n"
                    "Accept: application/dns-message\r\n"
                    "Content-Type: application/dns-message\r\n"
                    f"Content-Length: {len(packet)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii") + packet
                tls.sendall(request)
                data = bytearray()
                while True:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                split = data.find(b"\r\n\r\n")
                if split < 0 or not data.startswith(b"HTTP/1.1 200") and not data.startswith(b"HTTP/1.0 200"):
                    raise OSError("DoH resolver returned an invalid response")
                headers = data[:split].lower()
                body = bytes(data[split + 4:])
                if b"transfer-encoding: chunked" in headers:
                    body = self._unchunk(body)
                elif b"content-length:" in headers:
                    marker = b"content-length:"
                    length = int(headers.split(marker, 1)[1].split(b"\r\n", 1)[0].strip())
                    body = body[:length]
                return body
            finally:
                tls.close()
        finally:
            if raw is not None:
                raw.close()

    @staticmethod
    def _read_exact(sock: socket.socket, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise OSError("Unexpected EOF from Tor SOCKS proxy")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _unchunk(data: bytes) -> bytes:
        out = bytearray()
        while data:
            end = data.find(b"\r\n")
            if end < 0:
                break
            size = int(data[:end].split(b";", 1)[0], 16)
            data = data[end + 2:]
            if size == 0:
                break
            out.extend(data[:size])
            data = data[size + 2:]
        return bytes(out)

    def _serve(self) -> None:
        assert self.sock is not None
        while not self.stop_event.is_set():
            try:
                packet, client = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            try:
                answer = self._doh(packet)
                if answer:
                    self.sock.sendto(answer, client)
            except (OSError, ValueError, ssl.SSLError):
                # On resolver failure, return SERVFAIL rather than leak DNS directly.
                if len(packet) >= 12 and self.sock:
                    failed = bytearray(packet)
                    failed[2] |= 0x80
                    failed[3] = (failed[3] & 0xF0) | 0x02
                    try:
                        self.sock.sendto(failed, client)
                    except OSError:
                        pass


class WindowsVpn:
    """Owns tun2socks, the default route, Tor peer bypass routes, and DNS."""

    def __init__(self, app_dir: Path, socks_port: int, tor_pid: int, log) -> None:
        self.app_dir = app_dir
        self.socks_port = socks_port
        self.tor_pid = tor_pid
        self.log = log
        self.process: subprocess.Popen | None = None
        self.dns: DnsOverTor | None = None
        self.physical_index: int | None = None
        self.gateway: str | None = None
        self.tun_index: int | None = None
        self.route_added = False
        self.peer_routes: set[str] = set()
        self.stop_event = threading.Event()
        self.monitor: threading.Thread | None = None

    @staticmethod
    def supported() -> bool:
        return os.name == "nt"

    def start(self) -> None:
        if not self.supported():
            raise RuntimeError("System VPN mode is currently supported on Windows only.")
        if not _admin():
            raise RuntimeError("VPN mode needs administrator rights. Close Onionbread and run it as Administrator.")
        tun = self.app_dir / "runtime" / "vpn" / "tun2socks.exe"
        dll = tun.with_name("wintun.dll")
        if not tun.is_file() or not dll.is_file():
            raise RuntimeError("VPN runtime missing. Run tools/fetch_vpn_runtime.py first.")
        if not (1 <= self.socks_port <= 65535):
            raise RuntimeError("Invalid Tor SOCKS port.")
        route = json.loads(_powershell(
            "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop | "
            "Where-Object {$_.NextHop -ne '0.0.0.0'} | Sort-Object RouteMetric | "
            "Select-Object -First 1 InterfaceIndex,NextHop | ConvertTo-Json -Compress"
        ))
        self.physical_index = int(route["InterfaceIndex"])
        self.gateway = str(route["NextHop"])
        # Keep tun2socks' own proxy connection on the physical interface.
        adapter = _powershell(
            f"Get-NetAdapter -InterfaceIndex {self.physical_index} | Select-Object -ExpandProperty Name"
        ).strip()
        self.log(f"VPN: using physical adapter {adapter}, gateway {self.gateway}")
        command = [
            str(tun), "--device", "wintun",
            "--proxy", f"socks5://127.0.0.1:{self.socks_port}",
            "--interface", adapter, "--loglevel", "info",
        ]
        self.process = subprocess.Popen(
            command, cwd=str(tun.parent), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        try:
            self._wait_for_tun()
            self._run(
                f"Set-NetIPInterface -InterfaceIndex {self.tun_index} -AddressFamily IPv4 -Dhcp Disabled"
            )
            self._run(
                f"New-NetIPAddress -InterfaceIndex {self.tun_index} -IPAddress {TUN_ADDRESS} "
                "-PrefixLength 15 -AddressFamily IPv4 -ErrorAction Stop | Out-Null"
            )
            self.dns = DnsOverTor(self.socks_port)
            self.dns.start()
            self._run(
                f"Set-DnsClientServerAddress -InterfaceIndex {self.tun_index} -ServerAddresses 127.0.0.1"
            )
            self._run(
                f"New-NetRoute -DestinationPrefix '0.0.0.0/0' -InterfaceIndex {self.tun_index} "
                f"-NextHop {TUN_ADDRESS} -RouteMetric 1 -PolicyStore ActiveStore -ErrorAction Stop | Out-Null"
            )
            self.route_added = True
            self.log("VPN route installed. DNS queries are forwarded as DNS-over-HTTPS through Tor.")
            self.monitor = threading.Thread(target=self._monitor_tor_routes, daemon=True, name="Onionbread-route-monitor")
            self.monitor.start()
        except Exception:
            self.stop()
            raise

    def _wait_for_tun(self) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError("tun2socks exited during startup; check the Tor log.")
            output = _powershell(
                "Get-NetAdapter -Name 'wintun' -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 -ExpandProperty ifIndex"
            , check=False)
            if output.isdigit():
                self.tun_index = int(output)
                return
            time.sleep(0.25)
        raise RuntimeError("Wintun adapter did not appear. Check the VPN runtime and administrator permissions.")

    def _run(self, script: str) -> str:
        return _powershell(script)

    def _monitor_tor_routes(self) -> None:
        while not self.stop_event.wait(1.0):
            if not self.process or self.process.poll() is not None:
                self.log("VPN: tun2socks exited; restoring routes.")
                self.stop()
                break
            try:
                peers = json.loads(_powershell(
                    f"Get-NetTCPConnection -OwningProcess {self.tor_pid} -State Established -ErrorAction SilentlyContinue | "
                    "Where-Object {$_.RemoteAddress -match '^\\d+\\.\\d+\\.\\d+\\.\\d+$'} | "
                    "Select-Object -ExpandProperty RemoteAddress -Unique | ConvertTo-Json -Compress"
                , check=False) or "[]")
                if isinstance(peers, str):
                    peers = [peers]
                for ip in peers:
                    if not isinstance(ip, str) or ip in self.peer_routes or ip.startswith(("127.", "10.", "192.168.", "172.16.")):
                        continue
                    try:
                        self._run(
                            f"New-NetRoute -DestinationPrefix '{ip}/32' -InterfaceIndex {self.physical_index} "
                            f"-NextHop {self.gateway} -RouteMetric 1 -PolicyStore ActiveStore -ErrorAction Stop | Out-Null"
                        )
                        self.peer_routes.add(ip)
                        self.log(f"VPN: added direct route exception for Tor relay {ip}")
                    except RuntimeError:
                        pass
            except (RuntimeError, ValueError, OSError):
                pass

    def stop(self) -> None:
        self.stop_event.set()
        if self.dns:
            self.dns.stop()
            self.dns = None
        if self.route_added and self.tun_index is not None:
            _powershell(
                f"Get-NetRoute -DestinationPrefix '0.0.0.0/0' -InterfaceIndex {self.tun_index} "
                f"-ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue",
                check=False,
            )
            self.route_added = False
        for ip in tuple(self.peer_routes):
            if self.physical_index is not None:
                _powershell(
                    f"Get-NetRoute -DestinationPrefix '{ip}/32' -InterfaceIndex {self.physical_index} "
                    f"-NextHop '{self.gateway}' -ErrorAction SilentlyContinue | "
                    "Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue",
                    check=False,
                )
        self.peer_routes.clear()
        process = self.process
        self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self.monitor and self.monitor is not threading.current_thread():
            self.monitor.join(timeout=2)
        self.monitor = None
        self.tun_index = None
