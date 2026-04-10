from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .common import (
    RetryPolicy,
    dedupe_ordered,
    get_env_float,
    get_env_int,
    get_env_str,
    load_env,
    parse_csv,
    read_env_value,
    setup_logging,
    upsert_env_value,
)

logger = logging.getLogger("slave_fallback")


@dataclass(frozen=True)
class SlaveFallbackConfig:
    wifi_interface: str
    check_interval_seconds: float
    wifi_down_threshold: int
    wifi_up_stable_seconds: int
    hub_env_file: Path
    hub_service_name: str
    cloud_endpoint: str
    discovery_port: int
    discovery_shared_key: Optional[str]
    relay_path: str
    master_host_hints: List[str]
    discovery_subnets: List[str]
    discovery_workers: int
    max_scan_hosts_per_subnet: int
    discovery_connect_timeout: float
    discovery_read_timeout: float
    master_health_check_seconds: float
    nmap_binary: str
    nmap_scan_timeout_seconds: float
    nmap_host_timeout_seconds: float
    nmap_max_retries: int
    nmap_timing_template: str
    retry_policy: RetryPolicy


def load_config() -> SlaveFallbackConfig:
    load_env(get_env_str("RELAY_ENV_FILE"))

    hub_env_file = Path(get_env_str("HUB_ENV_FILE", "/opt/rpi-hub-server/.env") or "/opt/rpi-hub-server/.env")
    cloud_endpoint = get_env_str("CLOUD_ENDPOINT") or read_env_value(hub_env_file, "SERVER_ENDPOINT")
    if not cloud_endpoint:
        raise RuntimeError("CLOUD_ENDPOINT is required or must exist as SERVER_ENDPOINT in HUB_ENV_FILE")

    retry_policy = RetryPolicy(
        base_seconds=get_env_float("RETRY_BASE_SECONDS", 1.0),
        max_seconds=get_env_float("RETRY_MAX_SECONDS", 60.0),
        jitter_ratio=get_env_float("RETRY_JITTER_RATIO", 0.30),
    )

    timing_template = (get_env_str("NMAP_TIMING_TEMPLATE", "4") or "4").lstrip("Tt")
    if not timing_template:
        timing_template = "4"

    return SlaveFallbackConfig(
        wifi_interface=get_env_str("WIFI_INTERFACE", "wlan0") or "wlan0",
        check_interval_seconds=get_env_float("CHECK_INTERVAL_SECONDS", 5.0),
        wifi_down_threshold=get_env_int("WIFI_DOWN_THRESHOLD", 3),
        wifi_up_stable_seconds=get_env_int("WIFI_UP_STABLE_SECONDS", 60),
        hub_env_file=hub_env_file,
        hub_service_name=get_env_str("HUB_SERVICE_NAME", "rpi-hub-server.service") or "rpi-hub-server.service",
        cloud_endpoint=cloud_endpoint,
        discovery_port=get_env_int("DISCOVERY_PORT", 5319),
        discovery_shared_key=get_env_str("DISCOVERY_SHARED_KEY"),
        relay_path=get_env_str("RELAY_PATH", "/hub") or "/hub",
        master_host_hints=parse_csv(get_env_str("MASTER_HOST_HINTS")),
        discovery_subnets=parse_csv(get_env_str("DISCOVERY_SUBNETS", "192.168.1.0/24")),
        discovery_workers=get_env_int("DISCOVERY_WORKERS", 64),
        max_scan_hosts_per_subnet=get_env_int("MAX_SCAN_HOSTS_PER_SUBNET", 512),
        discovery_connect_timeout=get_env_float("DISCOVERY_CONNECT_TIMEOUT", 1.5),
        discovery_read_timeout=get_env_float("DISCOVERY_READ_TIMEOUT", 1.5),
        master_health_check_seconds=get_env_float("MASTER_HEALTH_CHECK_SECONDS", 10.0),
        nmap_binary=get_env_str("NMAP_BINARY", "nmap") or "nmap",
        nmap_scan_timeout_seconds=get_env_float("NMAP_SCAN_TIMEOUT_SECONDS", 30.0),
        nmap_host_timeout_seconds=get_env_float("NMAP_HOST_TIMEOUT_SECONDS", 2.0),
        nmap_max_retries=get_env_int("NMAP_MAX_RETRIES", 1),
        nmap_timing_template=timing_template,
        retry_policy=retry_policy,
    )


class SlaveFallbackDaemon:
    def __init__(self, config: SlaveFallbackConfig) -> None:
        self.config = config
        self._stop_event = asyncio.Event()
        self._mode = "normal"
        self._wifi_down_count = 0
        self._wifi_up_since: Optional[float] = None
        self._active_relay_endpoint: Optional[str] = None
        self._last_master_health_check = 0.0

    def request_shutdown(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        self._ensure_nmap_available()

        logger.info(
            "Slave fallback daemon started for interface=%s cloud=%s hub_env=%s",
            self.config.wifi_interface,
            self.config.cloud_endpoint,
            self.config.hub_env_file,
        )

        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("Tick failure: %s", exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.check_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        wifi_connected = self._is_wifi_connected(self.config.wifi_interface)

        if self._mode == "normal":
            if wifi_connected:
                self._wifi_down_count = 0
                return

            self._wifi_down_count += 1
            logger.warning(
                "WiFi appears down (%s/%s)",
                self._wifi_down_count,
                self.config.wifi_down_threshold,
            )

            if self._wifi_down_count < self.config.wifi_down_threshold:
                return

            relay_endpoint = await self._discover_master_with_retries(max_attempts=3)
            if not relay_endpoint:
                logger.warning("No master relay discovered yet; staying in normal mode")
                return

            await self._apply_endpoint(relay_endpoint, reason="wifi_down")
            self._active_relay_endpoint = relay_endpoint
            self._mode = "fallback"
            self._wifi_up_since = None
            self._last_master_health_check = 0.0
            logger.warning("Fallback mode active via %s", relay_endpoint)
            return

        # fallback mode
        if wifi_connected:
            if self._wifi_up_since is None:
                self._wifi_up_since = time.monotonic()

            stable_seconds = time.monotonic() - self._wifi_up_since
            if stable_seconds >= self.config.wifi_up_stable_seconds:
                await self._apply_endpoint(self.config.cloud_endpoint, reason="wifi_recovered")
                logger.info("WiFi stable for %.1fs; returning to cloud endpoint", stable_seconds)
                self._mode = "normal"
                self._wifi_down_count = 0
                self._wifi_up_since = None
                self._active_relay_endpoint = None
            return

        self._wifi_up_since = None
        await self._maintain_fallback_health()

    async def _maintain_fallback_health(self) -> None:
        now = time.monotonic()
        if now - self._last_master_health_check < self.config.master_health_check_seconds:
            return

        self._last_master_health_check = now

        if self._active_relay_endpoint and await self._endpoint_reachable(self._active_relay_endpoint):
            return

        logger.warning("Active relay endpoint is unreachable; rediscovering master")
        relay_endpoint = await self._discover_master_with_retries(max_attempts=3)
        if not relay_endpoint:
            logger.warning("Rediscovery failed; keeping current configuration")
            return

        if relay_endpoint != self._active_relay_endpoint:
            await self._apply_endpoint(relay_endpoint, reason="relay_failover")
            self._active_relay_endpoint = relay_endpoint
            logger.warning("Switched fallback relay endpoint to %s", relay_endpoint)

    async def _discover_master_with_retries(self, max_attempts: Optional[int]) -> Optional[str]:
        attempt = 1
        while not self._stop_event.is_set():
            relay_endpoint = await self._discover_master_once()
            if relay_endpoint:
                return relay_endpoint

            if max_attempts is not None and attempt >= max_attempts:
                return None

            delay = self.config.retry_policy.delay_for(attempt)
            logger.info("Master discovery retry in %.2fs", delay)
            attempt += 1
            await asyncio.sleep(delay)

        return None

    async def _discover_master_once(self) -> Optional[str]:
        scan_targets = self._scan_targets()
        if not scan_targets:
            logger.warning("No discovery candidates available")
            return None

        candidates = await self._scan_open_hosts_with_nmap(scan_targets)
        if not candidates:
            logger.warning("nmap found no hosts with discovery port %s open", self.config.discovery_port)
            return None

        semaphore = asyncio.Semaphore(self.config.discovery_workers)

        async def probe(host: str) -> Optional[str]:
            async with semaphore:
                return await self._probe_host(host)

        tasks = [asyncio.create_task(probe(host)) for host in candidates]
        try:
            for task in asyncio.as_completed(tasks):
                endpoint = await task
                if endpoint:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    return endpoint
            return None
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _scan_targets(self) -> List[str]:
        targets: List[str] = list(self.config.master_host_hints)

        for subnet_text in self.config.discovery_subnets:
            try:
                network = ipaddress.ip_network(subnet_text, strict=False)
            except ValueError:
                logger.warning("Ignoring invalid subnet %s", subnet_text)
                continue

            host_count = max(0, network.num_addresses - 2)
            if host_count > self.config.max_scan_hosts_per_subnet:
                logger.warning(
                    "Skipping subnet %s with %s hosts (limit=%s)",
                    subnet_text,
                    host_count,
                    self.config.max_scan_hosts_per_subnet,
                )
                continue

            targets.append(str(network))

        return dedupe_ordered(targets)

    async def _scan_open_hosts_with_nmap(self, scan_targets: List[str]) -> List[str]:
        logger.info(
            "Running nmap discovery on %s targets for port %s",
            len(scan_targets),
            self.config.discovery_port,
        )

        nmap_command = [
            self.config.nmap_binary,
            "-n",
            "-p",
            str(self.config.discovery_port),
            "--open",
            "--max-retries",
            str(max(0, self.config.nmap_max_retries)),
            "--host-timeout",
            f"{max(1, int(self.config.nmap_host_timeout_seconds * 1000))}ms",
            f"-T{self.config.nmap_timing_template}",
            "-oG",
            "-",
            *scan_targets,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *nmap_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"nmap is required but not found in PATH (configured binary: {self.config.nmap_binary})"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.nmap_scan_timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            logger.warning("nmap discovery timed out after %.1fs", self.config.nmap_scan_timeout_seconds)
            return []

        if process.returncode != 0:
            logger.warning(
                "nmap command failed (rc=%s): %s",
                process.returncode,
                (stderr.decode("utf-8", errors="ignore") if stderr else "").strip(),
            )
            return []

        output = stdout.decode("utf-8", errors="ignore") if stdout else ""
        discovered_hosts = self._parse_nmap_open_hosts(output)
        logger.info("nmap discovery found %s candidate hosts", len(discovered_hosts))
        return dedupe_ordered(discovered_hosts)

    def _parse_nmap_open_hosts(self, output: str) -> List[str]:
        hosts: List[str] = []
        expected_fragment = f"{self.config.discovery_port}/open"

        for line in output.splitlines():
            stripped = line.strip()
            if not stripped.startswith("Host:") or "Ports:" not in stripped:
                continue

            if expected_fragment not in stripped:
                continue

            parts = stripped.split()
            if len(parts) < 2:
                continue

            hosts.append(parts[1])

        return hosts

    def _ensure_nmap_available(self) -> None:
        if shutil.which(self.config.nmap_binary):
            return

        raise RuntimeError(
            "nmap is required for slave discovery but was not found. "
            "Install nmap and ensure it is available in PATH."
        )

    async def _probe_host(self, host: str) -> Optional[str]:
        writer: Optional[asyncio.StreamWriter] = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, self.config.discovery_port),
                timeout=self.config.discovery_connect_timeout,
            )

            request = "DISCOVER_MASTER"
            if self.config.discovery_shared_key:
                request = f"{request} {self.config.discovery_shared_key}"

            writer.write((request + "\n").encode("utf-8"))
            await writer.drain()

            raw_reply = await asyncio.wait_for(reader.readline(), timeout=self.config.discovery_read_timeout)
            if not raw_reply:
                return None

            payload = json.loads(raw_reply.decode("utf-8", errors="ignore"))
            if payload.get("type") != "master_ack":
                return None

            relay_endpoint = payload.get("relay_endpoint")
            if relay_endpoint:
                return str(relay_endpoint)

            relay_port = int(payload.get("relay_port", 5320))
            relay_path = str(payload.get("relay_path", self.config.relay_path))
            return f"ws://{host}:{relay_port}{relay_path}"
        except Exception:
            return None
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()

    async def _apply_endpoint(self, endpoint: str, reason: str) -> None:
        current_endpoint = read_env_value(self.config.hub_env_file, "SERVER_ENDPOINT")
        if current_endpoint == endpoint:
            logger.info("SERVER_ENDPOINT already set to %s (reason=%s)", endpoint, reason)
            return

        changed = upsert_env_value(self.config.hub_env_file, "SERVER_ENDPOINT", endpoint)
        if not changed:
            logger.info("No env file changes required for endpoint update")
            return

        logger.warning("Updated SERVER_ENDPOINT to %s (reason=%s)", endpoint, reason)
        await self._restart_hub_service_with_retries()

    async def _restart_hub_service_with_retries(self) -> None:
        attempt = 1
        while not self._stop_event.is_set():
            result = subprocess.run(
                ["systemctl", "restart", self.config.hub_service_name],
                capture_output=True,
                text=True,
                check=False,
            )

            if result.returncode == 0:
                logger.info("Restarted %s", self.config.hub_service_name)
                return

            delay = self.config.retry_policy.delay_for(attempt)
            logger.warning(
                "Failed to restart %s (attempt=%s, rc=%s): %s; retry in %.2fs",
                self.config.hub_service_name,
                attempt,
                result.returncode,
                (result.stderr or result.stdout or "unknown error").strip(),
                delay,
            )
            attempt += 1
            await asyncio.sleep(delay)

    async def _endpoint_reachable(self, endpoint: str) -> bool:
        parsed = urlparse(endpoint)
        if not parsed.hostname:
            return False

        if parsed.port is not None:
            port = parsed.port
        elif parsed.scheme == "wss":
            port = 443
        else:
            port = 80

        writer: Optional[asyncio.StreamWriter] = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname, port),
                timeout=self.config.discovery_connect_timeout,
            )
            return True
        except Exception:
            return False
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()

    @staticmethod
    def _is_wifi_connected(interface_name: str) -> bool:
        state_path = Path(f"/sys/class/net/{interface_name}/operstate")
        if not state_path.exists():
            return False

        try:
            state = state_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return False

        if state != "up":
            return False

        addr_result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", interface_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return bool(addr_result.stdout.strip())


async def _main() -> None:
    config = load_config()
    setup_logging(get_env_str("LOG_LEVEL", "INFO") or "INFO")
    daemon = SlaveFallbackDaemon(config)

    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, daemon.request_shutdown)
        except NotImplementedError:
            # add_signal_handler may not be available on some platforms.
            pass

    await daemon.run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
