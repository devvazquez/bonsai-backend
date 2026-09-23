"""Bonsai backend: vision + voice + memory orchestrated in a single request.

`/look` is the main endpoint and the ESP32-S3 calls it directly: it takes the
photo, shrinks it, adds context (date and the device's memories), asks the
vision model (`vision.py`, Groq) for a description and returns raw streaming
audio, ready for the MAX98357A's I2S, with no base64 and nothing for the
microcontroller to decode.

`/ask` is the same path with voice in front: the photo and then whatever the
person wearing the glasses is saying. Whisper turbo on Groq (`stt.py`)
transcribes it and that sentence becomes the question put to the vision model.

The rest is service: `/memory` for memories, `/speak` for standalone
text-to-speech, the `/admin`panel (`panel.py`) to manage the database.
 There is no web app in the main path.

Everything that is API hangs off `/api/v1`. Pages do not:`/admin` opens 
in a browser and are not versioned.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import audios, images, memory, stt, tts, vision
from .vision import VisionRateLimit, describe_error

# Exposed to the internet, so without this anyone who finds the URL can spend
# your Groq quota. Empty means no token is required: handy locally, NOT on the VPS.
API_TOKEN = os.environ.get("BONSAI_API_TOKEN", "")

# Browser origins allowed. In production put the real domain instead of "*".
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

app = FastAPI(title="Bonsai Backend", version="2.0.0")

# --------------------------------------------------------------------------
# API version
# --------------------------------------------------------------------------
# Everything API hangs off /api/v1, so the day a response format or /ask's body
# has to change, /api/v2 goes up beside it and the glasses already out there
# keep working until their firmware is updated.
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

# Routes are declared here and mounted with the prefix at the end of the file.
# There is no unprefixed alias: plain /look returns 404.
api = APIRouter()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """Checks the X-API-Token header when a token is configured."""
    if not API_TOKEN:
        return
    if x_api_token != API_TOKEN:
        raise HTTPException(401, "Invalid or missing token (X-API-Token header).")


@app.on_event("startup")
async def _startup() -> None:
    memory.init_db()
    # Open the TLS connection to Groq before the first photo: ~220 ms that
    # otherwise the person wearing the glasses pays.
    await vision.warmup()
    # And fetch/load the Piper model: ~63 MB once and ~1,2 s of loading that
    # the first photo has no reason to pay either.
    await tts.warmup()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await vision.aclose()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class LookRequest(BaseModel):
    image: str = Field(..., description="Base64 image, WITHOUT the data: prefix")
    deviceId: str
    prompt: str | None = None
    lang: str | None = None   # 'ca', 'es', 'en'. The language picks the voice.
    # 'pcm16' (what the MAX98357A's I2S wants), 'mulaw' (half the bytes) or
    # 'wav' (with a header, for browsers).
    audioFormat: str | None = None
    sampleRate: int | None = None
    # Long side to shrink the photo to on the server. 0 disables it; unset
    # means IMAGE_MAX_SIDE decides.
    maxSide: int | None = None
    # What this device can do, in OpenAI function shape:
    # {"name": "change_lang", "description": "...", "parameters": {...}}.
    # The server never runs them (see /audios docs and TOOL_ACK); it forwards
    # them to the model and returns whatever it picks in X-Bonsai-Tools.
    tools: list[dict[str, Any]] | None = None


class MemoryRequest(BaseModel):
    deviceId: str
    fact: str


class MemoryEdit(BaseModel):
    fact: str


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
LANG_NAMES = {"ca": "Catalan", "es": "Spanish", "en": "English"}

# --------------------------------------------------------------------------
# Tools the device declares
# --------------------------------------------------------------------------
# The tool definitions are prompt tokens on every request, and Groq's ceiling is
# 8.000 tokens/minute. Over this the request is rejected with a 400 rather than
# silently truncated, which would be a mystery to debug from the firmware.
TOOLS_MAX_CHARS = int(os.environ.get("TOOLS_MAX_CHARS", "2000"))

# Said out loud when the model triggers a tool but returns no text of its own.
# Mute glasses are a bad answer, and asking the model again would be a second
# round trip, which is exactly what this project refuses to pay.
TOOL_ACK = {"ca": "Fet.", "es": "Hecho.", "en": "Done."}

# The photo comes from a cheap fixed-focus 3 MP sensor (OV3660) at 800x600: it is
# always soft, a bit noisy and sometimes hazy against a light. Without saying so
# the model treats every frame as a failed photo and answers "I can't see
# anything", which is the one answer the wearer can do nothing with.
CAMERA_NOTE = {
    "ca": "Sobre la càmera: les fotos surten d'una càmera petita de 3 MP amb focus "
          "fix, així que sempre seran una mica borroses, amb soroll o amb reflexos. "
          "És normal: no diguis mai que no hi veus o que la imatge és massa borrosa. "
          "Descriu el que sí es distingeix (formes, colors, objectes, text gran) i, "
          "si no n'estàs segur, digues-ho amb un «sembla». Només si realment no es "
          "distingeix res, demana que s'acostin o que hi hagi més llum.",
    "es": "Sobre la cámara: las fotos vienen de una cámara pequeña de 3 MP con foco "
          "fijo, así que siempre saldrán algo borrosas, con ruido o con reflejos. Es "
          "normal: nunca digas que no ves nada o que la imagen está demasiado borrosa. "
          "Describe lo que sí se distingue (formas, colores, objetos, texto grande) y, "
          "si no estás seguro, dilo con un «parece». Solo si de verdad no se distingue "
          "nada, pide que se acerquen o que haya más luz.",
    "en": "About the camera: photos come from a small fixed-focus 3 MP camera, so "
          "they will always be somewhat soft, noisy or hazy. That is expected: never "
          "say you cannot see or that the image is too blurry. Describe what can be "
          "made out (shapes, colours, objects, large text) and hedge with \"looks "
          "like\" when unsure. Only if nothing at all can be made out, ask them to get "
          "closer or find more light.",
}


def _clean_tools(tools: list[dict] | None) -> list[dict]:
    """Validates what the device sent before it reaches the model."""
    if not tools:
        return []
    if len(json.dumps(tools)) > TOOLS_MAX_CHARS:
        raise HTTPException(
            400,
            f"The tool definitions exceed {TOOLS_MAX_CHARS} characters. They "
            "are sent to the model on every request and Groq's ceiling is 8.000 "
            "tokens/minute: send only the tools that make sense right now.",
        )
    for t in tools:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
            raise HTTPException(400, f"Every tool needs a 'name': {t!r}")
    return tools


def build_system_prompt(lang: str, memory_context: str,
                        tools: list[dict] | None = None) -> str:
    lang = (lang or "ca").lower()
    today = datetime.now().strftime("%A, %d %B %Y")

    if lang == "ca":
        parts = [
            "Ets l'assistent de visió de les ulleres Bonsai: expliques a qui les "
            "porta què té al davant, sigui per orientar-se, llegir alguna cosa, "
            "identificar un objecte o per simple curiositat.",
            f"Avui és {today}.",
            "Instruccions de resposta:\n"
            "- Respon en 1 o 2 frases curtes, com si ho diguessis en veu alta: "
            "el més important primer i sense farciment.\n"
            "- No comencis mai amb «a la imatge hi ha», «es veu» ni frases similars.\n"
            "- No descriguis fons ni detalls irrellevants. El text llegible o "
            "qualsevol perill/obstacle té prioritat absoluta.\n"
            "- Si et fan una pregunta concreta, respon únicament a allò demanat.\n"
            "- Fes servir un català estàndard, normatiu, precís i natural. No "
            "inventis mai paraules ni barregis idiomes.",
            "Noms propis i llocs:\n"
            "No intentis endevinar mai noms propis: ni ciutats, places, carrers, "
            "botigues ni monuments. Digues-los només si els estàs llegint literalment "
            "en un rètol o cartell de la imatge (i especifica que ho estàs llegint). "
            "Davant del dubte, descriu el lloc sense posar-li nom: «una plaça gran amb "
            "terrasses» és millor que un nom inventat.",
            CAMERA_NOTE["ca"],
            "Exemples d'estil i to:\n"
            "- «Tens al davant un volant de simulador de conducció connectat a un ordinador, al costat d'un llit.»\n"
            "- «Compte amb el graó que hi ha a sota. A la dreta hi ha una ampolla d'aigua sobre la taula.»\n"
            "- «El cartell de la porta diu «Sortida d'emergència».»",
        ]
        if memory_context:
            parts.append(
                "Coses que la persona t'ha demanat que recordis anteriorment:\n" + memory_context
            )
        if tools:
            parts.append(
                "Tens eines disponibles. Quan la persona demani alguna cosa que una d'elles "
                "pot fer, crida-la i respon també en veu alta amb una frase curta confirmant-ho "
                "(per exemple «Entesos, ara parlaré en català»). Never go silent just because you used a tool."
            )

    elif lang == "es":
        parts = [
            "Eres el asistente visual de las gafas Bonsai: describes a quien las "
            "lleva qué tiene delante, sea para orientarse, leer algo, identificar "
            "un objeto o por curiosidad.",
            f"Hoy es {today}.",
            "Instrucciones de respuesta:\n"
            "- Responde en 1 o 2 frases cortas, como hablándole en voz alta: lo "
            "importante primero y sin relleno.\n"
            "- No empieces con «en la imagen hay» ni similar. Prioriza texto "
            "legible o posibles peligros.\n"
            "- Responde SIEMPRE en español claro y natural.",
            "No inventes nombres propios de lugares ni calles salvo que los leas en un cartel.",
            CAMERA_NOTE["es"],
        ]
        if memory_context:
            parts.append("Cosas que la persona te ha pedido recordar:\n" + memory_context)
        if tools:
            parts.append(
                "Tienes herramientas disponibles. Si usas una, confirma siempre en voz alta con una frase corta."
            )

    else:  # en / fallback
        parts = [
            "You are the vision assistant of the Bonsai glasses: you tell whoever "
            "is wearing them what is in front of them, whether to get their "
            "bearings, to read something, to identify an object or just out of "
            "curiosity.",
            f"Today is {today}.",
            "Answer in 1 or 2 short sentences, as if saying it out loud: what "
            "matters first and no filler. Do not start with «in the image there "
            "is». Readable text or anything dangerous comes first. ALWAYS answer in English.",
            "Never guess proper nouns (cities, streets, monuments) unless reading them on a sign.",
            CAMERA_NOTE["en"],
        ]
        if memory_context:
            parts.append("Things the person has asked you to remember before:\n" + memory_context)
        if tools:
            parts.append(
                "You have tools available. When using one, always reply out loud with a short confirmation."
            )

    return "\n\n".join(parts)


@api.get("/health")
def health() -> dict[str, Any]:
    """No token: Docker's healthcheck uses it."""
    return {
        "ok": True,
        "authRequired": bool(API_TOKEN),
        # So the firmware can check which version it is talking to without
        # deducing it from the URL it already knows.
        "api": {"version": API_VERSION, "prefix": API_PREFIX},
        "vision": {
            "model": vision.MODEL,
            "keyConfigured": bool(vision.api_key()),
        },
        "tts": {
            "format": "wav",
            "piper": tts.status(),
        },
        "stt": {
            # What /ask needs. Listed apart because it is another model, even
            # though the key is the same GROQ_API_KEY.
            "model": stt.MODEL,
            "keyConfigured": bool(stt.api_key()),
            "maxAudioSeconds": ASK_MAX_SECONDS,
        },
    }


async def _audio_plan(
    audio_format: str | None,
    lang: str | None,
    sample_rate: int | None,
) -> dict[str, Any]:
    """Decides and validates how we are going to speak, before spending anything.

    Done at the start of the request, before vision and before reading the mic:
    if the requested format does not exist, better to say so now than after
    burning quota or uploading half a megabyte.

    The voice is not requested: a language is, and the voice comes from
    `tts.VOICES`, which is where they get changed.
    """
    # An undefined language is reported, not quietly answered in Catalan: being
    # answered in the wrong language with no warning looks like a broken server.
    if lang and lang.lower() not in tts.languages():
        raise HTTPException(
            400,
            f"Unknown language: {lang!r}. Available: {', '.join(tts.languages())}. "
            "They are added in tts.VOICES.",
        )

    fmt = (audio_format or "pcm16").lower()
    if fmt not in tts.FORMATS:
        raise HTTPException(
            400,
            f"Unknown format: {fmt!r}. Use one of: {', '.join(tts.FORMATS)}",
        )

    if sample_rate and sample_rate not in tts.SAMPLE_RATES:
        raise HTTPException(
            400,
            f"Unsupported sampleRate: {sample_rate}. Use one of: "
            f"{', '.join(str(s) for s in tts.SAMPLE_RATES)}",
        )

    voice = tts.voice_for(lang or tts.DEFAULT_LANG)

    # Download the voice if missing (~63 MB, once). Deliberately before reading
    # the model's sample_rate: without the .onnx.json we would not know what
    # rate it speaks at, and assuming 22050 for a 16 kHz voice sounds sped up.
    # It sits here because it is before spending vision quota and, in /ask,
    # before the mic uploads anything.
    try:
        await tts.ensure_voice(voice)
    except Exception as e:
        raise HTTPException(
            502,
            f"Could not prepare the voice {voice!r} for language "
            f"{(lang or tts.DEFAULT_LANG)!r}: {describe_error(e)}",
        ) from e

    rate = sample_rate or tts.sample_rate_of(voice)
    bits = 8 if fmt == "mulaw" else 16

    return {"format": fmt, "voice": voice, "rate": rate, "bits": bits}


# Content-Type per format, so the client does not have to guess.
_AUDIO_TYPES = {
    "pcm16": "audio/L16;rate={rate};channels=1",
    "mulaw": "audio/basic;rate={rate}",
    "wav": "audio/wav",
}

# The body of /look, /ask and /speak is audio, not JSON. Without saying so,
# FastAPI writes application/json in the schema and /docs promises a lie.
_AUDIO_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The audio in the requested format. The text goes in "
                       "the X-Bonsai-Text header (UTF-8 in base64).",
        "content": {
            t.split(";")[0]: {"schema": {"type": "string", "format": "binary"}}
            for t in _AUDIO_TYPES.values()
        },
    }
}


async def _speak_response_stream(
    sentence_stream: AsyncIterator[str],
    plan: dict[str, Any],
    headers: dict[str, str],
    meta: dict[str, Any],
) -> Response:
    """Turns the sentence stream into audio chunks and streams them back."""
    fmt, rate = plan["format"], plan["rate"]
    chunks = tts.stream_raw_sentences(sentence_stream, plan["voice"], fmt, rate)

    # Pull the first chunk so the response headers can carry the real values.
    # By this time, Groq has already generated the text / tools.
    try:
        first = await anext(chunks)
        print(f"  [stream] first audio chunk emitted | format={fmt}@{rate}Hz", flush=True)
    except StopAsyncIteration:
        raise HTTPException(502, "Piper returned no audio.") from None
    except Exception as e:
        raise HTTPException(
            502, f"Failed to generate the audio: {describe_error(e)}"
        ) from e

    full_text = meta.get("text") or ""
    headers.update({
        "X-Bonsai-Text": base64.b64encode(full_text.encode()).decode("ascii"),
        "X-Bonsai-Tts": "piper",
        "X-Bonsai-Format": fmt,
        "X-Bonsai-Rate": str(rate),
        "X-Bonsai-Bits": str(plan["bits"]),
        "X-Bonsai-Channels": "1",
        "X-Bonsai-Voice": plan["voice"],
        "X-Bonsai-Vision-Ms": str(meta.get("vision_ms", 0)),
    })
    if meta.get("tools"):
        headers["X-Bonsai-Tools"] = base64.b64encode(
            json.dumps(meta["tools"]).encode()
        ).decode("ascii")

    exposed = sorted({*headers, "X-Bonsai-Text", "X-Bonsai-Tts",
                      "X-Bonsai-Format", "X-Bonsai-Rate", "X-Bonsai-Bits",
                      "X-Bonsai-Channels", "X-Bonsai-Voice", "X-Bonsai-Tools"})
    headers["Access-Control-Expose-Headers"] = ", ".join(exposed)

    if fmt == "wav":
        data = first + b"".join([c async for c in chunks])
        return Response(
            tts.wav_header(rate, plan["bits"], len(data)) + data,
            media_type=_AUDIO_TYPES[fmt].format(rate=rate),
            headers=headers,
        )

    async def body():
        yield first
        async for chunk in chunks:
            yield chunk

    return StreamingResponse(
        body(),
        media_type=_AUDIO_TYPES[fmt].format(rate=rate),
        headers=headers,
    )


async def _speak_response(
    text: str, plan: dict[str, Any], headers: dict[str, str]
) -> Response:
    """Turns a complete text into audio (compatibility helper)."""
    async def _single() -> AsyncIterator[str]:
        yield text

    meta = {"text": text, "vision_ms": int(headers.get("X-Bonsai-Vision-Ms", 0))}
    return await _speak_response_stream(_single(), plan, headers, meta)


async def _describe_stream(
    req: LookRequest, preamble: tuple[tuple[str, str], ...] | None = None
) -> tuple[AsyncIterator[str], dict[str, Any], dict[str, str]]:
    """The streaming vision half of /look and /ask."""
    api_key = vision.api_key()
    if not api_key:
        raise HTTPException(500, "GROQ_API_KEY is not configured on the server.")

    lang = (req.lang or tts.DEFAULT_LANG).lower()
    tools = _clean_tools(req.tools)
    timings: dict[str, int] = {}

    t0 = time.perf_counter()
    memory_context = memory.get_memory_context(req.deviceId)
    timings["memory_ms"] = int((time.perf_counter() - t0) * 1000)

    image_b64 = req.image
    max_side = req.maxSide if req.maxSide is not None else (
        images.MAX_SIDE if images.ENABLED else 0
    )
    if max_side > 0:
        t0 = time.perf_counter()
        image_b64, image_info = await asyncio.to_thread(
            images.resize, req.image, max_side
        )
        if image_info.get("resized"):
            timings["resize_ms"] = int((time.perf_counter() - t0) * 1000)

    try:
        raw_stream, meta = await vision.describe_image_stream(
            api_key=api_key,
            image_base64=image_b64,
            system_prompt=build_system_prompt(lang, memory_context, tools),
            user_prompt=req.prompt or "What is in front of me? Tell me in a sentence or two.",
            preamble=preamble,
            tools=tools,
        )
    except VisionRateLimit as e:
        headers = {}
        if e.retry_after is not None:
            headers["Retry-After"] = str(max(1, round(e.retry_after)))
        raise HTTPException(429, str(e), headers=headers) from e
    except Exception as e:
        raise HTTPException(
            502, f"Failed to describe the image: {describe_error(e)}"
        ) from e

    meta["resize_ms"] = timings.get("resize_ms", 0)
    meta["memory_ms"] = timings.get("memory_ms", 0)

    async def _safe_sentences() -> AsyncIterator[str]:
        has_any = False
        async for s in raw_stream:
            has_any = True
            yield s
        if not has_any:
            if meta.get("tools"):
                fallback = TOOL_ACK.get(lang, TOOL_ACK[tts.DEFAULT_LANG])
                meta["text"] = fallback
                yield fallback
            else:
                raise HTTPException(502, "The vision model returned an empty answer.")

    headers = {
        "X-Bonsai-Model": vision.MODEL,
        "X-Bonsai-Resize-Ms": str(meta["resize_ms"]),
    }
    return _safe_sentences(), meta, headers


async def _describe(
    req: LookRequest, preamble: tuple[tuple[str, str], ...] | None = None
) -> tuple[str, dict[str, str]]:
    """The non-streaming vision half of /look and /ask (compatibility wrapper)."""
    stream, meta, headers = await _describe_stream(req, preamble)
    sentences = []
    async for s in stream:
        sentences.append(s)
    text = meta.get("text") or " ".join(sentences).strip()
    headers["X-Bonsai-Vision-Ms"] = str(meta.get("vision_ms", 0))
    if meta.get("tools"):
        headers["X-Bonsai-Tools"] = base64.b64encode(
            json.dumps(meta["tools"]).encode()
        ).decode("ascii")
    return text, headers


@api.post("/look", dependencies=[Depends(require_token)],
          responses=_AUDIO_RESPONSE, response_class=Response)
async def look(req: LookRequest) -> Response:
    """The main endpoint: a photo goes in, streaming audio comes out."""
    plan = await _audio_plan(req.audioFormat, req.lang, req.sampleRate)
    print(f"\n[look] request from {req.deviceId} (lang={req.lang or tts.DEFAULT_LANG}, format={plan['format']}@{plan['rate']}Hz)", flush=True)
    sentence_stream, meta, headers = await _describe_stream(req)
    return await _speak_response_stream(sentence_stream, plan, headers, meta)


# --------------------------------------------------------------------------
# /ask: photo + voice -> spoken answer
# --------------------------------------------------------------------------
# Photo ceiling. An OV3660 shot at 3 MP is under 1 MB; 8 is plenty and stops a
# broken client from making us allocate without end.
ASK_MAX_IMAGE = int(os.environ.get("ASK_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))

# Recording ceiling. Not just memory: Whisper charges per second of audio, so a
# mic left open must not be able to spend the day's quota. 30 s is far more
# than a question lasts.
ASK_MAX_SECONDS = float(os.environ.get("ASK_MAX_AUDIO_SECONDS", "30"))

# Below this there is not even one word: a button pressed by accident.
ASK_MIN_SECONDS = 0.25

# How long to wait without receiving a single chunk before giving the request
# up for lost. It has to fit the glasses' «Digue'm» plus however long the
# person takes to start talking, while still dropping the connection if the
# firmware hangs mid-recording.
ASK_SILENCE = float(os.environ.get("ASK_SILENCE_TIMEOUT_SECONDS", "15"))


def _tools_from_header(request: Request) -> list[dict] | None:
    """Reads the device's tools from X-Bonsai-Tools.

    /ask's body is already `[length][photo][audio]`, so there is nowhere to put
    a JSON field: it rides in a header, base64'd like the X-Bonsai-* that go the
    other way, because HTTP headers are ASCII.
    """
    raw = request.headers.get("X-Bonsai-Tools")
    if not raw:
        return None
    try:
        tools = json.loads(base64.b64decode(raw))
    except Exception as e:
        raise HTTPException(
            400,
            f"X-Bonsai-Tools is not base64 of a JSON list: {describe_error(e)}",
        ) from e
    if not isinstance(tools, list):
        raise HTTPException(400, "X-Bonsai-Tools must be a JSON list of tools.")
    return tools


class _Frame:
    """Reads /ask's body as it arrives, in two stages.

    The body is raw and shaped like this:

        4 bytes  big-endian uint32  = how many bytes the photo takes
        N bytes  the photo (JPEG)
        rest     the mic audio, until the request closes

    Two methods and not one, on purpose. The point of uploading the audio in
    chunks is that the photo is here long before the person stops talking: read
    the whole body at once (or with `request.body()`) and the photo would not be
    saved until the end, making streaming pointless. This way `photo()` returns
    as soon as the image is complete, it gets saved, and only then do we wait
    for the rest.
    """

    def __init__(self, request: Request) -> None:
        self._chunks = request.stream().__aiter__()
        self._buf = bytearray()

    async def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                self._buf.extend(await self._chunks.__anext__())
            except StopAsyncIteration:
                raise HTTPException(
                    400,
                    f"The body ended early: expected {n} bytes and got "
                    f"{len(self._buf)}. The format is "
                    "[4 length bytes][photo][audio].",
                ) from None
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def photo(self) -> bytes:
        n = int.from_bytes(await self._take(4), "big")
        if not 0 < n <= ASK_MAX_IMAGE:
            raise HTTPException(
                400,
                f"Impossible image length: {n} bytes (the maximum is "
                f"{ASK_MAX_IMAGE}). Are the first 4 bytes big-endian?",
            )
        return await self._take(n)

    async def audio(self, max_bytes: int, timeout: float) -> bytes:
        """Waits for the mic until the request closes.

        In the glasses' flow a while passes between the photo and the first
        sample: the «Digue'm» plays and then the person thinks. Hence a
        per-chunk timeout and not a total one. If the mic goes silent
        altogether — firmware hung, WiFi gone — the connection has to be
        dropped instead of left open forever.
        """
        data = bytearray(self._buf)
        self._buf.clear()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self._chunks.__anext__(), timeout=timeout
                )
            except StopAsyncIteration:
                return bytes(data)
            except asyncio.TimeoutError:
                raise HTTPException(
                    408,
                    f"The mic has sent nothing for {timeout:g} s and the request "
                    "is still open. Close the body when you stop recording (the "
                    "final zero-length chunk), or raise "
                    "ASK_SILENCE_TIMEOUT_SECONDS.",
                ) from None
            data.extend(chunk)
            if len(data) > max_bytes:
                raise HTTPException(
                    413,
                    f"Audio too long: the cap is {ASK_MAX_SECONDS:g} s "
                    "(ASK_MAX_AUDIO_SECONDS).",
                )


@api.post("/ask", dependencies=[Depends(require_token)],
          responses=_AUDIO_RESPONSE, response_class=Response)
async def ask(
    request: Request,
    deviceId: str = "bonsai-01",
    lang: str | None = None,
    audioFormat: str | None = None,
    sampleRate: int | None = None,
    micRate: int = 16000,
    maxSide: int | None = None,
) -> Response:
    """Photo + spoken question -> spoken answer, in a single request.

    It is `/look` with voice in front: instead of sending the question written,
    the glasses send the photo and then whatever the wearer is saying. Whisper
    turbo on Groq transcribes it and that text becomes the question put to the
    vision model.

    The body is raw, no JSON and no base64 (see `_Frame`):

        [4 length bytes][JPEG photo][mic audio]

    The audio can be raw mono PCM16, which is what the XIAO ESP32-S3 Sense's PDM
    mic gives through I2S (say the rate with `micRate`), or a file with a header
    (WAV, OGG, m4a, MP3): it is detected automatically.

    It uploads in chunks (`Transfer-Encoding: chunked`), which is what lets the
    photo be saved while the person is still talking.

    The response is exactly `/look`'s: raw streaming audio with the text in
    `X-Bonsai-Text`. On top of that, what was understood comes back in
    `X-Bonsai-Transcript`, so you can see why it answered that.

    The voice is not requested: it comes from the language (`lang`), and which
    voice each language gets is decided in `tts.VOICES`.

    Tools work like in `/look`, but since the body is already binary they come
    in the `X-Bonsai-Tools` request header (base64 of the same JSON list), and
    the model's choices come back in the response header of the same name.

    On quota: transcribing does **not** spend Groq's text tokens (Whisper is
    billed per second of audio), so testing `/ask` does not leave you without
    `/look`.
    """
    # Everything that can be validated for free is validated before the device
    # uploads half a megabyte for nothing.
    plan = await _audio_plan(audioFormat, lang, sampleRate)
    tools = _clean_tools(_tools_from_header(request))
    if micRate <= 0:
        raise HTTPException(400, f"micRate must be positive, not {micRate}.")
    if not stt.api_key():
        raise HTTPException(
            500, "GROQ_API_KEY is not configured: /ask needs it to transcribe."
        )

    t_total = time.perf_counter()
    frame = _Frame(request)

    # The photo is saved as soon as it is complete, while the person is still
    # talking: this is where chunked upload pays off. It also leaves a record of
    # what they were looking at if something fails later.
    photo = await frame.photo()
    capture_id, _path = await asyncio.to_thread(memory.save_capture, deviceId, photo)
    t_photo = time.perf_counter()

    # The other prize for having the photo early: shrinking costs ~700 ms of CPU
    # on a 12 MP shot, and right now the server is only waiting for the person
    # to finish. Doing it in a thread meanwhile means it is ready by the time
    # the question arrives and adds nothing to the wait.
    side = maxSide if maxSide is not None else (
        images.MAX_SIDE if images.ENABLED else 0
    )
    resize_task = None
    image_b64 = base64.b64encode(photo).decode("ascii")
    if side > 0:
        async def _shrink() -> tuple[str, int]:
            t = time.perf_counter()
            shrunk, info = await asyncio.to_thread(images.resize, image_b64, side)
            return shrunk, int((time.perf_counter() - t) * 1000) if info.get("resized") else 0

        resize_task = asyncio.create_task(_shrink())

    try:
        audio = await frame.audio(int(ASK_MAX_SECONDS * micRate * 2), ASK_SILENCE)
    except BaseException:
        if resize_task is not None:
            resize_task.cancel()
        raise
    upload_ms = int((time.perf_counter() - t_photo) * 1000)

    resize_ms = resize_wait_ms = 0
    if resize_task is not None:
        t = time.perf_counter()
        image_b64, resize_ms = await resize_task
        # What was left of the resize after the person went quiet. With a
        # couple-of-seconds sentence it is 0: already done.
        resize_wait_ms = int((time.perf_counter() - t) * 1000)

    # Seconds are only known for raw PCM; an m4a or ogg would have to be
    # decoded, so there `seconds` comes back None and all we can check is that
    # something arrived.
    _, _, seconds = stt.wrap(audio, micRate)
    if seconds is not None and seconds < ASK_MIN_SECONDS:
        raise HTTPException(
            400,
            f"Barely any audio ({seconds:.2f} s). Was the button released "
            "before speaking, or is the mic not producing samples?",
        )
    if len(audio) < 256:
        raise HTTPException(
            400, f"Barely any audio ({len(audio)} bytes). Did anything reach us?"
        )

    t0 = time.perf_counter()
    try:
        transcript = await stt.transcribe(
            audio, sample_rate=micRate, lang=(lang or tts.DEFAULT_LANG).lower()
        )
    except VisionRateLimit as e:
        headers = {}
        if e.retry_after is not None:
            headers["Retry-After"] = str(max(1, round(e.retry_after)))
        raise HTTPException(429, str(e), headers=headers) from e
    except Exception as e:
        raise HTTPException(502, f"Failed to transcribe: {describe_error(e)}") from e
    stt_ms = int((time.perf_counter() - t0) * 1000)

    if not transcript:
        # Saved anyway: a repeated empty transcript is the symptom of a
        # misconfigured mic, and this way it shows up in /admin.
        await asyncio.to_thread(
            memory.finish_capture, capture_id,
            audio_secs=round(seconds, 2) if seconds is not None else None,
            stt_ms=stt_ms, transcript="",
        )
        how_much = f"{seconds:.1f} s of" if seconds is not None else f"{len(audio)} bytes of"
        raise HTTPException(
            422,
            f"Nothing was understood in {how_much} audio. If it always happens, "
            "check the mic gain; if it is a one-off, ask again or use /look, "
            "which needs no voice.",
        )

    print(f"\n[ask] request from {deviceId} (lang={lang or tts.DEFAULT_LANG}, micRate={micRate}) | transcribed in {stt_ms} ms: {transcript!r}", flush=True)

    request_model = LookRequest(
        image=image_b64,
        deviceId=deviceId,
        prompt=transcript,
        lang=lang,
        # Already shrunk (or it did not apply): do not let _vision_step redo it.
        maxSide=0,
        tools=tools,
    )
    # Here is the difference with /look: the two wake-word turns are taken as
    # said, so it answers like someone continuing a conversation instead of
    # someone taking a lone order.
    sentence_stream, meta, headers = await _describe_stream(
        request_model, vision.VOICE_PREAMBLE
    )

    headers.update({
        "X-Bonsai-Transcript": base64.b64encode(transcript.encode()).decode("ascii"),
        "X-Bonsai-Stt-Ms": str(stt_ms),
        "X-Bonsai-Stt-Model": stt.MODEL,
        "X-Bonsai-Audio-Bytes": str(len(audio)),
        # How long we waited for the mic once the photo was already in. That is
        # time the person spends talking, not server latency.
        "X-Bonsai-Upload-Ms": str(upload_ms),
        # Shrinking happens while waiting for the mic, so -Resize-Ms is work
        # done "for free" and -Resize-Wait-Ms is the only part that added to the
        # total. Normally 0.
        "X-Bonsai-Resize-Ms": str(resize_ms),
        "X-Bonsai-Resize-Wait-Ms": str(resize_wait_ms),
        "X-Bonsai-Capture-Id": capture_id,
    })
    if seconds is not None:
        headers["X-Bonsai-Audio-Secs"] = f"{seconds:.2f}"

    resp = await _speak_response_stream(sentence_stream, plan, headers, meta)

    total_ms = int((time.perf_counter() - t_total) * 1000)
    await asyncio.to_thread(
        memory.finish_capture, capture_id,
        audio_secs=round(seconds, 2) if seconds is not None else None,
        transcript=transcript, reply=meta.get("text") or "",
        stt_ms=stt_ms, vision_ms=int(meta.get("vision_ms", 0)),
        total_ms=total_ms,
    )
    await asyncio.to_thread(memory.prune_captures, deviceId)

    return resp


# GET as well as POST, and not on a whim: a browser asks for audio with
# `<audio src="...">`, which is always a GET. /docs' player did exactly that,
# got a 405 and sat at 0:00 in silence with the POST body downloaded beside it.
# And it fits: /speak changes nothing, everything it needs is in the query and
# asking twice gives the same thing.
#
# Two decorators and not one api_route(methods=[...]) because with both methods
# on a single route FastAPI generates the same operationId for the two and
# warns about the duplicate.
@api.get("/speak", dependencies=[Depends(require_token)],
         responses=_AUDIO_RESPONSE, response_class=Response)
@api.post("/speak", dependencies=[Depends(require_token)],
          responses=_AUDIO_RESPONSE, response_class=Response)
async def speak(
    text: str = Query(description="The text to say out loud."),
    lang: str = Query(
        default=tts.DEFAULT_LANG, description="Voice language: ca, es or en."
    ),
    audioFormat: str | None = Query(
        default=None,
        # No enum on purpose: a FastAPI 422 would say far less than
        # _audio_plan's 400, which lists what is available.
        description="pcm16 | mulaw | wav. Defaults to wav.",
    ),
    sampleRate: int | None = Query(
        default=None,
        description="8000, 16000 or 22050 Hz. Defaults to the voice model's "
                    "own rate (22050 for the Catalan ones).",
    ),
):
    """Text to speech only. Returns raw audio (handy for the ESP32).

    Works the same with GET and POST: it changes nothing on the server and
    everything it needs is in the query. With GET the URL can go straight into
    an `<audio src="...">` or the address bar, which is what /docs' player does.

    Formats accepted, the same as /look and /ask:

    | audioFormat | What comes out |
    | --- | --- |
    | `pcm16` | Signed 16-bit samples, no header: what the MAX98357A's I2S wants |
    | `mulaw` | 8-bit μ-law, half the bytes of pcm16 |
    | `wav`   | Same as pcm16 but with a RIFF header, for browsers. **The default here** |

    And `sampleRate` is 8000, 16000 or 22050 Hz. Anything else gives a 400 with
    the list, before synthesizing anything.

    Careful with the default format: here it is **wav**, not `pcm16` like in
    /look. It dates from when /speak was only for listening from a browser, and
    it stays so callers who say nothing keep working.

    Useful for making the recorded audios the glasses carry — the «Digue'm»
    that plays while the photo uploads, for instance — in the exact I2S format,
    with no conversions by hand:

        curl -X POST "$API/speak?text=Digue'm!&audioFormat=pcm16&sampleRate=16000" \
             -o digam.pcm

    It arrives in one go (~205 ms, there is nothing to split).

    The response's real format goes in the `X-Bonsai-Format`, `-Rate`, `-Bits`
    and `-Channels` headers, so nothing has to be guessed to set up the I2S.
    """
    plan = await _audio_plan(audioFormat, lang, sampleRate)
    # /speak was born returning WAV with Piper and some callers ask without
    # saying a format: honoured unless something else is requested.
    if audioFormat is None and plan["format"] == "pcm16":
        plan["format"] = "wav"
    return await _speak_response(text, plan, {})


@api.post("/memory", dependencies=[Depends(require_token)])
def add_memory(req: MemoryRequest) -> dict[str, Any]:
    item = memory.add_memory(req.deviceId, req.fact)
    return {"ok": True, "item": item}


@api.get("/memory", dependencies=[Depends(require_token)])
def list_devices() -> dict[str, Any]:
    """Every device with memories, plus the state of the database.

    Without this there was no way to know which deviceIds exist: you had to
    remember them.
    """
    return {"devices": memory.list_devices(), "stats": memory.stats()}


@api.get("/memory/{device_id}", dependencies=[Depends(require_token)])
def get_memories(device_id: str) -> dict[str, Any]:
    return {"deviceId": device_id, "memories": memory.list_memories(device_id)}


@api.patch("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def edit_memory(device_id: str, memory_id: str, req: MemoryEdit) -> dict[str, Any]:
    """Fixes a memory's text without deleting and recreating it."""
    text = req.fact.strip()
    if not text:
        raise HTTPException(400, "A memory cannot be left empty.")
    item = memory.update_memory(device_id, memory_id, text)
    if item is None:
        raise HTTPException(404, "That memory was not found.")
    return {"ok": True, "item": item}


@api.delete("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def remove_memory(device_id: str, memory_id: str) -> dict[str, Any]:
    deleted = memory.delete_memory(device_id, memory_id)
    if not deleted:
        raise HTTPException(404, "That memory was not found.")
    return {"ok": True}


@api.delete("/memory/{device_id}", dependencies=[Depends(require_token)])
def clear_device(device_id: str) -> dict[str, Any]:
    """Empties a whole device."""
    return {"ok": True, "deleted": memory.clear_device(device_id)}


# --------------------------------------------------------------------------
# /audios: the fixed phrases the glasses carry
# --------------------------------------------------------------------------
@api.get("/audios", dependencies=[Depends(require_token)])
def list_audios(lang: str = tts.DEFAULT_LANG) -> dict[str, Any]:
    """What each fixed phrase says in that language.

    The firmware does not need this (it asks /audios/{id} for the audio), but it
    shows at a glance what the device will say.
    """
    if lang.lower() not in tts.languages():
        raise HTTPException(
            400, f"Unknown language: {lang!r}. Available: {', '.join(tts.languages())}."
        )
    return {
        "lang": lang.lower(),
        "audios": audios.texts_of(lang),
        # Untranslated phrases show up here instead of coming out in another one.
        "missing": [a for a in audios.ids() if audios.text(a, lang) is None],
    }


@api.get("/audios/{audio_id}", dependencies=[Depends(require_token)],
         responses=_AUDIO_RESPONSE, response_class=Response)
async def audio_clip(
    audio_id: str,
    lang: str = tts.DEFAULT_LANG,
    audioFormat: str | None = None,
    sampleRate: int | None = None,
):
    """One of those phrases as audio, for the device to keep on its SD card.

    It is /speak with the text supplied by the server: the glasses ask for
    /audios/start_talking?lang=ca and carry no text of their own.
    """
    text = audios.text(audio_id, lang)
    if text is None:
        if audio_id not in audios.AUDIOS:
            raise HTTPException(
                404,
                f"There is no audio {audio_id!r}. Available: {', '.join(audios.ids())}.",
            )
        raise HTTPException(
            404,
            f"The audio {audio_id!r} is not in {lang!r}. It exists in: "
            f"{', '.join(audios.languages_of(audio_id))}. Add it in audios.py.",
        )

    plan = await _audio_plan(audioFormat, lang, sampleRate)
    # Same as /speak: whoever asks without a format is saving it to a file.
    if audioFormat is None and plan["format"] == "pcm16":
        plan["format"] = "wav"
    return await _speak_response(text, plan, {"X-Bonsai-Audio": audio_id})


# --------------------------------------------------------------------------
# Mounting the routes
# --------------------------------------------------------------------------
# /api/v1/look, /api/v1/ask, /api/v1/health...
app.include_router(api, prefix=API_PREFIX)
