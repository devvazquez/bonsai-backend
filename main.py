"""Backend de Bonsai: un único endpoint que orquesta visión + voz + memoria.

La ESP32 (o la app web) solo tiene que hacer POST /describe con la imagen;
aquí se añade contexto (fecha + recuerdos), se pide la descripción a Groq y se
convierte a audio con edge-tts.
"""

from __future__ import annotations

import base64
import os
import time
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

import memory
import tts
from groq_vision import describe_image

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Token para proteger el endpoint. Al estar expuesto a internet, sin esto
# cualquiera que descubra la URL podría gastar tu cuota de Groq.
# Si se deja vacío no se exige (cómodo en local, NO recomendable en la VPS).
API_TOKEN = os.environ.get("BONSAI_API_TOKEN", "")

# Orígenes permitidos para el navegador. En producción conviene poner el
# dominio de la app web en vez de "*".
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

app = FastAPI(title="Bonsai Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """Comprueba la cabecera X-API-Token si hay token configurado."""
    if not API_TOKEN:
        return
    if x_api_token != API_TOKEN:
        raise HTTPException(401, "Token inválido o ausente (cabecera X-API-Token).")


@app.on_event("startup")
def _startup() -> None:
    memory.init_db()


# --------------------------------------------------------------------------
# Modelos de petición
# --------------------------------------------------------------------------
class DescribeRequest(BaseModel):
    image: str = Field(..., description="Imagen en base64, SIN el prefijo data:")
    deviceId: str
    prompt: str | None = None
    lang: str | None = None   # 'ca', 'es', 'en'
    voice: str | None = None  # fuerza una voz concreta de edge-tts
    audio: bool = True        # a False, solo devuelve el texto (más rápido)


class MemoryRequest(BaseModel):
    deviceId: str
    fact: str


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
LANG_NAMES = {"ca": "catalán", "es": "castellano", "en": "inglés"}


def build_system_prompt(lang: str, memory_context: str) -> str:
    today = datetime.now().strftime("%A, %d de %B de %Y")
    lang_name = LANG_NAMES.get(lang, "catalán")

    parts = [
        "Eres el asistente de visión de las gafas Bonsai, pensadas para ayudar "
        "a personas con discapacidad visual a entender su entorno.",
        f"Hoy es {today}.",
        "Describe las imágenes de forma clara, breve y natural, priorizando lo "
        "que sea relevante o útil para la persona (obstáculos, texto visible, "
        "personas, etc.). Ve al grano: 2-3 frases salvo que se te pida más. "
        f"Responde SIEMPRE en {lang_name}.",
    ]
    if memory_context:
        parts.append(
            "Cosas que la persona te ha pedido recordar previamente:\n"
            + memory_context
        )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    """Sin token: lo usa el healthcheck de Docker."""
    return {
        "ok": True,
        "groqKeyConfigured": bool(GROQ_API_KEY),
        "authRequired": bool(API_TOKEN),
    }


@app.post("/describe", dependencies=[Depends(require_token)])
async def describe(req: DescribeRequest) -> dict[str, Any]:
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY no está configurada en el servidor.")

    lang = (req.lang or tts.DEFAULT_LANG).lower()
    timings: dict[str, int] = {}

    # 1. Contexto (memoria)
    t0 = time.perf_counter()
    memory_context = memory.get_memory_context(req.deviceId)
    timings["memoria_ms"] = int((time.perf_counter() - t0) * 1000)

    # 2. Visión
    t0 = time.perf_counter()
    try:
        description = await describe_image(
            api_key=GROQ_API_KEY,
            image_base64=req.image,
            system_prompt=build_system_prompt(lang, memory_context),
            user_prompt=req.prompt or "Describe lo que ves de forma breve y útil.",
        )
    except Exception as e:
        raise HTTPException(502, f"Fallo al describir la imagen: {e}") from e
    timings["vision_ms"] = int((time.perf_counter() - t0) * 1000)

    if not description:
        raise HTTPException(502, "El modelo de visión devolvió una respuesta vacía.")

    result: dict[str, Any] = {"text": description, "lang": lang}

    # 3. Voz (opcional)
    if req.audio:
        t0 = time.perf_counter()
        voice = tts.voice_for(lang, req.voice)
        try:
            audio_bytes = await tts.synthesize(description, voice)
        except Exception as e:
            raise HTTPException(502, f"Fallo al generar el audio: {e}") from e
        timings["tts_ms"] = int((time.perf_counter() - t0) * 1000)

        result["audio"] = base64.b64encode(audio_bytes).decode("ascii")
        result["audioFormat"] = "mp3"
        result["voice"] = voice

    result["timings"] = timings
    return result


@app.post("/speak", dependencies=[Depends(require_token)])
async def speak(text: str, lang: str = tts.DEFAULT_LANG, voice: str | None = None):
    """Solo texto a voz. Devuelve el MP3 en crudo (útil para la ESP32)."""
    audio_bytes = await tts.synthesize(text, tts.voice_for(lang, voice))
    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.post("/memory", dependencies=[Depends(require_token)])
def add_memory(req: MemoryRequest) -> dict[str, Any]:
    item = memory.add_memory(req.deviceId, req.fact)
    return {"ok": True, "item": item}


@app.get("/memory/{device_id}", dependencies=[Depends(require_token)])
def get_memories(device_id: str) -> dict[str, Any]:
    return {"memories": memory.list_memories(device_id)}


@app.delete("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def remove_memory(device_id: str, memory_id: str) -> dict[str, Any]:
    deleted = memory.delete_memory(device_id, memory_id)
    if not deleted:
        raise HTTPException(404, "No se encontró ese recuerdo.")
    return {"ok": True}


@app.get("/voices", dependencies=[Depends(require_token)])
async def voices(prefix: str = "") -> dict[str, Any]:
    """Lista las voces disponibles, p. ej. /voices?prefix=ca"""
    return {"voices": await tts.list_available_voices(prefix)}
