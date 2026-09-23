"""Uplink manager: policy, probes' parsers, routing commands, status file, full ticks."""

import json
import subprocess
from pathlib import Path

import pytest

from src import uplink_manager
from src.policy import CELLULAR, NONE, WIFI, UplinkPolicy
from src.probes import parse_iw_link, parse_mmcli_modem, parse_proc_net_wireless, parse_targets
from src.routes import RouteController
from src.status import write_status


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# --------------------------------------------------------------------------- policy


def test_starts_on_wifi():
    policy = UplinkPolicy(clock=FakeClock())
    decision = policy.step(wifi_ok=True, cellular_ok=True)
    assert (decision.preferred, decision.active, decision.changed) == (WIFI, WIFI, False)


def test_switches_to_cellular_after_threshold():
    policy = UplinkPolicy(wifi_down_threshold=3, clock=FakeClock())
    assert policy.step(False, True).preferred == WIFI
    assert policy.step(False, True).preferred == WIFI
    decision = policy.step(False, True)
    assert decision.preferred == CELLULAR and decision.changed and decision.active == CELLULAR


def test_single_wifi_blip_does_not_switch():
    policy = UplinkPolicy(wifi_down_threshold=3, clock=FakeClock())
    policy.step(False, True)
    policy.step(False, True)
    policy.step(True, True)
    assert policy.step(False, True).preferred == WIFI


def test_returns_to_wifi_only_after_stable_period():
    clock = FakeClock()
    policy = UplinkPolicy(wifi_down_threshold=1, wifi_up_stable_seconds=60, clock=clock)
    assert policy.step(False, True).preferred == CELLULAR

    clock.now = 10
    assert policy.step(True, True).preferred == CELLULAR  # good since t=10
    clock.now = 40
    assert policy.step(False, True).preferred == CELLULAR  # flap resets the timer
    clock.now = 50
    policy.step(True, True)
    clock.now = 109
    assert policy.step(True, True).preferred == CELLULAR
    clock.now = 110
    decision = policy.step(True, True)
    assert decision.preferred == WIFI and decision.changed


def test_cellular_failure_with_working_wifi_switches_back_immediately():
    policy = UplinkPolicy(wifi_down_threshold=1, wifi_up_stable_seconds=600, clock=FakeClock())
    policy.step(False, True)
    decision = policy.step(True, False)
    assert decision.preferred == WIFI and decision.active == WIFI


def test_does_not_switch_when_cellular_also_down():
    policy = UplinkPolicy(wifi_down_threshold=1, clock=FakeClock())
    decision = policy.step(False, False)
    assert decision.preferred == WIFI and decision.active == NONE


def test_hub_without_cellular_never_prefers_it():
    policy = UplinkPolicy(wifi_down_threshold=1, clock=FakeClock(), has_cellular=False)
    decision = policy.step(False, True)
    assert decision.preferred == WIFI and decision.active == NONE


# --------------------------------------------------------------------------- parsers


def test_parse_targets():
    assert parse_targets(["api.example.com:443", "1.1.1.1:80", "host"]) == [
        ("api.example.com", 443), ("1.1.1.1", 80), ("host", 443),
    ]


def test_parse_proc_net_wireless():
    content = (
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   frag\n"
        "wlan0: 0000   58.  -52.  -256        0      0      0\n"
    )
    assert parse_proc_net_wireless(content, "wlan0") == -52.0
    assert parse_proc_net_wireless(content, "wlan1") is None


def test_parse_iw_link():
    assert parse_iw_link("Connected to aa:bb\n\tSSID: lab\n\tsignal: -61 dBm\n") == -61.0
    assert parse_iw_link("Not connected.") is None


def test_parse_mmcli_modem():
    output = json.dumps({"modem": {"generic": {
        "state": "connected", "signal-quality": {"value": "72", "recent": "yes"}, "access-technologies": ["lte"],
    }}})
    assert parse_mmcli_modem(output) == {"state": "connected", "signal_quality": 72, "access_tech": "lte"}
    assert parse_mmcli_modem("not json") == {}


# --------------------------------------------------------------------------- routing


def test_route_commands_prefer_wifi_with_warm_standby():
    routes = RouteController("wlan0", "wwan0", "cellular", warm_standby=True)
    assert routes.commands_for(WIFI) == [
        ["nmcli", "connection", "up", "cellular"],
        ["nmcli", "device", "modify", "wlan0", "ipv4.route-metric", "100"],
        ["nmcli", "device", "modify", "wwan0", "ipv4.route-metric", "700"],
    ]


def test_route_commands_prefer_cellular():
    routes = RouteController("wlan0", "wwan0", "cellular", warm_standby=False)
    assert routes.commands_for(CELLULAR) == [
        ["nmcli", "connection", "up", "cellular"],
        ["nmcli", "device", "modify", "wlan0", "ipv4.route-metric", "700"],
        ["nmcli", "device", "modify", "wwan0", "ipv4.route-metric", "100"],
    ]
    # Without warm standby the cellular connection is dropped when back on Wi-Fi
    assert routes.commands_for(WIFI)[-1] == ["nmcli", "connection", "down", "cellular"]


def test_route_commands_without_cellular():
    routes = RouteController("wlan0", "wwan0", None)
    assert routes.commands_for(WIFI) == [["nmcli", "device", "modify", "wlan0", "ipv4.route-metric", "100"]]


def test_route_apply_reports_failures_and_dry_run():
    calls = []

    def runner(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1 if "wwan0" in args else 0, "", "no such device")

    assert RouteController("wlan0", "wwan0", "cellular", runner=runner).apply(WIFI) is False
    assert len(calls) == 3

    calls.clear()
    assert RouteController("wlan0", "wwan0", "cellular", dry_run=True, runner=runner).apply(WIFI) is True
    assert calls == []


# --------------------------------------------------------------------------- status file


def test_write_status_is_atomic_and_readable(tmp_path):
    path = tmp_path / "run" / "status.json"
    write_status(path, {"active": "wifi"})
    write_status(path, {"active": "cellular"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"active": "cellular"}
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]  # no temp files left


# --------------------------------------------------------------------------- manager


class FakeProbe:
    def __init__(self):
        self.ok = {"wlan0": True, "wwan0": True}

    def rtt_ms(self, interface):
        return 25.0 if self.ok.get(interface) else None


class FakeRoutes:
    def __init__(self):
        self.applied = []

    def apply(self, preferred):
        self.applied.append(preferred)
        return True


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(uplink_manager, "interface_up", lambda iface: True)
    monkeypatch.setattr(uplink_manager, "wifi_rssi_dbm", lambda iface: -50.0)
    monkeypatch.setattr(uplink_manager, "modem_status", lambda: {"state": "connected", "signal_quality": 70, "access_tech": "lte"})
    monkeypatch.setenv("CELLULAR_CONNECTION", "cellular")
    monkeypatch.setenv("WIFI_DOWN_THRESHOLD", "2")
    monkeypatch.setenv("STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setenv("UPLINK_ENV_FILE", str(tmp_path / "missing.env"))
    config = uplink_manager.load_config()
    probe, routes = FakeProbe(), FakeRoutes()
    return uplink_manager.UplinkManager(config, probe=probe, routes=routes), probe, routes


def read_status(manager):
    return json.loads(Path(manager.config.status_file).read_text(encoding="utf-8"))


def test_first_tick_applies_routing_and_writes_status(manager):
    mgr, _, routes = manager
    mgr.tick()
    status = read_status(mgr)
    assert routes.applied == [WIFI]
    assert status["active"] == "wifi"
    assert status["wifi"] == {"interface": "wlan0", "link": True, "probe_ok": True, "rtt_ms": 25.0, "rssi_dbm": -50.0}
    assert status["cellular"]["access_tech"] == "lte"
    assert status["updated_at"].endswith("Z")


def test_failover_and_recovery(manager, monkeypatch):
    mgr, probe, routes = manager
    clock = FakeClock()
    mgr.policy.clock = clock

    mgr.tick()
    probe.ok["wlan0"] = False
    mgr.tick()
    assert read_status(mgr)["active"] == "cellular"  # carried by cellular before the switch
    mgr.tick()
    assert routes.applied == [WIFI, CELLULAR]
    assert read_status(mgr)["preferred"] == "cellular"

    probe.ok["wlan0"] = True
    clock.now = 1
    mgr.tick()
    clock.now = 1 + mgr.config.wifi_up_stable_seconds
    mgr.tick()
    assert routes.applied == [WIFI, CELLULAR, WIFI]
    assert read_status(mgr)["active"] == "wifi"


def test_config_defaults(monkeypatch, tmp_path):
    for var in ("CELLULAR_CONNECTION", "CELLULAR_ENABLED", "PROBE_TARGETS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("UPLINK_ENV_FILE", str(tmp_path / "missing.env"))
    config = uplink_manager.load_config()
    assert config.cellular_enabled is False  # no connection name -> Wi-Fi only
    assert config.probe_targets == ["api.cornellhyperloop.com:443", "1.1.1.1:443"]
    assert config.status_file == Path("/run/hyperloop-uplink/status.json")
