"""Speech transcription with Whisper turbo on Groq.

First half of `/ask`: the glasses send what the wearer said and here it becomes
the text question handed to the vision model.

`whisper-large-v3-turbo` is used because on Groq's free tier it does **not**
spend the text quota — audio limits are per transcribed second, not the
200,000 tokens/day the photos eat. Testing `/ask` never breaks `/look`.

The API is OpenAI's (`/openai/v1/audio/transcriptions`), so it expects a real
audio file, not loose samples. The XIAO ESP32-S3 Sense mic is a PDM one read
through I2S, which yields raw PCM16; hence the 44-byte WAV header added here
instead of encoding anything on the microcontroller.
"""

from __future__ import annotations

import os
import re
import struct

import httpx

from .vision import VisionRateLimit, get_client

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Check the current name at https://console.groq.com/docs/models
MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# Same error shape as groq_vision: "Please try again in 16.56s".
_WAIT_RE = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)

# Formats Groq takes as-is. Audio starting with one of these signatures is
# sent untouched; anything else is assumed to be raw PCM from the mic.
_SIGNATURES = (
    (b"RIFF", "wav"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"\x1aE\xdf\xa3", "webm"),   # Matroska/WebM
    (b"ID3", "mp3"),
)

# m4a (and mp4) does not start with its signature: the first 4 bytes are the
# box size and "ftyp" follows. Without this an iPhone recording ended up
# wrapped in a WAV header and Whisper got garbage.
_FTYP = slice(4, 8)

# An MP3 without an ID3 tag starts with the frame sync (0xFF Ex). Deliberately
# not detected: those two bytes show up often in raw PCM and a false positive
# is worse than requiring a header.


def api_key() -> str:
    return os.environ.get("GROQ_API_KEY", "")


def wav_header(sample_rate: int, sample_bytes: int, bits: int = 16) -> bytes:
    """WAV header with the real lengths.

    Here the audio has already arrived whole, unlike `tts.wav_header`, which
    streams and must write 0xFFFFFFFF. Whisper rejects impossible lengths, so
    this one cannot reuse that one.
    """
    block = bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + sample_bytes) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                      sample_rate * block, block, bits)
        + b"data" + struct.pack("<I", sample_bytes)
    )


def wrap(audio: bytes, sample_rate: int) -> tuple[bytes, str, float | None]:
    """Gets the audio ready for Groq: (body, extension, seconds).

    Anything already carrying a header (WAV, OGG, m4a...) is passed through;
    raw PCM, which is what the ESP32's I2S gives, gets a WAV header.

    Seconds are only known for PCM, where it is a division: a compressed format
    would need the bitrate and a decode just to show a number. Hence the None.
    """
    for signature, ext in _SIGNATURES:
        if audio.startswith(signature):
            return audio, ext, None
    if audio[_FTYP] == b"ftyp":
        return audio, "m4a", None
    return (wav_header(sample_rate, len(audio)) + audio, "wav",
            pcm16_duration(len(audio), sample_rate))


def pcm16_duration(n_bytes: int, sample_rate: int) -> float:
    """Seconds that mono PCM16 lasts. To warn before spending quota."""
    return n_bytes / (sample_rate * 2) if sample_rate else 0.0


def _seconds_to_wait(resp: httpx.Response) -> float | None:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _WAIT_RE.search(resp.text)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return None


async def transcribe(
    audio: bytes,
    sample_rate: int = 16000,
    lang: str | None = "ca",
    api_key_: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Returns what was said, as text. Empty string if nothing is heard."""
    key = api_key_ or api_key()
    if not key:
        raise RuntimeError("GROQ_API_KEY is not configured: /ask needs it "
                           "to transcribe.")

    body, ext, _ = wrap(audio, sample_rate)

    data = {"model": MODEL, "response_format": "json", "temperature": "0"}
    # Telling it the language saves Whisper from guessing, which is about the
    # only thing that adds latency here. If unknown, better not to lie.
    if lang:
        data["language"] = lang

    resp = await get_client().post(
        GROQ_STT_URL,
        headers={"Authorization": f"Bearer {key}"},
        files={"file": (f"voice.{ext}", body, f"audio/{ext}")},
        data=data,
        timeout=timeout,
    )

    if resp.status_code == 429:
        raise VisionRateLimit(
            f"Groq transcription quota exhausted: {resp.text[:300]}",
            _seconds_to_wait(resp),
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Groq error while transcribing ({resp.status_code}): "
                           f"{resp.text[:300]}")

    return (resp.json().get("text") or "").strip()
