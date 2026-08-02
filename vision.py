"""Lo común a todos los proveedores de visión (Groq, Gemini).

Aquí vive lo que no depende de quién describa la imagen: el cliente HTTP
compartido, la excepción de cuota agotada y la detección del formato de la
imagen. Cada proveedor vive en su propio módulo (`groq_vision`,
`gemini_vision`) y expone la misma función `describe_image`.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

# Proveedor por defecto: Groq, porque es el más rápido y sobre todo el más
# regular. Medido con la misma imagen a 896 px: 552 ms de visión (551-554)
# frente a los 844 ms de Gemini (649-937).
#
# Su pega es la cuota: 8.000 tokens/minuto son unas 3 fotos por minuto. Para
# desarrollar sin pelearse con el 429, VISION_PROVIDER=gemini.
DEFAULT_PROVIDER = os.environ.get("VISION_PROVIDER", "groq").lower()

PROVIDERS = ("gemini", "groq")


class VisionRateLimit(Exception):
    """Se ha agotado la cuota del proveedor (429).

    Se distingue del resto de errores para poder devolver un 429 al cliente,
    con el rato que hay que esperar, en vez de un 502 genérico que parece un
    fallo del servidor cuando en realidad solo hay que esperar.
    """

    def __init__(self, mensaje: str, retry_after: float | None = None) -> None:
        super().__init__(mensaje)
        self.retry_after = retry_after


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
# Groq quiere un data URL y Gemini un `mimeType` aparte, pero los dos
# necesitan saber el formato de verdad: mandar un PNG diciendo que es JPEG
# funciona de milagro, no por diseño.
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
# Registro de proveedores
# --------------------------------------------------------------------------
def _modulo(provider: str) -> Any:
    # Importación diferida: los módulos de proveedor importan este, así que
    # hacerlo arriba sería un import circular.
    if provider == "groq":
        import groq_vision

        return groq_vision
    if provider == "gemini":
        import gemini_vision

        return gemini_vision
    raise ValueError(
        f"Proveedor de visión desconocido: {provider!r}. Usa uno de: {', '.join(PROVIDERS)}"
    )


def resolve(provider: str | None) -> str:
    """Normaliza el proveedor pedido y comprueba que existe."""
    elegido = (provider or DEFAULT_PROVIDER).lower()
    if elegido not in PROVIDERS:
        raise ValueError(
            f"Proveedor de visión desconocido: {elegido!r}. Usa uno de: {', '.join(PROVIDERS)}"
        )
    return elegido


def model_for(provider: str) -> str:
    return _modulo(resolve(provider)).MODEL


def api_key_for(provider: str) -> str:
    return _modulo(resolve(provider)).api_key()


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
    provider: str | None,
    image_base64: str,
    system_prompt: str,
    user_prompt: str,
    api_key: str | None = None,
    timeout: float = 30.0,
    preamble: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """Describe la imagen con el proveedor pedido (o el de por defecto).

    `preamble` son turnos ("user"/"assistant", texto) que se insertan entre el
    system prompt y el mensaje con la imagen. Lo usa /ask para dar por dicha la
    palabra de activación (ver PREAMBULO_VEU).
    """
    elegido = resolve(provider)
    modulo = _modulo(elegido)
    return await modulo.describe_image(
        api_key=api_key or modulo.api_key(),
        image_base64=image_base64,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout=timeout,
        preamble=preamble,
    )


async def warmup(provider: str | None = None) -> bool:
    """Abre la conexión TLS antes de la primera foto.

    El saludo TLS son ~220 ms que, si no se hace esto, los paga la primera
    persona que use las gafas. Pide un listado de modelos: no gasta ni un
    token de la cuota.
    """
    elegido = resolve(provider)
    modulo = _modulo(elegido)
    clave = modulo.api_key()
    if not clave:
        return False
    try:
        await get_client().get(
            modulo.WARMUP_URL,
            headers=modulo.auth_headers(clave),
            timeout=10.0,
        )
        return True
    except Exception:
        # Es un calentamiento: si falla, la primera petición real ya abrirá la
        # conexión. No es motivo para no arrancar.
        return False
