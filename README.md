<!--
Copyright (c) Meta Platforms, Inc. and affiliates.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# macOS Device SDK

This turns your Mac into a Muse gadget using Apple's native **CoreBluetooth** peripheral mode. Install it, pair it with the Muse app, and Muse can run commands and move files on your machine.

---

## 1. What You Need

- **A Mac with Bluetooth**: Apple Silicon or Intel running **macOS 12 (Monterey) or later**.
- **Bluetooth enabled** on your Mac.
- **Python 3.9+** (Python 3.11–3.14 supported) and [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`.
- **An SDK token** from [gadgets.muse.ai](https://gadgets.muse.ai/settings/sdk-tokens) (Account > SDK tokens).
- **The Muse mobile app** on your phone, to pair the device.

---

## 2. Setup

From the `macos/` directory:

### Using `uv` (Recommended)

```sh
cd macos
uv sync
```

### Using standard `pip` and virtual environment

```sh
cd macos
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

> [!NOTE]
> On macOS, `musegadget` stores its persistent identity and pairing state in `~/.musegadget/` by default. You do **not** need `sudo` to run pairing or services on macOS.

---

## 3. Configure Your SDK Token

Set your SDK token via environment variable or save it to your config directory:

```sh
export MUSEGADGET_SDK_TOKEN="mgst_YOUR_TOKEN_HERE"
```

Or save it permanently:

```sh
mkdir -p ~/.musegadget
echo "mgst_YOUR_TOKEN_HERE" > ~/.musegadget/sdk_token
chmod 600 ~/.musegadget/sdk_token
```

---

## 4. Pair with the Muse App

1. Check your device's identity and BLE name:
   ```sh
   uv run python3 -m musegadget info
   ```
   Output:
   ```text
   version:   0.1.0
   node id:   homelink-e8881d
   BLE name:  MuseGadgetE8881D
   paired:    no
   ```

2. Open Bluetooth pairing on your Mac:
   ```sh
   uv run python3 -m musegadget pair
   ```
   > [!IMPORTANT]
   > The first time you run this, macOS may display a system prompt:
   > *"Terminal would like to use Bluetooth."* Click **Allow**.
   > You can also check permissions in **System Settings > Privacy & Security > Bluetooth**.


3. In the **Muse mobile app**:
   - Go to **Settings > Devices** and enable **Developer mode**.
   - Tap **Add Device** (the **+** icon in the top right).
   - Choose the device matching your BLE name (e.g. `MuseGadgetE8881D`).
   - Muse warns that this is a community device. Continue if it's yours.
   - When asked for Wi-Fi, select the current network shown (since the Mac is already online, no password is needed).

4. Once the phone finishes provisioning, the terminal will report:
   ```text
   Paired.
   ```

---

## 5. Run the Service

After pairing, run the background service so Muse can communicate with your Mac:

```sh
uv run python3 -m musegadget run
```

This establishes an encrypted outbound connection to Meta's servers over HTTPS/WebSockets (no Bluetooth is used after initial pairing).

To run the service as a background LaunchAgent (auto-start on login), see [Automating with LaunchAgent](#automating-with-launchagent) below.

---

## 6. What Muse Can Do

| Command | What it does |
|---|---|
| `system.run` | Runs a shell command and returns its output and exit code |
| `file.read` | Reads a file, 64 KB at a time |
| `file.write` | Writes a file, 64 KB at a time, replacing it only when complete |
| `device.health` | Reports uptime, load, memory, and disk |

Ask Muse things like:

> What's using all the disk space on my Mac?

> Every morning at 8, check if my backups ran and tell me if they didn't.

---

## 7. Interact with Muse

While the service is running, programs on your Mac can send messages to Muse with no credentials of their own:

```sh
uv run python3 -m musegadget send-user-msg "Build finished successfully on my Mac."
```

Or start a dedicated side chat using a session UUID:

```sh
uv run python3 -m musegadget send-user-msg --session-id $(uuidgen) "Message in new side thread"
```

---

## 8. CLI Reference

| Command | Action |
|---|---|
| `musegadget info` | View device ID, BLE name, and current pairing status |
| `musegadget pair` | Open Bluetooth pairing window (default: 10 minutes) |
| `musegadget pair --force` | Re-open pairing even if already paired |
| `musegadget unpair` | Clear saved pairing credentials (preserves device identity) |
| `musegadget run` | Start the command executor and connection service |
| `musegadget send-user-msg "<text>"` | Send an inbound message to Muse |

---

## 9. Automating with LaunchAgent

To keep `musegadget` running automatically in the background across reboots on macOS:

1. Create `~/Library/LaunchAgents/com.musegadget.service.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.musegadget.service</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/uv</string>
        <string>run</string>
        <string>--directory</string>
        <string>/path/to/muse-gadget-sdk/macos</string>
        <string>python3</string>
        <string>-m</string>
        <string>musegadget</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/musegadget.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/musegadget.stderr.log</string>
</dict>
</plist>
```

2. Load the agent:
   ```sh
   launchctl load ~/Library/LaunchAgents/com.musegadget.service.plist
   ```

3. To stop:
   ```sh
   launchctl unload ~/Library/LaunchAgents/com.musegadget.service.plist
   ```

---

## 10. Development & Architecture

- **Tests**: Run unit tests with `uv run --with pytest pytest`.
- **Architecture & Porting**: See [PORTING.md](PORTING.md) for detailed descriptions of the BLE protocol, packet framing, and platform abstraction boundaries.

## 11. Android

Muse App in Android has BLE MTU issues which will cause the pairing to fail.
Until Meta fixes this issue, use iPhone or iPad to pair the device first. 
Once paired, you can switch back to Android app to send commands to the device or from the device.

## 12. send_chat.py

Send one chat message to the Muse as this paired device without running the musegadget service.
