"""Steer the default route between Wi-Fi and cellular with NetworkManager route metrics."""

from __future__ import annotations

import logging
import subprocess
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger("uplink.routes")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=False, timeout=30)


class RouteController:
    """Lower route metric wins. Both links stay up (cellular as warm standby unless disabled);
    switching only changes which default route the kernel uses, so existing sockets on the old
    link break and applications such as rpi-hub-server reconnect over the new one.

    Wi-Fi is adjusted on its device (`nmcli device modify wlan0 ...`, applied immediately).
    Cellular is adjusted on its connection profile and re-activated: for ModemManager modems the
    NetworkManager device is the control port (e.g. cdc-wdm0), not the wwan0 IP interface, so
    device-level commands against wwan0 would fail.
    """

    def __init__(
        self,
        wifi_interface: str,
        cellular_interface: str,
        cellular_connection: Optional[str],
        preferred_metric: int = 100,
        fallback_metric: int = 700,
        warm_standby: bool = True,
        dry_run: bool = False,
        runner: Runner = _run,
    ):
        self.wifi_interface = wifi_interface
        self.cellular_interface = cellular_interface
        self.cellular_connection = cellular_connection
        self.preferred_metric = preferred_metric
        self.fallback_metric = fallback_metric
        self.warm_standby = warm_standby
        self.dry_run = dry_run
        self.runner = runner

    def commands_for(self, preferred: str) -> List[List[str]]:
        wifi_metric = self.preferred_metric if preferred == "wifi" else self.fallback_metric
        cellular_metric = self.preferred_metric if preferred == "cellular" else self.fallback_metric
        commands: List[List[str]] = [
            ["nmcli", "device", "modify", self.wifi_interface, "ipv4.route-metric", str(wifi_metric)],
        ]
        if not self.cellular_connection:
            return commands
        if preferred == "cellular" or self.warm_standby:
            commands.append(
                ["nmcli", "connection", "modify", self.cellular_connection, "ipv4.route-metric", str(cellular_metric)]
            )
            commands.append(["nmcli", "connection", "up", self.cellular_connection])
        else:
            commands.append(["nmcli", "connection", "down", self.cellular_connection])
        return commands

    def apply(self, preferred: str) -> bool:
        """Apply routing for `preferred`; returns False if any command failed."""
        ok = True
        for command in self.commands_for(preferred):
            if self.dry_run:
                logger.info("DRY_RUN: %s", " ".join(command))
                continue
            result = self.runner(command)
            if result.returncode != 0:
                ok = False
                logger.warning("%s failed (rc=%s): %s", " ".join(command), result.returncode, (result.stderr or "").strip())
        return ok
