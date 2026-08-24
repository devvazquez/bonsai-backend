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
import time
from typing import Any, AsyncIterator

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


async def describe_image_stream(
    api_key: str,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
    preamble: tuple[tuple[str, str], ...] | None = None,
    tools: list[dict] | None = None,
) -> tuple[AsyncIterator[str], dict[str, Any]]:
    """Describes the image with Groq's vision model using streaming.

    Returns (sentence_stream, meta_dict).
    meta_dict is populated with 'text', 'tools', and 'vision_ms' as streaming progresses.
    """
    previous = [{"role": role, "content": text} for role, text in (preamble or ())]

    payload = {
        "model": MODEL,
        "temperature": 0.1,
        "max_completion_tokens": 150,
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
        "stream": True,
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
                            "url": f"data:{sniff_mime(image_base64)};base64,{image_base64}"
                        },
                    },
                ],
            },
        ],
    }

    if tools:
        payload["tools"] = _as_openai_tools(tools)
        payload["tool_choice"] = "auto"

    meta: dict[str, Any] = {"text": "", "tools": [], "vision_ms": 0}

    async def _stream() -> AsyncIterator[str]:
        t0 = time.perf_counter()
        async with get_client().stream(
            "POST",
            GROQ_URL,
            json=payload,
            headers=auth_headers(api_key),
            timeout=timeout,
        ) as resp:
            if resp.status_code == 429:
                body = await resp.aread()
                # Create a temporary response object to parse retry-after
                temp_resp = httpx.Response(resp.status_code, headers=resp.headers, text=body.decode(errors="replace"))
                raise VisionRateLimit(
                    f"Groq quota spent: {temp_resp.text[:300]}",
                    _seconds_to_wait(temp_resp),
                )

            if resp.status_code >= 400:
                body = await resp.aread()
                raise RuntimeError(f"Groq error ({resp.status_code}): {body.decode(errors='replace')[:300]}")

            content_type = resp.headers.get("content-type", "")
            tool_chunks: dict[int, dict] = {}
            full_text_list: list[str] = []
            buffer = ""
            in_think = False
            first_token = True

            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line or line.startswith(":"):
                    continue

                # Handle plain JSON mock or response
                if line.startswith("{") and not line.startswith("data:"):
                    try:
                        data = json.loads(line)
                        choice = (data.get("choices") or [{}])[0]
                        msg = choice.get("message") or {}
                        text = _THINK_RE.sub("", msg.get("content") or "").strip()
                        meta["text"] = text
                        meta["tools"] = _tool_calls(msg)
                        meta["vision_ms"] = int((time.perf_counter() - t0) * 1000)
                        if text:
                            yield text
                        return
                    except Exception:
                        pass

                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}

                    # Tool calls
                    if "tool_calls" in delta and delta["tool_calls"]:
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_chunks:
                                tool_chunks[idx] = {"name": "", "args": ""}
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                tool_chunks[idx]["name"] += fn["name"]
                            if fn.get("arguments"):
                                tool_chunks[idx]["args"] += fn["arguments"]

                    content = delta.get("content") or ""
                    if not content:
                        continue

                    if first_token:
                        meta["first_token_ms"] = int((time.perf_counter() - t0) * 1000)
                        first_token = False
                        print(f"  [vision] TTFT (first token): {meta['first_token_ms']} ms", flush=True)

                    full_text_list.append(content)
                    buffer += content

                    # Filter <think>...</think>
                    while "<think>" in buffer:
                        if "</think>" in buffer:
                            start = buffer.find("<think>")
                            end = buffer.find("</think>") + len("</think>")
                            buffer = buffer[:start] + buffer[end:]
                        else:
                            in_think = True
                            break
                    if in_think:
                        if "</think>" in buffer:
                            end = buffer.find("</think>") + len("</think>")
                            buffer = buffer[end:]
                            in_think = False
                        else:
                            continue

                    # Sentence splitter
                    while True:
                        m = re.search(r'([.!?\n]+)\s+', buffer)
                        if m:
                            end_idx = m.end()
                            sentence = buffer[:end_idx].strip()
                            buffer = buffer[end_idx:]
                            if sentence:
                                s_ms = int((time.perf_counter() - t0) * 1000)
                                print(f"  [vision] sentence at {s_ms} ms: {sentence!r}", flush=True)
                                yield sentence
                        else:
                            if len(buffer) > 80:
                                m_comma = re.search(r'([,;:—])\s+', buffer)
                                if m_comma and m_comma.end() < len(buffer):
                                    end_idx = m_comma.end()
                                    sentence = buffer[:end_idx].strip()
                                    buffer = buffer[end_idx:]
                                    if sentence:
                                        s_ms = int((time.perf_counter() - t0) * 1000)
                                        print(f"  [vision] sentence clause at {s_ms} ms: {sentence!r}", flush=True)
                                        yield sentence
                                        continue
                            break

            # Leftover buffer
            rem = buffer.strip()
            if rem:
                s_ms = int((time.perf_counter() - t0) * 1000)
                print(f"  [vision] final sentence at {s_ms} ms: {rem!r}", flush=True)
                yield rem

            meta["vision_ms"] = int((time.perf_counter() - t0) * 1000)
            meta["text"] = "".join(full_text_list).strip()
            print(f"  [vision] total Groq stream in {meta['vision_ms']} ms | reply: {meta['text']!r}", flush=True)

            final_tools = []
            for idx in sorted(tool_chunks):
                tc = tool_chunks[idx]
                name = tc["name"]
                if not name:
                    continue
                try:
                    args = json.loads(tc["args"] or "{}")
                except Exception:
                    continue
                final_tools.append({"name": name, "args": args if isinstance(args, dict) else {}})
            meta["tools"] = final_tools

    return _stream(), meta


async def describe_image(
    api_key: str,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
    preamble: tuple[tuple[str, str], ...] | None = None,
    tools: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Describes the image with Groq's vision model (accumulated result)."""
    stream, meta = await describe_image_stream(
        api_key=api_key,
        image_base64=image_base64,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout=timeout,
        preamble=preamble,
        tools=tools,
    )
    sentences = []
    async for s in stream:
        sentences.append(s)
    text = meta.get("text") or " ".join(sentences).strip()
    return text, meta.get("tools", [])


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
