"""Cliente de la API de Groq para describir imágenes (modelo de visión).

Misma interfaz que `gemini_vision`, así que `vision.py` puede usar uno u otro
sin que `main.py` se entere.

Es el proveedor más rápido de los dos, pero su capa gratuita es muy justa
(8.000 tokens/minuto y 200.000 al día, y el límite es por organización, no por
API key), así que no es el de por defecto.
"""

from __future__ import annotations

import os
import re

import httpx

from vision import VisionRateLimit, get_client, sniff_mime

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Sirve para abrir la conexión TLS sin gastar tokens (ver vision.warmup).
WARMUP_URL = "https://api.groq.com/openai/v1/models"

# Comprueba el nombre vigente en https://console.groq.com/docs/models
# (Groq renombra y retira modelos con frecuencia).
MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Groq no siempre manda la cabecera retry-after, pero sí dice el rato en el
# texto del error: "Please try again in 16.56s" (o "in 12m39.024s").
_ESPERA_RE = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)


# Se mantiene el nombre antiguo: era lo que capturaba main.py y puede haber
# código fuera que lo importe. Ahora es la excepción compartida.
GroqRateLimit = VisionRateLimit


def api_key() -> str:
    return os.environ.get("GROQ_API_KEY", "")


def auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _segundos_de_espera(resp: httpx.Response) -> float | None:
    cabecera = resp.headers.get("retry-after")
    if cabecera:
        try:
            return float(cabecera)
        except ValueError:
            pass
    m = _ESPERA_RE.search(resp.text)
    if m:
        minutos = float(m.group(1) or 0)
        return minutos * 60 + float(m.group(2))
    return None


async def describe_image(
    api_key: str,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
) -> str:
    payload = {
        "model": MODEL,
        "temperature": 0.4,
        # Techo de seguridad: la respuesta se lee en voz alta, así que una
        # respuesta larga son segundos de espera. El límite real lo pone el
        # prompt (1-2 frases); esto solo evita que se desmadre.
        "max_completion_tokens": 150,
        # Desactiva el razonamiento paso a paso: para describir una imagen no
        # aporta nada y solo añade latencia y tokens.
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            # El formato se detecta de verdad: antes iba
                            # "image/jpeg" fijo aunque la imagen fuese PNG.
                            "url": f"data:{sniff_mime(image_base64)};base64,{image_base64}"
                        },
                    },
                ],
            },
        ],
    }

    resp = await get_client().post(
        GROQ_URL,
        json=payload,
        headers=auth_headers(api_key),
        timeout=timeout,
    )

    if resp.status_code == 429:
        raise VisionRateLimit(
            f"Cuota de Groq agotada: {resp.text[:300]}",
            _segundos_de_espera(resp),
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"Error de Groq ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    # Red de seguridad por si el modelo ignora reasoning_format.
    return _THINK_RE.sub("", raw).strip()
