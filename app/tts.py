"""Text to speech with Piper, local and offline.

It is the only TTS provider. edge-tts (Microsoft voices) was dropped: on the
same Catalan sentence Piper is 205 ms median (182-422) against 1,320 ms
(949-2,089), and above all it is predictable — no network, no long tail.

The Catalan voices are the UPC ones published in the Piper repository.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import wave
from typing import AsyncIterator, Iterator

VOICES_DIR = os.environ.get(
    "PIPER_VOICES_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices"),
)

# ==========================================================================
# The voice of each language. THIS is what you edit to change a voice.
# ==========================================================================
# Names come from the Piper repository (rhasspy/piper-voices) and follow the
# `<locale>-<speaker>-<quality>` pattern; the download path is derived from the
# name (see `_hf_path`), so writing another name here is the whole change.
#
# Catalan only has three voices, checked by hand against the repository:
# upc_ona-medium (this one), upc_ona-x_low and upc_pau-x_low (male, faster:
# 145 ms vs 215, but 16 kHz and you can hear it).
VOICES = {
    "ca": "ca_ES-upc_ona-medium",
    "es": "es_ES-davefx-medium",
    "en": "en_GB-alba-medium",
}

DEFAULT_LANG = "ca"

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Loading the model costs ~1.2 s, so it is done once and kept. The synthesis
# itself is ~205 ms.
_loaded: dict[str, object] = {}


class PiperUnavailable(RuntimeError):
    """Missing model or library: without this the glasses go mute."""


# If Piper fails to start it is noted here and /health says so: a silent
# failure is worse than a visible one.
_failure: str | None = None


def languages() -> list[str]:
    """The languages defined above."""
    return sorted(VOICES)


def voice_for(lang: str | None) -> str:
    """The voice for a language. An undefined language falls back to the default."""
    return VOICES.get((lang or DEFAULT_LANG).lower(), VOICES[DEFAULT_LANG])


def voice_path(voice: str) -> str:
    return os.path.join(VOICES_DIR, f"{voice}.onnx")


def is_available(voice: str) -> bool:
    return os.path.isfile(voice_path(voice))


def local_voices() -> list[str]:
    """The ones already downloaded. Only so /health can report them."""
    if not os.path.isdir(VOICES_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx")
    )


def _hf_path(voice: str) -> str:
    """Where the voice lives in the repository, derived from its name.

    `ca_ES-upc_ona-medium` -> `ca/ca_ES/upc_ona/medium`. Everything comes from
    the name itself, so there is no path table to keep in sync with VOICES.
    The speaker may contain dashes, hence first/last partition, not split.
    """
    first, _, rest = voice.partition("-")
    speaker, _, quality = rest.rpartition("-")
    if not first or not speaker or not quality:
        raise PiperUnavailable(
            f"The name {voice!r} is not in the <locale>-<speaker>-<quality> form "
            "used by the Piper repository, so there is no way to know where to "
            f"download it from. Drop it by hand in {VOICES_DIR}."
        )
    language = first.split("_")[0]
    return f"{language}/{first}/{speaker}/{quality}"


# One download per voice: two simultaneous requests for the same missing
# language must not fetch 63 MB twice in parallel.
_locks: dict[str, asyncio.Lock] = {}


async def ensure_voice(voice: str, timeout: float = 300.0) -> str:
    """Makes sure the voice is on disk, downloading it if needed.

    ~63 MB, once per voice. The default one is fetched at startup
    (`tts.warmup`), so this only shows up on a brand new language.
    """
    if is_available(voice):
        return voice_path(voice)

    lock = _locks.setdefault(voice, asyncio.Lock())
    async with lock:
        # Another request may have downloaded it while waiting for the lock.
        if is_available(voice):
            return voice_path(voice)

        path = _hf_path(voice)

        from . import vision  # reuses the shared HTTP client

        os.makedirs(VOICES_DIR, exist_ok=True)
        client = vision.get_client()
        for ext in ("onnx", "onnx.json"):
            target = os.path.join(VOICES_DIR, f"{voice}.{ext}")
            if os.path.isfile(target):
                continue
            url = f"{BASE_URL}/{path}/{voice}.{ext}"
            r = await client.get(url, timeout=timeout, follow_redirects=True)
            if r.status_code >= 400:
                raise PiperUnavailable(
                    f"Could not download {url} (HTTP {r.status_code}). A "
                    "misspelled voice makes the repository return 404: check "
                    "the name in tts.VOICES."
                )
            # The model is tens of MB and the .json a few KB: anything tiny is
            # not the file we asked for, and saving it gives a cryptic
            # KeyError at load time.
            if len(r.content) < 1024:
                raise PiperUnavailable(
                    f"{url} returned only {len(r.content)} bytes, which is not a "
                    f"real {ext}: {r.content[:120]!r}"
                )
            # Temporary file first: an interrupted download must not leave a
            # half-written .onnx that fails to load later.
            tmp = target + ".partial"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, target)
        return voice_path(voice)


def _loaded_voice(voice: str):
    if voice in _loaded:
        return _loaded[voice]

    path = voice_path(voice)
    if not os.path.isfile(path):
        # Should not happen: this is called after ensure_voice(), which fetches it.
        raise PiperUnavailable(
            f"Model {path} is missing and was not downloaded before synthesizing."
        )
    try:
        from piper import PiperVoice
    except ImportError as e:
        raise PiperUnavailable(
            "The piper-tts library is missing (pip install piper-tts)."
        ) from e

    _loaded[voice] = PiperVoice.load(path)
    return _loaded[voice]


def _synthesize_wav(text: str, voice: str) -> bytes:
    """Returns a complete WAV. Runs outside the event loop."""
    v = _loaded_voice(voice)
    chunks = list(v.synthesize(text))
    if not chunks:
        raise RuntimeError("Piper returned no audio.")
    pcm = b"".join(c.audio_int16_bytes for c in chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(chunks[0].sample_channels)
        w.setsampwidth(chunks[0].sample_width)
        w.setframerate(chunks[0].sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def synthesize(text: str, voice: str) -> bytes:
    """Complete WAV in memory.

    Piper is synchronous and CPU bound, so it goes to a thread: otherwise it
    would block the event loop and queue up concurrent requests.
    """
    return await asyncio.to_thread(_synthesize_wav, text, voice)


async def stream(text: str, voice: str) -> AsyncIterator[bytes]:
    """A single chunk: splitting would gain nothing, synthesis is ~205 ms."""
    yield await synthesize(text, voice)


# --------------------------------------------------------------------------
# Raw audio for the ESP32
# --------------------------------------------------------------------------
# The MAX98357A is an I2S amplifier: it wants signed 16-bit PCM samples.
# Anything else (WAV header, MP3) makes the microcontroller work before sounding.
FORMATS = ("pcm16", "mulaw", "wav")

# 22,050 Hz is what the model outputs, so nothing is resampled. 16,000 cuts
# bandwidth by 27 % and is barely audible for speech. The MAX98357A takes
# 8 to 96 kHz.
SAMPLE_RATES = (8000, 16000, 22050)


def _resample(samples, source: int, target: int):
    """Linear resampling. Plenty for speech, and microseconds with numpy."""
    import numpy as np

    if source == target:
        return samples
    n = int(len(samples) * target / source)
    if n <= 0:
        return samples[:0]
    x = np.linspace(0, len(samples) - 1, n)
    return np.interp(x, np.arange(len(samples)), samples).astype(np.int16)


def _to_mulaw(samples):
    """PCM16 -> 8-bit mu-law (G.711).

    Decoded on the ESP32 with a 256-entry table, no library and barely any
    CPU, at half the size. Useful when the WiFi is tight.
    """
    import numpy as np

    BIAS, CLIP = 0x84, 32635
    x = np.clip(samples.astype(np.int32), -CLIP, CLIP)
    sign = (x < 0).astype(np.uint8) * 0x80
    x = np.abs(x) + BIAS
    exponent = np.zeros_like(x, dtype=np.uint8)
    for e in range(7, 0, -1):
        exponent = np.where((exponent == 0) & (x >= (1 << (e + 7))), e, exponent)
    mantissa = ((x >> (exponent.astype(np.int32) + 3)) & 0x0F).astype(np.uint8)
    return (~(sign | (exponent << 4) | mantissa)).astype(np.uint8).tobytes()


def convert(chunk, fmt: str, rate: int | None) -> tuple[bytes, int]:
    """Turns a Piper chunk into the requested format. Returns (bytes, sample_rate)."""
    import numpy as np

    samples = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
    target = rate or chunk.sample_rate
    samples = _resample(samples, chunk.sample_rate, target)
    if fmt == "mulaw":
        return _to_mulaw(samples), target
    return samples.astype("<i2").tobytes(), target


def wav_header(
    sample_rate: int, bits: int = 16, data_bytes: int | None = None
) -> bytes:
    """WAV header. With `data_bytes`, carrying the real lengths.

    Without `data_bytes` the lengths are 0xFFFFFFFF, the only option when
    streaming before knowing the size. The ESP32 does not care, but a player
    cannot compute the duration and shows 0:00 without sounding — so pass
    `data_bytes` whenever the audio is already complete in memory (`/speak`
    with `wav` does).
    """
    import struct

    block = bits // 8
    riff = 0xFFFFFFFF if data_bytes is None else 36 + data_bytes
    data = 0xFFFFFFFF if data_bytes is None else data_bytes
    return (
        b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * block, block, bits)
        + b"data" + struct.pack("<I", data)
    )


def _sync_chunks(text: str, voice: str) -> Iterator:
    return _loaded_voice(voice).synthesize(text)


async def stream_raw(
    text: str, voice: str, fmt: str = "pcm16", rate: int | None = None
) -> AsyncIterator[bytes]:
    """Emits audio as Piper produces it.

    Piper yields one chunk per sentence, so the ESP32 can start playing the
    first while the second is synthesized; from then on the download outruns
    playback and stops counting. Runs in a thread because Piper is synchronous.
    """
    if fmt not in FORMATS:
        raise ValueError(
            f"Unknown audio format: {fmt!r}. Use one of: {', '.join(FORMATS)}"
        )

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def producer() -> None:
        try:
            for chunk in _sync_chunks(text, voice):
                data, _ = convert(chunk, fmt, rate)
                loop.call_soon_threadsafe(queue.put_nowait, data)
        except Exception as e:  # re-raised on the async side
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=producer, daemon=True).start()

    while True:
        item = await queue.get()
        if item is None:
            return
        if isinstance(item, Exception):
            raise item
        yield item


def sample_rate_of(voice: str, rate: int | None = None) -> int:
    if rate:
        return rate
    with open(voice_path(voice) + ".json", encoding="utf-8") as f:
        import json

        return int(json.load(f).get("audio", {}).get("sample_rate", 22050))


def status() -> dict[str, object]:
    """What /health reports about Piper: whether it started and each language's voice."""
    return {
        "ok": _failure is None,
        "error": _failure,
        "voicesDir": VOICES_DIR,
        # Missing ones are downloaded on first use.
        "voices": {
            lang: {"voice": voice, "downloaded": is_available(voice)}
            for lang, voice in sorted(VOICES.items())
        },
    }


async def warmup(voice: str | None = None) -> bool:
    """Downloads the model if missing and loads it, at server startup.

    That way the ~1.2 s load is not paid by the first photo. On failure the
    reason is stored in `_failure`, which is what /health shows.
    """
    global _failure
    v = voice or VOICES[DEFAULT_LANG]
    try:
        await ensure_voice(v)
        await asyncio.to_thread(_loaded_voice, v)
        _failure = None
        return True
    except Exception as e:
        _failure = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return False
