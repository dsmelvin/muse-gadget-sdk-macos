# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import subprocess

from musegadget import network

WPA_STATUS = """bssid=00:11:22:33:44:55
freq=5180
ssid=Example Net
id=0
mode=station
wifi_generation=6
pairwise_cipher=CCMP
group_cipher=CCMP
key_mgmt=WPA2-PSK
wpa_state=COMPLETED
ip_address=192.168.1.20
"""


def _fake_run(output: str):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
    return run


def _sysfs(monkeypatch, tmp_path, interfaces):
    """Point the module's sysfs lookup at tmp_path."""
    for name, wireless in interfaces:
        (tmp_path / name).mkdir()
        if wireless:
            (tmp_path / name / "wireless").mkdir()

    from pathlib import Path as RealPath

    def fake_path(arg):
        return tmp_path if str(arg) == "/sys/class/net" else RealPath(arg)

    monkeypatch.setattr(network, "Path", fake_path, raising=False)


def test_reports_the_ssid_on_a_host_networkmanager_does_not_manage(monkeypatch, tmp_path):
    """netplan / systemd-networkd drive wpa_supplicant directly, so nmcli
    reports every device as unmanaged and lists no networks even while Wi-Fi is
    associated. Without a second source this returned None, so
    current_connection_entry() fell back to CURRENT_CONNECTION_LABEL and the
    Muse app stopped before sending provision_v2.
    """
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/bin/" + name)
    _sysfs(monkeypatch, tmp_path, [("eth0", False), ("wlan0", True)])

    def run(argv, **kwargs):
        if argv[0] == "nmcli":
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "wpa_cli":
            return subprocess.CompletedProcess(argv, 0, stdout=WPA_STATUS, stderr="")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(network.subprocess, "run", run)

    assert network.active_wifi_ssid() == "Example Net"
    assert network.current_connection_entry()["ssid"] == "Example Net"


def test_wpa_cli_is_not_used_when_nmcli_answers(monkeypatch):
    monkeypatch.setattr(network, "_ssid_from_nmcli", lambda: "From NM")
    monkeypatch.setattr(network, "_ssid_from_wpa_cli", lambda: "From wpa")
    assert network.active_wifi_ssid() == "From NM"


def test_falls_back_to_wpa_cli_when_nmcli_finds_nothing(monkeypatch):
    monkeypatch.setattr(network, "_ssid_from_nmcli", lambda: None)
    monkeypatch.setattr(network, "_ssid_from_wpa_cli", lambda: "From wpa")
    assert network.active_wifi_ssid() == "From wpa"


def test_wpa_cli_reads_the_ssid_of_an_associated_interface(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/sbin/wpa_cli")
    monkeypatch.setattr(network, "_wireless_interfaces", lambda: ["wlan0"])
    monkeypatch.setattr(network.subprocess, "run", _fake_run(WPA_STATUS))
    assert network._ssid_from_wpa_cli() == "Example Net"


def test_wpa_cli_decodes_an_escaped_ssid(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/sbin/wpa_cli")
    monkeypatch.setattr(network, "_wireless_interfaces", lambda: ["wlan0"])
    monkeypatch.setattr(
        network.subprocess, "run",
        _fake_run("wpa_state=COMPLETED\nssid=\\xf0\\x9f\\x8f\\xa0 Caf\\xc3\\xa9 \n"),
    )
    assert network._ssid_from_wpa_cli() == "\U0001f3e0 Café "


def test_wpa_unescape():
    for printed, ssid in (
        ("Example Net", "Example Net"),
        ("Caf\\xc3\\xa9", "Café"),
        ('a\\\\b\\"c', 'a\\b"c'),
        ("\\\\x41", "\\x41"),
        ("tab\\there", "tab\there"),
        ("bad\\xzz", "bad\\xzz"),
        ("Latin-1 \\xe9", "Latin-1 �"),
    ):
        assert network._wpa_unescape(printed) == ssid


def test_nmcli_unescapes_colons_and_backslashes(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(
        network.subprocess, "run", _fake_run("no:Other\nyes:My\\:Net\\\\5G\n")
    )
    assert network._ssid_from_nmcli() == "My:Net\\5G"


def test_wpa_cli_ignores_an_interface_that_is_not_associated(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/sbin/wpa_cli")
    monkeypatch.setattr(network, "_wireless_interfaces", lambda: ["wlan0"])
    monkeypatch.setattr(
        network.subprocess, "run", _fake_run("wpa_state=SCANNING\nssid=Example Net\n")
    )
    assert network._ssid_from_wpa_cli() is None


def test_wpa_cli_is_skipped_when_the_binary_is_missing(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: None)
    assert network._ssid_from_wpa_cli() is None


def test_wpa_cli_survives_a_timeout(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/sbin/wpa_cli")
    monkeypatch.setattr(network, "_wireless_interfaces", lambda: ["wlan0"])

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(network.subprocess, "run", boom)
    assert network._ssid_from_wpa_cli() is None


def test_wireless_interfaces_reads_sysfs(monkeypatch, tmp_path):
    for name, wireless in (("eth0", False), ("wlan1", True), ("wlan0", True)):
        (tmp_path / name).mkdir()
        if wireless:
            (tmp_path / name / "wireless").mkdir()

    real_path = network.Path

    def fake_path(arg):
        return tmp_path if str(arg) == "/sys/class/net" else real_path(arg)

    monkeypatch.setattr(network, "Path", fake_path)
    assert network._wireless_interfaces() == ["wlan0", "wlan1"]


def test_wireless_interfaces_is_empty_without_sysfs(monkeypatch):
    def boom(arg):
        raise OSError("no sysfs")

    monkeypatch.setattr(network, "Path", boom)
    assert network._wireless_interfaces() == []


def test_current_connection_entry_uses_the_label_without_an_ssid(monkeypatch):
    monkeypatch.delenv("MUSE_WIFI_SECURE", raising=False)
    monkeypatch.setattr(network, "active_wifi_ssid", lambda: None)
    entry = network.current_connection_entry()
    assert entry["ssid"] == network.CURRENT_CONNECTION_LABEL
    assert entry["secure"] is False


def test_current_connection_entry_prefers_the_real_ssid(monkeypatch):
    monkeypatch.delenv("MUSE_WIFI_SECURE", raising=False)
    monkeypatch.setattr(network, "active_wifi_ssid", lambda: "Example Net")
    entry = network.current_connection_entry()
    assert entry["ssid"] == "Example Net"
    assert entry["secure"] is True


def test_current_connection_entry_secure_env_override(monkeypatch):
    monkeypatch.setattr(network, "active_wifi_ssid", lambda: "Example Net")
    for val in ("0", "false", "False", "no", "NO"):
        monkeypatch.setenv("MUSE_WIFI_SECURE", val)
        assert network.current_connection_entry()["secure"] is False

    monkeypatch.setattr(network, "active_wifi_ssid", lambda: None)
    for val in ("1", "true", "True", "yes", "YES"):
        monkeypatch.setenv("MUSE_WIFI_SECURE", val)
        assert network.current_connection_entry()["secure"] is True


def test_active_wifi_ssid_env_override(monkeypatch):
    monkeypatch.setenv("MUSE_WIFI_SSID", "OverrideNetwork")
    assert network.active_wifi_ssid() == "OverrideNetwork"
    assert network.current_connection_entry()["ssid"] == "OverrideNetwork"


def test_macos_wifi_interfaces_discovers_wifi(monkeypatch):
    ports_output = """Hardware Port: Ethernet Adapter (en4)
Device: en4

Hardware Port: Wi-Fi
Device: en0

Hardware Port: Thunderbolt 1
Device: en1
"""
    monkeypatch.setattr(network.shutil, "which", lambda cmd: "/usr/sbin/" + cmd)
    monkeypatch.setattr(network.subprocess, "run", _fake_run(ports_output))
    ifaces = network._macos_wifi_interfaces()
    assert ifaces[0] == "en0"
    assert "en1" in ifaces
    assert "en2" in ifaces
    assert "en3" in ifaces


def test_macos_wifi_interfaces_fallback_when_missing(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda cmd: None)
    assert network._macos_wifi_interfaces() == ["en0", "en1", "en2", "en3"]


def test_ssid_from_ipconfig_parses_ssid(monkeypatch):
    ipconfig_out = """<dictionary> {
  InterfaceType : WiFi
  LinkStatusActive : TRUE
  SSID : TestHomeWiFi
}
"""
    monkeypatch.setattr(network.shutil, "which", lambda cmd: "/usr/sbin/ipconfig")
    monkeypatch.setattr(network.subprocess, "run", _fake_run(ipconfig_out))
    assert network._ssid_from_ipconfig("en0") == "TestHomeWiFi"


def test_ssid_from_ipconfig_returns_none_when_empty(monkeypatch):
    ipconfig_out = """<dictionary> {
  InterfaceType : Ethernet
}
"""
    monkeypatch.setattr(network.shutil, "which", lambda cmd: "/usr/sbin/ipconfig")
    monkeypatch.setattr(network.subprocess, "run", _fake_run(ipconfig_out))
    assert network._ssid_from_ipconfig("en0") is None


def test_ssid_from_airport(monkeypatch):
    airport_out = """     agrCtlRSSI: -45
          BSSID: 0:11:22:33:44:55
           SSID: AirportWiFi
            MCS: 9
"""
    monkeypatch.setattr(network.Path, "is_file", lambda self: True)
    monkeypatch.setattr(network.subprocess, "run", _fake_run(airport_out))
    assert network._ssid_from_airport() == "AirportWiFi"


def test_ssid_from_networksetup(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda cmd: "/usr/sbin/networksetup")
    monkeypatch.setattr(
        network.subprocess, "run",
        _fake_run("Current Wi-Fi Network: SetupNetwork\n"),
    )
    assert network._ssid_from_networksetup("en0") == "SetupNetwork"


def test_ssid_from_networksetup_not_associated(monkeypatch):
    monkeypatch.setattr(network.shutil, "which", lambda cmd: "/usr/sbin/networksetup")
    monkeypatch.setattr(
        network.subprocess, "run",
        _fake_run("You are not associated with an AirPort network.\n"),
    )
    assert network._ssid_from_networksetup("en0") is None


def test_ssid_from_system_profiler(monkeypatch):
    profiler_out = """Wi-Fi:
      Interfaces:
        en0:
          Card Type: Wi-Fi
          Status: Connected
          Current Network Information:
            ProfilerNetwork:
              PHY Mode: 802.11ax
              Channel: 48
"""
    monkeypatch.setattr(network.shutil, "which", lambda cmd: "/usr/sbin/system_profiler")
    monkeypatch.setattr(network.subprocess, "run", _fake_run(profiler_out))
    assert network._ssid_from_system_profiler() == "ProfilerNetwork"


def test_ssid_from_macos_prefers_unredacted(monkeypatch):
    monkeypatch.delenv("MUSE_WIFI_SSID", raising=False)
    monkeypatch.setattr(network, "_macos_wifi_interfaces", lambda: ["en0"])
    monkeypatch.setattr(network, "_ssid_from_ipconfig", lambda iface: "<redacted>")
    monkeypatch.setattr(network, "_ssid_from_airport", lambda: None)
    monkeypatch.setattr(network, "_ssid_from_networksetup", lambda iface: None)
    monkeypatch.setattr(network, "_ssid_from_system_profiler", lambda: "RealNetwork")
    assert network._ssid_from_macos() == "RealNetwork"


def test_ssid_from_macos_falls_back_to_redacted_if_only_redacted(monkeypatch):
    monkeypatch.delenv("MUSE_WIFI_SSID", raising=False)
    monkeypatch.setattr(network, "_macos_wifi_interfaces", lambda: ["en0"])
    monkeypatch.setattr(network, "_ssid_from_ipconfig", lambda iface: "<redacted>")
    monkeypatch.setattr(network, "_ssid_from_airport", lambda: None)
    monkeypatch.setattr(network, "_ssid_from_networksetup", lambda iface: None)
    monkeypatch.setattr(network, "_ssid_from_system_profiler", lambda: "<redacted>")
    assert network._ssid_from_macos() == "<redacted>"
