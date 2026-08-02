#!/usr/bin/env python3
"""Genera los clips que las gafas llevan grabados, en ./assets.

De momento solo hay uno: el «Diga'm» que suena por el altavoz mientras la
foto sube al backend, entre que se detecta «Hey Bonsai» y se abre el micro.

Se genera con el mismo Piper y la misma voz que las respuestas, así que suena
igual y no se nota el salto entre el clip grabado y lo que contesta el modelo.

    python generar_clips.py

Salen dos ficheros por clip:

    digam-16k.pcm   las muestras en crudo, que es lo que se copia a la SD y se
                    escribe tal cual al I2S del MAX98357A
    digam-16k.wav   el mismo audio con cabecera, solo para poder escucharlo

Si cambias el texto, cambia también ASK_WAKE_REPLY en el .env: los dos turnos
que se le dan por dichos al modelo tienen que coincidir con lo que la persona
ha oído de verdad.
"""

from __future__ import annotations

import asyncio
import os
import sys
import wave

import piper_tts
import tts

# El directorio va al lado del código, no en /data: son parte del proyecto y
# tienen que estar en el repositorio para poder flashearlos.
DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

# 16 kHz porque es lo que ya usa el micro y de sobras para una palabra. A este
# ritmo, medio segundo de audio son 16 KB.
RATE = 16000

CLIPS = {
    # nombre de fichero -> texto. El texto por defecto sale del .env para que
    # no haya dos sitios donde cambiarlo.
    "digam": os.environ.get("ASK_WAKE_REPLY", "Diga’m!"),
}


async def genera(nombre: str, texto: str, voz: str) -> int:
    crudo = b"".join([t async for t in piper_tts.stream_raw(texto, voz, "pcm16", RATE)])

    pcm = os.path.join(DESTINO, f"{nombre}-{RATE // 1000}k.pcm")
    with open(pcm, "wb") as f:
        f.write(crudo)

    # El WAV se escribe con las longitudes de verdad, no con las 0xFFFFFFFF de
    # piper_tts.cabecera_wav: aquí ya sabemos cuánto audio hay y esto es un
    # fichero para escuchar, no una respuesta en streaming.
    wav = os.path.join(DESTINO, f"{nombre}-{RATE // 1000}k.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(crudo)

    print(f"✅ {nombre}: «{texto}»  {len(crudo)} bytes, "
          f"{len(crudo) / (RATE * 2):.2f} s  ->  {os.path.basename(pcm)}")
    return len(crudo)


async def main() -> int:
    os.makedirs(DESTINO, exist_ok=True)
    voz = tts.voice_for("ca", None, "piper")

    if not piper_tts.is_available(voz):
        print(f"Falta la voz {voz}. Bájala antes con:\n    python descargar_voces.py")
        return 1

    for nombre, texto in CLIPS.items():
        await genera(nombre, texto, voz)

    print(f"\nEn {DESTINO}. Copia el .pcm a la SD de las gafas.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
