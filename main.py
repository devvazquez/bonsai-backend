"""Backend de Bonsai: orquesta visión + voz + memoria en una sola petición.

`/look` es el endpoint principal y lo llama la ESP32-S3 directamente: recibe la
foto, la reduce, le añade contexto (fecha y recuerdos del dispositivo), pide la
descripción al modelo de visión (`vision.py`, Groq) y devuelve el audio en
crudo y en streaming, listo para el I2S del MAX98357A, sin base64 ni nada que
descodificar en el microcontrolador.

`/ask` es el mismo camino pero con voz por delante: la foto y, a continuación,
lo que está diciendo quien lleva las gafas. Se transcribe con Whisper turbo en
Groq (`stt.py`) y esa frase pasa a ser la pregunta que se le hace al modelo de
visión.

El resto es servicio: `/memory` para los recuerdos, `/speak` para texto a voz
suelto, la página `/provar` para probarlo desde el móvil y el panel `/admin`
(`panel.py`) para administrar la base de datos. No hay ninguna aplicación web
en el camino principal.

Todo lo que es API cuelga de `/api/v1` (`/api/v1/look`, `/api/v1/ask`...). Las
páginas no: `/provar` y `/admin` se abren en el navegador y no tienen versión.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import clips
import imagen
import memory
import stt
import tts
import vision
from vision import VisionRateLimit, describe_error

# Token para proteger el endpoint. Al estar expuesto a internet, sin esto
# cualquiera que descubra la URL podría gastar tu cuota del proveedor de visión.
# Si se deja vacío no se exige (cómodo en local, NO recomendable en la VPS).
API_TOKEN = os.environ.get("BONSAI_API_TOKEN", "")

# Orígenes permitidos para el navegador. En producción conviene poner el
# dominio de la app web en vez de "*".
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()
]

app = FastAPI(title="Bonsai Backend", version="2.0.0")

# --------------------------------------------------------------------------
# Versión de la API
# --------------------------------------------------------------------------
# Todo lo que es API cuelga de /api/v1. Así, el día que haga falta cambiar un
# formato de respuesta o el cuerpo de /ask, se monta /api/v2 al lado y las
# gafas que hay por ahí siguen funcionando hasta que se les actualice el
# firmware.
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

# Las rutas se declaran aquí y al final del fichero se montan con el prefijo.
# No hay alias sin prefijo: /look a secas devuelve 404.
api = APIRouter()

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
    # Abre la conexión TLS con Groq antes de la primera foto: son ~220 ms que,
    # si no, los paga quien lleve las gafas.
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
class LookRequest(BaseModel):
    image: str = Field(..., description="Imagen en base64, SIN el prefijo data:")
    deviceId: str
    prompt: str | None = None
    lang: str | None = None   # 'ca', 'es', 'en'. La voz la decide el idioma.
    # 'pcm16' (lo que quiere el I2S del MAX98357A), 'mulaw' (la mitad de
    # bytes) o 'wav' (con cabecera, para navegadores).
    audioFormat: str | None = None
    sampleRate: int | None = None
    # Lado largo al que reducir la foto en el servidor. 0 desactiva; si no
    # se dice nada, manda IMAGE_MAX_SIDE.
    maxSide: int | None = None


class MemoryRequest(BaseModel):
    deviceId: str
    fact: str


class MemoryEdit(BaseModel):
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
        # Los modelos sueltan topónimos con total aplomo y se equivocan: con
        # una foto de una plaza de Reus, uno dijo Vilanova i la Geltrú y otro
        # la plaça Reial de Barcelona. Quien lleva las gafas no tiene forma de
        # saber que es mentira, así que un nombre inventado desorienta más que
        # no decir ninguno.
        "No adivines nunca nombres propios: ni ciudades, ni plazas, ni calles, "
        "ni comercios, ni monumentos. Dilos solo si los estás leyendo en un "
        "cartel o un rótulo de la imagen, y entonces di que los lees. Si no, "
        "describe el sitio por lo que es. Ante la duda, calla el nombre: es "
        "mejor «una plaza grande con terrazas» que un nombre equivocado.",
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
def _pagina(nombre: str) -> HTMLResponse:
    """Sirve un HTML de static/. Sin token: es una página, no datos.

    Lo que hay detrás sí está protegido: la página pide los datos a la API con
    la cabecera X-API-Token, así que servir el HTML no expone nada.
    """
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", nombre)
    try:
        with open(ruta, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        raise HTTPException(404, f"Falta static/{nombre} en el servidor.") from None


# Dos nombres para la misma página: "provar" es como se dice en catalán, que es
# el idioma del proyecto, y "probar" es el que ya está escrito en sitios y en
# los marcadores de alguien. Cambiar uno por otro solo rompería enlaces.
@app.get("/provar", response_class=HTMLResponse)
@app.get("/probar", response_class=HTMLResponse)
def probar() -> HTMLResponse:
    """Página de prueba para el móvil: hace una foto y reproduce la respuesta.

    Se sirve desde el propio backend para que no haya CORS ni haya que montar
    nada aparte: se abre la IP del servidor en el móvil y ya está.
    """
    return _pagina("probar.html")


@api.get("/health")
def health() -> dict[str, Any]:
    """Sin token: lo usa el healthcheck de Docker."""
    return {
        "ok": True,
        "authRequired": bool(API_TOKEN),
        # Para que el firmware pueda comprobar contra qué versión habla sin
        # tener que deducirlo de la URL que ya conoce.
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
            # Lo que necesita /ask. Va aparte porque es otro modelo, aunque la
            # clave sea la misma GROQ_API_KEY.
            "model": stt.MODEL,
            "keyConfigured": bool(stt.api_key()),
            "maxAudioSeconds": ASK_MAX_SEGUNDOS,
        },
    }


async def _plan_de_audio(
    audio_format: str | None,
    lang: str | None,
    sample_rate: int | None,
) -> dict[str, Any]:
    """Decide y valida con qué se va a hablar, antes de gastar nada.

    Se hace al principio de la petición, antes de la visión y antes de leer el
    audio del micro: si el formato pedido no existe, más vale decirlo enseguida
    que después de haber gastado cuota o de haber subido medio megabyte.

    La voz no se pide: se pide un idioma y la voz sale de `tts.VOICES`, que es
    donde se cambian.
    """
    # Un idioma que no esté definido se dice, no se traduce por lo bajo al
    # catalán: si alguien pide 'fr' y le contestan en catalán sin avisar,
    # parece que el servidor esté roto.
    if lang and lang.lower() not in tts.idiomas():
        raise HTTPException(
            400,
            f"Idioma desconocido: {lang!r}. Hay {', '.join(tts.idiomas())}. "
            "Se añaden en tts.VOICES.",
        )

    formato = (audio_format or "pcm16").lower()
    if formato not in tts.FORMATOS:
        raise HTTPException(
            400,
            f"Formato desconocido: {formato!r}. Usa uno de: "
            f"{', '.join(tts.FORMATOS)}",
        )

    if sample_rate and sample_rate not in tts.SAMPLE_RATES:
        raise HTTPException(
            400,
            f"sampleRate no soportado: {sample_rate}. Usa uno de: "
            f"{', '.join(str(s) for s in tts.SAMPLE_RATES)}",
        )

    voz = tts.voice_for(lang or tts.DEFAULT_LANG)

    # Si la voz de ese idioma no está en disco, se baja aquí (~63 MB, una sola
    # vez). Va antes de leer el sample_rate del modelo a propósito: sin el
    # .onnx.json no se sabe a qué frecuencia habla la voz, y suponer 22050
    # cuando es de 16 kHz haría que sonara acelerada.
    #
    # Y va en este punto de la petición porque es antes de gastar cuota de
    # visión y, en /ask, antes de que el micro suba nada.
    try:
        await tts.ensure_voice(voz)
    except Exception as e:
        raise HTTPException(
            502,
            f"No se ha podido preparar la voz {voz!r} para el idioma "
            f"{(lang or tts.DEFAULT_LANG)!r}: {describe_error(e)}",
        ) from e

    rate = sample_rate or tts.sample_rate_de(voz)
    bits = 8 if formato == "mulaw" else 16

    return {"formato": formato, "voz": voz, "rate": rate, "bits": bits}


# Content-Type de cada formato, para que el cliente no tenga que adivinar.
_TIPOS_AUDIO = {
    "pcm16": "audio/L16;rate={rate};channels=1",
    "mulaw": "audio/basic;rate={rate}",
    "wav": "audio/wav",
}

# El cuerpo de /look, /ask y /speak es audio, no JSON. Sin decirlo, FastAPI
# apunta application/json en el esquema y /docs promete algo que no es.
_RESPUESTA_AUDIO: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "El audio en el formato pedido. El texto va en la "
                       "cabecera X-Bonsai-Text (UTF-8 en base64).",
        "content": {
            tipo.split(";")[0]: {"schema": {"type": "string", "format": "binary"}}
            for tipo in _TIPOS_AUDIO.values()
        },
    }
}


async def _responde_con_voz(
    texto: str, plan: dict[str, Any], cabeceras: dict[str, str]
) -> Response:
    """Convierte el texto en audio y lo devuelve en streaming.

    Es el final común de /look y de /ask: los dos acaban con una frase que hay
    que decir en voz alta y en el formato que quiera el I2S.
    """
    formato, rate = plan["formato"], plan["rate"]
    trozos = tts.stream_raw(texto, plan["voz"], formato, rate)

    expuestas = sorted({*cabeceras, "X-Bonsai-Text", "X-Bonsai-Tts",
                        "X-Bonsai-Format", "X-Bonsai-Rate", "X-Bonsai-Bits",
                        "X-Bonsai-Channels", "X-Bonsai-Voice"})
    cabeceras.update({
        "X-Bonsai-Text": base64.b64encode(texto.encode()).decode("ascii"),
        # Se mantiene aunque ya no haya donde elegir: lo lee /provar.
        "X-Bonsai-Tts": "piper",
        "X-Bonsai-Format": formato,
        "X-Bonsai-Rate": str(rate),
        "X-Bonsai-Bits": str(plan["bits"]),
        "X-Bonsai-Channels": "1",
        "X-Bonsai-Voice": plan["voz"],
        # Sin esto el navegador no deja leer las X-Bonsai-* desde JavaScript.
        "Access-Control-Expose-Headers": ", ".join(expuestas),
    })

    # El primer trozo se pide antes de responder: si el TTS falla, todavía
    # estamos a tiempo de devolver un error de verdad y no un 200 vacío.
    try:
        primero = await anext(trozos)
    except StopAsyncIteration:
        raise HTTPException(502, "Piper no devolvió audio.") from None
    except Exception as e:
        raise HTTPException(
            502, f"Fallo al generar el audio: {describe_error(e)}"
        ) from e

    # El WAV se junta entero antes de responder, y así la cabecera lleva las
    # longitudes de verdad. Con 0xFFFFFFFF el reproductor no sabe cuánto dura:
    # enseña 0:00 y no suena (pasaba al probar /speak desde /docs).
    #
    # No se pierde nada por no trocearlo: el wav es el formato de navegador
    # —el I2S se lleva pcm16 o mulaw, que siguen en streaming— y quien lo pide
    # se espera igualmente a tener el fichero entero para poder reproducirlo.
    # Con Piper son los ~205 ms de la síntesis.
    if formato == "wav":
        datos = primero + b"".join([t async for t in trozos])
        return Response(
            tts.cabecera_wav(rate, plan["bits"], len(datos)) + datos,
            media_type=_TIPOS_AUDIO[formato].format(rate=rate),
            headers=cabeceras,
        )

    async def cuerpo():
        yield primero
        async for trozo in trozos:
            yield trozo

    return StreamingResponse(
        cuerpo(),
        media_type=_TIPOS_AUDIO[formato].format(rate=rate),
        headers=cabeceras,
    )


async def _describir(
    req: LookRequest, preamble: tuple[tuple[str, str], ...] | None = None
) -> tuple[str, dict[str, str]]:
    """La parte de visión de /look y de /ask.

    Devuelve el texto y unas cabeceras con el detalle de qué lo generó, para
    poder darlo sin cuerpo JSON (el cuerpo es el audio).
    """
    texto, timings = await _vision_paso(req, preamble)
    return texto, {
        "X-Bonsai-Model": vision.MODEL,
        "X-Bonsai-Vision-Ms": str(timings.get("vision_ms", 0)),
        # Reducir una foto de 12 MP son ~200-300 ms que si no aparecen aquí
        # descuadran cualquier medición hecha desde fuera.
        "X-Bonsai-Resize-Ms": str(timings.get("reducir_ms", 0)),
    }


async def _vision_paso(
    req: LookRequest, preamble: tuple[tuple[str, str], ...] | None = None
) -> tuple[str, dict[str, int]]:
    api_key = vision.api_key()
    if not api_key:
        raise HTTPException(500, "GROQ_API_KEY no está configurada en el servidor.")

    lang = (req.lang or tts.DEFAULT_LANG).lower()
    timings: dict[str, int] = {}

    t0 = time.perf_counter()
    memory_context = memory.get_memory_context(req.deviceId)
    timings["memoria_ms"] = int((time.perf_counter() - t0) * 1000)

    # Reducir la foto, si toca. Va a un hilo porque Pillow es CPU pura y
    # bloquearía el bucle de eventos con una foto de 12 MP.
    imagen_b64 = req.image
    reducir = req.maxSide if req.maxSide is not None else (
        imagen.MAX_SIDE if imagen.ENABLED else 0
    )
    if reducir > 0:
        t0 = time.perf_counter()
        imagen_b64, info_img = await asyncio.to_thread(
            imagen.reducir, req.image, reducir
        )
        if info_img.get("resized"):
            timings["reducir_ms"] = int((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    try:
        description = await vision.describe_image(
            api_key=api_key,
            image_base64=imagen_b64,
            system_prompt=build_system_prompt(lang, memory_context),
            user_prompt=req.prompt or "¿Qué tengo delante? Dímelo en una o dos frases.",
            preamble=preamble,
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

    return description, timings


@api.post("/look", dependencies=[Depends(require_token)],
          responses=_RESPUESTA_AUDIO, response_class=Response)
async def look(req: LookRequest) -> Response:
    """El endpoint principal: foto entra, audio sale, y el audio va en streaming.

    Pensado para que la ESP32-S3 lo llame directamente:

    - El audio va en crudo, sin base64: un 33 % menos de bytes y nada que
      descodificar en el microcontrolador.
    - Empieza a llegar en cuanto el TTS genera la primera frase, así que el
      ESP32 puede sonar mientras se sintetiza el resto. Como el audio viaja
      más rápido de lo que se escucha, a partir de ahí la descarga se solapa
      con la reproducción y deja de sumar latencia.
    - Con `pcm16` (lo de por defecto) son muestras de 16 bits con signo tal
      como las quiere el I2S del MAX98357A: se escriben directamente, sin
      cabecera ni conversión.

    El texto va en la cabecera `X-Bonsai-Text` (UTF-8 en base64, porque las
    cabeceras HTTP son ASCII), y el formato en `X-Bonsai-Rate`, `-Bits` y
    `-Channels`, para no tener que adivinar nada al configurar el I2S.
    """
    plan = await _plan_de_audio(req.audioFormat, req.lang,
                                req.sampleRate)
    texto, cabeceras = await _describir(req)
    return await _responde_con_voz(texto, plan, cabeceras)


# --------------------------------------------------------------------------
# /ask: foto + voz -> respuesta hablada
# --------------------------------------------------------------------------
# Tope de la foto. Una de la OV3660 a 3 MP no llega a 1 MB; 8 es de sobra y
# evita que un cliente roto reserve memoria sin fin.
ASK_MAX_IMAGEN = int(os.environ.get("ASK_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))

# Tope de la grabación. No es solo memoria: Whisper cobra por segundos de
# audio, así que un micro que se quede abierto no debe poder gastar la cuota
# del día. 30 s es mucho más de lo que dura una pregunta.
ASK_MAX_SEGUNDOS = float(os.environ.get("ASK_MAX_AUDIO_SECONDS", "30"))

# Por debajo de esto no hay ni una palabra: es un botón pulsado sin querer.
ASK_MIN_SEGUNDOS = 0.25

# Cuánto se espera sin recibir ni un trozo antes de dar la petición por
# perdida. Tiene que caber el «Digue'm» de las gafas más lo que la persona
# tarde en arrancar a hablar, y aun así soltar la conexión si el firmware se
# cuelga a media grabación.
ASK_SILENCIO = float(os.environ.get("ASK_SILENCE_TIMEOUT_SECONDS", "15"))


class _Trama:
    """Lee el cuerpo de /ask a medida que llega, en dos tiempos.

    El cuerpo va en crudo y con esta forma:

        4 bytes  uint32 big-endian  = cuántos bytes ocupa la foto
        N bytes  la foto (JPEG)
        resto    el audio del micro, hasta que se cierra la petición

    Van en dos métodos y no en uno a propósito. La gracia de que el audio suba
    en trozos es que la foto ya está aquí mucho antes de que la persona acabe
    de hablar: si se leyera el cuerpo entero de una vez (o con `request.body()`)
    la foto no se guardaría hasta el final y subir en streaming no serviría de
    nada. Con esto, `foto()` vuelve en cuanto la imagen está completa, se
    guarda, y solo entonces se espera al resto.
    """

    def __init__(self, request: Request) -> None:
        self._trozos = request.stream().__aiter__()
        self._buf = bytearray()

    async def _toma(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                self._buf.extend(await self._trozos.__anext__())
            except StopAsyncIteration:
                raise HTTPException(
                    400,
                    f"El cuerpo se acabó antes de tiempo: esperaba {n} bytes y "
                    f"llegaron {len(self._buf)}. El formato es "
                    "[4 bytes de longitud][foto][audio].",
                ) from None
        salida = bytes(self._buf[:n])
        del self._buf[:n]
        return salida

    async def foto(self) -> bytes:
        n = int.from_bytes(await self._toma(4), "big")
        if not 0 < n <= ASK_MAX_IMAGEN:
            raise HTTPException(
                400,
                f"Longitud de imagen imposible: {n} bytes (el máximo son "
                f"{ASK_MAX_IMAGEN}). ¿Los 4 primeros bytes van en big-endian?",
            )
        return await self._toma(n)

    async def audio(self, max_bytes: int, timeout: float) -> bytes:
        """Espera al micro hasta que se cierre la petición.

        Con el flujo de las gafas, entre la foto y la primera muestra pasa un
        rato: suena el «Digue'm» y luego la persona se lo piensa. Por eso hay
        un tope por trozo y no uno total. Si el micro se queda mudo del todo
        —se cuelga el firmware, se va el WiFi— hay que soltar la conexión en
        vez de dejarla abierta para siempre.
        """
        datos = bytearray(self._buf)
        self._buf.clear()
        while True:
            try:
                trozo = await asyncio.wait_for(
                    self._trozos.__anext__(), timeout=timeout
                )
            except StopAsyncIteration:
                return bytes(datos)
            except asyncio.TimeoutError:
                raise HTTPException(
                    408,
                    f"El micro lleva {timeout:g} s sin mandar nada y la petición "
                    "sigue abierta. Cierra el cuerpo cuando dejes de grabar "
                    "(el trozo final de longitud 0), o sube "
                    "ASK_SILENCE_TIMEOUT_SECONDS.",
                ) from None
            datos.extend(trozo)
            if len(datos) > max_bytes:
                raise HTTPException(
                    413,
                    f"Audio demasiado largo: el tope son {ASK_MAX_SEGUNDOS:g} s "
                    "(ASK_MAX_AUDIO_SECONDS).",
                )


@api.post("/ask", dependencies=[Depends(require_token)],
          responses=_RESPUESTA_AUDIO, response_class=Response)
async def ask(
    request: Request,
    deviceId: str = "bonsai-01",
    lang: str | None = None,
    audioFormat: str | None = None,
    sampleRate: int | None = None,
    micRate: int = 16000,
    maxSide: int | None = None,
) -> Response:
    """Foto + pregunta hablada -> respuesta hablada, en una sola petición.

    Es `/look` con voz por delante: en vez de mandar la pregunta escrita, las
    gafas mandan la foto y a continuación lo que está diciendo quien las lleva.
    Aquí se transcribe con Whisper turbo en Groq y el texto pasa a ser la
    pregunta que se le hace al modelo de visión.

    El cuerpo va en crudo, sin JSON ni base64 (ver `_Trama`):

        [4 bytes de longitud][foto JPEG][audio del micro]

    El audio puede ser PCM16 mono en crudo, que es lo que sale del micro PDM de
    la XIAO ESP32-S3 Sense leído por I2S (di a qué frecuencia con `micRate`), o
    un fichero con cabecera (WAV, OGG, m4a, MP3): se detecta solo.

    Sube en trozos (`Transfer-Encoding: chunked`), que es lo que permite que la
    foto ya esté guardada mientras la persona todavía habla.

    La respuesta es exactamente la de `/look`: el audio en crudo y en
    streaming, con el texto en `X-Bonsai-Text`. Además vuelve lo que se entendió
    en `X-Bonsai-Transcript`, para poder ver por qué ha contestado eso.

    La voz no se pide: sale del idioma (`lang`), y qué voz le toca a cada
    idioma se decide en `tts.VOICES`.

    Sobre la cuota: transcribir **no** gasta los tokens de texto de Groq
    (Whisper se factura por segundos de audio), así que probar `/ask` no te
    deja sin `/look`.
    """
    # Todo lo que se puede validar sin gastar nada se valida antes de que el
    # dispositivo suba medio megabyte para nada.
    plan = await _plan_de_audio(audioFormat, lang, sampleRate)
    if micRate <= 0:
        raise HTTPException(400, f"micRate ha de ser positivo, no {micRate}.")
    if not stt.api_key():
        raise HTTPException(
            500, "GROQ_API_KEY no está configurada: /ask la necesita para transcribir."
        )

    t_total = time.perf_counter()
    trama = _Trama(request)

    # La foto se guarda en cuanto está entera, mientras la persona todavía
    # está hablando: aquí es donde se aprovecha que el audio suba en trozos.
    # Además, si algo falla después, queda constancia de qué estaba mirando.
    foto = await trama.foto()
    capture_id, _ruta = await asyncio.to_thread(memory.save_capture, deviceId, foto)
    t_foto = time.perf_counter()

    # Y aquí está el otro premio de tener la foto pronto: reducirla cuesta
    # ~700 ms de CPU con una de 12 MP, y ahora mismo lo único que hace el
    # servidor es esperar a que la persona acabe de hablar. Se hace en un hilo
    # mientras tanto, así que para cuando llegue la pregunta ya está lista y
    # no suma nada al tiempo que se espera con las gafas puestas.
    lado = maxSide if maxSide is not None else (
        imagen.MAX_SIDE if imagen.ENABLED else 0
    )
    tarea_imagen = None
    imagen_b64 = base64.b64encode(foto).decode("ascii")
    if lado > 0:
        async def _reduce() -> tuple[str, int]:
            t = time.perf_counter()
            reducida, info = await asyncio.to_thread(imagen.reducir, imagen_b64, lado)
            return reducida, int((time.perf_counter() - t) * 1000) if info.get("resized") else 0

        tarea_imagen = asyncio.create_task(_reduce())

    try:
        audio = await trama.audio(
            int(ASK_MAX_SEGUNDOS * micRate * 2), ASK_SILENCIO
        )
    except BaseException:
        if tarea_imagen is not None:
            tarea_imagen.cancel()
        raise
    subida_ms = int((time.perf_counter() - t_foto) * 1000)

    resize_ms = espera_img_ms = 0
    if tarea_imagen is not None:
        t = time.perf_counter()
        imagen_b64, resize_ms = await tarea_imagen
        # Lo que ha sobrado de reducir después de que la persona callara. Con
        # una frase de un par de segundos es 0: ya estaba hecho.
        espera_img_ms = int((time.perf_counter() - t) * 1000)

    # Los segundos solo se saben si es PCM en crudo; con un m4a o un ogg
    # habría que descodificarlo, así que ahí `segundos` viene a None y lo
    # único que se puede mirar es que no venga vacío.
    _, _, segundos = stt.envolver(audio, micRate)
    if segundos is not None and segundos < ASK_MIN_SEGUNDOS:
        raise HTTPException(
            400,
            f"Apenas hay audio ({segundos:.2f} s). ¿Se ha soltado el botón "
            "antes de hablar o el micro no está dando muestras?",
        )
    if len(audio) < 256:
        raise HTTPException(
            400, f"Apenas hay audio ({len(audio)} bytes). ¿Ha llegado algo del micro?"
        )

    t0 = time.perf_counter()
    try:
        transcripcion = await stt.transcribe(
            audio, sample_rate=micRate, lang=(lang or tts.DEFAULT_LANG).lower()
        )
    except VisionRateLimit as e:
        cabeceras = {}
        if e.retry_after is not None:
            cabeceras["Retry-After"] = str(max(1, round(e.retry_after)))
        raise HTTPException(429, str(e), headers=cabeceras) from e
    except Exception as e:
        raise HTTPException(
            502, f"Fallo al transcribir: {describe_error(e)}"
        ) from e
    stt_ms = int((time.perf_counter() - t0) * 1000)

    if not transcripcion:
        # Se guarda igualmente: una transcripción vacía repetida es el síntoma
        # de un micro mal configurado, y así se ve en /admin.
        await asyncio.to_thread(
            memory.finish_capture, capture_id,
            audio_secs=round(segundos, 2) if segundos is not None else None,
            stt_ms=stt_ms, transcript="",
        )
        cuanto = f"{segundos:.1f} s de" if segundos is not None else f"{len(audio)} bytes de"
        raise HTTPException(
            422,
            f"No se ha entendido nada en {cuanto} audio. Si pasa siempre, mira "
            "la ganancia del micro; si es puntual, vuelve a preguntar o tira "
            "de /look, que no necesita voz.",
        )

    peticion = LookRequest(
        image=imagen_b64,
        deviceId=deviceId,
        prompt=transcripcion,
        lang=lang,
        # Ya está reducida (o no tocaba): que _vision_paso no lo repita.
        maxSide=0,
    )
    # Aquí está la diferencia con /look: se le dan por dichos los dos turnos de
    # la palabra de activación, para que conteste como quien sigue una
    # conversación y no como quien recibe una orden suelta.
    texto, cabeceras = await _describir(peticion, vision.PREAMBULO_VEU)

    total_ms = int((time.perf_counter() - t_total) * 1000)
    await asyncio.to_thread(
        memory.finish_capture, capture_id,
        audio_secs=round(segundos, 2) if segundos is not None else None,
        transcript=transcripcion, reply=texto,
        stt_ms=stt_ms, vision_ms=int(cabeceras.get("X-Bonsai-Vision-Ms", 0)),
        total_ms=total_ms,
    )
    await asyncio.to_thread(memory.prune_captures, deviceId)

    cabeceras.update({
        "X-Bonsai-Transcript": base64.b64encode(
            transcripcion.encode()).decode("ascii"),
        "X-Bonsai-Stt-Ms": str(stt_ms),
        "X-Bonsai-Stt-Model": stt.MODEL,
        "X-Bonsai-Audio-Bytes": str(len(audio)),
        # Cuánto se ha estado esperando al micro después de tener ya la foto.
        # Es tiempo que la persona pasa hablando, no latencia del servidor.
        "X-Bonsai-Upload-Ms": str(subida_ms),
        # Reducir la foto se hace mientras se espera al micro, así que
        # -Resize-Ms es trabajo hecho "gratis" y -Resize-Wait-Ms es lo único
        # que ha llegado a sumar al total. Normalmente, 0.
        "X-Bonsai-Resize-Ms": str(resize_ms),
        "X-Bonsai-Resize-Wait-Ms": str(espera_img_ms),
        "X-Bonsai-Capture-Id": capture_id,
    })
    if segundos is not None:
        cabeceras["X-Bonsai-Audio-Secs"] = f"{segundos:.2f}"
    return await _responde_con_voz(texto, plan, cabeceras)


# GET además de POST. No es capricho: un navegador pide el audio con
# `<audio src="...">`, y eso es siempre un GET. El reproductor de /docs hacía
# justo eso, se comía un 405 y se quedaba en 0:00 sin sonar, con el cuerpo del
# POST ya descargado al lado. Y encaja: /speak no cambia nada, todo lo que
# necesita va en la query y pedirlo dos veces da lo mismo.
#
# Van dos decoradores y no un api_route(methods=[...]) porque con los dos
# métodos en una sola ruta FastAPI genera el mismo operationId para los dos y
# avisa de que está duplicado.
@api.get("/speak", dependencies=[Depends(require_token)],
         responses=_RESPUESTA_AUDIO, response_class=Response)
@api.post("/speak", dependencies=[Depends(require_token)],
          responses=_RESPUESTA_AUDIO, response_class=Response)
async def speak(
    text: str = Query(description="El texto que hay que decir en voz alta."),
    lang: str = Query(
        default=tts.DEFAULT_LANG, description="Idioma de la voz: ca, es o en."
    ),
    audioFormat: str | None = Query(
        default=None,
        # Sin enum a propósito: un 422 de FastAPI diría bastante menos que el
        # 400 de _plan_de_audio, que lista los que hay.
        description="pcm16 | mulaw | wav. Si no se dice nada, wav.",
    ),
    sampleRate: int | None = Query(
        default=None,
        description="8000, 16000 o 22050 Hz. Por defecto, el del modelo de la "
                    "voz (22050 en las catalanas).",
    ),
):
    """Solo texto a voz. Devuelve el audio en crudo (útil para la ESP32).

    Vale igual con GET que con POST: no cambia nada en el servidor y todo lo
    que necesita va en la query. Con GET se puede poner la URL tal cual en un
    `<audio src="...">` o en la barra del navegador, que es lo que hace el
    reproductor de esta misma página.

    Formatos que acepta, que son los mismos de /look y /ask:

    | audioFormat | Qué sale |
    | --- | --- |
    | `pcm16` | Muestras de 16 bits con signo, sin cabecera: lo que quiere el I2S del MAX98357A |
    | `mulaw` | μ-law de 8 bits, la mitad de bytes que pcm16 |
    | `wav`   | Lo mismo que pcm16 pero con cabecera RIFF, para navegadores. **Es el de por defecto aquí** |

    Y `sampleRate` es 8000, 16000 o 22050 Hz. Cualquier otro valor da un 400
    con la lista, antes de sintetizar nada.

    Ojo con el formato de por defecto: aquí es **wav**, no `pcm16` como en
    /look. Es de cuando /speak solo servía para escuchar cosas desde el
    navegador, y se mantiene para no romper a quien lo llama sin decir nada.

    Sirve para fabricar los clips que las gafas llevan grabados —el «Digue'm»
    que suena mientras sube la foto, por ejemplo— en el formato exacto del I2S,
    sin conversiones a mano:

        curl -X POST "$API/speak?text=Digue'm!&audioFormat=pcm16&sampleRate=16000" \
             -o digam.pcm

    Llega de una vez (~205 ms, no hay nada que trocear).

    El formato de verdad de la respuesta va en las cabeceras `X-Bonsai-Format`,
    `-Rate`, `-Bits` y `-Channels`, para no tener que adivinar nada al
    configurar el I2S.
    """
    plan = await _plan_de_audio(audioFormat, lang, sampleRate)
    # /speak nació devolviendo WAV con Piper y hay quien lo llama sin decir
    # formato: se respeta salvo que se pida otra cosa.
    if audioFormat is None and plan["formato"] == "pcm16":
        plan["formato"] = "wav"
    return await _responde_con_voz(text, plan, {})


@api.post("/memory", dependencies=[Depends(require_token)])
def add_memory(req: MemoryRequest) -> dict[str, Any]:
    item = memory.add_memory(req.deviceId, req.fact)
    return {"ok": True, "item": item}


@api.get("/memory", dependencies=[Depends(require_token)])
def list_devices() -> dict[str, Any]:
    """Todos los dispositivos con recuerdos, y el estado de la base de datos.

    Sin esto no había forma de saber qué deviceId existen: había que
    acordarse de ellos.
    """
    return {"devices": memory.list_devices(), "stats": memory.stats()}


@api.get("/memory/{device_id}", dependencies=[Depends(require_token)])
def get_memories(device_id: str) -> dict[str, Any]:
    return {"deviceId": device_id, "memories": memory.list_memories(device_id)}


@api.patch("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def edit_memory(device_id: str, memory_id: str, req: MemoryEdit) -> dict[str, Any]:
    """Corrige el texto de un recuerdo, sin borrarlo y volverlo a crear."""
    texto = req.fact.strip()
    if not texto:
        raise HTTPException(400, "El recuerdo no puede quedar vacío.")
    item = memory.update_memory(device_id, memory_id, texto)
    if item is None:
        raise HTTPException(404, "No se encontró ese recuerdo.")
    return {"ok": True, "item": item}


@api.delete("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def remove_memory(device_id: str, memory_id: str) -> dict[str, Any]:
    deleted = memory.delete_memory(device_id, memory_id)
    if not deleted:
        raise HTTPException(404, "No se encontró ese recuerdo.")
    return {"ok": True}


@api.delete("/memory/{device_id}", dependencies=[Depends(require_token)])
def clear_device(device_id: str) -> dict[str, Any]:
    """Vacía un dispositivo entero. Pide confirm=true para no borrar sin querer."""
    borrados = memory.clear_device(device_id)
    return {"ok": True, "deleted": borrados}


# --------------------------------------------------------------------------
# /clips: the fixed phrases the glasses carry
# --------------------------------------------------------------------------
@api.get("/clips", dependencies=[Depends(require_token)])
def listar_clips(lang: str = tts.DEFAULT_LANG) -> dict[str, Any]:
    """What each fixed phrase says in that language.

    The firmware does not need this (it asks /clips/{id} for the audio), but it
    shows at a glance what the device will say.
    """
    if lang.lower() not in tts.idiomas():
        raise HTTPException(
            400, f"Idioma desconocido: {lang!r}. Hay {', '.join(tts.idiomas())}."
        )
    return {
        "lang": lang.lower(),
        "clips": clips.textos_de(lang),
        # Untranslated clips show up here instead of coming out in another language.
        "missing": [c for c in clips.ids() if clips.texto(c, lang) is None],
    }


@api.get("/clips/{clip_id}", dependencies=[Depends(require_token)],
         responses=_RESPUESTA_AUDIO, response_class=Response)
async def clip(
    clip_id: str,
    lang: str = tts.DEFAULT_LANG,
    audioFormat: str | None = None,
    sampleRate: int | None = None,
):
    """One of those phrases as audio, for the device to store on its SD card.

    It is /speak with the text put in by the server: the glasses ask for
    /clips/start_talking?lang=ca and carry no text of their own.
    """
    texto = clips.texto(clip_id, lang)
    if texto is None:
        if clip_id not in clips.CLIPS:
            raise HTTPException(
                404,
                f"No hay ningún clip {clip_id!r}. Hay: {', '.join(clips.ids())}.",
            )
        raise HTTPException(
            404,
            f"El clip {clip_id!r} no está en {lang!r}. Lo hay en: "
            f"{', '.join(clips.idiomas_de(clip_id))}. Se añade en clips.py.",
        )

    plan = await _plan_de_audio(audioFormat, lang, sampleRate)
    # Same as /speak: whoever asks without a format is saving it to a file.
    if audioFormat is None and plan["formato"] == "pcm16":
        plan["formato"] = "wav"
    return await _responde_con_voz(texto, plan, {"X-Bonsai-Clip": clip_id})


# --------------------------------------------------------------------------
# Montaje de las rutas
# --------------------------------------------------------------------------
# /api/v1/look, /api/v1/ask, /api/v1/health...
app.include_router(api, prefix=API_PREFIX)
