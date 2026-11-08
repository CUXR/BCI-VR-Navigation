> The Unity source is archived outside the repository. For the prebuilt APK,
> use the [standalone build guide](../releases/environment_1/README.md) and USB forwarding.
> The source-editing instructions below require restoring that archive.

# Running the BCI Realtime Pipeline on Meta Quest

This doc explains how to run the `FreeNavScene` (and the AutoMove data-collection
scene) on a Meta Quest headset, talking to the Python pipeline in
[`pipeline/`](../pipeline/README.md).

There are two paths:

- **Path A — Quest Link / Air Link** (PC-tethered, easiest dev loop). The app
  runs on the Mac; the Quest is just a display. The default
  `127.0.0.1:8765` / `127.0.0.1:8766` work unchanged.
- **Path B — Native Android build** (untethered APK installed on the Quest).
  The Quest runs the app standalone; you need an `adb reverse` tunnel or the
  Mac's LAN IP for the WebSocket connections.

The pipeline uses two sockets. FreeNavScene needs port `8765`; the AutoMove
data-collection scene needs port `8766`:

| Direction | URL | Server | Used by scene |
|---|---|---|---|
| Unity → Python (markers) | `ws://127.0.0.1:8766` | `pipeline collect` (`MarkerServer`) | AutoMove (data collection) |
| Python → Unity (predictions / state) | `ws://127.0.0.1:8765` | `pipeline realtime` (`JsonWSServer`) | FreeNavScene (realtime) |

See [`pipeline/README.md`](../pipeline/README.md) for the JSON wire format on
each socket and the Unity component wiring (`MarkerWebSocketClient`,
`PredictionWebSocketClient`, `FreeNavController`,
`PersonalizationOverlayController`).

---

## Path A — Quest Link / Air Link (recommended for development)

### One-time setup

1. Install the **Oculus PC app** on your Mac (or use Air Link over Wi-Fi).
2. In Unity, confirm the build target and XR plug-in:
   - **File → Build Settings**: target is **PC, Mac & Linux Standalone**.
   - **Edit → Project Settings → XR Plug-in Management → Standalone tab**:
     check **Oculus**.
3. Install Python deps once:
   ```bash
   pip install -r eeg_to_meta/requirements.txt
   pip install -r ml_pipeline/requirements.txt
   ```

### Each session — data collection (AutoMove scene)

1. Connect Quest to Mac via Quest Link cable, or start Air Link from the
   headset's Quick Settings, and accept the Link prompt.
2. Start the collector in a Mac terminal (mock mode if no Muse is attached):
   ```bash
   python -m pipeline collect --participant 5 --name "Asheeb" --runs 3
   # add --mock to run without a Muse
   ```
   You should see the `MarkerServer` listening on `ws://127.0.0.1:8766`.
3. In Unity, open `Assets/Scenes/SampleScene.unity` (the AutoMove
   data-collection scene), confirm `MarkerWebSocketClient` is wired into
   `AutoMoveDataCollectionController.markerWebSocketClient`, and that
   `sequenceRuns` matches the CLI `--runs`.
4. Press ▶ **Play**. The Game view streams to the headset; cues drive the
   scene; markers stream to the Python collector.

### Each session — realtime (FreeNavScene)

1. After collecting and training (`pipeline train` + `pipeline personalize`,
   see the pipeline README), start the realtime server on the Mac:
   ```bash
   python -m pipeline realtime --participant 5 \
       --mi-threshold 0.58 --blink-threshold 0.62
   # add --mock to run without a Muse
   ```
   The `JsonWSServer` listens on `ws://127.0.0.1:8765`.
2. In Unity, open `Assets/Scenes/FreeNavScene.unity` and press ▶ **Play**.
3. The personalisation overlay should clear once the realtime loop emits a
   `state: realtime_ready` frame. Predictions then drive the rig.

That's it for Path A — no `adb`, no IP juggling.

---

## Path B — Native Android build (untethered)

### Prerequisites

#### 1. Install `adb` on the Mac

```bash
brew install --cask android-platform-tools
adb version    # confirm
```

#### 2. Enable Developer Mode on the Quest (one-time, via phone)

1. Install the **Meta Quest** mobile app on your phone (App Store / Google Play).
2. Sign in with the same Meta account your headset uses.
3. **Devices → your Quest → Headset Settings → Developer Mode → On**.
4. First time only: Meta will ask you to register as a developer at
   developer.oculus.com — free; create an "organization" with any name.
5. Reboot the headset (hold the power button → Restart) so the toggle takes
   effect.

#### 3. Authorize the Mac for USB debugging (one-time)

1. Plug the Quest into your Mac with a **data-capable** USB-C cable
   (charge-only cables silently fail).
2. Put the headset on. You'll see a prompt: **"Allow USB debugging?"**
3. Check **"Always allow from this computer"**, tap **Allow**.
4. Verify on the Mac:
   ```bash
   adb devices
   ```
   Expected:
   ```
   List of devices attached
   1WMHHxxxxxxxxx    device
   ```
   If it says `unauthorized`, the prompt wasn't accepted — put the headset
   back on and look for it again. If the device list is empty, try a
   different USB-C cable.

### Build the APK

In Unity:

1. **File → Build Settings → Android → Switch Platform** (this can take
   several minutes the first time).
2. **XR Plug-in Management → Android tab**: enable **Oculus**.
3. **Player Settings → Android → Other Settings**:
   - **Minimum API Level**: 29 (Quest 2 minimum)
   - **Target Architectures**: ARM64 only
   - **Internet Access**: **Require** (this puts
     `<uses-permission android:name="android.permission.INTERNET"/>` in
     `AndroidManifest.xml`. NativeWebSocket cannot connect without it.)
4. Pick the scene to ship in **Scenes In Build**:
   - For data collection, include the AutoMove scene (`SampleScene.unity`).
   - For realtime, include `Assets/Scenes/FreeNavScene.unity`.
5. **File → Build Settings → Build And Run** (with the Quest plugged in).
   Unity will compile, install, and launch the app on the headset.

### Networking: pick one

When the app runs natively on the Quest, `localhost` / `127.0.0.1` means
*the Quest itself*, not your Mac. The pipeline servers are on the Mac, so
you need a way for the headset to reach **both** ports — `8765` (predictions
into Unity) and `8766` (markers out of Unity).

#### Option 1 — `adb reverse` (recommended for development)

Tunnels the Quest's `localhost:<port>` to the Mac's port over USB. The
default `host = 127.0.0.1` on `MarkerWebSocketClient` and
`PredictionWebSocketClient` keeps working — no Inspector changes needed.

```bash
adb devices                    # confirm Quest is listed as "device"
adb reverse tcp:8765 tcp:8765  # predictions Mac → Quest
adb reverse tcp:8766 tcp:8766  # markers Quest → Mac
```

Re-run both `adb reverse` lines every time you reboot the Quest or
unplug/replug.

Then start the relevant Python server on the Mac (`pipeline collect` for
AutoMove, `pipeline realtime` for FreeNavScene) and launch the app on the
Quest.

#### Option 2 — LAN connection (untethered, no USB)

1. Find the Mac's local IP:
   ```bash
   ipconfig getifaddr en0    # Wi-Fi
   # or: ipconfig getifaddr en1 for Ethernet
   ```
   Example: `192.168.1.42`.

2. Set the WebSocket clients' `Host` field to that IP **before building**.
   On each client component (`MarkerWebSocketClient` and/or
   `PredictionWebSocketClient`), in the Inspector:
   - **Host** = `192.168.1.42`
   - **Port** = `8766` (markers) / `8765` (predictions)

   Prefer setting these in the scene rather than editing the C# field
   initializers, so the IP doesn't get committed.

3. Make sure the Mac and Quest are on the same Wi-Fi network.

4. Allow inbound ports `8765` and `8766` through the Mac's firewall (System
   Settings → Network → Firewall — either disable temporarily or add a rule
   for `python`).

5. By default the Python prediction server binds to `127.0.0.1`, which is
   unreachable from the LAN. For FreeNavScene, start it with
   `--ws-host 0.0.0.0`; keep the Unity Host field set to the Mac's actual IP.
   See the [indoor Quest guide](../releases/environment_1/README.md) for the
   complete live and synthetic test commands.

### Run

1. Make sure the relevant Python server is running on the Mac:
   ```bash
   # data collection
   python -m pipeline collect --participant 5 --name "Asheeb" --runs 3
   # realtime
   python -m pipeline realtime --participant 5 \
       --mi-threshold 0.58 --blink-threshold 0.62
   ```
2. (Option 1 only) Run both `adb reverse` lines after each Quest
   reboot/reconnect.
3. Put on the Quest. Find your app under **Apps → Unknown Sources** in the
   Quest's library, launch it.
4. Confirm `Connected` log lines for the WebSocket clients you expect (see
   "Verifying it works on-device" below).

---

## Verifying it works on-device

Stream the Quest's logs back to your Mac:

```bash
adb logcat | grep -E "MarkerWebSocketClient|PredictionWebSocketClient|FreeNavController|PersonalizationOverlayController|Unity"
```

Expected lines on a successful realtime connect:

```
[PredictionWebSocketClient] Connected to ws://127.0.0.1:8765
[PersonalizationOverlayController] state=personalizing_start ...
[PersonalizationOverlayController] state=realtime_ready ...
```

Expected lines on a successful collect connect:

```
[MarkerWebSocketClient] Connected to ws://127.0.0.1:8766
```

If you see `Error: ...` or `Closed (code=...). Reconnecting` repeatedly, the
headset can't reach the server — see Troubleshooting.

---

## Troubleshooting

**`adb devices` shows nothing.**
The cable is charge-only or the Quest isn't authorized. Try a different
USB-C cable; re-check Developer Mode in the phone app; re-accept the USB
debugging prompt in the headset.

**`adb devices` shows `unauthorized`.**
The "Allow USB debugging?" prompt in the headset wasn't accepted. Put the
headset back on, look for the prompt, tap Allow.

**App launches on Quest but never logs `Connected`.**
The headset can't reach the WebSocket server. Most common causes:
- Forgot to run `adb reverse` for **both** `8765` and `8766` after a
  reboot/replug (Option 1).
- Mac firewall blocking port `8765` / `8766` (Option 2).
- Mac and Quest not on the same Wi-Fi network (Option 2).
- Python server bound to `127.0.0.1` only — unreachable from the LAN
  (Option 2). Bind to `0.0.0.0` instead.
- IP in the build is stale because the Mac's DHCP lease changed (Option 2).

**FreeNavScene loads but the personalisation overlay never clears.**
The realtime server hasn't emitted `state: realtime_ready`. Check the
Mac-side `pipeline realtime` log for personalisation errors, and confirm
`ml_pipeline/models/participants/sub<NN>.pkl` exists for that participant.

**`Switch Platform` to Android takes forever or fails.**
Unity needs Android Build Support installed via Unity Hub. Open Unity Hub →
Installs → your editor's gear icon → **Add modules** → check **Android
Build Support** (with **OpenJDK** and **Android SDK & NDK Tools**
sub-components). Reinstall, then retry.

**Movement feels wrong in FreeNavScene (snapping the wrong way, or the floor
moves with me).** The XR Origin reference may not be the one you expect.
Check the Console in the Editor (Path A) or `adb logcat` (Path B) for
warnings from `FreeNavController`. Drag the correct GameObject into its
`Rig Root` Inspector slot.

---

## Quick reference

| What | Path A (Link) | Path B (APK) |
|---|---|---|
| Build target | PC Standalone | Android |
| Marker URL (Unity → Python) | `ws://127.0.0.1:8766` | `ws://127.0.0.1:8766` (with `adb reverse tcp:8766`) **or** `ws://<mac-ip>:8766` |
| Prediction URL (Python → Unity) | `ws://127.0.0.1:8765` | `ws://127.0.0.1:8765` (with `adb reverse tcp:8765`) **or** `ws://<mac-ip>:8765` |
| Needs `adb` | No | Yes |
| Needs Quest Developer Mode | No | Yes |
| Tethered to PC at runtime | Yes | No (after install) |
| Best for | Day-to-day iteration | Demos, untethered testing |
