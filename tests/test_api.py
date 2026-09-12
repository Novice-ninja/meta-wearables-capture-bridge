import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import backend.app as api


class CaptureBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        api.DATA_DIR = Path(self.temp_dir.name)
        api.API_KEY = "test-key"
        api.AGENT_WEBHOOK_URL = ""
        self.client = TestClient(api.app)
        self.headers = {"X-Capture-Key": "test-key"}

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_upload_and_fetch_manifest_and_media(self):
        response = self.client.post(
            "/v1/captures",
            headers=self.headers,
            data={
                "captured_at": "2026-09-12T12:00:00Z",
                "source_json": '{"platform":"ios","device":"meta_wearable"}',
                "metadata_json": '{"location":{"latitude":47.6,"longitude":-122.3}}',
                "instruction": "Read this parking sign",
            },
            files={
                "image": ("image.jpg", b"jpeg-data", "image/jpeg"),
                "audio": ("audio.m4a", b"audio-data", "audio/mp4"),
            },
        )
        self.assertEqual(response.status_code, 201)
        manifest = response.json()
        self.assertEqual(manifest["schema_version"], "wearable.capture.v1")
        self.assertEqual(manifest["image"]["bytes"], 9)
        capture_id = manifest["capture_id"]
        self.assertEqual(
            self.client.get(f"/v1/captures/{capture_id}", headers=self.headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/v1/captures/{capture_id}/image", headers=self.headers).content,
            b"jpeg-data",
        )
        listed = self.client.get("/v1/captures", headers=self.headers).json()
        self.assertEqual(listed["items"][0]["capture_id"], capture_id)

    def test_requires_key(self):
        response = self.client.post(
            "/v1/captures",
            data={"captured_at": "2026-09-12T12:00:00Z", "source_json": "{}"},
            files={"image": ("image.jpg", b"x", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
