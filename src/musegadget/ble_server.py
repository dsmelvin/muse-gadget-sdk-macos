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

"""CoreBluetooth GATT peripheral for Muse Gadget setup on macOS.

Advertises the setup service and exposes two characteristics: RX (the phone
writes commands) and TX (the device notifies responses). Implements the
setup Transport protocol using Apple's native CoreBluetooth framework.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Callable

try:
    import CoreBluetooth as cb
    import objc
    from Foundation import (
        NSData,
        NSDate,
        NSDefaultRunLoopMode,
        NSObject,
        NSRunLoop,
    )
except ImportError as exc:
    raise ImportError(
        "CoreBluetooth support on macOS requires PyObjC. "
        "Install with `pip install pyobjc-framework-CoreBluetooth`."
    ) from exc

from musegadget.ble_framing import CHUNK_STAGGER_S, MAX_PACKET_BYTES

log = logging.getLogger(__name__)

SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"

_ASSUMED_MTU = MAX_PACKET_BYTES + 3


class _PeripheralDelegate(NSObject):
    def initWithServer_(self, server: BleServer):
        self = objc.super(_PeripheralDelegate, self).init()
        if self is None:
            return None
        self._server = server
        return self

    def peripheralManagerDidUpdateState_(self, peripheral):
        state = peripheral.state()
        state_names = {
            cb.CBManagerStateUnknown: "Unknown",
            cb.CBManagerStateResetting: "Resetting",
            cb.CBManagerStateUnsupported: "Unsupported",
            cb.CBManagerStateUnauthorized: "Unauthorized",
            cb.CBManagerStatePoweredOff: "PoweredOff",
            cb.CBManagerStatePoweredOn: "PoweredOn",
        }
        log.info("Bluetooth state: %s", state_names.get(state, str(state)))
        if state == cb.CBManagerStatePoweredOn:
            self._server._setup_service_and_advertise()
        elif state == cb.CBManagerStatePoweredOff:
            log.warning("Bluetooth is powered off on this Mac.")
        elif state == cb.CBManagerStateUnauthorized:
            log.error(
                "Bluetooth access unauthorized. "
                "Check macOS System Settings > Privacy & Security > Bluetooth."
            )

    def peripheralManager_didAddService_error_(self, peripheral, service, error):
        if error:
            log.error("Failed to add GATT service: %s", error)
            self._server._fatal("GATT service registration failed", error)
        else:
            log.info("GATT service registered")
            self._server._start_advertising()

    def peripheralManagerDidStartAdvertising_error_(self, peripheral, error):
        if error:
            log.error("Failed to start advertising: %s", error)
            self._server._fatal("Advertising failed", error)
        else:
            log.info("advertising as %s", self._server._local_name)

    def peripheralManager_central_didSubscribeToCharacteristic_(
        self, peripheral, central, characteristic
    ):
        log.info(
            "central %s subscribed to %s",
            central.identifier().UUIDString(),
            characteristic.UUID().UUIDString(),
        )
        self._server._handle_central_subscribe(central)

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(
        self, peripheral, central, characteristic
    ):
        log.info(
            "central %s unsubscribed from %s",
            central.identifier().UUIDString(),
            characteristic.UUID().UUIDString(),
        )
        self._server._handle_central_unsubscribe(central)

    def peripheralManager_didReceiveWriteRequests_(self, peripheral, requests):
        n_req = len(requests) if requests else 0
        log.info("didReceiveWriteRequests: %d request(s)", n_req)
        for i, request in enumerate(requests or []):
            chr_uuid = (
                request.characteristic().UUID().UUIDString()
                if request.characteristic()
                else "?"
            )
            val_bytes = bytes(request.value()) if request.value() else b""
            offset = request.offset() if hasattr(request, "offset") else 0
            hex_preview = val_bytes.hex()
            if len(hex_preview) > 120:
                hex_preview = f"{hex_preview[:120]}...({len(val_bytes)} bytes)"
            log.info(
                "RX write request [%d/%d]: chr=%s, offset=%d, len=%d, hex=%s",
                i + 1,
                n_req,
                chr_uuid,
                offset,
                len(val_bytes),
                hex_preview,
            )

        # Reassemble contiguous ATT long write (Prepare Write) if present
        is_contiguous_long_write = (
            n_req > 1
            and all(hasattr(r, "offset") for r in requests)
            and requests[0].offset() == 0
            and all(
                requests[j].offset()
                == requests[j - 1].offset() + len(bytes(requests[j - 1].value() or b""))
                for j in range(1, n_req)
            )
        )
        if is_contiguous_long_write:
            full_val = b"".join(bytes(r.value() or b"") for r in requests)
            log.info(
                "Reassembled %d contiguous prepare write requests into %d bytes",
                n_req,
                len(full_val),
            )
            self._server._handle_write(full_val)
        else:
            for request in requests or []:
                val = bytes(request.value()) if request.value() else b""
                self._server._handle_write(val)

        if requests and len(requests) > 0:
            first = requests[0]
            ignore = False
            if hasattr(first, "ignoreResponse"):
                try:
                    ignore = bool(first.ignoreResponse())
                except Exception:
                    pass
            if not ignore:
                log.debug("responding to write request with CBATTErrorSuccess")
                peripheral.respondToRequest_withResult_(first, cb.CBATTErrorSuccess)
            else:
                log.debug("write request has ignoreResponse=True; skipping respondToRequest")

    def peripheralManager_didReceiveReadRequest_(self, peripheral, request):
        request.setValue_(NSData.data())
        peripheral.respondToRequest_withResult_(request, cb.CBATTErrorSuccess)

    def peripheralManagerIsReadyToUpdateSubscribers_(self, peripheral):
        self._server._ready_event.set()


# ---------------------------------------------------------------------------
# macOS Bluetooth GAP name helpers
# ---------------------------------------------------------------------------
# macOS legacy BLE advertising packets are limited to 31 bytes.
# When advertising ONLY CBAdvertisementDataLocalNameKey (omitting the 128-bit
# service UUID), the custom local name fits in the primary advertising packet,
# preventing macOS from falling back to ComputerName.

_SCUTIL = "/usr/sbin/scutil"


def _get_computer_name() -> str | None:
    """Return the current macOS ComputerName, or None on failure."""
    try:
        result = subprocess.run(
            [_SCUTIL, "--get", "ComputerName"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception as exc:
        log.warning("Could not read ComputerName: %s", exc)
        return None


class BleServer:
    """Owns the CoreBluetooth peripheral while setup is open; implements the setup Transport."""

    def __init__(
        self,
        local_name: str,
        on_write: Callable[[bytes], None],
        on_disconnect: Callable[[], None],
        auto_rename: bool | None = None,
    ) -> None:
        self._local_name = local_name
        self._on_write = on_write
        self._on_disconnect = on_disconnect
        if auto_rename is not None:
            self._auto_rename = bool(auto_rename)
        else:
            self._auto_rename = os.environ.get("MUSE_AUTO_RENAME", "").lower() in ("1", "true", "yes")
        self._mtu = _ASSUMED_MTU
        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._ready_event.set()
        self._stopped = False
        self._manager: cb.CBPeripheralManager | None = None
        self._delegate: _PeripheralDelegate | None = None
        self._service: cb.CBMutableService | None = None
        self._tx: cb.CBMutableCharacteristic | None = None
        self._central: cb.CBCentral | None = None
        # Retained for backwards compatibility; ComputerName is not modified.
        self._saved_computer_name: str | None = None

    @property
    def auto_rename(self) -> bool:
        return self._auto_rename

    # -- Transport --------------------------------------------------------------

    def mtu(self) -> int:
        with self._lock:
            return self._mtu

    def send_packets(self, packets: list[bytes]) -> None:
        for i, packet in enumerate(packets):
            if i:
                time.sleep(CHUNK_STAGGER_S)
            self._send_one(packet)

    def _send_one(self, packet: bytes) -> None:
        data = NSData.dataWithBytes_length_(packet, len(packet))
        self._ready_event.wait(timeout=2.0)
        with self._lock:
            if not self._manager or not self._tx:
                return
            success = self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(
                data, self._tx, None
            )
            if not success:
                self._ready_event.clear()
        if not success:
            if self._ready_event.wait(timeout=2.0):
                with self._lock:
                    if self._manager and self._tx:
                        self._manager.updateValue_forCharacteristic_onSubscribedCentrals_(
                            data, self._tx, None
                        )

    def disconnect(self, delay: float) -> None:
        def _delayed():
            time.sleep(delay)
            with self._lock:
                self._central = None
                self._mtu = _ASSUMED_MTU
            self._on_disconnect()

        threading.Thread(target=_delayed, name="mac-ble-dc", daemon=True).start()

    def _maybe_rename_computer_name(self) -> None:
        """Deprecated no-op.

        Previously renamed macOS ComputerName to match local_name during setup.
        Now that name-only BLE advertising is used, the local name fits within the
        31-byte legacy packet limit without falling back to ComputerName, so system
        renaming is no longer required.
        """
        pass

    def _restore_computer_name(self) -> None:
        """Deprecated no-op.

        Previously restored the original macOS ComputerName after advertising stopped.
        Retained as a safe no-op for backwards compatibility.
        """
        pass

    # -- Lifecycle --------------------------------------------------------------

    def run(self) -> None:
        """Start advertising and serve until :meth:`stop`. Blocks."""
        self._stopped = False
        self._ready_event.set()

        delegate = _PeripheralDelegate.alloc().initWithServer_(self)
        self._delegate = delegate
        self._manager = cb.CBPeripheralManager.alloc().initWithDelegate_queue_options_(
            delegate, None, {cb.CBPeripheralManagerOptionShowPowerAlertKey: True}
        )
        log.info("CoreBluetooth peripheral initialized")
        try:
            while not self._stopped:
                NSRunLoop.currentRunLoop().runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    NSDate.dateWithTimeIntervalSinceNow_(0.1),
                )
        finally:
            self._teardown()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    # -- CoreBluetooth callbacks / helpers --------------------------------------

    def _setup_service_and_advertise(self) -> None:
        with self._lock:
            if not self._manager:
                return
            svc_uuid = cb.CBUUID.UUIDWithString_(SERVICE_UUID)
            rx_uuid = cb.CBUUID.UUIDWithString_(RX_UUID)
            tx_uuid = cb.CBUUID.UUIDWithString_(TX_UUID)

            service = cb.CBMutableService.alloc().initWithType_primary_(svc_uuid, True)

            rx = cb.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                rx_uuid,
                cb.CBCharacteristicPropertyWrite | cb.CBCharacteristicPropertyWriteWithoutResponse,
                None,
                cb.CBAttributePermissionsWriteable,
            )
            tx = cb.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                tx_uuid,
                cb.CBCharacteristicPropertyRead | cb.CBCharacteristicPropertyNotify,
                None,
                cb.CBAttributePermissionsReadable,
            )
            service.setCharacteristics_([rx, tx])
            self._tx = tx
            self._service = service
            self._manager.addService_(service)

    def _start_advertising(self) -> None:
        with self._lock:
            if not self._manager or not self._service:
                return
            # BLE legacy advertising packets are limited to 31 bytes.
            # A 128-bit service UUID consumes 18 bytes (16 bytes data + 2 bytes header),
            # which leaves insufficient room for the full local name. When both the
            # 128-bit service UUID and local name are provided and exceed 31 bytes,
            # macOS CoreBluetooth drops the local name from the primary advertising packet
            # and falls back to advertising the system ComputerName instead.
            #
            # Advertising ONLY CBAdvertisementDataLocalNameKey without
            # CBAdvertisementDataServiceUUIDsKey ensures the local name fits completely
            # within the 31-byte legacy advertising packet, preventing macOS from falling
            # back to ComputerName. Central devices discover the peripheral by its local
            # name, and the service UUID is discovered upon connection.
            adv_data = {
                cb.CBAdvertisementDataLocalNameKey: self._local_name,
            }
            self._manager.startAdvertising_(adv_data)

    def _handle_write(self, value: bytes) -> None:
        log.debug("BleServer._handle_write: delivering %d bytes to callback", len(value))
        self._on_write(value)

    def _handle_central_subscribe(self, central: cb.CBCentral) -> None:
        with self._lock:
            self._central = central
            max_len = central.maximumUpdateValueLength()
            if max_len > 0:
                self._mtu = max_len + 3
                log.info("ATT MTU %d", self._mtu)

    def _handle_central_unsubscribe(self, central: cb.CBCentral) -> None:
        with self._lock:
            if self._central and self._central.identifier().isEqual_(central.identifier()):
                self._central = None
                self._mtu = _ASSUMED_MTU
        self._on_disconnect()

    def _fatal(self, what: str, err: Exception) -> None:
        log.error("%s: %s", what, err)
        self.stop()

    def _teardown(self) -> None:
        with self._lock:
            if self._manager is not None:
                try:
                    self._manager.stopAdvertising()
                    self._manager.removeAllServices()
                except Exception as err:
                    log.warning("error in teardown: %s", err)
                self._manager = None
                self._tx = None
                self._service = None
                self._central = None
            self._ready_event.set()
