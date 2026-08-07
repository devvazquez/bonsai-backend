"""Texto a voz: capa común con dos proveedores.

- `piper_tts` (por defecto): en local, sin red. ~205 ms medidos en catalán.
- edge-tts (aquí abajo): voces neuronales de Microsoft, mejor calidad de voz
  pero 1.320 ms de mediana con texto nuevo, y con cola hasta 2 s largos.

edge-tts necesita un WebSocket con cabeceras propias sobre un socket TCP real,
algo que Cloudflare Workers no permite pero un entorno Python normal sí. Fue el
motivo de pasar el backend a Python; ahora Piper hace innecesaria esa
dependencia, pero se mantiene como alternativa por la calidad de voz.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import edge_tts

import piper_tts

# 'piper' o 'edge'. Piper por defecto: el TTS era la etapa más lenta y en local
# es 6,4 veces más rápido que edge-tts con texto nuevo.
DEFAULT_PROVIDER = os.environ.get("TTS_PROVIDER", "piper").lower()

PROVIDERS = ("piper", "edge")

# Formato que devuelve cada proveedor. Piper da WAV (PCM tal cual sale del
# modelo) y edge-tts, MP3. La respuesta de /describe lo dice en "audioFormat",
# así que el cliente no tiene que adivinarlo.
FORMATS = {"piper": "wav", "edge": "mp3"}
MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg"}

# La voz de edge-tts para cada idioma. El equivalente de piper_tts.VOICES: se
# cambia aquí y ya está. Los nombres son los de Microsoft, que no hace falta
# bajar nada.
VOICES = {
    "ca": "ca-ES-JoanaNeural",   # catalán (alternativa: ca-ES-EnricNeural)
    "es": "es-ES-ElviraNeural",  # castellano (alternativa: es-ES-AlvaroNeural)
    "en": "en-GB-SoniaNeural",
}

DEFAULT_LANG = "ca"

# Si Piper no está disponible (falta el modelo o la librería), se usa edge-tts
# para no dejar las gafas mudas. Se anota aquí y /health lo dice: un apaño
# silencioso es peor que uno visible.
_piper_fallo: str | None = None


def resolve(provider: str | None) -> str:
    elegido = (provider or DEFAULT_PROVIDER).lower()
    if elegido not in PROVIDERS:
        raise ValueError(
            f"Proveedor de TTS desconocido: {elegido!r}. Usa uno de: {', '.join(PROVIDERS)}"
        )
    return elegido


def effective_provider(provider: str | None = None) -> str:
    """El que se va a usar de verdad, ya contando el apaño si Piper falló."""
    elegido = resolve(provider)
    if elegido == "piper" and _piper_fallo:
        return "edge"
    return elegido


def piper_status() -> dict[str, object]:
    return {
        "ok": _piper_fallo is None,
        "error": _piper_fallo,
        "voicesDir": piper_tts.VOICES_DIR,
        # Qué voz le toca a cada idioma y si ya está en disco. Las que no lo
        # estén se bajan solas la primera vez que se pidan.
        "voices": {
            lang: {"voice": voz, "downloaded": piper_tts.is_available(voz)}
            for lang, voz in sorted(piper_tts.VOICES.items())
        },
    }


def format_for(provider: str | None = None) -> str:
    return FORMATS[effective_provider(provider)]


def media_type_for(provider: str | None = None) -> str:
    return MEDIA_TYPES[format_for(provider)]


def idiomas() -> list[str]:
    """Los idiomas que se pueden pedir. Los dos proveedores tienen los mismos."""
    return sorted(set(VOICES) | set(piper_tts.VOICES))


def voice_for(lang: str | None, provider: str | None = None) -> str:
    """La voz del idioma, según quién vaya a sintetizar.

    La voz no se elige por petición: se pide un idioma y cada proveedor tiene
    la suya definida en su mapa ('ca-ES-JoanaNeural' en edge-tts,
    'ca_ES-upc_ona-medium' en Piper).
    """
    if effective_provider(provider) == "piper":
        return piper_tts.voice_for(lang)
    return VOICES.get((lang or DEFAULT_LANG).lower(), VOICES[DEFAULT_LANG])


async def ensure_voice(voice: str, provider: str | None = None) -> None:
    """Con Piper, baja el modelo si no está. Con edge-tts no hay nada que bajar."""
    if effective_provider(provider) == "piper":
        await piper_tts.ensure_voice(voice)


async def _edge_stream(text: str, voice: str) -> AsyncIterator[bytes]:
    """Va entregando el MP3 a trozos, en cuanto Microsoft los manda.

    Así quien lo consume puede empezar a reproducir sin esperar a que esté
    generado el audio entero, que es donde se va la mayor parte del tiempo.
    """
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


async def stream(text: str, voice: str, provider: str | None = None) -> AsyncIterator[bytes]:
    if effective_provider(provider) == "piper":
        async for trozo in piper_tts.stream(text, voice):
            yield trozo
        return
    async for trozo in _edge_stream(text, voice):
        yield trozo


async def synthesize(text: str, voice: str, provider: str | None = None) -> bytes:
    """Genera el audio completo y lo devuelve en memoria (sin tocar disco)."""
    if effective_provider(provider) == "piper":
        return await piper_tts.synthesize(text, voice)
    audio = b"".join([chunk async for chunk in _edge_stream(text, voice)])
    if not audio:
        raise RuntimeError("edge-tts no devolvió audio.")
    return audio


async def warmup() -> bool:
    """Prepara Piper al arrancar: baja el modelo si falta y lo carga.

    Son ~63 MB una sola vez y ~1,2 s de carga, que así no los paga la primera
    foto. Si algo falla se apunta el motivo y se sigue con edge-tts.
    """
    global _piper_fallo
    if DEFAULT_PROVIDER != "piper":
        return True

    voz = piper_tts.VOICES[piper_tts.DEFAULT_LANG]
    try:
        await piper_tts.ensure_voice(voz)
        import asyncio

        await asyncio.to_thread(piper_tts._voz_cargada, voz)
        _piper_fallo = None
        return True
    except Exception as e:
        _piper_fallo = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return False
