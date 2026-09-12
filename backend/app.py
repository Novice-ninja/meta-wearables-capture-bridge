"""Capture-only bridge: Meta wearable -> iOS -> Hermes/OpenClaw.

This service deliberately does not interpret parking signs or execute actions.
It stores each capture, exposes a stable manifest/media API, and optionally
notifies an agent webhook that a new capture is ready to fetch.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel


DATA_DIR = Path(os.getenv("CAPTURE_BRIDGE_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_BASE_URL = os.getenv("CAPTURE_BRIDGE_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_WEBHOOK_URL = os.getenv("AGENT_WEBHOOK_URL", "")
API_KEY = os.getenv("CAPTURE_BRIDGE_API_KEY", "")

app = FastAPI(title="Meta Wearables Capture Bridge", version="0.1.0")


class MediaObject(BaseModel):
    content_type: str
    bytes: int
    url: str


class CaptureManifest(BaseModel):
    schema_version: str = "wearable.capture.v1"
    capture_id: str
    captured_at: str
    received_at: str
    source: dict[str, Any]
    instruction: str | None = None
    metadata: dict[str, Any]
    image: MediaObject
    audio: MediaObject | None = None


def authorize(x_capture_key: str | None = Header(None)) -> None:
    if API_KEY and x_capture_key != API_KEY:
        raise HTTPException(401, "invalid X-Capture-Key")


def _capture_dir(capture_id: str) -> Path:
    try:
        uuid.UUID(capture_id)
    except ValueError as exc:
        raise HTTPException(404, "capture not found") from exc
    directory = DATA_DIR / capture_id
    if not directory.is_dir():
        raise HTTPException(404, "capture not found")
    return directory


def _read_manifest(capture_id: str) -> dict[str, Any]:
    path = _capture_dir(capture_id) / "manifest.json"
    if not path.exists():
        raise HTTPException(404, "capture not found")
    return json.loads(path.read_text())


async def _notify_agent(manifest: dict[str, Any]) -> None:
    if not AGENT_WEBHOOK_URL:
        return
    headers = {"X-Capture-Key": API_KEY} if API_KEY else {}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            AGENT_WEBHOOK_URL,
            json={
                "event": "wearable.capture.created",
                "capture_id": manifest["capture_id"],
                "manifest_url": f'{PUBLIC_BASE_URL}/v1/captures/{manifest["capture_id"]}',
                "manifest": manifest,
            },
            headers=headers,
        )
        response.raise_for_status()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "webhook_configured": bool(AGENT_WEBHOOK_URL)}


@app.post(
    "/v1/captures",
    response_model=CaptureManifest,
    dependencies=[Depends(authorize)],
    status_code=201,
)
async def create_capture(
    captured_at: str = Form(...),
    source_json: str = Form(...),
    metadata_json: str = Form("{}"),
    instruction: str | None = Form(None),
    image: UploadFile = File(...),
    audio: UploadFile | None = File(None),
) -> dict[str, Any]:
    try:
        source = json.loads(source_json)
        metadata = json.loads(metadata_json)
        if not isinstance(source, dict) or not isinstance(metadata, dict):
            raise ValueError
        datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "captured_at/source_json/metadata_json is invalid") from exc

    image_bytes = await image.read()
    audio_bytes = await audio.read() if audio else None
    if not image_bytes:
        raise HTTPException(400, "image is empty")
    if len(image_bytes) > 15 * 1024 * 1024:
        raise HTTPException(413, "image exceeds 15 MB")
    if audio_bytes and len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(413, "audio exceeds 20 MB")

    capture_id = str(uuid.uuid4())
    directory = DATA_DIR / capture_id
    directory.mkdir()
    (directory / "image.jpg").write_bytes(image_bytes)
    if audio_bytes:
        (directory / "audio.m4a").write_bytes(audio_bytes)

    base = f"{PUBLIC_BASE_URL}/v1/captures/{capture_id}"
    manifest = CaptureManifest(
        capture_id=capture_id,
        captured_at=captured_at,
        received_at=datetime.now(UTC).isoformat(),
        source=source,
        instruction=instruction or None,
        metadata=metadata,
        image=MediaObject(
            content_type=image.content_type or "image/jpeg",
            bytes=len(image_bytes),
            url=f"{base}/image",
        ),
        audio=(
            MediaObject(
                content_type=audio.content_type or "audio/mp4",
                bytes=len(audio_bytes),
                url=f"{base}/audio",
            )
            if audio and audio_bytes
            else None
        ),
    ).model_dump()
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))

    try:
        await _notify_agent(manifest)
    except httpx.HTTPError as exc:
        # The capture remains available even when the consumer is temporarily down.
        print(f"agent webhook delivery failed for {capture_id}: {exc}", flush=True)
    return manifest


@app.get(
    "/v1/captures/{capture_id}",
    response_model=CaptureManifest,
    dependencies=[Depends(authorize)],
)
async def get_capture(capture_id: str) -> dict[str, Any]:
    return _read_manifest(capture_id)


@app.get("/v1/captures", dependencies=[Depends(authorize)])
async def list_captures(limit: int = 25) -> dict[str, Any]:
    """Polling fallback for consumers that cannot receive webhooks."""
    limit = max(1, min(limit, 100))
    manifest_paths = sorted(
        DATA_DIR.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )[:limit]
    return {"items": [json.loads(path.read_text()) for path in manifest_paths]}


@app.get("/v1/captures/{capture_id}/image", dependencies=[Depends(authorize)])
async def get_image(capture_id: str) -> FileResponse:
    return FileResponse(_capture_dir(capture_id) / "image.jpg", media_type="image/jpeg")


@app.get("/v1/captures/{capture_id}/audio", dependencies=[Depends(authorize)])
async def get_audio(capture_id: str) -> FileResponse:
    path = _capture_dir(capture_id) / "audio.m4a"
    if not path.exists():
        raise HTTPException(404, "audio not present")
    return FileResponse(path, media_type="audio/mp4")
