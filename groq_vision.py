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
        "max_completion_tokens": 400,
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

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            GROQ_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"Error de Groq ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    # Red de seguridad por si el modelo ignora reasoning_format.
    return _THINK_RE.sub("", raw).strip()
