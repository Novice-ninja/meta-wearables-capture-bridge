# Meta Wearables Capture Bridge (iOS)

This repository is intentionally limited to one job: expose a photo from Meta
Ray-Ban glasses, an optional voice/text instruction, and capture metadata to a
Hermes/OpenClaw-compatible HTTP consumer.

```text
Ray-Ban Meta camera
        │  Meta DAT 0.9.0
        ▼
iOS Capture Bridge ──multipart POST──▶ Relay API
                                          ├── manifest + media URLs
                                          └── wearable.capture.created webhook
                                                        │
                                                        ▼
                                                 Hermes / OpenClaw
```

The bridge does **not** recognize signs, discover parking providers, fill forms,
take payments, or send parking alerts.

## Run the relay

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

export CAPTURE_BRIDGE_PUBLIC_URL="http://$(ipconfig getifaddr en0):8000"
export CAPTURE_BRIDGE_API_KEY="hackathon-secret"
# Optional: Hermes/OpenClaw event receiver
export AGENT_WEBHOOK_URL="http://127.0.0.1:3000/hooks/wearable-capture"

uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for the generated API explorer.

## Configure and run iOS

1. Open `ios/ParkPilot/CameraAccess.xcodeproj` in Xcode 16 or newer.
2. In `CameraAccess/Info.plist`, set `CaptureBridgeURL` to the Mac's LAN URL,
   such as `http://192.168.1.50:8000`, and set `CaptureBridgeAPIKey` to the same
   value exported above. `127.0.0.1` will not reach the Mac from a physical iPhone.
3. Set your Apple development team and a unique bundle identifier.
4. For real glasses, configure `META_APP_ID` and `CLIENT_TOKEN` from Meta's
   Wearables Developer Center. Pair the glasses in the Meta AI app first.
5. Run on an iPhone. Register, start the DAT session, start Preview, optionally
   record an instruction, then tap **Capture & expose**.

For a glasses-free demo, use the sample's debug Mock Device Kit. This project is
based on Meta's official Camera Access sample and pins `meta-wearables-dat-ios`
to `0.9.0`.

## Agent contract

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
    "audio": { "content_type": "audio/mp4", "bytes": 6789, "url": ".../audio" }
  }
}
```

The exact machine-readable contract is in
`docs/wearable.capture.v1.schema.json`. If a shared key is configured, the relay
sends `X-Capture-Key` to the webhook and requires it when fetching manifests or
media.

## Current Meta SDK boundary

- Pairing/registration remains owned by the Meta AI app.
- DAT provides the camera stream and `PhotoData` JPEG bytes.
- Audio goes through the standard iOS audio route. The manifest records whether
  the selected input was the Ray-Ban Bluetooth HFP route or the iPhone mic.
- The public DAT photo is stream-derived; do not assume the native 12 MP camera
  asset or EXIF metadata is available.

