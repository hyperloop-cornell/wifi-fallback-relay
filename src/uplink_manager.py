"""Uplink manager: keep the cellular hub online, preferring Wi-Fi and falling back to cellular.

Runs as root on the one hub with a cellular HAT. Every CHECK_INTERVAL_SECONDS it probes both
links, decides which should carry the default route (src/policy.py), updates NetworkManager route
metrics when that changes (src/routes.py), and writes STATUS_FILE for rpi-hub-server to report.
It never restarts or reconfigures rpi-hub-server.
"""

from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import get_env_bool, get_env_float, get_env_int, get_env_str, load_env, parse_csv, setup_logging
from .policy import CELLULAR, WIFI, UplinkPolicy
from .probes import ReachabilityProbe, interface_up, modem_status, parse_targets, wifi_rssi_dbm
from .routes import RouteController
from .status import utc_iso, write_status

logger = logging.getLogger("uplink")

DEFAULT_TARGETS = "api.cornellhyperloop.com:443,1.1.1.1:443"


@dataclass(frozen=True)
class UplinkConfig:
    wifi_interface: str
    cellular_interface: str
    cellular_connection: Optional[str]
    cellular_enabled: bool
    probe_targets: List[str]
    probe_timeout_seconds: float
    check_interval_seconds: float
    wifi_down_threshold: int
    wifi_up_stable_seconds: float
    preferred_metric: int
    fallback_metric: int
    warm_standby: bool
    status_file: Path
    dry_run: bool


def load_config() -> UplinkConfig:
    load_env(get_env_str("UPLINK_ENV_FILE"))
    cellular_connection = get_env_str("CELLULAR_CONNECTION") or None
    return UplinkConfig(
        wifi_interface=get_env_str("WIFI_INTERFACE", "wlan0") or "wlan0",
        cellular_interface=get_env_str("CELLULAR_INTERFACE", "wwan0") or "wwan0",
        cellular_connection=cellular_connection,
        cellular_enabled=get_env_bool("CELLULAR_ENABLED", cellular_connection is not None),
        probe_targets=parse_csv(get_env_str("PROBE_TARGETS", DEFAULT_TARGETS)),
        probe_timeout_seconds=get_env_float("PROBE_TIMEOUT_SECONDS", 3.0),
        check_interval_seconds=get_env_float("CHECK_INTERVAL_SECONDS", 5.0),
        wifi_down_threshold=get_env_int("WIFI_DOWN_THRESHOLD", 3),
        wifi_up_stable_seconds=get_env_float("WIFI_UP_STABLE_SECONDS", 60.0),
        preferred_metric=get_env_int("PREFERRED_ROUTE_METRIC", 100),
        fallback_metric=get_env_int("FALLBACK_ROUTE_METRIC", 700),
        warm_standby=get_env_bool("CELLULAR_WARM_STANDBY", True),
        status_file=Path(get_env_str("STATUS_FILE", "/run/hyperloop-uplink/status.json") or "/run/hyperloop-uplink/status.json"),
        dry_run=get_env_bool("DRY_RUN", False),
    )


class UplinkManager:
    def __init__(
        self,
        config: UplinkConfig,
        probe: Optional[ReachabilityProbe] = None,
        routes: Optional[RouteController] = None,
        policy: Optional[UplinkPolicy] = None,
    ):
        self.config = config
        self.probe = probe or ReachabilityProbe(parse_targets(config.probe_targets), timeout=config.probe_timeout_seconds)
        self.routes = routes or RouteController(
            wifi_interface=config.wifi_interface,
            cellular_interface=config.cellular_interface,
            cellular_connection=config.cellular_connection,
            preferred_metric=config.preferred_metric,
            fallback_metric=config.fallback_metric,
            warm_standby=config.warm_standby,
            dry_run=config.dry_run,
        )
        self.policy = policy or UplinkPolicy(
            wifi_down_threshold=config.wifi_down_threshold,
            wifi_up_stable_seconds=config.wifi_up_stable_seconds,
            has_cellular=config.cellular_enabled,
        )
        self._stop = threading.Event()
        self._routing_applied = False
        self._active_since = datetime.now(timezone.utc)
        self._last_active: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def measure(self) -> Dict[str, Dict[str, Any]]:
        cfg = self.config
        wifi_link = interface_up(cfg.wifi_interface)
        wifi_rtt = self.probe.rtt_ms(cfg.wifi_interface) if wifi_link else None
        wifi = {
            "interface": cfg.wifi_interface,
            "link": wifi_link,
            "probe_ok": wifi_rtt is not None,
            "rtt_ms": wifi_rtt,
            "rssi_dbm": wifi_rssi_dbm(cfg.wifi_interface) if wifi_link else None,
        }

        cellular: Dict[str, Any] = {"interface": cfg.cellular_interface, "enabled": cfg.cellular_enabled}
        if cfg.cellular_enabled:
            cell_link = interface_up(cfg.cellular_interface)
            cell_rtt = self.probe.rtt_ms(cfg.cellular_interface) if cell_link else None
            cellular.update({"link": cell_link, "probe_ok": cell_rtt is not None, "rtt_ms": cell_rtt, **modem_status()})
        return {"wifi": wifi, "cellular": cellular}

    def tick(self) -> Dict[str, Any]:
        measurements = self.measure()
        decision = self.policy.step(
            wifi_ok=measurements["wifi"]["probe_ok"],
            cellular_ok=bool(measurements["cellular"].get("probe_ok")),
        )

        if decision.changed or not self._routing_applied:
            if decision.changed:
                logger.warning("Switching preferred uplink to %s", decision.preferred)
            self._routing_applied = self.routes.apply(decision.preferred)

        if decision.active != self._last_active:
            if self._last_active is not None:
                logger.warning("Active uplink: %s -> %s", self._last_active, decision.active)
            self._last_active = decision.active
            self._active_since = datetime.now(timezone.utc)

        status = {
            "active": decision.active,
            "preferred": decision.preferred,
            "since": utc_iso(self._active_since),
            "updated_at": utc_iso(),
            **measurements,
        }
        write_status(self.config.status_file, status)
        return status

    def run(self) -> None:
        cfg = self.config
        logger.info(
            "Uplink manager started: wifi=%s cellular=%s (%s) status=%s dry_run=%s",
            cfg.wifi_interface,
            cfg.cellular_interface if cfg.cellular_enabled else "disabled",
            cfg.cellular_connection or "no connection name",
            cfg.status_file,
            cfg.dry_run,
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Uplink check failed")
            self._stop.wait(cfg.check_interval_seconds)


def main() -> None:
    config = load_config()
    setup_logging(get_env_str("LOG_LEVEL", "INFO") or "INFO")
    manager = UplinkManager(config)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: manager.stop())
    manager.run()


if __name__ == "__main__":
    main()
