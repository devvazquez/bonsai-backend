#!/usr/bin/env python3
"""Generates the fixed audios the glasses carry, into ./assets.

    python scripts/generate_audios.py [lang] [id ...]

The phrases come from `app/audios.py`, the same table the API serves, so there
is only one place to change what the device says. They are synthesized with the
same Piper voice as the answers, so there is no audible jump between a recorded
phrase and what the model replies.

Two files per audio:

    start_talking-16k.pcm   raw samples, copied to the SD and written straight
                            to the MAX98357A's I2S
    start_talking-16k.wav   the same audio with a header, just to listen to it

The device normally downloads these itself from /audios/{id} on first boot, so
this script is only for having them at hand from a computer.
"""

from __future__ import annotations

import asyncio
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import audios, tts  # noqa: E402

# In the repository root and not next to this script: the audios belong to the
# project, not to scripts/.
TARGET = os.path.join(ROOT, "assets")

# 16 kHz because it is what the mic already uses and plenty for a phrase. At
# this rate half a second of audio is 16 KB.
RATE = 16000


async def generate(audio_id: str, text: str, voice: str) -> int:
    raw = b"".join([c async for c in tts.stream_raw(text, voice, "pcm16", RATE)])

    pcm = os.path.join(TARGET, f"{audio_id}-{RATE // 1000}k.pcm")
    with open(pcm, "wb") as f:
        f.write(raw)

    # Real lengths here, not tts.wav_header's 0xFFFFFFFF: we already know how
    # much audio there is and this is a file to listen to, not a stream.
    wav = os.path.join(TARGET, f"{audio_id}-{RATE // 1000}k.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(raw)

    print(f"✅ {audio_id}: «{text}»  {len(raw)} bytes, "
          f"{len(raw) / (RATE * 2):.2f} s  ->  {os.path.basename(pcm)}")
    return len(raw)


async def main(argv: list[str]) -> int:
    lang = argv[0] if argv else tts.DEFAULT_LANG
    if lang not in tts.languages():
        print(f"Unknown language: {lang!r}. Available: {', '.join(tts.languages())}.")
        return 2

    wanted = argv[1:] or audios.ids()
    unknown = [a for a in wanted if a not in audios.AUDIOS]
    if unknown:
        print(f"No such audio: {', '.join(unknown)}. Available: {', '.join(audios.ids())}.")
        return 2

    os.makedirs(TARGET, exist_ok=True)
    voice = tts.voice_for(lang)
    # Downloaded on demand if the model is not on disk (~63 MB, once).
    await tts.ensure_voice(voice)

    for audio_id in wanted:
        text = audios.text(audio_id, lang)
        if text is None:
            print(f"⚠️  {audio_id} has no {lang!r} text, skipped.")
            continue
        await generate(audio_id, text, voice)

    print(f"\nIn {TARGET}. Copy the .pcm files to the glasses' SD card.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
