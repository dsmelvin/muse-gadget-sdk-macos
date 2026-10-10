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

import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="CoreBluetooth BleServer is macOS-specific",
)

from musegadget.ble_server import _ASSUMED_MTU, RX_UUID, SERVICE_UUID, TX_UUID, BleServer


def test_uuids():
    assert SERVICE_UUID == "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
    assert RX_UUID == "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"
    assert TX_UUID == "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"


def test_init_and_mtu():
    on_write = MagicMock()
    on_disconnect = MagicMock()
    server = BleServer("MuseGadgetTEST", on_write=on_write, on_disconnect=on_disconnect)
    assert server.mtu() == _ASSUMED_MTU


def test_handle_write_invokes_callback():
    writes = []
    server = BleServer(
        "MuseGadgetTEST",
        on_write=lambda data: writes.append(data),
        on_disconnect=MagicMock(),
    )
    server._handle_write(b"\xfe\x00\x01test")
    assert writes == [b"\xfe\x00\x01test"]


def test_central_subscribe_and_unsubscribe():
    disconnected = threading.Event()
    server = BleServer(
        "MuseGadgetTEST",
        on_write=MagicMock(),
        on_disconnect=lambda: disconnected.set(),
    )

    mock_central = MagicMock()
    mock_central.maximumUpdateValueLength.return_value = 180
    mock_central.identifier.return_value.UUIDString.return_value = "UUID-1234"
    mock_central.identifier.return_value.isEqual_.return_value = True

    server._handle_central_subscribe(mock_central)
    assert server.mtu() == 183

    server._handle_central_unsubscribe(mock_central)
    assert server.mtu() == _ASSUMED_MTU
    assert disconnected.is_set()


def test_disconnect_delayed():
    disconnected = threading.Event()
    server = BleServer(
        "MuseGadgetTEST",
        on_write=MagicMock(),
        on_disconnect=lambda: disconnected.set(),
    )

    server.disconnect(delay=0.05)
    assert not disconnected.is_set()
    assert disconnected.wait(timeout=0.5)


def test_stop_and_teardown():
    server = BleServer("MuseGadgetTEST", on_write=MagicMock(), on_disconnect=MagicMock())
    server.stop()
    assert server._stopped is True
    server._teardown()
    assert server._manager is None


def test_start_advertising_uses_name_only():
    import CoreBluetooth as cb

    server = BleServer("MuseGadgetTEST", on_write=MagicMock(), on_disconnect=MagicMock())
    server._manager = MagicMock()
    server._service = MagicMock()

    server._start_advertising()

    server._manager.startAdvertising_.assert_called_once()
    adv_dict = server._manager.startAdvertising_.call_args[0][0]

    assert cb.CBAdvertisementDataLocalNameKey in adv_dict
    assert adv_dict[cb.CBAdvertisementDataLocalNameKey] == "MuseGadgetTEST"
    assert cb.CBAdvertisementDataServiceUUIDsKey not in adv_dict
    assert set(adv_dict.keys()) == {cb.CBAdvertisementDataLocalNameKey}


def test_did_receive_write_requests_single():
    writes = []
    server = BleServer("MuseGadgetTEST", on_write=lambda d: writes.append(d), on_disconnect=MagicMock())
    from musegadget.ble_server import _PeripheralDelegate
    delg = _PeripheralDelegate.alloc().initWithServer_(server)

    mock_peripheral = MagicMock()
    mock_request = MagicMock()
    mock_request.value.return_value = b"hello"
    mock_request.offset.return_value = 0
    mock_request.characteristic.return_value.UUID.return_value.UUIDString.return_value = RX_UUID
    mock_request.ignoreResponse.return_value = False

    delg.peripheralManager_didReceiveWriteRequests_(mock_peripheral, [mock_request])
    assert writes == [b"hello"]
    mock_peripheral.respondToRequest_withResult_.assert_called_once()


def test_did_receive_write_requests_contiguous_long_write():
    writes = []
    server = BleServer("MuseGadgetTEST", on_write=lambda d: writes.append(d), on_disconnect=MagicMock())
    from musegadget.ble_server import _PeripheralDelegate
    delg = _PeripheralDelegate.alloc().initWithServer_(server)

    mock_peripheral = MagicMock()
    req1 = MagicMock()
    req1.value.return_value = b"part1_"
    req1.offset.return_value = 0
    req1.characteristic.return_value.UUID.return_value.UUIDString.return_value = RX_UUID

    req2 = MagicMock()
    req2.value.return_value = b"part2"
    req2.offset.return_value = 6
    req2.characteristic.return_value.UUID.return_value.UUIDString.return_value = RX_UUID

    delg.peripheralManager_didReceiveWriteRequests_(mock_peripheral, [req1, req2])
    assert writes == [b"part1_part2"]


def test_auto_rename_property_default_disabled(monkeypatch):
    monkeypatch.delenv("MUSE_AUTO_RENAME", raising=False)
    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock())
    assert server.auto_rename is False


def test_auto_rename_property_env_var(monkeypatch):
    for val in ("1", "true", "True", "yes", "YES"):
        monkeypatch.setenv("MUSE_AUTO_RENAME", val)
        server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock())
        assert server.auto_rename is True

    for val in ("0", "false", "False", "no", "NO", ""):
        monkeypatch.setenv("MUSE_AUTO_RENAME", val)
        server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock())
        assert server.auto_rename is False


def test_auto_rename_property_explicit_param(monkeypatch):
    monkeypatch.setenv("MUSE_AUTO_RENAME", "1")
    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock(), auto_rename=False)
    assert server.auto_rename is False

    monkeypatch.setenv("MUSE_AUTO_RENAME", "0")
    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock(), auto_rename=True)
    assert server.auto_rename is True


def test_maybe_rename_computer_name_is_safe_noop(monkeypatch):
    import subprocess
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock(), auto_rename=False)
    server._maybe_rename_computer_name()

    mock_run.assert_not_called()
    assert server._saved_computer_name is None


def test_maybe_rename_computer_name_auto_rename_is_safe_noop(monkeypatch):
    import subprocess
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock(), auto_rename=True)
    server._maybe_rename_computer_name()

    mock_run.assert_not_called()
    assert server._saved_computer_name is None


def test_restore_computer_name_is_safe_noop(monkeypatch):
    import subprocess
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)

    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock(), auto_rename=True)
    server._saved_computer_name = "OriginalComputerName"
    server._restore_computer_name()

    mock_run.assert_not_called()


def test_rename_methods_do_not_throw_errors():
    server = BleServer("MuseGadgetTEST", MagicMock(), MagicMock())
    server._maybe_rename_computer_name()
    server._restore_computer_name()


def test_cli_pair_auto_rename_handling(monkeypatch):
    import argparse
    from musegadget import cli

    created_servers = []

    def mock_server_init(local_name, on_write, on_disconnect, auto_rename=None):
        srv = MagicMock()
        srv.auto_rename = auto_rename
        created_servers.append(srv)
        return srv

    monkeypatch.setattr("musegadget.ble_server.BleServer", mock_server_init)
    monkeypatch.setattr("musegadget.config.load_json", lambda path: None)
    monkeypatch.setattr("musegadget.config.sdk_token", lambda: "test-token")
    mock_ident = MagicMock()
    mock_ident.ble_name = "MuseGadgetTEST"
    mock_ident.node_id = "test-node"
    mock_ident.device_id = "test-device"
    mock_ident.mac = "00:11:22:33:44:55"
    monkeypatch.setattr("musegadget.identity.load_or_create", lambda: mock_ident)
    monkeypatch.setattr("musegadget.cli.SetupController", MagicMock())

    # Case 1: Default (no flags, no env var) -> auto_rename is False
    monkeypatch.delenv("MUSE_AUTO_RENAME", raising=False)
    created_servers.clear()
    cli.cmd_pair(argparse.Namespace(force=False, timeout=60, auto_rename=None))
    assert len(created_servers) == 1
    assert created_servers[0].auto_rename is False

    # Case 2: --auto-rename flag -> auto_rename is True
    created_servers.clear()
    cli.cmd_pair(argparse.Namespace(force=False, timeout=60, auto_rename=True))
    assert len(created_servers) == 1
    assert created_servers[0].auto_rename is True

    # Case 3: Env var MUSE_AUTO_RENAME=1 without flag -> auto_rename is True
    monkeypatch.setenv("MUSE_AUTO_RENAME", "1")
    created_servers.clear()
    cli.cmd_pair(argparse.Namespace(force=False, timeout=60, auto_rename=None))
    assert len(created_servers) == 1
    assert created_servers[0].auto_rename is True

    # Case 4: Env var MUSE_AUTO_RENAME=1 with --no-auto-rename (auto_rename=False) -> False
    created_servers.clear()
    cli.cmd_pair(argparse.Namespace(force=False, timeout=60, auto_rename=False))
    assert len(created_servers) == 1
    assert created_servers[0].auto_rename is False


def test_cli_argument_parsing():
    from unittest.mock import patch
    from musegadget.cli import main

    with patch("musegadget.cli.cmd_pair") as mock_pair:
        main(["pair"])
        assert mock_pair.call_args[0][0].auto_rename is None

    with patch("musegadget.cli.cmd_pair") as mock_pair:
        main(["pair", "--auto-rename"])
        assert mock_pair.call_args[0][0].auto_rename is True

    with patch("musegadget.cli.cmd_pair") as mock_pair:
        main(["pair", "--rename-hostname"])
        assert mock_pair.call_args[0][0].auto_rename is True

    with patch("musegadget.cli.cmd_pair") as mock_pair:
        main(["pair", "--no-auto-rename"])
        assert mock_pair.call_args[0][0].auto_rename is False
