# Indoor Quest navigation

Open this project with Unity 2022.3.0f1. `Assets/Scenes/FreeNavScene.unity` is
the indoor supermarket with BCI navigation on its XR Origin. The original
`SampleScene.unity` remains the data-collection scene.

The Quest app receives prediction frames at `ws://<host>:8765`. The checked-in
scene uses `127.0.0.1`, which requires `adb reverse` for a standalone Quest.
Forward and backward predictions move in the headset's horizontal facing direction;
left and right predictions rotate the XR Origin. Legacy left/right model
labels also rotate. Blink is not mapped to movement. An unstable, low-confidence,
disconnected, or stale prediction does not move the rig. Prediction frames must
include a Unix `timestamp` in seconds; the client rejects old frames queued
while the headset sleeps.

The Python runtime sends at most one valid navigation prediction every three
seconds. The scene moves for up to 0.5 seconds after that frame, then stops
until another accepted command arrives. Blink predictions are not rate-limited.

## Build and connect

1. In Unity, select Android and build `FreeNavScene` (the enabled scene in Build
   Settings). Android XR loading, Oculus XR, ARM64, and Internet permission are
   configured in this project.
2. Install and launch the APK on the Quest. With the headset connected over USB,
   run `adb reverse tcp:8765 tcp:8765`. Re-run this after reconnecting the cable
   or if `adb reverse --list` is empty.
3. Run the Python prediction server on the Mac. For a controlled movement test,
   from the repository root run:

   ```sh
   .venv/bin/python unity/environment_1/tools/smoke_predictions.py
   ```

   Enter one direction at a time; each command sends five frames over half a
   second. The server reports when the Quest connects. Stop it with `q` before
   starting the real inference server, since both use port 8765.

## Connect over Wi-Fi without a USB tether

1. Put the Mac and Quest on a network where the Quest can reach the Mac. Find
   the Mac's Wi-Fi IP with `ipconfig getifaddr en0` (check the active network
   interface if this returns nothing).
2. In Unity, open `Assets/Scenes/FreeNavScene.unity`. Select the XR Origin's
   `PredictionWebSocketClient` component and set **Host** to that IP, leaving
   **Port** at `8765`. Build and install the updated APK. The IP is stored in
   the build, so update it and rebuild if the Mac's IP changes.
3. On the Mac, start the live prediction server from the repository root:

   ```sh
   uv run python -m pipeline realtime --model ml_pipeline/models/realtime_models.pkl \
     --skip-personalize --serial 15C3 --ws-host 0.0.0.0
   ```

   `0.0.0.0` is the server's bind address; enter the Mac's actual IP in Unity.
   The prediction WebSocket has no authentication or encryption, so use a
   trusted Wi-Fi network. Allow inbound Python connections through the Mac
   firewall if prompted. No `adb reverse` is needed during use.
4. To test the wireless link and movement without Muse EEG, stop the live
   server and run:

   ```sh
   uv run python unity/environment_1/tools/smoke_predictions.py --host 0.0.0.0
   ```

   Launch the app and wait for `Quest connected` before entering a direction.
   Only one server can use port `8765` at a time.

The current checkout has a legacy two-class model but no four-class or
participant bundle under `ml_pipeline/models/`. The legacy model can drive
left/right turns; forward/backward from live EEG needs a compatible four-class
bundle. The synthetic server exercises all four Unity movement mappings.

## Market decor

The furniture, plants, signs, wood floor, and warm lighting in `FreeNavScene`
come from `Assets/Editor/RelaxedMarketBuilder.cs`. Run **BCI > Rebuild Relaxed
Market** (or `-executeMethod RelaxedMarketBuilder.BuildAndSave` in batch mode)
after changing the layout in that file. The builder deletes and recreates the
`_RelaxedMarket` object, so edit the script rather than the objects under it. It
does not move the original shelves or the named food items that `ObjectPicker`
and the voice prompts use. It logs a warning for any prop that overlaps a wall
or sits within 0.8 m of the auto-move data collection route.

`SceneCapture.CaptureAll` renders a top-down view and eye-level views to the
folder passed as `-captureDir`, for reviewing layout changes without a headset.
