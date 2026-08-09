#!/usr/bin/env python3
"""Latency benchmark for the Bonsai backend.

Latency is what decides whether the glasses feel instant or clumsy, so this
measures it in parts: resizing the image, encoding it, the network, the vision
model and the TTS. Without the breakdown there is no way to know what to attack.

MIND THE QUOTA. By default it **spends nothing**: it shows the plan and what it
would cost. You have to pass `--yes` for it to actually call the provider.

    # Not a single token: checks the code and estimates the cost
    python scripts/bench_latency.py --selftest
    python scripts/bench_latency.py --image photo.jpg

    # Spends quota: 1 request per size (the minimum to get the number)
    python scripts/bench_latency.py --image photo.jpg --yes

    # Time-to-first-token, to see whether streaming is worth it
    python scripts/bench_latency.py --image photo.jpg --mode ttft --yes
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

API_URL = os.environ.get("BONSAI_API_URL", "http://127.0.0.1:8080")
# API version prefix (the same API_PREFIX as main.py).
API_PREFIX = os.environ.get("BONSAI_API_PREFIX", "/api/v1")
API_TOKEN = os.environ.get("BONSAI_API_TOKEN", "")
DEVICE_ID = "bench"

# Long sides to compare. 896 px is the interesting one: below it you lose the
# detail needed to read text, above it you only pay tokens and latency.
SIZES = (672, 896, 1568)

# Cap on the tokens one run may spend. A net so we don't repeat burning the
# 200,000 tokens of the day on six tests.
DEFAULT_BUDGET = 20_000


# --------------------------------------------------------------------------
# Token estimation (approximate, to decide whether a run is worth launching)
# --------------------------------------------------------------------------
def estimated_tokens(width: int, height: int) -> int:
    """Approximate input-token cost of an image.

    Groq charges proportionally to pixels. Calibrated against its own 429 error,
    "Requested 2656" for 672x896 (602,112 px), not against the docs, which
    didn't add up.
    """
    return round(width * height / 227) or 1


# --------------------------------------------------------------------------
# Image preparation
# --------------------------------------------------------------------------
def variants(path: str, sizes: tuple[int, ...]) -> list[dict]:
    """Returns the original image and its downscaled versions, already base64.

    Resize and encode time are measured here because they count too: they are
    part of what the person waits for.
    """
    raw = open(path, "rb").read()

    t0 = time.perf_counter()
    b64 = base64.b64encode(raw).decode()
    encode_ms = (time.perf_counter() - t0) * 1000

    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("⚠️  No Pillow: only the original image is measured.")
        print("   Install it with: pip install pillow\n")
        return [
            {
                "name": "original",
                "b64": b64,
                "bytes": len(raw),
                "width": 0,
                "height": 0,
                "resize_ms": 0.0,
                "encode_ms": encode_ms,
            }
        ]

    import io

    original = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    out = [
        {
            "name": f"original {original.width}x{original.height}",
            "b64": b64,
            "bytes": len(raw),
            "width": original.width,
            "height": original.height,
            "resize_ms": 0.0,
            "encode_ms": encode_ms,
        }
    ]

    for side in sizes:
        if max(original.size) <= side:
            continue
        t0 = time.perf_counter()
        im = original.copy()
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80, optimize=True)
        raw_r = buf.getvalue()
        resize_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        b64_r = base64.b64encode(raw_r).decode()
        enc_ms = (time.perf_counter() - t0) * 1000

        out.append(
            {
                "name": f"{side} px ({im.width}x{im.height})",
                "b64": b64_r,
                "bytes": len(raw_r),
                "width": im.width,
                "height": im.height,
                "resize_ms": resize_ms,
                "encode_ms": enc_ms,
            }
        )
    return out


# --------------------------------------------------------------------------
# Measurement against the server (/look)
# --------------------------------------------------------------------------
def measure_server(client: httpx.Client, var: dict, prompt: str | None) -> dict:
    """Measures /look, the only image endpoint there is.

    What matters here is the **first audio byte**: that is when the ESP32 could
    start playing. The total matters far less, because from the first byte on
    the download overlaps with playback.
    """
    body = {
        "deviceId": DEVICE_ID,
        "image": var["b64"],
        "lang": "es",
        "audioFormat": "pcm16",
        "sampleRate": 16000,
    }
    if prompt:
        body["prompt"] = prompt

    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["X-API-Token"] = API_TOKEN

    t0 = time.perf_counter()
    first_ms, n = None, 0
    try:
        with client.stream(
            "POST", f"{API_URL}{API_PREFIX}/look", json=body, headers=headers
        ) as r:
            if r.status_code == 429:
                r.read()
                wait = r.headers.get("Retry-After", "?")
                return {"error": f"429 quota exhausted (Retry-After: {wait}s)",
                        "stop": True}
            if r.status_code >= 400:
                r.read()
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            head = r.headers
            for chunk in r.iter_bytes():
                if chunk:
                    if first_ms is None:
                        first_ms = (time.perf_counter() - t0) * 1000
                    n += len(chunk)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    total_ms = (time.perf_counter() - t0) * 1000

    vision = int(head.get("x-bonsai-vision-ms", 0))
    resize = int(head.get("x-bonsai-resize-ms", 0))
    return {
        "total_ms": total_ms,
        "first_audio_ms": first_ms or total_ms,
        "vision_ms": vision,
        "resize_ms": resize,
        # Whatever isn't vision or resize is synthesis and moving the bytes.
        "audio_ms": max(0.0, (first_ms or total_ms) - vision - resize),
        "bytes": n,
        "text": base64.b64decode(head.get("x-bonsai-text", "")).decode(
            "utf-8", "replace"),
        "model": head.get("x-bonsai-model", "?"),
    }


# --------------------------------------------------------------------------
# Time-to-first-token, talking straight to the provider
# --------------------------------------------------------------------------
# Kept in Spanish on purpose: the server path above is measured with lang="es",
# so both modes ask the model for the same kind of answer.
SYSTEM_TTFT = (
    "Eres el asistente de visión de unas gafas. Contesta en 1 o 2 frases "
    "cortas, en castellano, sin preámbulos."
)
USER_TTFT = "¿Qué tengo delante? Dímelo en una o dos frases."


def measure_ttft(var: dict) -> dict:
    """Measures how long the first chunk of text takes to arrive.

    Interesting because if TTFT is much lower than the total it pays to feed the
    TTS as the text arrives instead of waiting for the whole sentence. Already
    measured and discarded: 1,246 ms vs 1,303 ms.
    """
    from app import vision as mod

    key = mod.api_key()
    url = mod.GROQ_URL
    payload = {
        "model": mod.MODEL,
        "temperature": 0.4,
        "max_completion_tokens": 150,
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_TTFT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": USER_TTFT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mod.sniff_mime(var['b64'])};base64,{var['b64']}"
                        },
                    },
                ],
            },
        ],
    }

    if not key:
        return {"error": "No GROQ_API_KEY"}

    t0 = time.perf_counter()
    ttft_ms = None
    chunks = 0
    text = []
    try:
        with httpx.Client(timeout=60.0) as c:
            with c.stream(
                "POST", url, json=payload, headers=mod.auth_headers(key)
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    if r.status_code == 429:
                        return {"error": "429 quota exhausted", "stop": True}
                    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    piece = _text_of_chunk(data)
                    if not piece:
                        continue
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    chunks += 1
                    text.append(piece)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    total_ms = (time.perf_counter() - t0) * 1000
    if ttft_ms is None:
        return {"error": "No token arrived"}
    return {
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "chunks": chunks,
        "text": "".join(text).strip(),
    }


def _text_of_chunk(data: str) -> str:
    try:
        d = json.loads(data)
    except json.JSONDecodeError:
        return ""
    delta = (d.get("choices") or [{}])[0].get("delta") or {}
    return delta.get("content") or ""


# --------------------------------------------------------------------------
# Checks that don't spend a single token
# --------------------------------------------------------------------------
def selftest() -> int:
    """Validates everything that can be validated without calling anyone."""
    from app import vision

    failures = []

    def check(name: str, got, expected) -> None:
        ok = got == expected
        print(f"  {'✅' if ok else '❌'} {name}: {got!r}")
        if not ok:
            failures.append(f"{name}: expected {expected!r}, got {got!r}")

    print("Image format (sniffed, no longer hardcoded to 'jpeg'):")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode()
    jpg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()
    webp = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 ").decode()
    gif = base64.b64encode(b"GIF89a" + b"\x00" * 40).decode()
    check("PNG", vision.sniff_mime(png), "image/png")
    check("JPEG", vision.sniff_mime(jpg), "image/jpeg")
    check("WEBP", vision.sniff_mime(webp), "image/webp")
    check("GIF", vision.sniff_mime(gif), "image/gif")
    check("garbage -> jpeg", vision.sniff_mime("!!!"), "image/jpeg")

    print("\nError messages (the timeout bug with an empty str(e)):")
    check(
        "timeout with no message",
        vision.describe_error(httpx.ReadTimeout("")),
        "ReadTimeout",
    )
    check("normal error", vision.describe_error(ValueError("broken")), "broken")

    print("\nReading the wait time out of a 429:")
    r_groq = httpx.Response(
        429, text='{"error":{"message":"Please try again in 12m39.024s"}}'
    )
    check("from the text", vision._seconds_to_wait(r_groq), 759.024)
    check(
        "from the header",
        vision._seconds_to_wait(httpx.Response(429, headers={"retry-after": "30"}, text="x")),
        30.0,
    )

    print("\nToken estimate per image (Groq charges by pixels):")
    for w, h in ((672, 896), (3024, 4032)):
        print(f"  {w}x{h}: ~{estimated_tokens(w, h):,} tokens")

    if failures:
        print(f"\n❌ {len(failures)} failure(s):")
        for f in failures:
            print(f"   - {f}")
        return 1
    print("\n✅ All correct (0 tokens spent)")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Measures backend latency. By default it does NOT spend quota.",
    )
    p.add_argument("--image", help="Path of the test image")
    p.add_argument("--mode", default="server", choices=["server", "ttft"])
    p.add_argument("--repeat", type=int, default=1, help="Repetitions (default 1)")
    p.add_argument("--prompt", help="A specific question instead of the description")
    p.add_argument("--sizes", default=",".join(str(t) for t in SIZES))
    p.add_argument("--only-small", action="store_true",
                   help="Only the smallest size: the cheapest in quota")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                   help=f"Cap on estimated tokens (default {DEFAULT_BUDGET})")
    p.add_argument("--yes", action="store_true",
                   help="Confirms it may spend quota. Without this it only shows the plan.")
    p.add_argument("--selftest", action="store_true",
                   help="Checks the code without calling anyone (0 tokens)")
    args = p.parse_args()

    if args.selftest:
        return selftest()

    if not args.image:
        print("Missing --image. Example:\n  python scripts/bench_latency.py --image photo.jpg --yes")
        print("\nOr check the code without spending anything:\n  python scripts/bench_latency.py --selftest")
        return 2
    if not os.path.isfile(args.image):
        print(f"Image not found: {args.image}")
        return 2

    sizes = tuple(int(s) for s in args.sizes.split(",") if s.strip())
    vars_ = variants(args.image, sizes)
    if args.only_small:
        vars_ = [min(vars_, key=lambda v: v["bytes"])]

    # --- Plan and cost before spending anything --------------------------
    print(f"\nPlan: mode {args.mode}, {args.repeat} repetition(s)")
    print(f"{'image':<26} {'KB':>7} {'estimated tokens/request':>28}")
    print("-" * 64)
    cost = 0
    for v in vars_:
        est = estimated_tokens(v["width"], v["height"])
        cost += est * args.repeat
        print(f"{v['name']:<26} {v['bytes']/1024:>7.0f} {'~' + f'{est:,}':>28}")
    requests = len(vars_) * args.repeat
    print("-" * 64)
    print(f"Total: {requests} request(s), ~{cost:,} estimated input tokens")

    if cost > args.budget:
        print(f"\n⛔ Over budget ({args.budget:,} tokens). Options:")
        print("   --only-small          only the downscaled image")
        print("   --sizes 896           a single size")
        print(f"   --budget {cost}   raise the cap deliberately")
        return 1
    if not args.yes:
        print("\nDry run: nothing was spent. Add --yes to measure for real.")
        return 0

    # --- Measurement -----------------------------------------------------
    results: dict[str, list[dict]] = {}
    stop = False
    with httpx.Client(timeout=120.0) as client:
        for v in vars_:
            if stop:
                break
            name = v["name"]
            results[name] = []
            for i in range(args.repeat):
                if args.mode == "ttft":
                    r = measure_ttft(v)
                else:
                    r = measure_server(client, v, args.prompt)
                if "error" in r:
                    print(f"⚠️  {name}: {r['error']}")
                    if r.get("stop"):
                        print("   Quota exhausted: stopping so we don't insist.")
                        stop = True
                    break
                results[name].append(r)
                print(f"   {name} [{i+1}/{args.repeat}] ok")

    # --- Results ---------------------------------------------------------
    useful = {k: v for k, v in results.items() if v}
    if not useful:
        print("\nNo valid measurements.")
        return 1

    def med(samples: list[dict], field: str) -> float:
        vals = [m[field] for m in samples if field in m]
        return statistics.median(vals) if vals else 0.0

    print("\n" + "=" * 78)
    if args.mode == "ttft":
        print("TIME TO FIRST TOKEN (medians)")
        print(f"{'image':<38} {'TTFT':>10} {'total':>10} {'chunks':>8}")
        print("-" * 78)
        for name, ms in useful.items():
            print(f"{name:<38} {med(ms,'ttft_ms'):>9.0f}ms "
                  f"{med(ms,'total_ms'):>9.0f}ms {med(ms,'chunks'):>8.0f}")
        print("-" * 78)
        worst = max(useful.items(), key=lambda kv: med(kv[1], "total_ms"))
        saving = med(worst[1], "total_ms") - med(worst[1], "ttft_ms")
        print(f"\nFeeding the TTS as soon as the first token arrives would gain\n"
              f"up to ~{saving:.0f} ms on {worst[0]}.")
    else:
        print("LATENCY BREAKDOWN (medians)")
        print(f"{'image':<34} {'resize':>9} {'vision':>9} "
              f"{'audio':>9} {'1st byte':>10} {'KB':>7}")
        print("-" * 82)
        for name, ms in useful.items():
            print(f"{name:<34} {med(ms,'resize_ms'):>8.0f}ms "
                  f"{med(ms,'vision_ms'):>8.0f}ms {med(ms,'audio_ms'):>8.0f}ms "
                  f"{med(ms,'first_audio_ms'):>9.0f}ms {med(ms,'bytes')/1024:>7.0f}")
        print("-" * 82)
        best = min(useful.items(), key=lambda kv: med(kv[1], "first_audio_ms"))
        print(f"\nFastest to the first audio byte: {best[0]} "
              f"({med(best[1],'first_audio_ms'):.0f} ms)")
        for v in vars_:
            if v["resize_ms"]:
                print(f"Resize to {v['name']}: {v['resize_ms']:.0f} ms of CPU "
                      f"(+{v['encode_ms']:.0f} ms of base64)")

    print("\nReturned texts:")
    for name, ms in useful.items():
        if ms and ms[0].get("text"):
            print(f"  [{name}] {ms[0]['text'][:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
