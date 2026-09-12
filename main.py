"""
parking_a2a_agent.py

A single-file A2A Parking Vision Agent for Amazon Bedrock AgentCore Runtime.

Responsibility:
    A2A FilePart (parking-sign image)
        -> decode/validate image
        -> Amazon Bedrock Converse multimodal inference
        -> validate normalized parking JSON
        -> return JSON as an A2A DataPart

This agent intentionally DOES NOT pay for parking.  It only turns a parking
sign image into machine-readable parking/payment metadata.

Environment:
    AWS_REGION=us-west-2
    BEDROCK_MODEL_ID=<vision-capable Bedrock model or inference profile>

The AWS credentials are picked up from the normal boto3 credential chain
(AWS_PROFILE, ~/.aws/credentials, IAM role in AgentCore, etc.).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any, Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field, ValidationError

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import DataPart, FileWithBytes, Part, UnsupportedOperationError
from a2a.types import (AgentCard,AgentCapabilities,AgentSkill)
from a2a.utils import new_agent_parts_message
from a2a.utils.parts import get_file_parts
from a2a.utils.errors import ServerError

from bedrock_agentcore.runtime import serve_a2a


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID")

MAX_IMAGE_BYTES = 3_750_000  # Bedrock Converse image limit is 3.75 MB.
ALLOWED_MIME_TYPES = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("parking-a2a-agent")


# ---------------------------------------------------------------------------
# Structured output contract
# ---------------------------------------------------------------------------

class ParkingAction(BaseModel):
    type: Literal["parking_payment"] = "parking_payment"
    provider: str | None = None
    location_id: str | None = None


class ParkingContext(BaseModel):
    site_name: str | None = None


class ParkingPayment(BaseModel):
    phone_number: str | None = None
    payment_required: bool | None = None
    payment_methods: list[str] = Field(default_factory=list)


class ParkingConstraints(BaseModel):
    annual_pass_free: bool | None = None
    pass_must_be_visible: bool | None = None
    restrictions: list[str] = Field(default_factory=list)


class ParkingResult(BaseModel):
    action: ParkingAction
    context: ParkingContext
    payment: ParkingPayment
    constraints: ParkingConstraints
    alternative_payment_locations: list[str] = Field(default_factory=list)
    requires_user_input: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
# ---------------------------------------------------------------------------
# Bedrock prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are a parking-sign interpretation service.

You receive ONE image containing parking signage. Extract only information
visibly supported by the image. Never invent, infer, or guess a location ID,
provider, price, restriction, phone number, exemption, or payment status.

Return ONLY a single JSON object. Do not use Markdown fences and do not add
explanatory prose.

The JSON MUST match this shape exactly:

{
  "action": {
    "type": "parking_payment",
    "provider": string | null,
    "location_id": string | null
  },
  "context": {
    "site_name": string | null
  },
  "payment": {
    "phone_number": string | null,
    "payment_required": boolean | null,
    "payment_methods": [string]
  },
  "constraints": {
    "annual_pass_free": boolean | null,
    "pass_must_be_visible": boolean | null,
    "restrictions": [string]
  },
  "alternative_payment_locations": [string],
  "requires_user_input": [string],
  "confidence": number
}

Rules:
- Preserve identifiers exactly as printed, including leading zeros.
- If the sign names a parking app/provider, put it in action.provider.
- If the sign shows a zone/location number, put it in action.location_id.
- payment_required should be true only when the sign indicates payment is
  required for the normal case; account for explicitly displayed exemptions
  separately under constraints.
- requires_user_input should list information needed to continue a payment
  workflow that is not visible in the sign, such as parking duration, vehicle
  identifier, annual-pass status, or payment authorization.
- confidence is your confidence in the extraction as a whole, from 0 to 1.
"""


# ---------------------------------------------------------------------------
# Bedrock invocation
# ---------------------------------------------------------------------------
def _log_identity(label: str, session=None):
    session = session or boto3.Session()
    sts = session.client("sts")
    identity = sts.get_caller_identity()

    logger.info(
        "%s AWS identity: account=%s arn=%s",
        label,
        identity["Account"],
        identity["Arn"],
    )

def _bedrock_client():
    if not BEDROCK_MODEL_ID:
        raise RuntimeError(
            "BEDROCK_MODEL_ID is not set. Set it to a vision-capable "
            "Amazon Bedrock model/inference-profile ID."
        )

    role_arn = os.getenv("BEDROCK_CROSS_ACCOUNT_ROLE_ARN")

    # If no cross-account role is configured, use the agent's
    # normal/default AWS credentials.
    if not role_arn:
        return boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
        )

    # Assume the Bedrock-access role in the target AWS account.
    sts = boto3.client("sts")

    assumed_role = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="parking-a2a-bedrock",
    )

    credentials = assumed_role["Credentials"]

    # Create an isolated session using the temporary credentials
    # from the target account.
    assumed_session = boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=AWS_REGION,
    )

    return assumed_session.client(
        "bedrock-runtime",
        region_name=AWS_REGION,
    )

def _extract_json(text: str) -> dict[str, Any]:
    """
    Parse model output defensively.

    The prompt requests raw JSON.  This also tolerates accidental ```json fences
    so a harmless formatting deviation does not break the A2A contract.
    """
    text = text.strip()

    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Bedrock response was JSON but not a JSON object.")
    return value


def analyze_parking_image(image_bytes: bytes, image_format: str) -> ParkingResult:
    client = _bedrock_client()

    response = client.converse(
        modelId=BEDROCK_MODEL_ID,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Analyze this parking sign image and return the "
                            "structured parking-payment JSON."
                        )
                    },
                    {
                        "image": {
                            "format": image_format,
                            "source": {"bytes": image_bytes},
                        }
                    },
                ],
            }
        ],
        inferenceConfig={
            "maxTokens": 1200,
        },
    )

    content = response["output"]["message"]["content"]
    response_text = next(
        (block["text"] for block in content if "text" in block),
        None,
    )
    if not response_text:
        raise ValueError("Bedrock returned no text output.")

    raw = _extract_json(response_text)
    return ParkingResult.model_validate(raw)


# ---------------------------------------------------------------------------
# A2A input handling
# ---------------------------------------------------------------------------

def _extract_inline_image(context: RequestContext) -> tuple[bytes, str]:
    """
    Accept exactly one inline image from the incoming A2A Message FilePart.

    A2A FileWithBytes carries base64 on the wire.  We intentionally reject URI
    inputs here so this demo does not become an arbitrary server-side URL
    fetcher.  URI/S3 handling can be added later with an explicit allow-list.
    """
    if context.message is None:
        raise ValueError("A2A request contains no message.")

    files = get_file_parts(context.message.parts)
    if not files:
        raise ValueError("A2A request must include one image FilePart.")

    if len(files) != 1:
        raise ValueError("A2A request must include exactly one image.")

    image_file = files[0]

    if not isinstance(image_file, FileWithBytes):
        raise ValueError(
            "Only inline A2A FileWithBytes images are accepted; URI files are disabled."
        )

    mime_type = image_file.mime_type
    if not mime_type:
        raise ValueError("Image FilePart must include mimeType.")

    mime_type = mime_type.lower()
    image_format = ALLOWED_MIME_TYPES.get(mime_type)
    if image_format is None:
        raise ValueError(
            f"Unsupported image MIME type: {mime_type}. "
            f"Supported: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )

    try:
        image_bytes = base64.b64decode(image_file.bytes, validate=True)
    except Exception as exc:
        raise ValueError("Image FilePart contains invalid base64 data.") from exc

    if not image_bytes:
        raise ValueError("Image is empty.")

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image is too large ({len(image_bytes)} bytes); "
            f"maximum is {MAX_IMAGE_BYTES} bytes."
        )

    return image_bytes, image_format


# ---------------------------------------------------------------------------
# A2A executor
# ---------------------------------------------------------------------------

class ParkingVisionExecutor(AgentExecutor):
    """
    A deliberately narrow A2A executor:

        image -> Bedrock vision -> validated ParkingResult -> A2A DataPart

    No payment side effects live in this agent.
    """

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        try:
            image_bytes, image_format = _extract_inline_image(context)

            result = analyze_parking_image(
                image_bytes=image_bytes,
                image_format=image_format,
            )

            payload = result.model_dump(mode="json")

            # DataPart keeps the result machine-readable for the calling A2A
            # client instead of forcing it to scrape prose.
            message = new_agent_parts_message(
                parts=[
                    Part(
                        root=DataPart(
                            data=payload,
                            metadata={
                                "contentType": "application/json",
                                "schema": "parking-result/v1",
                            },
                        )
                    )
                ],
                context_id=getattr(context.message, "context_id", None),
                task_id=getattr(context.message, "task_id", None),
            )

            await event_queue.enqueue_event(message)

        except (ValueError, ValidationError) as exc:
            logger.warning("Invalid parking request/result: %s", exc)

            error_payload = {
                "error": "invalid_request_or_model_output",
                "message": str(exc),
            }
            message = new_agent_parts_message(
                parts=[Part(root=DataPart(data=error_payload))]
            )
            await event_queue.enqueue_event(message)

        except (ClientError, BotoCoreError) as exc:
            logger.exception("Bedrock invocation failed")
            raise RuntimeError("Amazon Bedrock invocation failed.") from exc

        except Exception:
            logger.exception("Unhandled parking-agent failure")
            raise

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        # The current operation is a single Bedrock inference call and is not
        # implemented as a cancellable long-running task.
        raise ServerError(error=UnsupportedOperationError())
# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting Parking A2A Agent: region=%s model=%s",
        AWS_REGION,
        BEDROCK_MODEL_ID or "<not set>",
    )

    parking_skill = AgentSkill(
        id="analyze-parking-sign",
        name="Analyze Parking Sign",
        description=(
            "Analyzes a parking sign image and extracts structured "
            "parking-payment metadata including provider, location ID, "
            "payment requirements, exemptions, restrictions, and missing "
            "information required by the caller."
        ),
        tags=[
            "parking",
            "vision",
            "image-understanding",
            "structured-extraction",
        ],
        examples=[
            "Analyze this parking sign image and return structured parking metadata."
        ],
        input_modes=[
            "image/jpeg",
            "image/png",
            "image/webp",
        ],
        output_modes=[
            "application/json",
        ],
    )

    parking_agent_card = AgentCard(
        name="Parking Vision Agent",

        description=(
            "Accepts parking sign images, analyzes them using Amazon Bedrock, "
            "and returns structured parking metadata."
        ),

        url="http://localhost:9000/",

        version="1.0.0",

        capabilities=AgentCapabilities(
            streaming=False,
        ),

        default_input_modes=[
            "image/jpeg",
            "image/png",
            "image/webp",
        ],

        default_output_modes=[
            "application/json",
        ],

        skills=[
            parking_skill,
        ],
    )

    # AgentCore's A2A helper exposes the root A2A endpoint, health endpoint,
    # Agent Card support, header propagation, and port 9000 expected by
    # AgentCore Runtime.
    serve_a2a(
        ParkingVisionExecutor(),
        agent_card=parking_agent_card,
    )
