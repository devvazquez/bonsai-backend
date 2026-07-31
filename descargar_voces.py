#!/usr/bin/env python3
"""Baja las voces de Piper a ./voices (o a PIPER_VOICES_DIR).

El servidor ya lo hace solo al arrancar, así que esto es para tenerlas antes:
al construir la imagen de Docker, o para probar sin esperar la descarga.

    python descargar_voces.py                    # la de por defecto (catalán)
    python descargar_voces.py ca_ES-upc_pau-x_low
    python descargar_voces.py --todas
"""

from __future__ import annotations

import asyncio
import os
import sys

import piper_tts


async def main(nombres: list[str]) -> int:
    if not nombres:
        nombres = [piper_tts.VOICES[piper_tts.DEFAULT_LANG]]

    fallos = 0
    for nombre in nombres:
        if piper_tts.is_available(nombre):
            tam = os.path.getsize(piper_tts.voice_path(nombre)) / 1e6
            print(f"✅ {nombre} ya está ({tam:.0f} MB)")
            continue
        print(f"⬇️  bajando {nombre}…", flush=True)
        try:
            ruta = await piper_tts.download_voice(nombre)
            print(f"✅ {nombre} -> {ruta} ({os.path.getsize(ruta)/1e6:.0f} MB)")
        except Exception as e:
            print(f"❌ {nombre}: {type(e).__name__}: {e}")
            fallos += 1

    import vision

    await vision.aclose()
    print(f"\nVoces en {piper_tts.VOICES_DIR}: {', '.join(piper_tts.local_voices()) or '(ninguna)'}")
    return 1 if fallos else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--todas" in sys.argv:
        args = list(piper_tts._RUTAS)
    sys.exit(asyncio.run(main(args)))
