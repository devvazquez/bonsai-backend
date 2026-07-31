#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Terminal de pruebas para el backend de Bonsai.

Sin dependencias: usa solo la librería estándar de Python.
(Ya no hace falta curl_cffi: ese truco era para esquivar el anti-bot del
dominio compartido *.workers.dev, que ya no usamos.)

    python test_bonsai.py

Comandos disponibles: escribe "help".
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# La consola de Windows suele venir en cp1252, y ahí los emojis y los acentos
# de más abajo revientan con UnicodeEncodeError. Forzamos UTF-8 en la salida.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuración (cámbiala aquí o con el comando "config" en marcha)
# ---------------------------------------------------------------------------
API_URL = "http://127.0.0.1:8080"  # en producción: https://bonsai.tudominio.com
DEVICE_ID = "bonsai-01"
LANG = "ca"  # 'ca' catalán, 'es' castellano, 'en' inglés
API_TOKEN = ""  # el mismo BONSAI_API_TOKEN del servidor (vacío = sin auth)
TIMEOUT = 60


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def call(path: str, method: str = "GET", body: dict | None = None, raw: bool = False):
    """Devuelve (respuesta, segundos). respuesta es dict, bytes o None si falla."""
    url = f"{API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["X-API-Token"] = API_TOKEN
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            elapsed = time.perf_counter() - start
            content = resp.read()
            return (content if raw else json.loads(content)), elapsed
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - start
        print(f"⚠️  HTTP {e.code} ({ms(elapsed)}): {e.read().decode('utf-8', 'ignore')[:400]}")
    except Exception as e:
        elapsed = time.perf_counter() - start
        print(f"⚠️  Error de conexión ({ms(elapsed)}): {e}")
    return None, time.perf_counter() - start


def play(path: str) -> None:
    """Abre el audio con el reproductor por defecto (Windows/macOS/Linux)."""
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows
    except AttributeError:
        os.system(f'(xdg-open "{path}" || open "{path}") >/dev/null 2>&1 &')
    except Exception:
        pass


def cmd_describe(args: list[str]) -> None:
    if not args:
        print("Uso: describe <ruta_imagen> [prompt opcional]")
        return
    path, prompt = args[0], " ".join(args[1:]) or None
    if not os.path.isfile(path):
        print(f"No encuentro el archivo: {path}")
        return

    t0 = time.perf_counter()
    img_bytes = open(path, "rb").read()
    img_b64 = base64.b64encode(img_bytes).decode()
    t_encode = time.perf_counter() - t0

    body = {"deviceId": DEVICE_ID, "image": img_b64, "lang": LANG}
    if prompt:
        body["prompt"] = prompt

    print(f"📤 Enviando ({len(img_bytes)/1024:.1f} KB, codificada en {ms(t_encode)})...")
    data, elapsed = call("/describe", "POST", body)
    if not data:
        return

    print(f"✅ Respuesta en {ms(elapsed)}")
    print(f"📝 {data['text']}")

    t_decode = 0.0
    if data.get("audio"):
        out = f"respuesta_{int(time.time())}.mp3"
        t1 = time.perf_counter()
        open(out, "wb").write(base64.b64decode(data["audio"]))
        t_decode = time.perf_counter() - t1
        print(f"🔊 {out} (voz: {data.get('voice')})")
        play(out)

    print("\n⏱️  Tiempos:")
    print(f"   Codificar imagen : {ms(t_encode)}")
    for k, v in (data.get("timings") or {}).items():
        print(f"   {k:<17}: {v} ms  (en el servidor)")
    print(f"   Ida y vuelta     : {ms(elapsed)}")
    if t_decode:
        print(f"   Decodificar audio: {ms(t_decode)}")
    print(f"   TOTAL            : {ms(t_encode + elapsed + t_decode)}")


def cmd_speak(args: list[str]) -> None:
    if not args:
        print("Uso: speak <texto>")
        return
    text = " ".join(args)
    qs = urllib.parse.urlencode({"text": text, "lang": LANG})
    audio, elapsed = call(f"/speak?{qs}", "POST", raw=True)
    if not audio:
        return
    out = f"voz_{int(time.time())}.mp3"
    open(out, "wb").write(audio)
    print(f"🔊 {out} ({len(audio)/1024:.1f} KB en {ms(elapsed)})")
    play(out)


def cmd_remember(args: list[str]) -> None:
    if not args:
        print("Uso: remember <texto a recordar>")
        return
    fact = " ".join(args)
    data, elapsed = call("/memory", "POST", {"deviceId": DEVICE_ID, "fact": fact})
    if data:
        print(f"✅ Guardado en {ms(elapsed)}: {fact}")


def cmd_memories(_: list[str]) -> None:
    data, elapsed = call(f"/memory/{DEVICE_ID}")
    if not data:
        return
    mems = data.get("memories", [])
    print(f"📚 {len(mems)} recuerdo(s) ({ms(elapsed)}):")
    for m in mems:
        print(f"   [{m['id'][:8]}] {m['fact']}")


def cmd_forget(args: list[str]) -> None:
    if not args:
        print("Uso: forget <id o prefijo>")
        return
    data, elapsed = call(f"/memory/{DEVICE_ID}/{args[0]}", "DELETE")
    if data:
        print(f"🗑️  Borrado en {ms(elapsed)}")


def cmd_voices(args: list[str]) -> None:
    prefix = args[0] if args else LANG
    data, elapsed = call(f"/voices?prefix={prefix}")
    if data:
        print(f"🎙️  ({ms(elapsed)}) {', '.join(data.get('voices', []))}")


def cmd_health(_: list[str]) -> None:
    data, elapsed = call("/health")
    if data:
        print(f"💚 {data} ({ms(elapsed)})")


def cmd_config(args: list[str]) -> None:
    global API_URL, DEVICE_ID, LANG, API_TOKEN
    if not args:
        shown = (API_TOKEN[:4] + "…") if API_TOKEN else "(sin token)"
        print(f"API_URL   = {API_URL}\nDEVICE_ID = {DEVICE_ID}\n"
              f"LANG      = {LANG}\nTOKEN     = {shown}")
        return
    key, *rest = args
    if not rest:
        print("Uso: config url <url> | device <id> | lang <ca|es|en> | token <token>")
        return
    if key == "url":
        API_URL = rest[0].rstrip("/")
    elif key == "device":
        DEVICE_ID = rest[0]
    elif key == "lang":
        LANG = rest[0]
    elif key == "token":
        API_TOKEN = rest[0]
    else:
        print("Clave desconocida. Usa: url, device, lang o token")
        return
    cmd_config([])


HELP = """
Comandos:
  describe <imagen> [prompt]   Envía una imagen, mide tiempos y reproduce el audio
  speak <texto>                Solo texto a voz
  remember <texto>             Guarda un recuerdo
  memories                     Lista los recuerdos
  forget <id>                  Borra un recuerdo (vale el prefijo)
  voices [prefijo]             Lista voces disponibles (p. ej. "voices ca")
  health                       Comprueba que el servidor responde
  config [url|device|lang|token] <v>  Ver o cambiar configuración
  help / exit
"""

COMMANDS = {
    "describe": cmd_describe, "speak": cmd_speak, "remember": cmd_remember,
    "memories": cmd_memories, "forget": cmd_forget, "voices": cmd_voices,
    "health": cmd_health, "config": cmd_config,
}


def main() -> None:
    print("=" * 60)
    print("  Bonsai Backend — terminal de pruebas")
    print(f"  API: {API_URL} | Device: {DEVICE_ID} | Idioma: {LANG}")
    print("=" * 60)
    print(HELP)

    while True:
        try:
            line = input("bonsai> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        cmd, *args = line.split()
        cmd = cmd.lower()
        if cmd in ("exit", "quit"):
            break
        if cmd == "help":
            print(HELP)
        elif cmd in COMMANDS:
            COMMANDS[cmd](args)
        else:
            print(f"Comando desconocido: {cmd}. Escribe 'help'.")


if __name__ == "__main__":
    main()
