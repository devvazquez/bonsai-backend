"""Cliente de la API de Groq para describir imágenes (modelo de visión)."""

from __future__ import annotations

import os
import re

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Comprueba el nombre vigente en https://console.groq.com/docs/models
# (Groq renombra y retira modelos con frecuencia).
MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Un solo cliente para todo el proceso, en vez de uno nuevo por petición:
# abrir la conexión TLS con api.groq.com cuesta ~220 ms medidos, y así se paga
# una vez y no en cada foto. `retries` cubre el caso de que Groq haya cerrado
# la conexión que teníamos guardada mientras no se usaba.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=2),
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300),
        )
    return _client


async def aclose() -> None:
    """Cierra la conexión al apagar el servidor."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


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
                            "url": f"data:image/jpeg;base64,{image_base64}"
                        },
                    },
                ],
            },
        ],
    }

    resp = await _get_client().post(
        GROQ_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"Error de Groq ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    # Red de seguridad por si el modelo ignora reasoning_format.
    return _THINK_RE.sub("", raw).strip()
