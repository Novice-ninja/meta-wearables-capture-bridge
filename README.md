# Meta Wearables Capture Bridge (iOS)

This repository is intentionally limited to one job: expose a photo or video
from Meta Ray-Ban glasses, an optional voice/text instruction, and capture
metadata to an OpenClaw-compatible HTTP consumer.

```text
Ray-Ban Meta camera
        │  Meta DAT 0.9.0
        ▼
iOS Capture Bridge ──multipart POST──▶ Relay API
                                          ├── manifest + media URLs
                                          └── OpenClaw/Hermes event webhook
                                                        │
                                                        ▼
                                                 Hermes / OpenClaw
```

The bridge does **not** recognize signs, discover parking providers, fill forms,
take payments, or send parking alerts.

For hackathon testing the app uses Bluetooth Classic DAT transport, not the
glasses Wi-Fi hotspot. This keeps the iPhone online for relay uploads and avoids
the Hotspot Configuration and Access Wi-Fi Information entitlements that cannot
be signed by an Apple Personal Team. Bluetooth has lower image/video throughput,
so the app uses a 15 FPS low-resolution preview and prioritizes a JPEG when the
user taps **Capture & expose**.

## Run the relay

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

export CAPTURE_BRIDGE_PUBLIC_URL="http://$(ipconfig getifaddr en0):8000"
export CAPTURE_BRIDGE_API_KEY="hackathon-secret"
# Optional: direct OpenClaw delivery. It receives a small manifest URL payload,
# never the full image/audio/video binary.
export OPENCLAW_HOOK_URL="http://127.0.0.1:18789/hooks/agent"
export OPENCLAW_HOOK_TOKEN="your-openclaw-hook-token"
# Leave blank to use OpenClaw's default agent.
export OPENCLAW_AGENT_ID=""

uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for the generated API explorer.

## Configure Meta and run iOS

1. In the Apple Developer portal, register an App ID for the bundle ID you will
   use, for example `com.yourteam.wearablecapturebridge`.
2. In Wearables Developer Center, create a DAT project for that exact iOS bundle
   identifier. Copy its **Meta App ID** and **Client Token**. Do not commit them.
3. Pair the glasses with the Meta AI app on the test iPhone. In Meta AI, open
   **Settings → App Info**, tap **App version** five times, then enable
   **Developer Mode**. Verify glasses firmware is supported and up to date.
4. Open `ios/ParkPilot/CameraAccess.xcodeproj` in Xcode. In the **CameraAccess**
   target's **Signing & Capabilities**, select your Apple team and set its bundle
   identifier to the one from step 1. Automatic signing is already enabled.
5. Copy the local configuration template, then edit only the ignored local
   copy with the generated Meta credentials and relay address:

   ```bash
   cp ios/ParkPilot/Config/CaptureBridge.local.xcconfig.example \
      ios/ParkPilot/Config/CaptureBridge.local.xcconfig
   ```

   Set `META_APP_ID`, `CLIENT_TOKEN`, `CAPTURE_BRIDGE_URL`, and
   `CAPTURE_BRIDGE_API_KEY` in that file. The Mac LAN IP—not `127.0.0.1`—is
   required when running on a physical iPhone. The local file is ignored by Git.
6. Connect the iPhone to the Mac, select it as the run destination, trust the
   development certificate if iOS asks, and press Run. In the app, select
   **Connect**, complete the Meta AI callback, start the session, then Preview.
7. Tap **Capture & expose** for a JPEG. Optionally record an instruction first.
   The app captures its best available phone location after permission is granted.
   To relay video, use the normal Record control, stop it, then choose **Expose**
   from the video preview.

For a glasses-free demo, use the sample's debug Mock Device Kit. This project is
based on Meta's official Camera Access sample and pins `meta-wearables-dat-ios`
to `0.9.0`.

## OpenClaw connection

The relay calls the native OpenClaw agent ingress with this small JSON shape:

```json
{
  "name": "Meta wearable capture",
  "sessionMode": "isolated",
  "idempotencyKey": "<capture UUID>",
  "message": "A new wearable capture is ready…\nmanifest: http://<relay>/v1/captures/<id>"
}
```

Configure OpenClaw's hook token and use the same `OPENCLAW_HOOK_TOKEN` in the
relay process. The agent should fetch the manifest and media URLs using
`X-Capture-Key: <CAPTURE_BRIDGE_API_KEY>`. If OpenClaw is on another computer,
`CAPTURE_BRIDGE_PUBLIC_URL` must be a LAN URL or HTTPS URL reachable from that
computer.

Minimal OpenClaw configuration (replace `main` if your target agent uses a
different ID):

```json5
{
  hooks: {
    enabled: true,
    token: "your-openclaw-hook-token",
    path: "/hooks",
    allowedAgentIds: ["main"]
  }
}
```

After restarting OpenClaw, verify the hook independently before testing glasses:

```bash
curl -X POST http://127.0.0.1:18789/hooks/agent \
  -H "Authorization: Bearer your-openclaw-hook-token" \
  -H "Content-Type: application/json" \
  -d '{"name":"Bridge check","agentId":"main","message":"Capture bridge connectivity check","sessionMode":"isolated"}'
```

## Generic agent contract

If `AGENT_WEBHOOK_URL` is configured, the relay sends:

```json
{
  "event": "wearable.capture.created",
  "capture_id": "09b790c8-...",
  "manifest_url": "http://192.168.1.50:8000/v1/captures/09b790c8-...",
  "manifest": {
    "schema_version": "wearable.capture.v1",
    "instruction": "Park here for one hour",
    "source": {
      "platform": "ios",
      "capture_device": "meta_wearable",
      "sdk": "meta-wearables-dat-ios",
      "sdk_version": "0.9.0"
    },
    "metadata": {
      "image_origin": "MWDATCamera.PhotoData",
      "audio_input": "Ray-Ban Meta",
      "audio_input_type": "BluetoothHFP"
    },
    "image": { "content_type": "image/jpeg", "bytes": 12345, "url": ".../image" },
    "audio": { "content_type": "audio/mp4", "bytes": 6789, "url": ".../audio" },
    "video": null
  }
}
```

The exact machine-readable contract is in
`docs/wearable.capture.v1.schema.json`. If a shared key is configured, the relay
sends `X-Capture-Key` to the webhook and requires it when fetching manifests or
media.

## Current Meta SDK boundary

- Pairing/registration remains owned by the Meta AI app.
- DAT provides the camera stream and `PhotoData` JPEG bytes. Recorded MP4 video
  is generated from that DAT stream and relayed after recording stops.
- Audio goes through the standard iOS audio route. The manifest records whether
  the selected input was the Ray-Ban Bluetooth HFP route or the iPhone mic.
- The public DAT photo is stream-derived; do not assume the native 12 MP camera
  asset or EXIF metadata is available.
- The phone location is best-effort and permission-gated. A capture is never
  delayed waiting for location permission or a GPS fix.
