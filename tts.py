"""Texto a voz con Piper, en local y sin red.

Es el único proveedor de TTS. Hubo edge-tts (voces de Microsoft) y se quitó:
medido con la misma frase catalana, Piper son 205 ms de mediana (182-422)
frente a 1.320 ms (949-2.089) de edge-tts con texto nuevo. Son 6,4 veces, y
sobre todo es **predecible**: no hay red, ni cola larga, ni un servicio ajeno
del que depender.

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

# ==========================================================================
# La voz de cada idioma. ESTO es lo que se toca para cambiar una voz.
# ==========================================================================
# Una entrada por idioma y nada más: quien llama a la API pide un idioma
# (`lang`) y aquí se decide con qué voz se le contesta. Si la voz que se pone
# no está en disco, se baja sola la primera vez que hace falta.
#
# Los nombres son los del repositorio de Piper (rhasspy/piper-voices) y siguen
# el patrón `<locale>-<locutor>-<calidad>`. Para cambiar de voz basta con
# escribir otro nombre de ahí: la ruta de descarga se deduce (ver `_ruta_hf`).
#
# En catalán solo hay tres voces, comprobado a mano contra el repositorio:
# upc_ona-medium (la de aquí), upc_ona-x_low y upc_pau-x_low (masculina, más
# rápida: 145 ms frente a 215, pero a 16 kHz y se nota).
VOICES = {
    "ca": "ca_ES-upc_ona-medium",
    "es": "es_ES-davefx-medium",
    "en": "en_GB-alba-medium",
}

DEFAULT_LANG = "ca"

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Cargar el modelo cuesta ~1,2 s, así que se hace una vez y se guarda. La
# síntesis en sí son ~205 ms.
_cargadas: dict[str, object] = {}


class PiperNoDisponible(RuntimeError):
    """Falta el modelo o la librería: sin esto las gafas se quedan mudas."""


# Si Piper falla al arrancar se anota aquí y /health lo dice: un fallo
# silencioso es peor que uno visible.
_fallo: str | None = None


def idiomas() -> list[str]:
    """Los idiomas que hay definidos arriba."""
    return sorted(VOICES)


def voice_for(lang: str | None) -> str:
    """La voz del idioma. Un idioma que no esté definido cae al de por defecto."""
    return VOICES.get((lang or DEFAULT_LANG).lower(), VOICES[DEFAULT_LANG])


def voice_path(voice: str) -> str:
    return os.path.join(VOICES_DIR, f"{voice}.onnx")


def is_available(voice: str) -> bool:
    return os.path.isfile(voice_path(voice))


def local_voices() -> list[str]:
    """Las que ya están bajadas. Solo para que /health lo pueda decir."""
    if not os.path.isdir(VOICES_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(VOICES_DIR) if f.endswith(".onnx")
    )


def _ruta_hf(voice: str) -> str:
    """Dónde vive la voz dentro del repositorio, deducido de su nombre.

    `ca_ES-upc_ona-medium` -> `ca/ca_ES/upc_ona/medium`. El idioma aparece dos
    veces (suelto y con región) y la calidad va aparte, pero todo sale del
    propio nombre, así que no hay que mantener una tabla de rutas al lado del
    mapa de idiomas: cambiar la voz de un idioma es escribir otro nombre.

    El locutor puede llevar guiones (`en_US-libritts_r-medium` no, pero alguno
    sí), así que se parte por el primer y el último guion, no por todos.
    """
    primero, _, resto = voice.partition("-")
    locutor, _, calidad = resto.rpartition("-")
    if not primero or not locutor or not calidad:
        raise PiperNoDisponible(
            f"El nombre {voice!r} no tiene la forma <locale>-<locutor>-<calidad> "
            "que usa el repositorio de Piper, así que no sé de dónde bajarlo. "
            f"Déjalo a mano en {VOICES_DIR}."
        )
    idioma = primero.split("_")[0]
    return f"{idioma}/{primero}/{locutor}/{calidad}"


# Una descarga por voz: si llegan dos peticiones a la vez del mismo idioma que
# todavía no está bajado, la segunda espera a la primera en vez de bajar otros
# 63 MB en paralelo.
_candados: dict[str, asyncio.Lock] = {}


async def ensure_voice(voice: str, timeout: float = 300.0) -> str:
    """Se asegura de que la voz esté en disco, bajándola si hace falta.

    Son ~63 MB y se hace una sola vez por voz. La de por defecto se baja al
    arrancar (`tts.warmup`), así que esto solo se nota la primera vez que se
    pide un idioma nuevo.
    """
    if is_available(voice):
        return voice_path(voice)

    candado = _candados.setdefault(voice, asyncio.Lock())
    async with candado:
        # Otra petición puede haberla bajado mientras se esperaba el candado.
        if is_available(voice):
            return voice_path(voice)

        ruta = _ruta_hf(voice)

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
                    f"No se pudo bajar {url} (HTTP {r.status_code}). Si la voz "
                    "está mal escrita, el repositorio devuelve 404: mira el "
                    "nombre en tts.VOICES."
                )
            # El modelo son decenas de MB y el .json unos cuantos KB: cualquier
            # cosa diminuta es una respuesta que no es la que se pedía, y
            # guardarla da un KeyError críptico al cargarla.
            if len(r.content) < 1024:
                raise PiperNoDisponible(
                    f"{url} devolvió solo {len(r.content)} bytes, que no es un "
                    f"{ext} de verdad: {r.content[:120]!r}"
                )
            # A un fichero temporal primero: si se corta la descarga, no queda
            # un .onnx a medias que luego falle al cargar.
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
        # No debería pasar: se llama después de ensure_voice(), que la baja.
        raise PiperNoDisponible(
            f"Falta el modelo {ruta} y no se ha bajado antes de sintetizar."
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
    """Un solo trozo: trocearlo no aportaría nada, la síntesis son ~205 ms."""
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


def cabecera_wav(
    sample_rate: int, bits: int = 16, datos_bytes: int | None = None
) -> bytes:
    """Cabecera WAV. Con `datos_bytes`, con las longitudes de verdad.

    Sin `datos_bytes` se ponen 0xFFFFFFFF, que es lo único que se puede hacer
    si se empieza a responder antes de saber cuánto audio habrá. Al ESP32 le da
    igual (lee las muestras y punto), pero **un reproductor no**: no puede
    calcular la duración, así que enseña 0:00 y no suena nada. Un `<audio>` del
    navegador o el propio /docs se quedan así.

    O sea que si el audio ya está entero en memoria, hay que pasar
    `datos_bytes`. Es lo que hace `/speak` con `wav`.
    """
    import struct

    bloque = bits // 8
    riff = 0xFFFFFFFF if datos_bytes is None else 36 + datos_bytes
    datos = 0xFFFFFFFF if datos_bytes is None else datos_bytes
    return (
        b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * bloque, bloque, bits)
        + b"data" + struct.pack("<I", datos)
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


def status() -> dict[str, object]:
    """Lo que /health cuenta de Piper: si arrancó y qué voz tiene cada idioma."""
    return {
        "ok": _fallo is None,
        "error": _fallo,
        "voicesDir": VOICES_DIR,
        # Las que no estén en disco se bajan solas la primera vez que se pidan.
        "voices": {
            lang: {"voice": voz, "downloaded": is_available(voz)}
            for lang, voz in sorted(VOICES.items())
        },
    }


async def warmup(voice: str | None = None) -> bool:
    """Baja el modelo si falta y lo carga, al arrancar el servidor.

    Así el ~1,2 s de carga no lo paga la primera foto. Si falla se apunta el
    motivo en `_fallo`, que es lo que enseña /health.
    """
    global _fallo
    v = voice or VOICES[DEFAULT_LANG]
    try:
        await ensure_voice(v)
        await asyncio.to_thread(_voz_cargada, v)
        _fallo = None
        return True
    except Exception as e:
        _fallo = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        return False
