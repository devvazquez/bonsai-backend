"""Backend de Bonsai: orquesta visión + voz + memoria en una sola petición.

`/look` es el endpoint principal y lo llama la ESP32-S3 directamente: recibe la
foto, la reduce, le añade contexto (fecha y recuerdos del dispositivo), pide la
descripción al proveedor de visión (`vision.py`: Groq o Gemini) y devuelve el
audio en crudo y en streaming, listo para el I2S del MAX98357A, sin base64 ni
nada que descodificar en el microcontrolador.

`/ask` es el mismo camino pero con voz por delante: la foto y, a continuación,
lo que está diciendo quien lleva las gafas. Se transcribe con Whisper turbo en
Groq (`stt.py`) y esa frase pasa a ser la pregunta que se le hace al modelo de
visión.

El resto es servicio: `/memory` para los recuerdos, `/speak` para texto a voz
suelto, la página `/provar` para probarlo desde el móvil y el panel `/admin`
(`panel.py`) para administrar la base de datos. No hay ninguna aplicación web
en el camino principal.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

import imagen
import memory
import piper_tts
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
class LookRequest(BaseModel):
    image: str = Field(..., description="Imagen en base64, SIN el prefijo data:")
    deviceId: str
    prompt: str | None = None
    lang: str | None = None   # 'ca', 'es', 'en'
    voice: str | None = None  # fuerza una voz concreta
    # 'groq' o 'gemini'. Si no se dice nada, el de VISION_PROVIDER (groq).
    provider: str | None = None
    # 'piper' o 'edge'. Si no se dice nada, el de TTS_PROVIDER (piper).
    tts: str | None = None
    # 'pcm16' (lo que quiere el I2S del MAX98357A), 'mulaw' (la mitad de
    # bytes), 'wav' (con cabecera, para navegadores) o 'mp3' (solo con edge).
    audioFormat: str | None = None
    sampleRate: int | None = None
    # Lado largo al que reducir la foto en el servidor. 0 desactiva; si no
    # se dice nada, manda IMAGE_MAX_SIDE según el proveedor.
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
        "stt": {
            # Lo que necesita /ask. Va aparte de "providers" porque siempre es
            # Groq, aunque la visión sea Gemini: la clave que mira es la misma
            # GROQ_API_KEY, y sin ella /ask da 500 aunque /look funcione.
            "model": stt.MODEL,
            "keyConfigured": bool(stt.api_key()),
            "maxAudioSeconds": ASK_MAX_SEGUNDOS,
        },
    }


def _plan_de_audio(
    tts_provider: str | None,
    audio_format: str | None,
    lang: str | None,
    voice: str | None,
    sample_rate: int | None,
) -> dict[str, Any]:
    """Decide y valida con qué se va a hablar, antes de gastar nada.

    Se hace al principio de la petición, antes de la visión y antes de leer el
    audio del micro: si el formato pedido no existe, más vale decirlo enseguida
    que después de haber gastado cuota o de haber subido medio megabyte.
    """
    try:
        proveedor = tts.effective_provider(tts_provider)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # Con edge-tts el formato no se elige: Microsoft manda MP3 y punto. Con
    # Piper sí, porque las muestras salen del modelo y se convierten aquí.
    if proveedor == "edge":
        if audio_format and audio_format.lower() != "mp3":
            raise HTTPException(
                400,
                f"Con edge-tts el audio solo puede ser mp3, no {audio_format!r}. "
                "Para pcm16 o mulaw usa Piper, que es el de por defecto.",
            )
        formato = "mp3"
    else:
        formato = (audio_format or "pcm16").lower()
        if formato not in piper_tts.FORMATOS:
            raise HTTPException(
                400,
                f"Formato desconocido: {formato!r}. Usa uno de: "
                f"{', '.join(piper_tts.FORMATOS)}",
            )

    if sample_rate and sample_rate not in piper_tts.SAMPLE_RATES:
        raise HTTPException(
            400,
            f"sampleRate no soportado: {sample_rate}. Usa uno de: "
            f"{', '.join(str(s) for s in piper_tts.SAMPLE_RATES)}",
        )

    voz = tts.voice_for(lang or tts.DEFAULT_LANG, voice, proveedor)
    if formato == "mp3":
        rate, bits = 24000, 16          # lo que devuelve edge-tts
    else:
        rate = sample_rate or piper_tts.sample_rate_de(voz)
        bits = 8 if formato == "mulaw" else 16

    return {"tts": proveedor, "formato": formato, "voz": voz,
            "rate": rate, "bits": bits}


# Content-Type de cada formato, para que el cliente no tenga que adivinar.
_TIPOS_AUDIO = {
    "pcm16": "audio/L16;rate={rate};channels=1",
    "mulaw": "audio/basic;rate={rate}",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}


async def _responde_con_voz(
    texto: str, plan: dict[str, Any], cabeceras: dict[str, str]
) -> StreamingResponse:
    """Convierte el texto en audio y lo devuelve en streaming.

    Es el final común de /look y de /ask: los dos acaban con una frase que hay
    que decir en voz alta y en el formato que quiera el I2S.
    """
    formato, rate = plan["formato"], plan["rate"]
    if formato == "mp3":
        trozos = tts.stream(texto, plan["voz"], "edge")
    else:
        trozos = piper_tts.stream_raw(texto, plan["voz"], formato, rate)

    expuestas = sorted({*cabeceras, "X-Bonsai-Text", "X-Bonsai-Tts",
                        "X-Bonsai-Format", "X-Bonsai-Rate", "X-Bonsai-Bits",
                        "X-Bonsai-Channels", "X-Bonsai-Voice"})
    cabeceras.update({
        "X-Bonsai-Text": base64.b64encode(texto.encode()).decode("ascii"),
        "X-Bonsai-Tts": plan["tts"],
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
        raise HTTPException(502, f"{plan['tts']} no devolvió audio.") from None
    except Exception as e:
        raise HTTPException(
            502, f"Fallo al generar el audio: {describe_error(e)}"
        ) from e

    async def cuerpo():
        if formato == "wav":
            yield piper_tts.cabecera_wav(rate, plan["bits"])
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
    texto, timings, provider = await _vision_paso(req, preamble)
    return texto, {
        "X-Bonsai-Provider": provider,
        "X-Bonsai-Model": vision.model_for(provider),
        "X-Bonsai-Vision-Ms": str(timings.get("vision_ms", 0)),
        # Reducir una foto de 12 MP son ~200-300 ms que si no aparecen aquí
        # descuadran cualquier medición hecha desde fuera.
        "X-Bonsai-Resize-Ms": str(timings.get("reducir_ms", 0)),
    }


async def _vision_paso(
    req: LookRequest, preamble: tuple[tuple[str, str], ...] | None = None
) -> tuple[str, dict[str, int], str]:
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

    t0 = time.perf_counter()
    memory_context = memory.get_memory_context(req.deviceId)
    timings["memoria_ms"] = int((time.perf_counter() - t0) * 1000)

    # Reducir la foto, si toca. Va a un hilo porque Pillow es CPU pura y
    # bloquearía el bucle de eventos con una foto de 12 MP.
    imagen_b64 = req.image
    reducir = req.maxSide if req.maxSide is not None else (
        imagen.MAX_SIDE if imagen.enabled_for(provider) else 0
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
            provider=provider,
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

    return description, timings, provider


@app.post("/look", dependencies=[Depends(require_token)])
async def look(req: LookRequest) -> StreamingResponse:
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
    plan = _plan_de_audio(req.tts, req.audioFormat, req.lang, req.voice,
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

    async def audio(self, max_bytes: int) -> bytes:
        datos = bytearray(self._buf)
        self._buf.clear()
        async for trozo in self._trozos:
            datos.extend(trozo)
            if len(datos) > max_bytes:
                raise HTTPException(
                    413,
                    f"Audio demasiado largo: el tope son {ASK_MAX_SEGUNDOS:g} s "
                    "(ASK_MAX_AUDIO_SECONDS).",
                )
        return bytes(datos)


@app.post("/ask", dependencies=[Depends(require_token)])
async def ask(
    request: Request,
    deviceId: str = "bonsai-01",
    lang: str | None = None,
    provider: str | None = None,
    tts_provider: str | None = Query(default=None, alias="tts"),
    audioFormat: str | None = None,
    sampleRate: int | None = None,
    micRate: int = 16000,
    maxSide: int | None = None,
) -> StreamingResponse:
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

    Sobre la cuota: transcribir **no** gasta los tokens de texto de Groq
    (Whisper se factura por segundos de audio), así que probar `/ask` no te
    deja sin `/look`.
    """
    # Todo lo que se puede validar sin gastar nada se valida antes de que el
    # dispositivo suba medio megabyte para nada.
    plan = _plan_de_audio(tts_provider, audioFormat, lang, None, sampleRate)
    if micRate <= 0:
        raise HTTPException(400, f"micRate ha de ser positivo, no {micRate}.")
    if not stt.api_key():
        raise HTTPException(
            500,
            "GROQ_API_KEY no está configurada: /ask la necesita para "
            "transcribir, aunque la visión vaya con Gemini.",
        )

    t_total = time.perf_counter()
    trama = _Trama(request)

    # La foto se guarda en cuanto está entera, mientras la persona todavía
    # está hablando: aquí es donde se aprovecha que el audio suba en trozos.
    # Además, si algo falla después, queda constancia de qué estaba mirando.
    foto = await trama.foto()
    capture_id, _ruta = await asyncio.to_thread(memory.save_capture, deviceId, foto)
    t_foto = time.perf_counter()

    audio = await trama.audio(int(ASK_MAX_SEGUNDOS * micRate * 2))
    subida_ms = int((time.perf_counter() - t_foto) * 1000)

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
        image=base64.b64encode(foto).decode("ascii"),
        deviceId=deviceId,
        prompt=transcripcion,
        lang=lang,
        provider=provider,
        maxSide=maxSide,
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
        "X-Bonsai-Capture-Id": capture_id,
    })
    if segundos is not None:
        cabeceras["X-Bonsai-Audio-Secs"] = f"{segundos:.2f}"
    return await _responde_con_voz(texto, plan, cabeceras)


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


@app.get("/memory", dependencies=[Depends(require_token)])
def list_devices() -> dict[str, Any]:
    """Todos los dispositivos con recuerdos, y el estado de la base de datos.

    Sin esto no había forma de saber qué deviceId existen: había que
    acordarse de ellos.
    """
    return {"devices": memory.list_devices(), "stats": memory.stats()}


@app.get("/memory/{device_id}", dependencies=[Depends(require_token)])
def get_memories(device_id: str) -> dict[str, Any]:
    return {"deviceId": device_id, "memories": memory.list_memories(device_id)}


@app.patch("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def edit_memory(device_id: str, memory_id: str, req: MemoryEdit) -> dict[str, Any]:
    """Corrige el texto de un recuerdo, sin borrarlo y volverlo a crear."""
    texto = req.fact.strip()
    if not texto:
        raise HTTPException(400, "El recuerdo no puede quedar vacío.")
    item = memory.update_memory(device_id, memory_id, texto)
    if item is None:
        raise HTTPException(404, "No se encontró ese recuerdo.")
    return {"ok": True, "item": item}


@app.delete("/memory/{device_id}/{memory_id}", dependencies=[Depends(require_token)])
def remove_memory(device_id: str, memory_id: str) -> dict[str, Any]:
    deleted = memory.delete_memory(device_id, memory_id)
    if not deleted:
        raise HTTPException(404, "No se encontró ese recuerdo.")
    return {"ok": True}


@app.delete("/memory/{device_id}", dependencies=[Depends(require_token)])
def clear_device(device_id: str) -> dict[str, Any]:
    """Vacía un dispositivo entero. Pide confirm=true para no borrar sin querer."""
    borrados = memory.clear_device(device_id)
    return {"ok": True, "deleted": borrados}


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


# --------------------------------------------------------------------------
# Panel de administración de la base de datos, en /admin
# --------------------------------------------------------------------------
# Es acceso SQL completo, así que solo se monta si hay ADMIN_PASSWORD. Sin
# ella la ruta no existe, que es más seguro que existir y estar abierta.
# Los recuerdos también se pueden tocar por la API con /memory, sin SQL.
if os.environ.get("ADMIN_PASSWORD"):
    import panel as _panel
    from nicegui import ui as _ui

    _panel.construeix("/admin")
    _ui.run_with(
        app,
        mount_path="/admin",
        # Firma la cookie de sesión del panel. Se deriva del propio password
        # para no obligar a definir otra variable más.
        storage_secret=os.environ["ADMIN_PASSWORD"],
        title="Bonsai · base de dades",
    )
