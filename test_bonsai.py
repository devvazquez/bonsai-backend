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
# Vacíos = lo que tenga configurado el servidor (groq + piper por defecto).
# Se cambian en marcha con "config provider gemini" y "config tts edge".
PROVIDER = ""   # '' | 'groq' | 'gemini'
TTS = ""        # '' | 'piper' | 'edge'
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
    """Manda una foto a /look y reproduce el audio que va llegando."""
    if not args:
        print("Uso: describe <ruta_imagen> [pregunta opcional]")
        return
    path, prompt = args[0], " ".join(args[1:]) or None
    if not os.path.isfile(path):
        print(f"No encuentro el archivo: {path}")
        return

    t0 = time.perf_counter()
    img_bytes = open(path, "rb").read()
    img_b64 = base64.b64encode(img_bytes).decode()
    t_encode = time.perf_counter() - t0

    # wav para poder reproducirlo aquí; el ESP32 pide pcm16 en crudo.
    body = {"deviceId": DEVICE_ID, "image": img_b64, "lang": LANG,
            "audioFormat": "wav"}
    if prompt:
        body["prompt"] = prompt
    if PROVIDER:
        body["provider"] = PROVIDER
    if TTS:
        body["tts"] = TTS

    print(f"📤 Enviando ({len(img_bytes)/1024:.1f} KB, codificada en {ms(t_encode)})...")

    req = urllib.request.Request(
        f"{API_URL}/look", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"X-API-Token": API_TOKEN} if API_TOKEN else {})})
    t0 = time.perf_counter()
    primero = None
    trozos = []
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            cab = resp.headers
            while True:
                trozo = resp.read(8192)
                if not trozo:
                    break
                if primero is None:
                    primero = time.perf_counter() - t0
                trozos.append(trozo)
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "ignore")
        if e.code == 429:
            print(f"⚠️  Cuota agotada. Reintenta en {e.headers.get('Retry-After','?')} s")
        print(f"⚠️  HTTP {e.code}: {cuerpo[:300]}")
        return
    except Exception as e:
        print(f"⚠️  Error de conexión: {e}")
        return
    elapsed = time.perf_counter() - t0

    audio = b"".join(trozos)
    texto = base64.b64decode(cab.get("X-Bonsai-Text", "")).decode("utf-8", "replace")
    print(f"✅ Respuesta en {ms(elapsed)}")
    print(f"📝 {texto}")
    print(f"👁️  {cab.get('X-Bonsai-Provider')} · {cab.get('X-Bonsai-Model')}")

    out = f"respuesta_{int(time.time())}.{cab.get('X-Bonsai-Format', 'wav')}"
    open(out, "wb").write(audio)
    print(f"🔊 {out} ({cab.get('X-Bonsai-Tts')} · {cab.get('X-Bonsai-Voice')}, "
          f"{len(audio)/1024:.0f} KB)")
    play(out)

    print("\n⏱️  Tiempos:")
    print(f"   Codificar imagen : {ms(t_encode)}")
    print(f"   Reducir (servidor): {cab.get('X-Bonsai-Resize-Ms','0')} ms")
    print(f"   Visión (servidor) : {cab.get('X-Bonsai-Vision-Ms','?')} ms")
    if primero is not None:
        # Es el número que importa: cuando el ESP32 podría empezar a sonar.
        print(f"   Primer byte audio : {ms(primero)}")
    print(f"   Audio completo    : {ms(elapsed)}")
    print(f"   TOTAL             : {ms(t_encode + elapsed)}")


def cmd_speak(args: list[str]) -> None:
    if not args:
        print("Uso: speak <texto>")
        return
    text = " ".join(args)
    params = {"text": text, "lang": LANG}
    if TTS:
        params["tts_provider"] = TTS
    audio, elapsed = call(f"/speak?{urllib.parse.urlencode(params)}", "POST", raw=True)
    if not audio:
        return
    # Sin leer la cabecera no se sabe el formato, pero un WAV empieza por RIFF.
    ext = "wav" if audio[:4] == b"RIFF" else "mp3"
    out = f"voz_{int(time.time())}.{ext}"
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
    if not data:
        return
    print(f"💚 servidor vivo ({ms(elapsed)})")
    for nombre, p in (data.get("providers") or {}).items():
        marca = "◀ por defecto" if nombre == data.get("defaultProvider") else ""
        clave = "clave ok" if p.get("keyConfigured") else "SIN CLAVE"
        print(f"   👁️  {nombre:<7} {p.get('model'):<24} {clave:<10} {marca}")
    t = data.get("tts") or {}
    aviso = ""
    if t.get("active") != t.get("configured"):
        # Es el caso que importa ver: Piper no arrancó y se tiró de edge-tts.
        aviso = f"  ⚠️  se pidió {t.get('configured')}: {(t.get('piper') or {}).get('error')}"
    print(f"   🔊 {t.get('active')} ({t.get('format')}){aviso}")


def cmd_config(args: list[str]) -> None:
    global API_URL, DEVICE_ID, LANG, API_TOKEN, PROVIDER, TTS
    if not args:
        shown = (API_TOKEN[:4] + "…") if API_TOKEN else "(sin token)"
        print(f"API_URL   = {API_URL}\nDEVICE_ID = {DEVICE_ID}\n"
              f"LANG      = {LANG}\nTOKEN     = {shown}\n"
              f"PROVIDER  = {PROVIDER or '(el del servidor)'}\n"
              f"TTS       = {TTS or '(el del servidor)'}")
        return
    key, *rest = args
    if not rest:
        print("Uso: config url <url> | device <id> | lang <ca|es|en> | token <token>"
              " | provider <gemini|groq> | tts <piper|edge>")
        return
    if key == "url":
        API_URL = rest[0].rstrip("/")
    elif key == "device":
        DEVICE_ID = rest[0]
    elif key == "lang":
        LANG = rest[0]
    elif key == "token":
        API_TOKEN = rest[0]
    elif key == "provider":
        # "auto" o "-" vuelve a dejar decidir al servidor.
        PROVIDER = "" if rest[0] in ("auto", "-") else rest[0]
    elif key == "tts":
        TTS = "" if rest[0] in ("auto", "-") else rest[0]
    else:
        print("Clave desconocida. Usa: url, device, lang, token, provider o tts")
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
  config [url|device|lang|token|provider|tts] <v>   Ver o cambiar configuración
  help / exit

Para comparar proveedores sin reiniciar el servidor:
  config provider gemini       visión con Gemini (cuota holgada, más lento)
  config provider groq         visión con Groq (el de por defecto)
  config tts piper             voz en local (rápida)
  config tts edge              voz de Microsoft (mejor timbre, más lenta)
  config provider auto         volver a lo que tenga el servidor
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
