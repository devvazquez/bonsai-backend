"""Texto a voz con edge-tts (voces neuronales de Microsoft, gratuitas).

Aquí sí funciona sin problemas: edge-tts necesita un WebSocket con cabeceras
propias sobre un socket TCP real, algo que Cloudflare Workers no permite pero
un entorno Python normal sí.
"""

from __future__ import annotations

from typing import AsyncIterator

import edge_tts

# Voz por defecto para cada idioma soportado.
VOICES = {
    "ca": "ca-ES-JoanaNeural",   # catalán (alternativa: ca-ES-EnricNeural)
    "es": "es-ES-ElviraNeural",  # castellano (alternativa: es-ES-AlvaroNeural)
    "en": "en-GB-SoniaNeural",
}

DEFAULT_LANG = "ca"


def voice_for(lang: str | None, voice: str | None = None) -> str:
    """Elige la voz: explícita > por idioma > por defecto."""
    if voice:
        return voice
    return VOICES.get((lang or DEFAULT_LANG).lower(), VOICES[DEFAULT_LANG])


async def stream(text: str, voice: str) -> AsyncIterator[bytes]:
    """Va entregando el MP3 a trozos, en cuanto Microsoft los manda.

    Así quien lo consume puede empezar a reproducir sin esperar a que esté
    generado el audio entero, que es donde se va la mayor parte del tiempo.
    """
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


async def synthesize(text: str, voice: str) -> bytes:
    """Genera el MP3 completo y lo devuelve en memoria (sin tocar disco)."""
    audio = b"".join([chunk async for chunk in stream(text, voice)])
    if not audio:
        raise RuntimeError("edge-tts no devolvió audio.")
    return audio


async def list_available_voices(prefix: str = "") -> list[str]:
    """Útil para descubrir voces (p. ej. prefix='ca' o 'es')."""
    voices = await edge_tts.list_voices()
    return sorted(
        v["ShortName"] for v in voices if v["Locale"].lower().startswith(prefix.lower())
    )
