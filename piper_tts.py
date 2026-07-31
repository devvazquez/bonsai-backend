"""Texto a voz con Piper, en local y sin red.

Es el proveedor de TTS por defecto porque el TTS era la etapa más lenta de
todas. Medido con la misma frase catalana:

    upc_ona medium (Piper)   205 ms de mediana (182-422)
    edge-tts, texto nuevo  1.320 ms de mediana (949-2.089)

Son 6,4 veces, y sobre todo es **predecible**: no hay red, ni cola larga, ni un
servicio ajeno del que depender. Ojo al comparar con edge-tts: sus cifras
buenas suelen venir de la caché de Microsoft (ver README).

Las voces catalanas son de la UPC, publicadas en el repositorio de Piper.
"""

from __future__ import annotations

import asyncio
import io
import os
import wave
from typing import AsyncIterator

VOICES_DIR = os.environ.get(
    "PIPER_VOICES_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "voices"),
)

# Voz por defecto para cada idioma. En catalán, upc_ona medium: es la elegida
# tras escuchar las muestras. upc_pau x_low es más rápida (122 ms) pero a
# 16 kHz y suena peor.
VOICES = {
    "ca": "ca_ES-upc_ona-medium",
    "es": "es_ES-davefx-medium",
    "en": "en_GB-alba-medium",
}

DEFAULT_LANG = "ca"

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Dónde vive cada voz dentro del repositorio de Hugging Face. Hace falta porque
# la ruta no se deduce del nombre: el idioma va dos veces y la calidad aparte.
_RUTAS = {
    "ca_ES-upc_ona-medium": "ca/ca_ES/upc_ona/medium",
    "ca_ES-upc_pau-x_low": "ca/ca_ES/upc_pau/x_low",
    "es_ES-davefx-medium": "es/es_ES/davefx/medium",
    "en_GB-alba-medium": "en/en_GB/alba/medium",
}

# Cargar el modelo cuesta ~1,2 s, así que se hace una vez y se guarda. La
# síntesis en sí son ~205 ms.
_cargadas: dict[str, object] = {}


class PiperNoDisponible(RuntimeError):
    """Falta el modelo o la librería: hay que caer a otro proveedor."""


def voice_for(lang: str | None, voice: str | None = None) -> str:
    if voice:
        return voice
    return VOICES.get((lang or DEFAULT_LANG).lower(), VOICES[DEFAULT_LANG])


def voice_path(voice: str) -> str:
    return os.path.join(VOICES_DIR, f"{voice}.onnx")


def is_available(voice: str) -> bool:
    return os.path.isfile(voice_path(voice))


def local_voices() -> list[str]:
    if not os.path.isdir(VOICES_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx")
    )


async def download_voice(voice: str, timeout: float = 300.0) -> str:
    """Baja el modelo de Hugging Face si no está ya en disco.

    Son 63 MB para upc_ona medium y se hace una sola vez. Se llama al arrancar,
    así que quien lleve las gafas no espera nunca por esto.
    """
    ruta = _RUTAS.get(voice)
    if ruta is None:
        raise PiperNoDisponible(
            f"No sé de dónde bajar la voz {voice!r}. Déjala a mano en "
            f"{VOICES_DIR} o añádela a piper_tts._RUTAS."
        )

    import vision  # reutiliza el cliente HTTP compartido

    os.makedirs(VOICES_DIR, exist_ok=True)
    cliente = vision.get_client()
    for ext in ("onnx", "onnx.json"):
        destino = os.path.join(VOICES_DIR, f"{voice}.{ext}")
        if os.path.isfile(destino):
            continue
        url = f"{BASE_URL}/{ruta}/{voice}.{ext}"
        r = await cliente.get(url, timeout=timeout, follow_redirects=True)
        if r.status_code >= 400:
            raise PiperNoDisponible(
                f"No se pudo bajar {url} (HTTP {r.status_code})."
            )
        # A un fichero temporal primero: si se corta la descarga, no queda un
        # .onnx a medias que luego falle al cargar.
        tmp = destino + ".parcial"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, destino)
    return voice_path(voice)


def _voz_cargada(voice: str):
    if voice in _cargadas:
        return _cargadas[voice]

    ruta = voice_path(voice)
    if not os.path.isfile(ruta):
        raise PiperNoDisponible(
            f"Falta el modelo {ruta}. Bájalo con: python descargar_voces.py"
        )
    try:
        from piper import PiperVoice
    except ImportError as e:
        raise PiperNoDisponible(
            "Falta la librería piper-tts (pip install piper-tts)."
        ) from e

    _cargadas[voice] = PiperVoice.load(ruta)
    return _cargadas[voice]


def _sintetizar_wav(text: str, voice: str) -> bytes:
    """Devuelve un WAV completo. Se ejecuta fuera del bucle de eventos."""
    voz = _voz_cargada(voice)
    trozos = list(voz.synthesize(text))
    if not trozos:
        raise RuntimeError("Piper no devolvió audio.")
    pcm = b"".join(c.audio_int16_bytes for c in trozos)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(trozos[0].sample_channels)
        w.setsampwidth(trozos[0].sample_width)
        w.setframerate(trozos[0].sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


async def synthesize(text: str, voice: str) -> bytes:
    """WAV completo en memoria.

    Piper es síncrono y consume CPU, así que va a un hilo: si no, bloquearía
    el bucle de eventos y las peticiones en paralelo se pondrían en cola.
    """
    return await asyncio.to_thread(_sintetizar_wav, text, voice)


async def stream(text: str, voice: str) -> AsyncIterator[bytes]:
    """Mismo interfaz que edge-tts, pero de un solo trozo.

    Trocearlo no aportaría nada: la síntesis entera son ~205 ms, menos de lo
    que tarda edge-tts en entregar el primer trozo.
    """
    yield await synthesize(text, voice)


async def warmup(voice: str | None = None) -> bool:
    """Baja el modelo si falta y lo carga, al arrancar el servidor.

    Así el ~1,2 s de carga no lo paga la primera foto.
    """
    v = voice or VOICES[DEFAULT_LANG]
    try:
        if not is_available(v):
            await download_voice(v)
        await asyncio.to_thread(_voz_cargada, v)
        return True
    except Exception:
        return False
