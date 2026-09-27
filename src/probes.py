"""Measure each uplink: reachability through a specific interface, Wi-Fi signal, modem state."""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("uplink.probes")

Target = Tuple[str, int]


def parse_targets(values: List[str]) -> List[Target]:
    targets = []
    for value in values:
        host, _, port = value.rpartition(":")
        if not host:
            host, port = value, "443"
        targets.append((host, int(port)))
    return targets


class ReachabilityProbe:
    """TCP connect to well-known targets, forced out of one interface (SO_BINDTODEVICE).

    Resolved addresses are cached, so a dead default route (which also breaks DNS) does not stop
    the other interface from being probed.
    """

    def __init__(self, targets: List[Target], timeout: float = 3.0):
        self.targets = targets
        self.timeout = timeout
        self._dns_cache: Dict[str, List[str]] = {}

    def _addresses(self, host: str) -> List[str]:
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
            addresses = sorted({info[4][0] for info in infos})
            if addresses:
                self._dns_cache[host] = addresses
        except OSError as e:
            logger.debug("DNS lookup for %s failed (%s); using cached addresses", host, e)
        return self._dns_cache.get(host, [])

    def rtt_ms(self, interface: str) -> Optional[float]:
        """Round-trip of a TCP handshake via `interface`, or None if no target is reachable."""
        for host, port in self.targets:
            for address in self._addresses(host):
                rtt = self._connect(interface, address, port)
                if rtt is not None:
                    return rtt
        return None

    def _connect(self, interface: str, address: str, port: int) -> Optional[float]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            if hasattr(socket, "SO_BINDTODEVICE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
            started = time.monotonic()
            sock.connect((address, port))
            return round((time.monotonic() - started) * 1000, 1)
        except OSError:
            return None
        finally:
            sock.close()


def interface_up(interface: str) -> bool:
    """Link is up and has an IPv4 address."""
    state_path = Path(f"/sys/class/net/{interface}/operstate")
    try:
        if state_path.read_text(encoding="utf-8").strip().lower() not in ("up", "unknown"):
            return False
    except OSError:
        return False
    result = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", interface], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def parse_proc_net_wireless(content: str, interface: str) -> Optional[float]:
    """Signal level (dBm) for `interface` from /proc/net/wireless."""
    for line in content.splitlines():
        if line.strip().startswith(f"{interface}:"):
            fields = line.split()
            if len(fields) >= 4:
                try:
                    return float(fields[3].rstrip("."))
                except ValueError:
                    return None
    return None


def parse_iw_link(output: str) -> Optional[float]:
    match = re.search(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", output)
    return float(match.group(1)) if match else None


def wifi_rssi_dbm(interface: str) -> Optional[float]:
    try:
        rssi = parse_proc_net_wireless(Path("/proc/net/wireless").read_text(encoding="utf-8"), interface)
        if rssi is not None:
            return rssi
    except OSError:
        pass
    try:
        result = subprocess.run(["iw", "dev", interface, "link"], capture_output=True, text=True, check=False, timeout=5)
        return parse_iw_link(result.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_mmcli_modem(output: str) -> Dict[str, object]:
    """Extract state, signal quality and access technology from `mmcli -m any -J`."""
    try:
        generic = json.loads(output).get("modem", {}).get("generic", {})
    except (json.JSONDecodeError, AttributeError):
        return {}
    quality = generic.get("signal-quality", {})
    value = quality.get("value") if isinstance(quality, dict) else None
    technologies = generic.get("access-technologies") or []
    return {
        "state": generic.get("state"),
        "signal_quality": int(value) if isinstance(value, str) and value.isdigit() else None,
        "access_tech": ",".join(technologies) if isinstance(technologies, list) and technologies else None,
    }


def modem_status() -> Dict[str, object]:
    try:
        result = subprocess.run(["mmcli", "-m", "any", "-J"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_mmcli_modem(result.stdout) if result.returncode == 0 else {}
