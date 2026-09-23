# Uplink Manager (wifi-fallback-relay)

Keeps the **cellular hub** online: Wi-Fi when it works, the cellular HAT when it does not.
It runs only on the one Raspberry Pi with a cellular HAT; lab hubs do not need it.

This repository used to hold a master/slave relay. That design is gone: hubs no longer relay
through each other, and nothing here edits or restarts `rpi-hub-server` anymore.

## What it does

Every `CHECK_INTERVAL_SECONDS`:

1. Probes each link by opening TCP connections to `PROBE_TARGETS`, forced out of that interface
   (`SO_BINDTODEVICE`). Resolved addresses are cached so a dead default route (which also breaks
   DNS) does not stop the other link from being tested.
2. Decides which link the default route should prefer (`src/policy.py`):
   - Wi-Fi is preferred. After `WIFI_DOWN_THRESHOLD` consecutive failed Wi-Fi probes it switches
     to cellular, if cellular works.
   - It returns to Wi-Fi once Wi-Fi has probed good for `WIFI_UP_STABLE_SECONDS`, or immediately if
     cellular fails while Wi-Fi works.
3. When the preference changes, sets NetworkManager route metrics (lower wins):
   `nmcli device modify <iface> ipv4.route-metric <n>`. Open sockets on the old link break and
   applications reconnect over the new one; rpi-hub-server's uplink reconnects on its own.
4. Atomically writes `STATUS_FILE` (default `/run/hyperloop-uplink/status.json`), which
   rpi-hub-server includes in its health reports so the GUI can show Wi-Fi/cellular state.

## Status file

```json
{
  "active": "wifi",
  "preferred": "wifi",
  "since": "2026-09-23T12:00:00Z",
  "updated_at": "2026-09-23T12:05:00Z",
  "wifi": {"interface": "wlan0", "link": true, "probe_ok": true, "rtt_ms": 31.2, "rssi_dbm": -55.0},
  "cellular": {"interface": "wwan0", "enabled": true, "link": true, "probe_ok": true, "rtt_ms": 88.0,
               "state": "connected", "signal_quality": 72, "access_tech": "lte"}
}
```

- `active`: the link that can carry traffic now (`wifi`, `cellular` or `none`).
- `preferred`: the link the default route prefers.
- rpi-hub-server treats the file as stale when `updated_at` is older than 30 seconds.

## Install (cellular hub only)

Full walkthrough, including the NetworkManager cellular connection: `.claude/rpi-hub-setup.md`
(section "Cellular hub") in the hyperloop-gui repository. In short:

```bash
sudo git clone https://github.com/hyperloop-cornell/wifi-fallback-relay.git /opt/hyperloop-uplink
sudo python3 -m venv /opt/hyperloop-uplink/.venv
sudo /opt/hyperloop-uplink/.venv/bin/pip install -r /opt/hyperloop-uplink/requirements.txt
sudo mkdir -p /etc/hyperloop-uplink
sudo cp /opt/hyperloop-uplink/.env.example /etc/hyperloop-uplink/config.env   # then edit it
sudo cp /opt/hyperloop-uplink/systemd/hyperloop-uplink.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperloop-uplink
journalctl -u hyperloop-uplink -f
```

Set `DRY_RUN=true` first to see the routing commands it would run without changing anything.

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `WIFI_INTERFACE` | `wlan0` | Wi-Fi interface |
| `CELLULAR_INTERFACE` | `wwan0` | Cellular interface (`nmcli device status`) |
| `CELLULAR_CONNECTION` | (empty) | NetworkManager connection name for the HAT; empty = Wi-Fi only |
| `PROBE_TARGETS` | `api.cornellhyperloop.com:443,1.1.1.1:443` | Any successful connect = link usable |
| `CHECK_INTERVAL_SECONDS` | `5` | Probe period |
| `WIFI_DOWN_THRESHOLD` | `3` | Failed Wi-Fi probes before switching to cellular |
| `WIFI_UP_STABLE_SECONDS` | `60` | Good Wi-Fi time before switching back |
| `CELLULAR_WARM_STANDBY` | `true` | Keep cellular connected while on Wi-Fi |
| `STATUS_FILE` | `/run/hyperloop-uplink/status.json` | Must match rpi-hub-server `uplink.status_file` |
| `DRY_RUN` | `false` | Log routing commands instead of running them |

## Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```
