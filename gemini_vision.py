"""Cliente de la API de Gemini (Google AI Studio) para describir imágenes.

Misma interfaz que `groq_vision`, así que `vision.py` puede usar uno u otro
sin que `main.py` se entere.

Por qué es el proveedor por defecto: la capa gratuita de Google es mucho más
holgada que la de Groq (250.000 tokens/minuto y 1.500 peticiones/día frente a
8.000 tokens/minuto y 200.000 al día). Ojo con la contrapartida: en la capa
gratuita Google entrena con lo que le mandas; en la de pago, no.
"""

from __future__ import annotations

import os
import re

import httpx

from vision import VisionRateLimit, get_client, sniff_mime

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Comprueba los nombres vigentes en https://ai.google.dev/gemini-api/docs/models
# En la capa gratuita están `gemini-3.1-flash-lite` y `gemini-3.5-flash`.
# Por defecto la Lite: para una descripción de una o dos frases es la más
# rápida, que es lo que importa aquí. Si necesitas más calidad de lectura de
# texto o de escenas complicadas, prueba `gemini-3.5-flash`.
MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-3.1-flash-lite")

# El equivalente al `reasoning_effort: none` de Groq: para describir una foto
# el razonamiento paso a paso no aporta nada. Y no es cosmético: medido con
# thinkingLevel "medium", el modelo gasta ~140 tokens pensando, se come los 150
# de maxOutputTokens y devuelve la frase cortada (finishReason MAX_TOKENS).
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "minimal")

# Cuántos tokens gasta la imagen. A diferencia de Groq, aquí el tamaño de la
# imagen NO cambia el precio: Gemini la normaliza y cobra fijo. Medido con
# gemini-3.1-flash-lite y la misma foto:
#
#   MEDIA_RESOLUTION_LOW     286 tokens  — pero confundió la plaza de la foto
#   MEDIA_RESOLUTION_MEDIUM  577 tokens  — misma respuesta que HIGH
#   MEDIA_RESOLUTION_HIGH  1.133 tokens  — es lo que usa si no dices nada
#
# Se deja el valor de la API por defecto: bajarlo es la forma de estirar la
# cuota, pero con LOW ya se vio perder precisión y esto son unas gafas para
# orientarse. Ponlo a MEDIUM si necesitas el doble de pruebas por cuota.
MEDIA_RESOLUTION = os.environ.get("GEMINI_MEDIA_RESOLUTION", "").upper()

# Sirve para abrir la conexión TLS sin gastar tokens (ver vision.warmup).
WARMUP_URL = f"{BASE_URL}/models"

# Gemini manda el rato que hay que esperar en un RetryInfo dentro de
# error.details, con formato "27s" o "1.5s".
_RETRY_RE = re.compile(r'"retryDelay"\s*:\s*"([\d.]+)s"')

# Si el modelo no acepta thinkingLevel, la API responde 400. Lo recordamos
# para no pagar el reintento en cada foto.
_sin_thinking_level = False


def api_key() -> str:
    return os.environ.get("GEMINI_API_KEY", "")


def auth_headers(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key, "Content-Type": "application/json"}


def _segundos_de_espera(resp: httpx.Response) -> float | None:
    cabecera = resp.headers.get("retry-after")
    if cabecera:
        try:
            return float(cabecera)
        except ValueError:
            pass
    m = _RETRY_RE.search(resp.text)
    return float(m.group(1)) if m else None


def _payload(
    image_base64: str, system_prompt: str, user_prompt: str, con_thinking: bool
) -> dict:
    generation: dict = {
        "temperature": 0.4,
        # Techo de seguridad: la respuesta se lee en voz alta, así que una
        # respuesta larga son segundos de espera. El límite real lo pone el
        # prompt (1-2 frases); esto solo evita que se desmadre.
        "maxOutputTokens": 150,
        "candidateCount": 1,
    }
    if con_thinking:
        # Va anidado en thinkingConfig. Suelto en generationConfig la API lo
        # rechaza con 400 ("Unknown name thinkingLevel"), comprobado contra
        # gemini-3.1-flash-lite.
        generation["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL}
    if MEDIA_RESOLUTION:
        # Acepta tanto "LOW" como "MEDIA_RESOLUTION_LOW", que es lo que
        # espera la API.
        res = MEDIA_RESOLUTION
        generation["mediaResolution"] = (
            res if res.startswith("MEDIA_RESOLUTION_") else f"MEDIA_RESOLUTION_{res}"
        )

    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": user_prompt},
                    {
                        "inlineData": {
                            "mimeType": sniff_mime(image_base64),
                            "data": image_base64,
                        }
                    },
                ],
            }
        ],
        "generationConfig": generation,
    }


def _texto_de_la_respuesta(data: dict) -> str:
    """Saca el texto y convierte los casos raros en errores con sentido."""
    bloqueo = (data.get("promptFeedback") or {}).get("blockReason")
    if bloqueo:
        raise RuntimeError(f"Gemini bloqueó la petición ({bloqueo}).")

    candidatos = data.get("candidates") or []
    if not candidatos:
        raise RuntimeError("Gemini no devolvió ningún candidato.")

    candidato = candidatos[0]
    partes = (candidato.get("content") or {}).get("parts") or []
    texto = "".join(
        p["text"] for p in partes if isinstance(p.get("text"), str)
    ).strip()
    if texto:
        return texto

    # Sin texto: el motivo está en finishReason y conviene decirlo, porque
    # cada caso se arregla de forma distinta.
    motivo = candidato.get("finishReason") or "desconocido"
    if motivo == "MAX_TOKENS":
        raise RuntimeError(
            "Gemini agotó maxOutputTokens sin llegar a escribir la respuesta "
            "(prueba a subir maxOutputTokens o a bajar thinkingLevel)."
        )
    raise RuntimeError(f"Gemini devolvió una respuesta vacía ({motivo}).")


async def describe_image(
    api_key: str,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
) -> str:
    global _sin_thinking_level

    url = f"{BASE_URL}/models/{MODEL}:generateContent"
    resp = await get_client().post(
        url,
        json=_payload(
            image_base64, system_prompt, user_prompt, not _sin_thinking_level
        ),
        headers=auth_headers(api_key),
        timeout=timeout,
    )

    # Modelo que no soporta thinkingLevel: se reintenta una vez sin él y se
    # recuerda, para no pagar dos peticiones en cada foto.
    if (
        resp.status_code == 400
        and not _sin_thinking_level
        and "thinking" in resp.text.lower()
    ):
        _sin_thinking_level = True
        resp = await get_client().post(
            url,
            json=_payload(image_base64, system_prompt, user_prompt, False),
            headers=auth_headers(api_key),
            timeout=timeout,
        )

    if resp.status_code == 429:
        raise VisionRateLimit(
            f"Cuota de Gemini agotada: {resp.text[:300]}",
            _segundos_de_espera(resp),
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"Error de Gemini ({resp.status_code}): {resp.text[:300]}")

    return _texto_de_la_respuesta(resp.json())
