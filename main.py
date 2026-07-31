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
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

import memory
import tts
import vision
from vision import VisionRateLimit, describe_error

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
async def _startup() -> None:
    memory.init_db()
    # Abre la conexión TLS con el proveedor por defecto antes de la primera
    # foto: son ~220 ms que, si no, los paga quien lleve las gafas.
    await vision.warmup()
    # Y baja/carga el modelo de Piper: ~63 MB una vez y ~1,2 s de carga que
    # tampoco tiene por qué pagar la primera foto.
    await tts.warmup()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await vision.aclose()


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
    # 'gemini' o 'groq'. Si no se dice nada, el de VISION_PROVIDER (gemini).
    provider: str | None = None
    # 'piper' o 'edge'. Si no se dice nada, el de TTS_PROVIDER (piper).
    tts: str | None = None


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
        "Eres el asistente de visión de las gafas Bonsai: le cuentas a quien "
        "las lleva lo que tiene delante, sea para orientarse, para leer algo, "
        "para identificar un objeto o simplemente por curiosidad.",
        f"Hoy es {today}.",
        # La respuesta se convierte en voz: cada frase de más son segundos de
        # espera. De ahí la insistencia en la brevedad.
        "Contesta en 1 o 2 frases cortas, como si se lo dijeras a alguien de "
        "viva voz: lo importante primero y nada de relleno. No empieces con "
        "«en la imagen se ve» ni parecidos, ni describas el fondo o detalles "
        "irrelevantes. Si hay texto legible o algo que suponga un peligro, eso "
        "es lo prioritario. Si te hacen una pregunta concreta, responde solo a "
        "esa pregunta. "
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
@app.get("/probar", response_class=HTMLResponse)
def probar() -> HTMLResponse:
    """Página de prueba para el móvil. Sin token: es solo HTML.

    Se sirve desde el propio backend para que no haya CORS ni haya que montar
    nada aparte: se abre la IP del servidor en el móvil y ya está.
    """
    pagina = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "probar.html")
    try:
        with open(pagina, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, "Falta static/probar.html en el servidor.") from None


@app.get("/health")
def health() -> dict[str, Any]:
    """Sin token: lo usa el healthcheck de Docker."""
    return {
        "ok": True,
        "authRequired": bool(API_TOKEN),
        "defaultProvider": vision.DEFAULT_PROVIDER,
        "providers": {
            p: {
                "model": vision.model_for(p),
                "keyConfigured": bool(vision.api_key_for(p)),
            }
            for p in vision.PROVIDERS
        },
        "tts": {
            # "configured" es lo que se pidió y "active" lo que se usará: si
            # Piper falló y se está tirando de edge-tts, aquí se ve.
            "configured": tts.DEFAULT_PROVIDER,
            "active": tts.effective_provider(),
            "format": tts.format_for(),
            "piper": tts.piper_status(),
        },
    }


@app.post("/describe", dependencies=[Depends(require_token)])
async def describe(req: DescribeRequest) -> dict[str, Any]:
    try:
        provider = vision.resolve(req.provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    api_key = vision.api_key_for(provider)
    if not api_key:
        variable = "GEMINI_API_KEY" if provider == "gemini" else "GROQ_API_KEY"
        raise HTTPException(
            500, f"{variable} no está configurada en el servidor (proveedor: {provider})."
        )

    lang = (req.lang or tts.DEFAULT_LANG).lower()
    timings: dict[str, int] = {}

    # 1. Contexto (memoria)
    t0 = time.perf_counter()
    memory_context = memory.get_memory_context(req.deviceId)
    timings["memoria_ms"] = int((time.perf_counter() - t0) * 1000)

    # 2. Visión
    t0 = time.perf_counter()
    try:
        description = await vision.describe_image(
            provider=provider,
            api_key=api_key,
            image_base64=req.image,
            system_prompt=build_system_prompt(lang, memory_context),
            user_prompt=req.prompt or "¿Qué tengo delante? Dímelo en una o dos frases.",
        )
    except VisionRateLimit as e:
        # 429 y no 502: no es que el servidor falle, es que hay que esperar.
        # Así el cliente puede reintentar solo en vez de dar error a la persona.
        cabeceras = {}
        if e.retry_after is not None:
            cabeceras["Retry-After"] = str(max(1, round(e.retry_after)))
        raise HTTPException(429, str(e), headers=cabeceras) from e
    except Exception as e:
        # describe_error y no str(e): los timeouts de httpx tienen el mensaje
        # vacío y el cliente recibía «Fallo al describir la imagen: » a secas.
        raise HTTPException(
            502, f"Fallo al describir la imagen: {describe_error(e)}"
        ) from e
    timings["vision_ms"] = int((time.perf_counter() - t0) * 1000)

    if not description:
        raise HTTPException(502, "El modelo de visión devolvió una respuesta vacía.")

    result: dict[str, Any] = {
        "text": description,
        "lang": lang,
        "provider": provider,
        "model": vision.model_for(provider),
    }

    # 3. Voz (opcional)
    if req.audio:
        try:
            tts_provider = tts.effective_provider(req.tts)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

        t0 = time.perf_counter()
        voice = tts.voice_for(lang, req.voice, tts_provider)
        try:
            audio_bytes = await tts.synthesize(description, voice, tts_provider)
        except Exception as e:
            raise HTTPException(
                502, f"Fallo al generar el audio: {describe_error(e)}"
            ) from e
        timings["tts_ms"] = int((time.perf_counter() - t0) * 1000)

        result["audio"] = base64.b64encode(audio_bytes).decode("ascii")
        # No va fijo a "mp3": Piper devuelve WAV y edge-tts, MP3.
        result["audioFormat"] = tts.format_for(tts_provider)
        result["voice"] = voice
        result["tts"] = tts_provider

    result["timings"] = timings
    return result


@app.post("/speak", dependencies=[Depends(require_token)])
async def speak(
    text: str,
    lang: str = tts.DEFAULT_LANG,
    voice: str | None = None,
    tts_provider: str | None = None,
):
    """Solo texto a voz. Devuelve el audio en crudo (útil para la ESP32).

    Con Piper llega de una vez (~205 ms, no hay nada que trocear). Con edge-tts
    va por trozos, así que se puede empezar a reproducir a los ~1,1 s en vez de
    esperar el MP3 entero.

    El Content-Type dice el formato: audio/wav con Piper, audio/mpeg con
    edge-tts.
    """
    try:
        proveedor = tts.effective_provider(tts_provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    trozos = tts.stream(text, tts.voice_for(lang, voice, proveedor), proveedor)

    # Pedimos el primer trozo antes de empezar a responder: si el TTS falla,
    # aún estamos a tiempo de devolver un error de verdad en vez de un 200 con
    # el cuerpo vacío.
    try:
        primero = await anext(trozos)
    except StopAsyncIteration:
        raise HTTPException(502, f"{proveedor} no devolvió audio.") from None
    except Exception as e:
        raise HTTPException(
            502, f"Fallo al generar el audio: {describe_error(e)}"
        ) from e

    async def cuerpo():
        yield primero
        async for trozo in trozos:
            yield trozo

    return StreamingResponse(cuerpo(), media_type=tts.media_type_for(proveedor))


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
async def voices(prefix: str = "", tts_provider: str | None = None) -> dict[str, Any]:
    """Lista las voces disponibles, p. ej. /voices?prefix=ca

    Con Piper son los modelos que hay en disco; con edge-tts, el catálogo de
    Microsoft.
    """
    try:
        proveedor = tts.effective_provider(tts_provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "tts": proveedor,
        "voices": await tts.list_available_voices(prefix, proveedor),
    }
