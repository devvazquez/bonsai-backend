"""Transcripción de voz con Whisper turbo en Groq.

Es la primera mitad de `/ask`: las gafas mandan lo que ha dicho quien las
lleva y aquí se convierte en texto para pasárselo al modelo de visión como
pregunta.

Se usa `whisper-large-v3-turbo` porque en la capa gratuita de Groq **no gasta
la cuota de texto**: los límites de audio van por segundos transcritos y por
peticiones, no por los 200.000 tokens/día que se comen las fotos. Es decir,
probar `/ask` no te deja sin `/look`.

La API es la de OpenAI (`/openai/v1/audio/transcriptions`), así que espera un
fichero de audio de verdad, no muestras sueltas. El micro del ESP32 (un INMP441
por I2S) da PCM en crudo, así que aquí se le pone una cabecera WAV antes de
mandarlo: son 44 bytes y evita tener que codificar nada en el microcontrolador.
"""

from __future__ import annotations

import os
import re
import struct

import httpx

from vision import VisionRateLimit, get_client

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Comprueba el nombre vigente en https://console.groq.com/docs/models
MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")

# Mismo formato de error que en groq_vision: "Please try again in 16.56s".
_ESPERA_RE = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)

# Formatos que Groq acepta tal cual. Si el audio empieza por una de estas
# firmas se manda sin tocar; si no, se asume PCM en crudo del micro.
_FIRMAS = (
    (b"RIFF", "wav"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"\x1aE\xdf\xa3", "webm"),   # Matroska/WebM
    (b"ID3", "mp3"),
)


def api_key() -> str:
    return os.environ.get("GROQ_API_KEY", "")


def cabecera_wav(sample_rate: int, muestras_bytes: int, bits: int = 16) -> bytes:
    """Cabecera WAV con las longitudes de verdad.

    Aquí sí se sabe cuánto audio hay (ya ha llegado entero), al revés que en
    `piper_tts.cabecera_wav`, que responde sobre la marcha y tiene que poner
    0xFFFFFFFF. Whisper rechaza un WAV con longitudes imposibles, así que esta
    no puede reutilizar aquella.
    """
    bloque = bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + muestras_bytes) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                      sample_rate * bloque, bloque, bits)
        + b"data" + struct.pack("<I", muestras_bytes)
    )


def envolver(audio: bytes, sample_rate: int) -> tuple[bytes, str]:
    """Deja el audio listo para Groq y dice con qué extensión mandarlo.

    Si ya viene en un formato con cabecera (WAV, OGG, MP3...) se respeta; si es
    PCM en crudo, que es lo que da el I2S del ESP32, se le pone la cabecera WAV.
    """
    for firma, ext in _FIRMAS:
        if audio.startswith(firma):
            return audio, ext
    return cabecera_wav(sample_rate, len(audio)) + audio, "wav"


def duracion_pcm16(n_bytes: int, sample_rate: int) -> float:
    """Segundos que dura ese PCM16 mono. Para avisar antes de gastar cuota."""
    return n_bytes / (sample_rate * 2) if sample_rate else 0.0


def _segundos_de_espera(resp: httpx.Response) -> float | None:
    cabecera = resp.headers.get("retry-after")
    if cabecera:
        try:
            return float(cabecera)
        except ValueError:
            pass
    m = _ESPERA_RE.search(resp.text)
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
    """Devuelve lo que se ha dicho, en texto. Cadena vacía si no se oye nada."""
    clave = api_key_ or api_key()
    if not clave:
        raise RuntimeError("GROQ_API_KEY no está configurada: /ask la necesita "
                           "para transcribir, aunque la visión use Gemini.")

    cuerpo, ext = envolver(audio, sample_rate)

    datos = {"model": MODEL, "response_format": "json", "temperature": "0"}
    # Decírselo ahorra que Whisper adivine el idioma, que es de lo poco que
    # añade latencia aquí. Si no se sabe, mejor no mentirle.
    if lang:
        datos["language"] = lang

    resp = await get_client().post(
        GROQ_STT_URL,
        headers={"Authorization": f"Bearer {clave}"},
        files={"file": (f"veu.{ext}", cuerpo, f"audio/{ext}")},
        data=datos,
        timeout=timeout,
    )

    if resp.status_code == 429:
        raise VisionRateLimit(
            f"Cuota de transcripción de Groq agotada: {resp.text[:300]}",
            _segundos_de_espera(resp),
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Error de Groq al transcribir ({resp.status_code}): "
                           f"{resp.text[:300]}")

    return (resp.json().get("text") or "").strip()
