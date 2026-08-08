"""Visión: describe una imagen con el modelo de Groq.

Además del cliente de Groq, aquí vive lo común que usan otros módulos: el
cliente HTTP compartido (`stt` y `tts` también lo reutilizan), la
excepción de cuota agotada y la detección del formato de la imagen.

Hubo un segundo proveedor (Gemini) y un módulo aparte por cada uno para poder
cambiarlos. Se quitó: Groq es más rápido y sobre todo más regular (552 ms de
visión frente a 844 ms, con la misma imagen), y mantener la capa de reparto
para una sola rama solo daba pie a que se quedara desfasada.
"""

from __future__ import annotations

import base64
import os
import re

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Sirve para abrir la conexión TLS sin gastar tokens (ver warmup).
WARMUP_URL = "https://api.groq.com/openai/v1/models"

# Comprueba el nombre vigente en https://console.groq.com/docs/models
# (Groq renombra y retira modelos con frecuencia).
MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Groq no siempre manda la cabecera retry-after, pero sí dice el rato en el
# texto del error: "Please try again in 16.56s" (o "in 12m39.024s").
_ESPERA_RE = re.compile(r"try again in\s+(?:(\d+)m)?([\d.]+)s", re.IGNORECASE)


class VisionRateLimit(Exception):
    """Se ha agotado la cuota de Groq (429).

    Se distingue del resto de errores para poder devolver un 429 al cliente,
    con el rato que hay que esperar, en vez de un 502 genérico que parece un
    fallo del servidor cuando en realidad solo hay que esperar.
    """

    def __init__(self, mensaje: str, retry_after: float | None = None) -> None:
        super().__init__(mensaje)
        self.retry_after = retry_after


# Se mantiene el nombre antiguo por si algo de fuera lo importa.
GroqRateLimit = VisionRateLimit


def describe_error(e: BaseException) -> str:
    """Texto para el cliente que nunca sale vacío.

    Los timeouts de httpx tienen `str(e)` vacío, así que sin esto el error que
    llegaba era «Fallo al describir la imagen: » y no había forma de saber qué
    había pasado.
    """
    return str(e) or type(e).__name__


# --------------------------------------------------------------------------
# Formato de la imagen
# --------------------------------------------------------------------------
# El data URL tiene que decir el formato de verdad: mandar un PNG diciendo que
# es JPEG funciona de milagro, no por diseño.
_FIRMAS = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(image_base64: str) -> str:
    """Detecta el formato mirando la cabecera, sin descodificar todo.

    Descodificar 4 MB de base64 solo para leer 8 bytes serían decenas de ms
    por petición, así que basta con los primeros 16 caracteres (12 bytes).
    """
    try:
        cabecera = base64.b64decode(image_base64[:16], validate=False)
    except Exception:
        return "image/jpeg"

    for firma, mime in _FIRMAS:
        if cabecera.startswith(firma):
            return mime
    if cabecera[:4] == b"RIFF" and cabecera[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# --------------------------------------------------------------------------
# Cliente HTTP compartido
# --------------------------------------------------------------------------
# Un solo cliente para todo el proceso, en vez de uno nuevo por petición:
# abrir la conexión TLS cuesta ~220 ms medidos, y así se paga una vez y no en
# cada foto. `retries` cubre el caso de que el servidor haya cerrado la
# conexión que teníamos guardada mientras no se usaba.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
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


# --------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------
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


# Turnos de conversación que van delante de la pregunta y la imagen.
#
# No son inventados: es lo que ha pasado de verdad justo antes. La persona ha
# dicho la palabra de activación y las gafas le han contestado con un clip ya
# grabado mientras subía la foto. Dándoselos por dichos, el modelo responde
# como quien continúa una conversación y no como quien recibe una orden.
#
# Van en variables de entorno porque tienen que coincidir con lo que hace el
# firmware: si cambias el clip que suena en las gafas, cambia también esto o
# le estarás contando al modelo una conversación que no ha ocurrido.
PREAMBULO_VEU: tuple[tuple[str, str], ...] = (
    ("user", os.environ.get("ASK_WAKE_PHRASE", "Hey Bonsai!")),
    ("assistant", os.environ.get("ASK_WAKE_REPLY", "Diga’m!")),
)


async def describe_image(
    api_key: str,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float = 30.0,
    preamble: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """Describe la imagen con el modelo de visión de Groq.

    `preamble` son turnos ("user"/"assistant", texto) que se insertan entre el
    system prompt y el mensaje con la imagen. Lo usa /ask para dar por dicha la
    palabra de activación (ver PREAMBULO_VEU).
    """
    # Turnos previos, si los hay: van entre el system y el mensaje con la
    # imagen. Aquí el formato es el de OpenAI, así que valen tal cual.
    previos = [{"role": rol, "content": texto} for rol, texto in (preamble or ())]

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
            *previos,
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


async def warmup() -> bool:
    """Abre la conexión TLS antes de la primera foto.

    El saludo TLS son ~220 ms que, si no se hace esto, los paga la primera
    persona que use las gafas. Pide un listado de modelos: no gasta ni un
    token de la cuota.
    """
    clave = api_key()
    if not clave:
        return False
    try:
        await get_client().get(
            WARMUP_URL,
            headers=auth_headers(clave),
            timeout=10.0,
        )
        return True
    except Exception:
        # Es un calentamiento: si falla, la primera petición real ya abrirá la
        # conexión. No es motivo para no arrancar.
        return False
