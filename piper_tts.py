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
import threading
import wave
from typing import AsyncIterator, Iterator

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
# En catalán solo hay estas tres en el repositorio (comprobado: ni "low" ni
# "high" de upc_ona, ni una medium de upc_pau; los tres dan 404).
_RUTAS = {
    "ca_ES-upc_ona-medium": "ca/ca_ES/upc_ona/medium",
    "ca_ES-upc_ona-x_low": "ca/ca_ES/upc_ona/x_low",
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


def catalogo() -> list[str]:
    """Las voces que sabemos de dónde bajar, estén en disco o no.

    Sirve para poder decirle a quien pide una voz que no está si es que le
    falta bajarla o es que se ha equivocado escribiéndola.
    """
    return sorted(_RUTAS)


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


# --------------------------------------------------------------------------
# Audio en crudo para el ESP32
# --------------------------------------------------------------------------
# El MAX98357A es un amplificador I2S: lo que quiere son muestras PCM de 16
# bits con signo. Cualquier otra cosa (WAV con cabecera, MP3) obliga al
# microcontrolador a trabajar de más antes de poder sonar.
FORMATOS = ("pcm16", "mulaw", "wav")

# 22.050 Hz es lo que saca el modelo, así que no hay que remuestrear nada.
# 16.000 baja el ancho de banda un 27 % y para voz se nota poco. El
# MAX98357A acepta de 8 a 96 kHz.
SAMPLE_RATES = (8000, 16000, 22050)


def _remuestrear(muestras, origen: int, destino: int):
    """Remuestreo lineal. De sobra para voz, y son microsegundos con numpy."""
    import numpy as np

    if origen == destino:
        return muestras
    n = int(len(muestras) * destino / origen)
    if n <= 0:
        return muestras[:0]
    x = np.linspace(0, len(muestras) - 1, n)
    return np.interp(x, np.arange(len(muestras)), muestras).astype(np.int16)


def _a_mulaw(muestras):
    """PCM16 -> mu-law de 8 bits (G.711).

    Se descodifica en el ESP32 con una tabla de 256 entradas, sin librería ni
    apenas CPU, y ocupa la mitad. Útil si el WiFi va justo.
    """
    import numpy as np

    BIAS, CLIP = 0x84, 32635
    x = np.clip(muestras.astype(np.int32), -CLIP, CLIP)
    signo = (x < 0).astype(np.uint8) * 0x80
    x = np.abs(x) + BIAS
    exponente = np.zeros_like(x, dtype=np.uint8)
    for e in range(7, 0, -1):
        exponente = np.where((exponente == 0) & (x >= (1 << (e + 7))), e, exponente)
    mantisa = ((x >> (exponente.astype(np.int32) + 3)) & 0x0F).astype(np.uint8)
    return (~(signo | (exponente << 4) | mantisa)).astype(np.uint8).tobytes()


def convertir(chunk, formato: str, rate: int | None) -> tuple[bytes, int]:
    """Pasa un trozo de Piper al formato pedido. Devuelve (bytes, sample_rate)."""
    import numpy as np

    muestras = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
    destino = rate or chunk.sample_rate
    muestras = _remuestrear(muestras, chunk.sample_rate, destino)
    if formato == "mulaw":
        return _a_mulaw(muestras), destino
    return muestras.astype("<i2").tobytes(), destino


def cabecera_wav(sample_rate: int, bits: int = 16) -> bytes:
    """Cabecera WAV de tamaño desconocido, para poder ir enviando sobre la marcha.

    Se pone 0xFFFFFFFF en las longitudes porque al empezar a responder todavía
    no sabemos cuánto audio habrá. Los reproductores en streaming lo aceptan;
    el ESP32 ni la necesita.
    """
    import struct

    bloque = bits // 8
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * bloque, bloque, bits)
        + b"data" + struct.pack("<I", 0xFFFFFFFF)
    )


def _trozos_sincronos(text: str, voice: str) -> Iterator:
    return _voz_cargada(voice).synthesize(text)


async def stream_raw(
    text: str, voice: str, formato: str = "pcm16", rate: int | None = None
) -> AsyncIterator[bytes]:
    """Va soltando el audio a medida que Piper lo genera.

    Piper entrega un trozo por frase, así que con una respuesta de dos frases
    el ESP32 puede empezar a sonar con la primera mientras se sintetiza la
    segunda. Y como el audio se transmite más rápido que se escucha, a partir
    del primer trozo la descarga deja de contar: se solapa con la reproducción.

    Va en un hilo porque Piper es síncrono y bloquearía el bucle de eventos.
    """
    if formato not in FORMATOS:
        raise ValueError(
            f"Formato de audio desconocido: {formato!r}. Usa uno de: {', '.join(FORMATOS)}"
        )

    cola: asyncio.Queue = asyncio.Queue()
    bucle = asyncio.get_running_loop()

    def productor() -> None:
        try:
            for chunk in _trozos_sincronos(text, voice):
                datos, _ = convertir(chunk, formato, rate)
                bucle.call_soon_threadsafe(cola.put_nowait, datos)
        except Exception as e:  # se re-lanza en el lado async
            bucle.call_soon_threadsafe(cola.put_nowait, e)
        finally:
            bucle.call_soon_threadsafe(cola.put_nowait, None)

    threading.Thread(target=productor, daemon=True).start()

    while True:
        item = await cola.get()
        if item is None:
            return
        if isinstance(item, Exception):
            raise item
        yield item


def sample_rate_de(voice: str, rate: int | None = None) -> int:
    if rate:
        return rate
    with open(voice_path(voice) + ".json", encoding="utf-8") as f:
        import json

        return int(json.load(f).get("audio", {}).get("sample_rate", 22050))


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
