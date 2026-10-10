#!/usr/bin/env python3
"""Send one chat message to the Muse as this paired device, then exit.

A one-shot alternative to running the musegadget service and talking to its
local socket. It reuses musegadget's own code for every step the service
performs:

  1. load pairing.json, rotating the device token if it is due (Service._maybe_refresh)
  2. GET /fetch_vms and pick the default VM
  3. open the Noise-over-WebSocket LinkSession and wait for link.register
  4. LinkSession.send_chat(...) -> POST /chat/stream inside the session
  5. print the reply as JSON and close the session

Run it with the service STOPPED. Token rotation replaces the refresh token, so
a second process refreshing in parallel can invalidate the running service's
credentials, and two sessions for one node_id may be refused.

Usage:
    PYTHONPATH=src python3 send_chat.py "hello" [--session-id side-chat-1]
    MUSEGADGET_STATE_DIR=~/.musegadget python3 send_chat.py "hello"

Device commands (system.run, file.read, ...) sent back by the Muse are refused;
this script never executes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
import time

from musegadget import __version__, config, muse_api
from musegadget.executor import COMMAND_SPECS
from musegadget.identity import load_or_create
from musegadget.link_client import DeviceDescription, LinkSession, Outcome
from musegadget.service import DEFAULT_NOISE_HOST, Service, _SESSION_ID_RE

REGISTER_TIMEOUT_S = 20.0


def refuse_command(command: str, params: dict, timeout_ms: int | None = None) -> dict:
    return {"ok": False, "error": "commands are disabled in send_chat.py"}


async def fetch_default_vm(service: Service, pairing: dict) -> tuple[dict | None, dict | None]:
    """Return (vm, pairing), refreshing the device token once on a 401."""
    for attempt in range(2):
        api = muse_api.api_root(pairing.get("api_url_v2", ""))
        vms, status = await asyncio.to_thread(
            muse_api.fetch_vms_with_status, pairing["access_token"], api)
        if status == 401 and attempt == 0:
            logging.warning("device token rejected; refreshing")
            pairing = await service._maybe_refresh(pairing, force=True)
            if pairing is None:
                return None, None
            continue
        vm = next((v for v in vms if v["is_default"]), vms[0] if vms else None)
        return vm, pairing
    return None, pairing


async def send_once(args: argparse.Namespace) -> dict:
    ident = load_or_create()
    pairing = config.load_json(config.PAIRING_FILE)
    if not pairing:
        return {"ok": False, "error": "not paired; run `musegadget pair` first"}

    # sdk_token=None: don't trigger the extra "report SDK token" rotation.
    service = Service(identity=ident, executor=None, sdk_token=None)
    pairing = await service._maybe_refresh(pairing)
    if pairing is None:
        return {"ok": False, "error": "device token refresh failed or pairing revoked"}

    vm, pairing = await fetch_default_vm(service, pairing)
    if pairing is None:
        return {"ok": False, "error": "device token refresh failed or pairing revoked"}
    if vm is None:
        return {"ok": False, "error": "no leased VM available"}

    device = DeviceDescription(
        node_id=ident.node_id,
        display_name=args.display_name or socket.gethostname(),
        version=__version__,
        commands=COMMAND_SPECS if args.advertise_commands else {},
    )
    session = LinkSession(
        noise_host=pairing.get("noise_host") or DEFAULT_NOISE_HOST,
        vm_id=vm["vm_id"] or vm["vm_name"],
        vm_auth_token=vm["vm_auth_token"],
        device=device,
        run_command=refuse_command,
    )
    logging.info("connecting to %s", vm["vm_name"] or vm["vm_id"])
    stop = asyncio.Event()
    task = asyncio.ensure_future(session.run(stop))

    try:
        deadline = time.monotonic() + REGISTER_TIMEOUT_S
        while session.registered_at is None:
            if task.done():
                try:
                    outcome = task.result()
                except Exception as exc:
                    return {"ok": False, "error": f"session failed: {type(exc).__name__}: {exc}"}
                hint = {Outcome.AUTH_REJECTED: "VM bearer rejected (401)",
                        Outcome.FORBIDDEN: "VM refused the device (403)",
                        Outcome.UNPAIRED: "the Muse removed this device"}.get(outcome, outcome.value)
                return {"ok": False, "error": f"session ended before registering: {hint}"}
            if time.monotonic() > deadline:
                return {"ok": False, "error": "timed out waiting for link.register"}
            await asyncio.sleep(0.1)

        return await session.send_chat(args.message, args.session_id)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, 5)
        except (asyncio.TimeoutError, Exception):
            task.cancel()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("message", help="text to send to the Muse")
    parser.add_argument("--session-id", help="side chat id (letters, digits, dashes); default: main chat")
    parser.add_argument("--display-name", help="device name shown to the Muse (default: hostname)")
    parser.add_argument("--advertise-commands", action="store_true",
                        help="register with the full command list (commands are still refused); "
                             "use if the Muse rejects registration without capabilities")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if not args.message.strip():
        parser.error("message is empty")
    if args.session_id is not None and not _SESSION_ID_RE.fullmatch(args.session_id):
        parser.error("session-id must be 1-64 letters, digits and dashes")

    result = asyncio.run(send_once(args))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
