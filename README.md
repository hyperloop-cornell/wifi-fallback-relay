# WiFi Fallback Relay (Standalone)

Standalone fallback package that is separate from rpi-hub-server code.

- Master Pi runs a relay daemon.
- Slave Pi runs a WiFi fallback daemon.
- When WiFi is healthy, slave stays on cloud endpoint.
- When WiFi is down, slave discovers master on TCP 5319 and switches SERVER_ENDPOINT to master relay.

## Prerequisites

- Python 3
- systemd
- nmap (required on slave nodes)

Install nmap on Raspberry Pi OS:

```bash
sudo apt-get update
sudo apt-get install -y nmap
```

The installer also checks for nmap and fails fast if missing.

## Discovery protocol

Slave discovery is nmap-first:

1. Slave scans configured targets with nmap for hosts with TCP 5319 open.
2. Slave sends DISCOVER_MASTER only to nmap-positive hosts.
3. First valid master_ack response is selected.

Slave sends one line to TCP port 5319:

DISCOVER_MASTER

Or with optional shared key:

DISCOVER_MASTER <DISCOVERY_SHARED_KEY>

Master replies JSON with relay endpoint:

{"type":"master_ack","master_id":"rpi-master-01","relay_endpoint":"ws://<master-ip>:5320/hub"}

## Install on any Pi

```bash
cd /path/to/electrical-gui/wifi-fallback-relay
bash scripts/install.sh
```

Edit env file created at:

- /etc/wifi-fallback-relay/config.env

Then start services:

```bash
bash scripts/start-services.sh master
# or
bash scripts/start-services.sh slave
```

## Required config for master

- MASTER_ID
- CLOUD_ENDPOINT
- DISCOVERY_PORT (default 5319)
- RELAY_PORT (default 5320)

## Required config for slave

- HUB_ENV_FILE (path to rpi-hub-server .env)
- HUB_SERVICE_NAME (systemd unit for rpi-hub-server)
- CLOUD_ENDPOINT (explicit recommended)
- MASTER_HOST_HINTS and/or DISCOVERY_SUBNETS

## Retry and redundancy

- Exponential backoff with jitter for discovery and reconnect.
- Infinite retry loops for service-level resilience.
- Slave only switches to fallback after consecutive WiFi failures.
- Slave only switches back after WiFi stability window.
- Fallback mode periodically checks relay reachability and rediscovers if needed.

## Operational flow you asked for

1. git pull
2. set token and endpoint values in /etc/wifi-fallback-relay/config.env
3. run installer script
4. run start script to launch systemd services

## Notes

- Keep tokens out of git-tracked files.
- Rotate any token that was accidentally exposed.
