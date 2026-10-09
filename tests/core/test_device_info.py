import pytest

from sysdroid.core.device_info import (
    STATUS_SCRIPT, parse_device_status, parse_mdns_services, validate_pairing,
)

FULL = """0
1
14
@@abi
arm64-v8a
arm64-v8a,armeabi-v7a,armeabi
@@selinux
Enforcing
@@fingerprint
google/oriole/oriole:14/AP2A.240805.005/12025142:user/release-keys
@@size
Physical size: 1080x2400
Override size: 720x1600
@@battery
Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  level: 85
  scale: 100
  temperature: 312
@@ip
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
30: wlan0    inet 192.168.1.23/24 brd 192.168.1.255 scope global wlan0\\       valid_lft forever
@@mounts
/dev/block/dm-0 / ext4 ro,seclabel 0 0
/dev/block/dm-1 /vendor ext4 rw,seclabel 0 0
"""


def test_status_script_is_one_shell_round_trip_with_markers():
    assert STATUS_SCRIPT.startswith("id -u; getprop ro.debuggable; getprop ro.build.version.release; ")
    for marker in ("@@abi", "@@selinux", "@@fingerprint", "@@size", "@@battery", "@@ip", "@@mounts"):
        assert f"echo {marker}" in STATUS_SCRIPT
    assert "\n" not in STATUS_SCRIPT


def test_full_status_output_is_parsed_into_card_fields():
    status = parse_device_status(FULL.replace("\n", "\r\n"))
    assert status.root and status.debuggable == "1" and status.release == "14"
    assert (status.abi, status.abilist) == ("arm64-v8a", "arm64-v8a,armeabi-v7a,armeabi")
    assert status.selinux == "Enforcing"
    assert status.fingerprint.startswith("google/oriole")
    assert status.resolution == "720x1600（物理 1080x2400）"
    assert status.battery == "85% · 充电中 · 供电 USB · 31.2°C"
    assert status.ip == "192.168.1.23（wlan0）"
    assert status.system_writable


def test_missing_blocks_degrade_to_empty_and_legacy_layout_still_works():
    status = parse_device_status("2000\n0\n13\n@@abi\n@@selinux\n@@size\nPhysical size: 1440x3200\n@@battery\n@@ip\n@@mounts\n/dev/root / ext4 ro 0 0\n")
    assert not status.root and status.resolution == "1440x3200"
    assert (status.abi, status.selinux, status.battery, status.ip, status.fingerprint) == ("", "", "", "", "")
    assert not status.system_writable
    legacy = parse_device_status("0\n1\n14\n/dev/root / ext4 rw 0 0\n")
    assert legacy.system_writable and legacy.abi == ""
    with pytest.raises(ValueError):
        parse_device_status("0\n1")


def test_mdns_services_parse_connect_and_pairing_entries():
    output = """List of discovered mdns services
adb-1A2B3C-xyz\t_adb-tls-connect._tcp\t192.168.1.23:37255
adb-1A2B3C-xyz\t_adb-tls-pairing._tcp.\t192.168.1.23:41913
adb-1A2B3C-xyz\t_adb-tls-connect._tcp\t192.168.1.23:37255
garbage line
"""
    services = parse_mdns_services(output)
    assert [(service.kind, service.address, service.pairing) for service in services] == [
        ("_adb-tls-connect._tcp", "192.168.1.23:37255", False),
        ("_adb-tls-pairing._tcp", "192.168.1.23:41913", True),
    ]
    assert services[1].label.startswith("配对 · 192.168.1.23:41913")
    assert parse_mdns_services("List of discovered mdns services\n") == []


@pytest.mark.parametrize("address,code,ok", [
    ("192.168.1.23:41913", "123456", True), (" phone.local:5555 ", " 000111 ", True),
    ("192.168.1.23", "123456", False), ("192.168.1.23:0", "123456", False),
    ("192.168.1.23:41913", "12345", False), ("192.168.1.23:41913", "12 456", False),
    ("a b:1", "123456", False),
])
def test_pairing_input_validation(address, code, ok):
    if ok:
        assert validate_pairing(address, code) == (address.strip(), code.strip())
    else:
        with pytest.raises(ValueError):
            validate_pairing(address, code)
