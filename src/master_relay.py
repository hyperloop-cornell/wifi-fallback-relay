from __future__ import annotations

import asyncio
import json
import logging
import signal
import ssl
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed

from .common import (
    RetryPolicy,
    get_env_bool,
    get_env_float,
    get_env_int,
    get_env_str,
    load_env,
    setup_logging,
)

logger = logging.getLogger("master_relay")


@dataclass(frozen=True)
class MasterRelayConfig:
    master_id: str
    cloud_endpoint: str
    discovery_bind_host: str
    discovery_port: int
    relay_bind_host: str
    relay_port: int
    relay_path: str
    relay_public_host: Optional[str]
    discovery_shared_key: Optional[str]
    max_slave_connections: int
    ws_ping_interval: int
    ws_ping_timeout: int
    connect_timeout: float
    ssl_verify: bool
    retry_policy: RetryPolicy


def load_config() -> MasterRelayConfig:
    load_env(get_env_str("RELAY_ENV_FILE"))

    cloud_endpoint = get_env_str("CLOUD_ENDPOINT")
    if not cloud_endpoint:
        raise RuntimeError("CLOUD_ENDPOINT is required for master relay")

    retry_policy = RetryPolicy(
        base_seconds=get_env_float("RETRY_BASE_SECONDS", 1.0),
        max_seconds=get_env_float("RETRY_MAX_SECONDS", 60.0),
        jitter_ratio=get_env_float("RETRY_JITTER_RATIO", 0.30),
    )

    return MasterRelayConfig(
        master_id=get_env_str("MASTER_ID", "rpi-master") or "rpi-master",
        cloud_endpoint=cloud_endpoint,
        discovery_bind_host=get_env_str("DISCOVERY_BIND_HOST", "0.0.0.0") or "0.0.0.0",
        discovery_port=get_env_int("DISCOVERY_PORT", 5319),
        relay_bind_host=get_env_str("RELAY_BIND_HOST", "0.0.0.0") or "0.0.0.0",
        relay_port=get_env_int("RELAY_PORT", 5320),
        relay_path=get_env_str("RELAY_PATH", "/hub") or "/hub",
        relay_public_host=get_env_str("RELAY_PUBLIC_HOST"),
        discovery_shared_key=get_env_str("DISCOVERY_SHARED_KEY"),
        max_slave_connections=get_env_int("MAX_SLAVE_CONNECTIONS", 64),
        ws_ping_interval=get_env_int("WS_PING_INTERVAL", 20),
        ws_ping_timeout=get_env_int("WS_PING_TIMEOUT", 10),
        connect_timeout=get_env_float("CONNECT_TIMEOUT", 15.0),
        ssl_verify=get_env_bool("CLOUD_SSL_VERIFY", True),
        retry_policy=retry_policy,
    )


class MasterRelayDaemon:
    def __init__(self, config: MasterRelayConfig) -> None:
        self.config = config
        self._stop_event = asyncio.Event()
        self._active_slaves = 0
        self._counter_lock = asyncio.Lock()
        self._discovery_server: Optional[asyncio.AbstractServer] = None
        self._relay_server = None
        self._ssl_context = self._build_ssl_context(config.cloud_endpoint, config.ssl_verify)

    def request_shutdown(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        self._discovery_server = await asyncio.start_server(
            self._handle_discovery_connection,
            host=self.config.discovery_bind_host,
            port=self.config.discovery_port,
        )

        self._relay_server = await websockets.serve(
            self._handle_slave_ws,
            host=self.config.relay_bind_host,
            port=self.config.relay_port,
            ping_interval=self.config.ws_ping_interval,
            ping_timeout=self.config.ws_ping_timeout,
            max_size=None,
        )

        logger.info(
            "Master relay ready: discovery=%s:%s relay=%s:%s%s cloud=%s",
            self.config.discovery_bind_host,
            self.config.discovery_port,
            self.config.relay_bind_host,
            self.config.relay_port,
            self.config.relay_path,
            self.config.cloud_endpoint,
        )

        await self._stop_event.wait()
        await self._shutdown_servers()

    async def _shutdown_servers(self) -> None:
        if self._relay_server:
            self._relay_server.close()
            await self._relay_server.wait_closed()

        if self._discovery_server:
            self._discovery_server.close()
            await self._discovery_server.wait_closed()

    async def _handle_discovery_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_text = str(peer)

        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if not raw:
                return

            request = raw.decode("utf-8", errors="ignore").strip()
            request_parts = request.split()

            if not request_parts or request_parts[0] != "DISCOVER_MASTER":
                writer.write(b'{"error":"bad_request"}\n')
                await writer.drain()
                return

            if self.config.discovery_shared_key:
                provided_key = request_parts[1] if len(request_parts) > 1 else ""
                if provided_key != self.config.discovery_shared_key:
                    writer.write(b'{"error":"unauthorized"}\n')
                    await writer.drain()
                    logger.warning("Rejected discovery request from %s due to key mismatch", peer_text)
                    return

            local_addr = writer.get_extra_info("sockname")
            detected_host = local_addr[0] if local_addr else self.config.relay_bind_host
            relay_host = self.config.relay_public_host or detected_host
            if relay_host in {"0.0.0.0", "::"}:
                relay_host = "127.0.0.1"

            payload = {
                "type": "master_ack",
                "master_id": self.config.master_id,
                "relay_endpoint": f"ws://{relay_host}:{self.config.relay_port}{self.config.relay_path}",
                "relay_port": self.config.relay_port,
                "relay_path": self.config.relay_path,
                "timestamp": int(time.time()),
            }
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as exc:
            logger.debug("Discovery error with %s: %s", peer_text, exc)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_slave_ws(self, websocket, path=None) -> None:
        request_path = path if path is not None else getattr(websocket, "path", "")
        if request_path != self.config.relay_path:
            logger.warning("Rejected websocket path: %s", request_path)
            await websocket.close(code=1008, reason="invalid path")
            return

        peer = websocket.remote_address
        peer_text = str(peer)

        async with self._counter_lock:
            if self._active_slaves >= self.config.max_slave_connections:
                logger.warning("Rejecting %s: max slave connections reached", peer_text)
                await websocket.close(code=1013, reason="server busy")
                return
            self._active_slaves += 1

        logger.info("Slave connected: %s (active=%s)", peer_text, self._active_slaves)

        try:
            await self._relay_slave_traffic(websocket, peer_text)
        finally:
            async with self._counter_lock:
                self._active_slaves = max(0, self._active_slaves - 1)
            logger.info("Slave disconnected: %s (active=%s)", peer_text, self._active_slaves)

    async def _relay_slave_traffic(self, slave_ws, peer_text: str) -> None:
        reconnect_attempt = 1

        while not self._stop_event.is_set() and not slave_ws.closed:
            try:
                upstream_ws = await self._connect_upstream(peer_text)
                reconnect_attempt = 1
                await self._proxy_until_disconnect(slave_ws, upstream_ws, peer_text)
            except ConnectionClosed:
                if slave_ws.closed:
                    break
                delay = self.config.retry_policy.delay_for(reconnect_attempt)
                logger.warning(
                    "Cloud relay dropped for %s; reconnecting in %.2fs",
                    peer_text,
                    delay,
                )
                reconnect_attempt += 1
                await asyncio.sleep(delay)
            except Exception as exc:
                if slave_ws.closed:
                    break
                delay = self.config.retry_policy.delay_for(reconnect_attempt)
                logger.warning(
                    "Relay error for %s: %s; reconnecting in %.2fs",
                    peer_text,
                    exc,
                    delay,
                )
                reconnect_attempt += 1
                await asyncio.sleep(delay)

    async def _connect_upstream(self, peer_text: str):
        attempt = 1

        while not self._stop_event.is_set():
            try:
                logger.debug("Connecting upstream for %s to %s", peer_text, self.config.cloud_endpoint)
                return await websockets.connect(
                    self.config.cloud_endpoint,
                    ssl=self._ssl_context,
                    ping_interval=self.config.ws_ping_interval,
                    ping_timeout=self.config.ws_ping_timeout,
                    open_timeout=self.config.connect_timeout,
                    max_size=None,
                )
            except Exception as exc:
                delay = self.config.retry_policy.delay_for(attempt)
                logger.warning(
                    "Upstream connect failed for %s (attempt=%s): %s; retry in %.2fs",
                    peer_text,
                    attempt,
                    exc,
                    delay,
                )
                attempt += 1
                await asyncio.sleep(delay)

        raise asyncio.CancelledError("Shutdown requested")

    async def _proxy_until_disconnect(self, slave_ws, upstream_ws, peer_text: str) -> None:
        async def forward(source_ws, target_ws, direction: str) -> None:
            async for payload in source_ws:
                await target_ws.send(payload)

        slave_to_cloud = asyncio.create_task(forward(slave_ws, upstream_ws, "slave_to_cloud"))
        cloud_to_slave = asyncio.create_task(forward(upstream_ws, slave_ws, "cloud_to_slave"))

        done, pending = await asyncio.wait(
            {slave_to_cloud, cloud_to_slave},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, ConnectionClosed):
                logger.debug("Proxy task ended with error for %s: %s", peer_text, exc)
                raise exc

        if not upstream_ws.closed:
            await upstream_ws.close()

    @staticmethod
    def _build_ssl_context(endpoint: str, verify: bool) -> Optional[ssl.SSLContext]:
        parsed = urlparse(endpoint)
        if parsed.scheme != "wss":
            return None

        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context


async def _main() -> None:
    config = load_config()
    setup_logging(get_env_str("LOG_LEVEL", "INFO") or "INFO")
    daemon = MasterRelayDaemon(config)

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
