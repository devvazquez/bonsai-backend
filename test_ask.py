"""Prueba de /ask de punta a punta, sin gastar un solo token de Groq.

    python test_ask.py        (sale con código 1 si algo falla)


Se sustituye el transporte HTTP compartido (vision.get_client) por uno de
mentira que responde lo que respondería Groq y, de paso, guarda lo que se le
mandó. Así se comprueba el payload de verdad: los dos turnos del preámbulo, la
cabecera WAV que se le pone al PCM del micro y el modelo de Whisper.

Piper sí es real: es local y no gasta cuota de nadie.
"""
import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import time

os.environ.setdefault("GROQ_API_KEY", "clau-de-mentida")
os.environ["BONSAI_DB_PATH"] = tempfile.mkdtemp(prefix="ask-") + "/bonsai.db"
os.environ["BONSAI_CAPTURES_DIR"] = tempfile.mkdtemp(prefix="captures-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import groq_vision
import memory
import stt
import vision
import main


def usa(transporte):
    """Cambia el cliente HTTP en los tres sitios que lo tienen importado.

    stt y groq_vision hacen `from vision import get_client`, así que se quedan
    con la función de entonces: tocar solo vision.get_client no les llega.
    """
    cliente = httpx.AsyncClient(transport=httpx.MockTransport(transporte))
    for modulo in (vision, stt, groq_vision):
        modulo.get_client = lambda c=cliente: c

enviadas = []
fallos = []


def check(nom, cond, extra=""):
    (print if cond else fallos.append)(("OK   " if cond else "FALLA ") + nom
                                       + (f"  {extra}" if extra else ""))
    if not cond:
        print("FALLA " + nom + (f"  {extra}" if extra else ""))


def responde(request: httpx.Request) -> httpx.Response:
    enviadas.append(request)
    if "audio/transcriptions" in str(request.url):
        return httpx.Response(200, json={"text": "Què diu aquest cartell?"})
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "Diu «Prohibit el pas»."}}]
    })


usa(responde)

# Una imagen JPEG mínima de verdad (firma incluida, para probar sniff_mime) y
# un segundo de PCM16 a 16 kHz.
FOTO = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 500
PCM = b"\x11\x22" * 16000        # 1,0 s a 16 kHz


def trama(foto: bytes, audio: bytes) -> bytes:
    return struct.pack(">I", len(foto)) + foto + audio


async def principal():
    memory.init_db()
    transporte = httpx.ASGITransport(app=main.app)
    # El prefijo de versión va en base_url, así que las rutas de abajo siguen
    # escribiéndose /ask, /look, /speak...
    async with httpx.AsyncClient(transport=transporte,
                                 base_url=f"http://test{main.API_PREFIX}",
                                 timeout=60) as c:

        # ---------------------------------------------------------------
        # 1. El camino feliz
        # ---------------------------------------------------------------
        r = await c.post("/ask?deviceId=ulleres-01&audioFormat=pcm16&sampleRate=16000",
                         content=trama(FOTO, PCM),
                         headers={"Content-Type": "application/octet-stream"})
        check("/ask responde 200", r.status_code == 200, r.text[:200])
        if r.status_code != 200:
            return

        text = base64.b64decode(r.headers["x-bonsai-text"]).decode()
        trans = base64.b64decode(r.headers["x-bonsai-transcript"]).decode()
        check("devuelve la transcripción", trans == "Què diu aquest cartell?", trans)
        check("devuelve la respuesta", text == "Diu «Prohibit el pas».", text)
        check("el cuerpo es audio de verdad", len(r.content) > 10000,
              f"{len(r.content)} bytes")
        check("formato pcm16 a 16 kHz",
              r.headers["x-bonsai-format"] == "pcm16"
              and r.headers["x-bonsai-rate"] == "16000")
        check("dice el modelo de whisper",
              r.headers["x-bonsai-stt-model"] == "whisper-large-v3-turbo",
              r.headers.get("x-bonsai-stt-model", "-"))
        check("dice cuánto audio era",
              r.headers["x-bonsai-audio-secs"] == "1.00",
              r.headers.get("x-bonsai-audio-secs"))
        check("expone las cabeceras a JS",
              "X-Bonsai-Transcript" in r.headers["access-control-expose-headers"])

        # ---------------------------------------------------------------
        # 2. Lo que se le mandó a Groq
        # ---------------------------------------------------------------
        stt_req, vis_req = enviadas[0], enviadas[1]
        check("primero transcribe, luego describe",
              "transcriptions" in str(stt_req.url)
              and "chat/completions" in str(vis_req.url))

        cuerpo_stt = stt_req.content
        check("al PCM se le pone cabecera WAV", b"RIFF" in cuerpo_stt[:600]
              and b"WAVEfmt" in cuerpo_stt[:600])
        check("con la longitud correcta, no 0xFFFFFFFF",
              struct.pack("<I", 32000) in cuerpo_stt[:600])
        check("pide whisper turbo", b"whisper-large-v3-turbo" in cuerpo_stt)
        check("le dice el idioma", b'name="language"' in cuerpo_stt
              and b"ca" in cuerpo_stt)

        payload = json.loads(vis_req.content)
        msgs = payload["messages"]
        check("4 mensajes: system + los dos turnos + la foto", len(msgs) == 4,
              str([m["role"] for m in msgs]))
        check("turno 1: user «Hey Bonsai!»",
              msgs[1] == {"role": "user", "content": "Hey Bonsai!"}, str(msgs[1]))
        check("turno 2: assistant «Diga’m!» (lo que suena en las gafas)",
              msgs[2] == {"role": "assistant", "content": "Diga’m!"}, str(msgs[2]))
        check("el preámbulo va ANTES del prompt y la imagen",
              msgs[3]["role"] == "user"
              and msgs[3]["content"][0]["text"] == "Què diu aquest cartell?"
              and msgs[3]["content"][1]["type"] == "image_url")
        check("la imagen va como jpeg detectado",
              msgs[3]["content"][1]["image_url"]["url"].startswith(
                  "data:image/jpeg;base64,"))
        check("qwen es el modelo de visión", payload["model"].startswith("qwen"),
              payload["model"])

        # ---------------------------------------------------------------
        # 3. La foto se ha guardado
        # ---------------------------------------------------------------
        cap_id = r.headers["x-bonsai-capture-id"]
        fila = memory.get_capture(cap_id)
        check("hay fila en captures", fila is not None)
        check("el fichero está en disco y pesa lo que la foto",
              os.path.getsize(fila["image_path"]) == len(FOTO))
        check("guarda lo que se dijo y lo que se contestó",
              fila["transcript"] == "Què diu aquest cartell?"
              and fila["reply"] == "Diu «Prohibit el pas».")
        check("guarda los tiempos", fila["total_ms"] >= fila["stt_ms"] >= 0
              and fila["vision_ms"] is not None)
        check("guarda de qué dispositivo es", fila["device_id"] == "ulleres-01")

        # ---------------------------------------------------------------
        # 3 bis. La foto se guarda MIENTRAS todavía sube el audio
        # ---------------------------------------------------------------
        # Esto estuvo mal una vez: se leía el cuerpo entero y solo entonces se
        # guardaba, así que subir en trozos no servía de nada. Aquí el cuerpo
        # es un generador que mira el disco antes de soltar el audio.
        antes = len(memory.list_captures())
        visto = {}

        async def cuerpo_a_trozos():
            yield struct.pack(">I", len(FOTO)) + FOTO
            # Ceder el control para que el servidor procese lo ya enviado.
            for _ in range(20):
                await asyncio.sleep(0.01)
                if len(memory.list_captures()) > antes:
                    break
            visto["guardada_abans"] = len(memory.list_captures()) > antes
            yield PCM

        r = await c.post("/ask?deviceId=solapada", content=cuerpo_a_trozos())
        check("con el cuerpo en trozos responde 200", r.status_code == 200,
              r.text[:150])
        check("la foto ya estaba guardada antes de mandar el audio",
              visto.get("guardada_abans") is True)

        # ---------------------------------------------------------------
        # 4. /look sigue sin preámbulo (no se ha cambiado su comportamiento)
        # ---------------------------------------------------------------
        enviadas.clear()
        r = await c.post("/look", json={
            "image": base64.b64encode(FOTO).decode(), "deviceId": "ulleres-01",
            "audioFormat": "wav",
        })
        check("/look sigue funcionando", r.status_code == 200, r.text[:200])
        msgs = json.loads(enviadas[0].content)["messages"]
        check("/look NO lleva el preámbulo", len(msgs) == 2,
              str([m["role"] for m in msgs]))
        check("/look devuelve WAV", r.content[:4] == b"RIFF")

        # ---------------------------------------------------------------
        # 4 bis. /speak sirve para fabricar el clip del "Diga'm"
        # ---------------------------------------------------------------
        r = await c.post("/speak?text=Diga%E2%80%99m!&audioFormat=pcm16&sampleRate=16000")
        check("/speak en pcm16 a 16 kHz", r.status_code == 200
              and r.headers["x-bonsai-format"] == "pcm16"
              and r.headers["x-bonsai-rate"] == "16000"
              and r.content[:4] != b"RIFF",       # crudo, sin cabecera
              f"{r.status_code} {r.headers.get('x-bonsai-format')}")
        r = await c.post("/speak?text=hola")
        check("/speak sin decir formato sigue dando WAV",
              r.status_code == 200 and r.content[:4] == b"RIFF")

        # Las longitudes de la cabecera, que es lo que hacía que /docs (y
        # cualquier <audio>) enseñara 0:00 y no sonara nada: con 0xFFFFFFFF el
        # reproductor no puede saber cuánto dura.
        riff, datos = (struct.unpack("<I", r.content[o:o + 4])[0] for o in (4, 40))
        check("el WAV lleva las longitudes de verdad, no 0xFFFFFFFF",
              riff == len(r.content) - 8 and datos == len(r.content) - 44,
              f"riff={riff} data={datos} fichero={len(r.content)}")
        check("y va con Content-Length, sin trocear",
              r.headers.get("content-length") == str(len(r.content))
              and "transfer-encoding" not in r.headers,
              str(dict(r.headers)))

        # Con GET también: un <audio src="..."> del navegador (y el
        # reproductor de /docs) pide siempre por GET, y antes se comía un 405.
        # No se comparan los bytes con los del POST, ni la longitud: Piper mete
        # ruido aleatorio en cada síntesis y para un "hola" la misma petición da
        # entre 16,9 KB y 22,6 KB (medido). Lo que tiene que coincidir es el
        # formato, no el tamaño.
        g = await c.get("/speak?text=hola")
        check("/speak también responde a GET",
              g.status_code == 200 and g.content[:4] == b"RIFF"
              and g.headers["content-type"] == r.headers["content-type"]
              and g.headers["x-bonsai-rate"] == r.headers["x-bonsai-rate"]
              and g.headers["x-bonsai-voice"] == r.headers["x-bonsai-voice"]
              and len(g.content) > 1000,
              f"{g.status_code}, {len(g.content)} bytes")
        rutas = main.app.openapi()["paths"]["/api/v1/speak"]
        check("y los dos métodos salen en /docs", sorted(rutas) == ["get", "post"],
              str(sorted(rutas)))

        # ---------------------------------------------------------------
        # 4 quater. La voz sale del idioma; no se puede pedir una voz
        # ---------------------------------------------------------------
        params = {q["name"] for q in rutas["get"]["parameters"]}
        check("/speak ya no tiene parámetro voice", "voice" not in params,
              str(sorted(params)))
        check("y sí tiene lang", "lang" in params)
        campos = main.app.openapi()["components"]["schemas"]["LookRequest"]["properties"]
        check("/look tampoco: lang sí, voice no",
              "lang" in campos and "voice" not in campos, str(sorted(campos)))

        # Un idioma que no existe se dice, no se contesta en catalán por lo bajo.
        r = await c.get("/speak?text=hola&lang=fr")
        check("un idioma desconocido -> 400 con la lista",
              r.status_code == 400 and "ca" in r.text, r.text[:160])

        # Y el que sí existe usa la voz de piper_tts.VOICES, sin pedirla.
        r = await c.get("/speak?text=hola&lang=ca")
        check("la voz del idioma es la del mapa",
              r.headers.get("x-bonsai-voice") == main.piper_tts.VOICES["ca"],
              r.headers.get("x-bonsai-voice", "(cap)"))
        check("el esquema dice que la respuesta es audio, no JSON",
              "audio/wav" in rutas["get"]["responses"]["200"]["content"]
              and "application/json" not in rutas["get"]["responses"]["200"]["content"],
              str(sorted(rutas["get"]["responses"]["200"]["content"])))

        # ---------------------------------------------------------------
        # 4 ter. La API está en /api/v1, y solo ahí
        # ---------------------------------------------------------------
        r = await c.get("/health")
        check("/api/v1/health responde 200", r.status_code == 200, r.text[:120])
        check("y dice qué versión es", r.json().get("api", {}).get("version") == "v1",
              str(r.json().get("api")))
        r = await c.get("http://test/health")     # sin prefijo, a propósito
        check("/health sin prefijo -> 404", r.status_code == 404, str(r.status_code))
        r = await c.post("http://test/look", json={
            "image": base64.b64encode(FOTO).decode(), "deviceId": "ulleres-01",
        })
        check("/look sin prefijo -> 404", r.status_code == 404, str(r.status_code))
        rutas = main.app.openapi()["paths"]
        check("el esquema solo lleva rutas versionadas",
              all(p.startswith("/api/v1") for p in rutas
                  if p not in ("/provar", "/probar")),
              str(sorted(rutas)))

        # ---------------------------------------------------------------
        # 5. Errores
        # ---------------------------------------------------------------
        r = await c.post("/ask", content=trama(FOTO, b"\x00" * 100))
        check("audio de 3 ms -> 400", r.status_code == 400, r.text[:120])

        r = await c.post("/ask", content=struct.pack(">I", 99_999_999) + b"xx")
        check("longitud imposible -> 400", r.status_code == 400, r.text[:120])

        r = await c.post("/ask", content=struct.pack(">I", 5000) + FOTO)
        check("cuerpo cortado -> 400", r.status_code == 400, r.text[:120])

        # Un m4a de iPhone: "ftyp" va en el byte 4, no al principio. Con un
        # startswith acababa envuelto en una cabecera WAV que no le tocaba.
        enviadas.clear()
        usa(responde)
        M4A = (struct.pack(">I", 28) + b"ftypM4A " + b"\x00" * 2000)
        r = await c.post("/ask", content=trama(FOTO, M4A))
        check("un m4a llega 200", r.status_code == 200, r.text[:120])
        cuerpo = enviadas[0].content
        check("el m4a se manda tal cual, sin envolver",
              b"RIFF" not in cuerpo[:400] and b'filename="veu.m4a"' in cuerpo)
        check("de un m4a no se inventa la duración",
              "x-bonsai-audio-secs" not in r.headers
              and r.headers["x-bonsai-audio-bytes"] == str(len(M4A)))

        # El flujo de las gafas: la foto sube, suena el "Diga'm" y solo
        # entonces empieza el micro. Entre medias el cuerpo está en silencio y
        # la petición no se puede caer por eso.
        async def con_pausa():
            yield struct.pack(">I", len(FOTO)) + FOTO
            await asyncio.sleep(0.8)          # el clip del "Diga'm"
            yield PCM

        r = await c.post("/ask?deviceId=amb-pausa", content=con_pausa())
        check("una pausa entre la foto y el micro no rompe nada",
              r.status_code == 200, r.text[:150])
        check("y la reducción se hace mientras se espera",
              r.headers.get("x-bonsai-resize-wait-ms") == "0",
              r.headers.get("x-bonsai-resize-wait-ms"))

        # Micro mudo del todo: hay que soltar la conexión, no colgarse.
        import main as _main
        antes_tope = _main.ASK_SILENCIO
        _main.ASK_SILENCIO = 0.4

        async def se_queda_mudo():
            yield struct.pack(">I", len(FOTO)) + FOTO
            await asyncio.sleep(5)            # el firmware se ha colgado
            yield PCM

        t = time.perf_counter()
        r = await c.post("/ask?deviceId=mut", content=se_queda_mudo())
        tardo = time.perf_counter() - t
        _main.ASK_SILENCIO = antes_tope
        check("un micro que enmudece -> 408 y no se cuelga",
              r.status_code == 408 and tardo < 3,
              f"{r.status_code} en {tardo:.1f} s")

        r = await c.post("/ask?audioFormat=flac", content=trama(FOTO, PCM))
        check("formato inventado -> 400 antes de leer nada",
              r.status_code == 400, r.text[:120])

        r = await c.post("/ask?micRate=16000",
                         content=trama(FOTO, b"\x11\x22" * 16000 * 31))
        check("audio de 31 s -> 413", r.status_code == 413, r.text[:120])

        # Transcripción vacía: el micro no ha dado nada aprovechable.
        enviadas.clear()

        def mudo(request):
            if "transcriptions" in str(request.url):
                return httpx.Response(200, json={"text": "   "})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        usa(mudo)
        r = await c.post("/ask", content=trama(FOTO, PCM))
        check("no se entiende nada -> 422", r.status_code == 422, r.text[:140])
        # 8 = la buena, la solapada, el m4a, la de la pausa, la muda, la de
        # 3 ms, la de 31 s y esta. Las que fallan antes de tener la foto
        # entera (trama mal formada, formato inventado) no dejan fila.
        check("aun así se guarda la foto",
              len(memory.list_captures()) == 8, str(len(memory.list_captures())))

        # 429 de la cuota de whisper
        def sin_cuota(request):
            return httpx.Response(429, text="Please try again in 12.7s")

        usa(sin_cuota)
        r = await c.post("/ask", content=trama(FOTO, PCM))
        check("cuota agotada -> 429 con Retry-After",
              r.status_code == 429 and r.headers.get("retry-after") == "13",
              f"{r.status_code} {r.headers.get('retry-after')}")

    print()
    print(f"{len(fallos)} fallos" if fallos else "TODO CORRECTO, 0 tokens gastados")
    return 1 if fallos else 0


sys.exit(asyncio.run(principal()))
