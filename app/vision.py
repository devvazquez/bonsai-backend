"""Vision: describe an image with Groq's model.

Besides the Groq client, this holds what other modules share: the pooled HTTP
client (`stt` and `tts` reuse it too), the out-of-quota exception and image
format sniffing.

There was a second provider (Gemini) and a module per provider so they could be
swapped. It went away: Groq is faster and far steadier (552 ms of vision against
844 ms on the same image), and keeping a dispatch layer over a single branch
only invites one of the two paths to rot.
"""

from __future__ import annotations

import base64
import json
import os
import re

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Opens the TLS connection without spending tokens (see warmup).
WARMUP_URL = "https://api.groq.com/openai/v1/models"

# Check the current name at https://console.groq.com/docs/models — Groq renames
# and retires models often.
MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Groq does not always send retry-after, but it does say the wait in the error
# text: "Please try again in 16.56s" (or "in 12m39.024s").
_WAIT_RE = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)


class VisionRateLimit(Exception):
    """Groq's quota is spent (429).

    Kept apart from other errors so the client gets a 429 with how long to wait
    instead of a generic 502 that looks like a broken server when all it has to
    do is wait.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def describe_error(e: BaseException) -> str:
    """Text for the client that is never empty.

    httpx timeouts have an empty `str(e)`, so without this the error reaching
    the client was «Failed to describe the image: » and said nothing.
    """
    return str(e) or type(e).__name__


# --------------------------------------------------------------------------
# Image format
# --------------------------------------------------------------------------
# The data URL has to state the real format: sending a PNG labelled JPEG works
# by luck, not by design.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(image_base64: str) -> str:
    """Detects the format from the header, without decoding everything.

    Decoding 4 MB of base64 just to read 8 bytes would be tens of ms per
    request, so the first 16 characters (12 bytes) are enough.
    """
    try:
        header = base64.b64decode(image_base64[:16], validate=False)
    except Exception:
        return "image/jpeg"

    for signature, mime in _SIGNATURES:
        if header.startswith(signature):
            return mime
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# --------------------------------------------------------------------------
# Shared HTTP client
# --------------------------------------------------------------------------
# One client for the whole process instead of one per request: the TLS
# handshake costs ~220 ms measured, so it is paid once and not on every photo.
# `retries` covers the server having closed our idle pooled connection.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=2),
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
        )
    return _client


async def aclose() -> None:
    """Closes the connection when the server shuts down."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# --------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------
def api_key() -> str:
    return os.environ.get("GROQ_API_KEY", "")


def auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _seconds_to_wait(resp: httpx.Response) -> float | None:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _WAIT_RE.search(resp.text)
    if m:
        minutes = float(m.group(1) or 0)
        return minutes * 60 + float(m.group(2))
    return None


# Conversation turns that go before the question and the image.
#
# They are not made up: it is what actually happened a moment earlier. The
# person said the wake word and the glasses answered with a recorded clip while
# the photo uploaded. Taking them as said, the model answers like someone
# continuing a conversation instead of someone taking an order.
#
# They live in env vars because they have to match what the firmware does: if
# you change the clip the glasses play, change this too or you are telling the
# model about a conversation that never happened.
VOICE_PREAMBLE: tuple[tuple[str, str], ...] = (
    ("user", os.environ.get("ASK_WAKE_PHRASE", "Hey Bonsai!")),
    ("assistant", os.environ.get("ASK_WAKE_REPLY", "Diga’m!")),
)


def _as_openai_tools(tools: list[dict]) -> list[dict]:
    """Wraps the device's tool list in the shape Groq's API expects.

    The ESP32 sends plain `{name, description, parameters}` because that is what
    it knows about itself; the OpenAI envelope is our problem, not its.
    """
    out = []
    for t in tools:
        fn = {"name": t["name"]}
        if t.get("description"):
            fn["description"] = t["description"]
        # An empty object and no `parameters` are different things to the API:
        # omit it entirely for a tool that takes no arguments.
        if t.get("parameters"):
            fn["parameters"] = t["parameters"]
        out.append({"type": "function", "function": fn})
    return out


def _tool_calls(message: dict) -> list[dict]:
    """Normalizes Groq's tool_calls into `{name, args}`.

    Arguments come as a JSON *string*, so they are parsed here. A malformed one
    drops only that call: the rest of the answer is still good, and the person
    would rather hear the sentence than get a 502.
    """
    calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        calls.append({"name": name, "args": args if isinstance(args, dict) else {}})
    return calls


async def describe_image(
    api_key: str,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
    preamble: tuple[tuple[str, str], ...] | None = None,
    tools: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Describes the image with Groq's vision model.

    Returns (text to say out loud, tool calls the model asked for).

    `preamble` are ("user"/"assistant", text) turns inserted between the system
    prompt and the message carrying the image. /ask uses it to take the wake
    word as said (see VOICE_PREAMBLE).

    `tools` are the actions the *device* says it can perform. We never run them:
    they go to the model and whatever it picks comes straight back to the
    firmware, which is the only side that knows what they mean.
    """
    # Previous turns, if any, go between the system message and the image. The
    # format here is OpenAI's, so they pass through as they are.
    previous = [{"role": role, "content": text} for role, text in (preamble or ())]

    payload = {
        "model": MODEL,
        "temperature": 0.4,
        # Safety ceiling: the answer is read out loud, so a long one is seconds
        # of waiting. The real limit is the prompt (1-2 sentences); this only
        # stops it running away.
        "max_completion_tokens": 150,
        # No step-by-step reasoning: it adds nothing to describing an image and
        # only costs latency and tokens.
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
        "messages": [
            {"role": "system", "content": system_prompt},
            *previous,
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            # Sniffed for real: this used to be a hardcoded
                            # "image/jpeg" even when the image was a PNG.
                            "url": f"data:{sniff_mime(image_base64)};base64,{image_base64}"
                        },
                    },
                ],
            },
        ],
    }

    # Only added when the device sent tools, so a request without them goes out
    # byte for byte as it always did.
    if tools:
        payload["tools"] = _as_openai_tools(tools)
        payload["tool_choice"] = "auto"

    resp = await get_client().post(
        GROQ_URL,
        json=payload,
        headers=auth_headers(api_key),
        timeout=timeout,
    )

    if resp.status_code == 429:
        raise VisionRateLimit(
            f"Groq quota spent: {resp.text[:300]}",
            _seconds_to_wait(resp),
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"Groq error ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    # Safety net in case the model ignores reasoning_format.
    text = _THINK_RE.sub("", message.get("content") or "").strip()
    return text, _tool_calls(message)


async def warmup() -> bool:
    """Opens the TLS connection before the first photo.

    The TLS handshake is ~220 ms that otherwise the first person to use the
    glasses pays. Asks for a model listing: not a single token of quota.
    """
    key = api_key()
    if not key:
        return False
    try:
        await get_client().get(WARMUP_URL, headers=auth_headers(key), timeout=10.0)
        return True
    except Exception:
        # It is a warmup: if it fails, the first real request opens the
        # connection anyway. Not a reason to refuse to start.
        return False
