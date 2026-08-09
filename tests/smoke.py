#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Manual test terminal for the Bonsai backend.

No dependencies: standard library only.
(curl_cffi is no longer needed: that trick was to dodge the anti-bot of the
shared *.workers.dev domain, which we no longer use.)

    python tests/smoke.py

Needs a server already running (see API_URL below). This is NOT an automated
test: `describe` hits the vision provider and spends real Groq quota.

Available commands: type "help".
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

# The Windows console usually comes as cp1252, where the emoji below blow up
# with UnicodeEncodeError. Force UTF-8 on the output.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Configuration (change it here or with the "config" command while running)
# ---------------------------------------------------------------------------
API_URL = "http://127.0.0.1:8080"  # in production: https://bonsai.yourdomain.com
# API version prefix. call() prepends it to every path, so the paths below are
# still written as /look, /speak, /memory...
API_PREFIX = "/api/v1"
DEVICE_ID = "bonsai-01"
LANG = "ca"  # 'ca' Catalan, 'es' Spanish, 'en' English
API_TOKEN = ""  # same BONSAI_API_TOKEN as the server (empty = no auth)
TIMEOUT = 60


def ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def call(path: str, method: str = "GET", body: dict | None = None, raw: bool = False):
    """Returns (response, seconds). response is dict, bytes or None on failure."""
    url = f"{API_URL}{API_PREFIX}{path}"
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
        print(f"⚠️  Connection error ({ms(elapsed)}): {e}")
    return None, time.perf_counter() - start


def play(path: str) -> None:
    """Opens the audio with the default player (Windows/macOS/Linux)."""
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows
    except AttributeError:
        os.system(f'(xdg-open "{path}" || open "{path}") >/dev/null 2>&1 &')
    except Exception:
        pass


def cmd_describe(args: list[str]) -> None:
    """Sends a photo to /look and plays the audio as it arrives."""
    if not args:
        print("Usage: describe <image_path> [optional prompt]")
        return
    path, prompt = args[0], " ".join(args[1:]) or None
    if not os.path.isfile(path):
        print(f"File not found: {path}")
        return

    t0 = time.perf_counter()
    img_bytes = open(path, "rb").read()
    img_b64 = base64.b64encode(img_bytes).decode()
    t_encode = time.perf_counter() - t0

    # wav so it can be played here; the ESP32 asks for raw pcm16.
    body = {"deviceId": DEVICE_ID, "image": img_b64, "lang": LANG,
            "audioFormat": "wav"}
    if prompt:
        body["prompt"] = prompt

    print(f"📤 Sending ({len(img_bytes)/1024:.1f} KB, encoded in {ms(t_encode)})...")

    req = urllib.request.Request(
        f"{API_URL}{API_PREFIX}/look", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 **({"X-API-Token": API_TOKEN} if API_TOKEN else {})})
    t0 = time.perf_counter()
    first = None
    chunks = []
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            head = resp.headers
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                if first is None:
                    first = time.perf_counter() - t0
                chunks.append(chunk)
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "ignore")
        if e.code == 429:
            print(f"⚠️  Quota exhausted. Retry in {e.headers.get('Retry-After','?')} s")
        print(f"⚠️  HTTP {e.code}: {payload[:300]}")
        return
    except Exception as e:
        print(f"⚠️  Connection error: {e}")
        return
    elapsed = time.perf_counter() - t0

    audio = b"".join(chunks)
    text = base64.b64decode(head.get("X-Bonsai-Text", "")).decode("utf-8", "replace")
    print(f"✅ Response in {ms(elapsed)}")
    print(f"📝 {text}")
    print(f"👁️  {head.get('X-Bonsai-Provider')} · {head.get('X-Bonsai-Model')}")

    out = f"response_{int(time.time())}.{head.get('X-Bonsai-Format', 'wav')}"
    open(out, "wb").write(audio)
    print(f"🔊 {out} ({head.get('X-Bonsai-Tts')} · {head.get('X-Bonsai-Voice')}, "
          f"{len(audio)/1024:.0f} KB)")
    play(out)

    print("\n⏱️  Timings:")
    print(f"   Encode image      : {ms(t_encode)}")
    print(f"   Resize (server)   : {head.get('X-Bonsai-Resize-Ms','0')} ms")
    print(f"   Vision (server)   : {head.get('X-Bonsai-Vision-Ms','?')} ms")
    if first is not None:
        # The number that matters: when the ESP32 could start playing.
        print(f"   First audio byte  : {ms(first)}")
    print(f"   Full audio        : {ms(elapsed)}")
    print(f"   TOTAL             : {ms(t_encode + elapsed)}")


def cmd_speak(args: list[str]) -> None:
    if not args:
        print("Usage: speak <text>")
        return
    text = " ".join(args)
    params = {"text": text, "lang": LANG}
    audio, elapsed = call(f"/speak?{urllib.parse.urlencode(params)}", "POST", raw=True)
    if not audio:
        return
    out = f"voice_{int(time.time())}.wav"
    open(out, "wb").write(audio)
    print(f"🔊 {out} ({len(audio)/1024:.1f} KB in {ms(elapsed)})")
    play(out)


def cmd_remember(args: list[str]) -> None:
    if not args:
        print("Usage: remember <text to remember>")
        return
    fact = " ".join(args)
    data, elapsed = call("/memory", "POST", {"deviceId": DEVICE_ID, "fact": fact})
    if data:
        print(f"✅ Saved in {ms(elapsed)}: {fact}")


def cmd_memories(_: list[str]) -> None:
    data, elapsed = call(f"/memory/{DEVICE_ID}")
    if not data:
        return
    mems = data.get("memories", [])
    print(f"📚 {len(mems)} memory(ies) ({ms(elapsed)}):")
    for m in mems:
        print(f"   [{m['id'][:8]}] {m['fact']}")


def cmd_forget(args: list[str]) -> None:
    if not args:
        print("Usage: forget <id or prefix>")
        return
    data, elapsed = call(f"/memory/{DEVICE_ID}/{args[0]}", "DELETE")
    if data:
        print(f"🗑️  Deleted in {ms(elapsed)}")


def cmd_health(_: list[str]) -> None:
    data, elapsed = call("/health")
    if not data:
        return
    print(f"💚 server alive ({ms(elapsed)})")
    v = data.get("vision") or {}
    key = "key ok" if v.get("keyConfigured") else "NO KEY"
    print(f"   👁️  {v.get('model'):<24} {key}")
    t = data.get("tts") or {}
    piper = t.get("piper") or {}
    # What matters here: if Piper did not start, the glasses stay mute.
    warning = "" if piper.get("ok") else f"  ⚠️  Piper did not start: {piper.get('error')}"
    print(f"   🔊 piper ({t.get('format')}){warning}")


def cmd_config(args: list[str]) -> None:
    global API_URL, DEVICE_ID, LANG, API_TOKEN
    if not args:
        shown = (API_TOKEN[:4] + "…") if API_TOKEN else "(no token)"
        print(f"API_URL   = {API_URL}{API_PREFIX}\nDEVICE_ID = {DEVICE_ID}\n"
              f"LANG      = {LANG}\nTOKEN     = {shown}")
        return
    key, *rest = args
    if not rest:
        print("Usage: config url <url> | device <id> | lang <ca|es|en> | token <token>")
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
        print("Unknown key. Use: url, device, lang or token")
        return
    cmd_config([])


HELP = """
Commands:
  describe <image> [prompt]    Send an image, measure timings and play the audio
  speak <text>                 Text to speech only
  remember <text>              Store a memory
  memories                     List the memories
  forget <id>                  Delete a memory (a prefix works)
  health                       Check that the server responds
  config [url|device|lang|token] <v>   Show or change configuration
  help / exit
"""

COMMANDS = {
    "describe": cmd_describe, "speak": cmd_speak, "remember": cmd_remember,
    "memories": cmd_memories, "forget": cmd_forget,
    "health": cmd_health, "config": cmd_config,
}


def main() -> None:
    print("=" * 60)
    print("  Bonsai Backend — manual test terminal")
    print(f"  API: {API_URL}{API_PREFIX} | Device: {DEVICE_ID} | Language: {LANG}")
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
            print(f"Unknown command: {cmd}. Type 'help'.")


if __name__ == "__main__":
    main()
