"""End-to-end test of /ask without spending a single Groq token.

    python tests/test_api.py        (exits with code 1 if anything fails)


The shared HTTP transport (vision.get_client) is swapped for a fake one that
answers what Groq would answer and records what was sent to it. That way the
real payload is checked: the two preamble turns, the WAV header put on the
mic's PCM and the Whisper model.

Piper is real: it is local and spends nobody's quota.
"""
import asyncio
import base64
import json
import os
import struct
import sys
import tempfile
import time

os.environ.setdefault("GROQ_API_KEY", "fake-key")
os.environ["BONSAI_DB_PATH"] = tempfile.mkdtemp(prefix="ask-") + "/bonsai.db"
os.environ["BONSAI_CAPTURES_DIR"] = tempfile.mkdtemp(prefix="captures-")
# The repo root, so the `app` package can be imported.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from app import main, memory, stt, tts, vision


def use(transport):
    """Swaps the HTTP client in both modules that imported it.

    stt does `from vision import get_client`, so it keeps the function it saw
    back then: patching only vision.get_client does not reach it.
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    for module in (vision, stt):
        module.get_client = lambda c=client: c

sent = []
failures = []


def check(name, cond, extra=""):
    (print if cond else failures.append)(("OK   " if cond else "FAIL ") + name
                                         + (f"  {extra}" if extra else ""))
    if not cond:
        print("FAIL " + name + (f"  {extra}" if extra else ""))


def respond(request: httpx.Request) -> httpx.Response:
    sent.append(request)
    if "audio/transcriptions" in str(request.url):
        return httpx.Response(200, json={"text": "Què diu aquest cartell?"})
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "Diu «Prohibit el pas»."}}]
    })


# The Piper voice is fetched BEFORE the fake transport is installed. Otherwise
# the download (which uses the same shared client) eats Groq's fake answer and
# leaves a 64-byte .onnx that later blows up with a cryptic KeyError.
asyncio.run(tts.ensure_voice(tts.voice_for("ca")))

use(respond)

# A real minimal JPEG (signature included, to exercise sniff_mime) and one
# second of PCM16 at 16 kHz.
PHOTO = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x00" * 500
PCM = b"\x11\x22" * 16000        # 1.0 s at 16 kHz


def frame(photo: bytes, audio: bytes) -> bytes:
    return struct.pack(">I", len(photo)) + photo + audio


async def real_socket():
    """The chunked upload over a real socket instead of ASGI.

    The rest of the test uses httpx.ASGITransport, which calls the app
    directly: that checks the code behaves, but not that uvicorn delivers the
    chunks as they arrive. The firmware sends `Transfer-Encoding: chunked`
    without `Content-Length`, which is exactly what broke once.
    """
    import socket
    import threading

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))          # ephemeral port: no fixed ports
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(main.app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if server.started:
            break

    before = len(memory.list_captures())
    s = socket.create_connection(("127.0.0.1", port), 5)
    s.sendall(
        f"POST /api/v1/ask?deviceId=socket&micRate=16000 HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/octet-stream\r\n"
        "Transfer-Encoding: chunked\r\n\r\n".encode()
    )

    def chunk(d: bytes) -> bytes:
        return f"{len(d):x}\r\n".encode() + d + b"\r\n"

    s.sendall(chunk(struct.pack(">I", len(PHOTO)) + PHOTO))

    # Half a second of speech in 50 ms chunks, like the mic would send it.
    saved_while_talking = False
    for _ in range(10):
        await asyncio.sleep(0.05)
        s.sendall(chunk(b"\x11\x22" * 800))
        if len(memory.list_captures()) > before:
            saved_while_talking = True
    s.sendall(b"0\r\n\r\n")

    response = b""
    while b"\r\n\r\n" not in response:
        data = await asyncio.to_thread(s.recv, 4096)
        if not data:
            break
        response += data
    s.close()
    headers = response.split(b"\r\n\r\n")[0].decode(errors="replace")

    check("over a real socket, chunked and without Content-Length -> 200",
          headers.startswith("HTTP/1.1 200"), headers.splitlines()[0])
    check("and the response comes back chunked too",
          "transfer-encoding: chunked" in headers.lower())
    check("the photo is saved while the mic is still uploading",
          saved_while_talking)

    server.should_exit = True
    thread.join(timeout=5)


async def main_test():
    memory.init_db()
    transport = httpx.ASGITransport(app=main.app)
    # The version prefix lives in base_url, so the paths below are still
    # written as /ask, /look, /speak...
    async with httpx.AsyncClient(transport=transport,
                                 base_url=f"http://test{main.API_PREFIX}",
                                 timeout=60) as c:

        # ---------------------------------------------------------------
        # 1. The happy path
        # ---------------------------------------------------------------
        r = await c.post("/ask?deviceId=ulleres-01&audioFormat=pcm16&sampleRate=16000",
                         content=frame(PHOTO, PCM),
                         headers={"Content-Type": "application/octet-stream"})
        check("/ask returns 200", r.status_code == 200, r.text[:200])
        if r.status_code != 200:
            return

        text = base64.b64decode(r.headers["x-bonsai-text"]).decode()
        trans = base64.b64decode(r.headers["x-bonsai-transcript"]).decode()
        check("returns the transcript", trans == "Què diu aquest cartell?", trans)
        check("returns the answer", text == "Diu «Prohibit el pas».", text)
        check("the body is real audio", len(r.content) > 10000,
              f"{len(r.content)} bytes")
        check("pcm16 format at 16 kHz",
              r.headers["x-bonsai-format"] == "pcm16"
              and r.headers["x-bonsai-rate"] == "16000")
        check("reports the whisper model",
              r.headers["x-bonsai-stt-model"] == "whisper-large-v3-turbo",
              r.headers.get("x-bonsai-stt-model", "-"))
        check("reports how much audio it was",
              r.headers["x-bonsai-audio-secs"] == "1.00",
              r.headers.get("x-bonsai-audio-secs"))
        check("exposes the headers to JS",
              "X-Bonsai-Transcript" in r.headers["access-control-expose-headers"])

        # ---------------------------------------------------------------
        # 2. What was actually sent to Groq
        # ---------------------------------------------------------------
        stt_req, vis_req = sent[0], sent[1]
        check("transcribes first, then describes",
              "transcriptions" in str(stt_req.url)
              and "chat/completions" in str(vis_req.url))

        stt_body = stt_req.content
        check("the PCM gets a WAV header", b"RIFF" in stt_body[:600]
              and b"WAVEfmt" in stt_body[:600])
        check("with the real length, not 0xFFFFFFFF",
              struct.pack("<I", 32000) in stt_body[:600])
        check("asks for whisper turbo", b"whisper-large-v3-turbo" in stt_body)
        check("tells it the language", b'name="language"' in stt_body
              and b"ca" in stt_body)

        payload = json.loads(vis_req.content)
        msgs = payload["messages"]
        check("4 messages: system + the two turns + the photo", len(msgs) == 4,
              str([m["role"] for m in msgs]))
        check("turn 1: user «Hey Bonsai!»",
              msgs[1] == {"role": "user", "content": "Hey Bonsai!"}, str(msgs[1]))
        check("turn 2: assistant «Diga’m!» (what the glasses play)",
              msgs[2] == {"role": "assistant", "content": "Diga’m!"}, str(msgs[2]))
        check("the preamble goes BEFORE the prompt and the image",
              msgs[3]["role"] == "user"
              and msgs[3]["content"][0]["text"] == "Què diu aquest cartell?"
              and msgs[3]["content"][1]["type"] == "image_url")
        check("the image goes as detected jpeg",
              msgs[3]["content"][1]["image_url"]["url"].startswith(
                  "data:image/jpeg;base64,"))
        check("qwen is the vision model", payload["model"].startswith("qwen"),
              payload["model"])

        # ---------------------------------------------------------------
        # 3. The photo has been saved
        # ---------------------------------------------------------------
        cap_id = r.headers["x-bonsai-capture-id"]
        row = memory.get_capture(cap_id)
        check("there is a row in captures", row is not None)
        check("the file is on disk and weighs what the photo does",
              os.path.getsize(row["image_path"]) == len(PHOTO))
        check("stores what was said and what was answered",
              row["transcript"] == "Què diu aquest cartell?"
              and row["reply"] == "Diu «Prohibit el pas».")
        check("stores the timings", row["total_ms"] >= row["stt_ms"] >= 0
              and row["vision_ms"] is not None)
        check("stores which device it came from", row["device_id"] == "ulleres-01")

        # ---------------------------------------------------------------
        # 3 bis. The photo is saved WHILE the audio is still uploading
        # ---------------------------------------------------------------
        # This was wrong once: the whole body was read and only then saved, so
        # chunked upload bought nothing. Here the body is a generator that
        # checks the disk before releasing the audio.
        before = len(memory.list_captures())
        seen = {}

        async def chunked_body():
            yield struct.pack(">I", len(PHOTO)) + PHOTO
            # Yield control so the server can process what was already sent.
            for _ in range(20):
                await asyncio.sleep(0.01)
                if len(memory.list_captures()) > before:
                    break
            seen["saved_before"] = len(memory.list_captures()) > before
            yield PCM

        r = await c.post("/ask?deviceId=solapada", content=chunked_body())
        check("with a chunked body it returns 200", r.status_code == 200,
              r.text[:150])
        check("the photo was already saved before sending the audio",
              seen.get("saved_before") is True)

        # ---------------------------------------------------------------
        # 4. /look still has no preamble (its behaviour is unchanged)
        # ---------------------------------------------------------------
        sent.clear()
        r = await c.post("/look", json={
            "image": base64.b64encode(PHOTO).decode(), "deviceId": "ulleres-01",
            "audioFormat": "wav",
        })
        check("/look still works", r.status_code == 200, r.text[:200])
        msgs = json.loads(sent[0].content)["messages"]
        check("/look does NOT carry the preamble", len(msgs) == 2,
              str([m["role"] for m in msgs]))
        check("/look returns WAV", r.content[:4] == b"RIFF")

        # ---------------------------------------------------------------
        # 4 bis. /speak is what makes the "Diga'm" clip
        # ---------------------------------------------------------------
        r = await c.post("/speak?text=Diga%E2%80%99m!&audioFormat=pcm16&sampleRate=16000")
        check("/speak in pcm16 at 16 kHz", r.status_code == 200
              and r.headers["x-bonsai-format"] == "pcm16"
              and r.headers["x-bonsai-rate"] == "16000"
              and r.content[:4] != b"RIFF",       # raw, no header
              f"{r.status_code} {r.headers.get('x-bonsai-format')}")
        r = await c.post("/speak?text=hola")
        check("/speak without a format still gives WAV",
              r.status_code == 200 and r.content[:4] == b"RIFF")

        # The header lengths, which is what made /docs (and any <audio>) show
        # 0:00 in silence: with 0xFFFFFFFF a player cannot tell the duration.
        riff, data_len = (struct.unpack("<I", r.content[o:o + 4])[0] for o in (4, 40))
        check("the WAV carries the real lengths, not 0xFFFFFFFF",
              riff == len(r.content) - 8 and data_len == len(r.content) - 44,
              f"riff={riff} data={data_len} file={len(r.content)}")
        check("and comes with Content-Length, unchunked",
              r.headers.get("content-length") == str(len(r.content))
              and "transfer-encoding" not in r.headers,
              str(dict(r.headers)))

        # With GET too: a browser's <audio src="..."> (and /docs' player)
        # always asks by GET, and used to get a 405.
        # Bytes and length are not compared with the POST's: Piper adds random
        # noise on every synthesis and the same "hola" gives between 16.9 KB
        # and 22.6 KB (measured). What must match is the format, not the size.
        g = await c.get("/speak?text=hola")
        check("/speak answers GET as well",
              g.status_code == 200 and g.content[:4] == b"RIFF"
              and g.headers["content-type"] == r.headers["content-type"]
              and g.headers["x-bonsai-rate"] == r.headers["x-bonsai-rate"]
              and g.headers["x-bonsai-voice"] == r.headers["x-bonsai-voice"]
              and len(g.content) > 1000,
              f"{g.status_code}, {len(g.content)} bytes")
        routes = main.app.openapi()["paths"]["/api/v1/speak"]
        check("and both methods show up in /docs", sorted(routes) == ["get", "post"],
              str(sorted(routes)))

        # ---------------------------------------------------------------
        # 4 quater. The voice comes from the language; it cannot be requested
        # ---------------------------------------------------------------
        params = {q["name"] for q in routes["get"]["parameters"]}
        check("/speak no longer has a voice parameter", "voice" not in params,
              str(sorted(params)))
        check("and it does have lang", "lang" in params)
        fields = main.app.openapi()["components"]["schemas"]["LookRequest"]["properties"]
        check("/look neither: lang yes, voice no",
              "lang" in fields and "voice" not in fields, str(sorted(fields)))

        # An unknown language is reported, not quietly answered in Catalan.
        r = await c.get("/speak?text=hola&lang=fr")
        check("an unknown language -> 400 with the list",
              r.status_code == 400 and "ca" in r.text, r.text[:160])

        # And a known one uses the voice from tts.VOICES, without asking.
        r = await c.get("/speak?text=hola&lang=ca")
        check("the language's voice is the one from the map",
              r.headers.get("x-bonsai-voice") == main.tts.VOICES["ca"],
              r.headers.get("x-bonsai-voice", "(none)"))
        check("the schema says the response is audio, not JSON",
              "audio/wav" in routes["get"]["responses"]["200"]["content"]
              and "application/json" not in routes["get"]["responses"]["200"]["content"],
              str(sorted(routes["get"]["responses"]["200"]["content"])))

        # ---------------------------------------------------------------
        # 4 ter. The API lives at /api/v1, and only there
        # ---------------------------------------------------------------
        r = await c.get("/health")
        check("/api/v1/health returns 200", r.status_code == 200, r.text[:120])
        check("and says which version it is", r.json().get("api", {}).get("version") == "v1",
              str(r.json().get("api")))
        r = await c.get("http://test/health")     # unprefixed, on purpose
        check("/health without prefix -> 404", r.status_code == 404, str(r.status_code))
        r = await c.post("http://test/look", json={
            "image": base64.b64encode(PHOTO).decode(), "deviceId": "ulleres-01",
        })
        check("/look without prefix -> 404", r.status_code == 404, str(r.status_code))
        routes = main.app.openapi()["paths"]
        check("the schema only carries versioned routes",
              all(p.startswith("/api/v1") for p in routes
                  if p not in ("/provar", "/probar")),
              str(sorted(routes)))

        # ---------------------------------------------------------------
        # 5. Errors
        # ---------------------------------------------------------------
        r = await c.post("/ask", content=frame(PHOTO, b"\x00" * 100))
        check("3 ms of audio -> 400", r.status_code == 400, r.text[:120])

        r = await c.post("/ask", content=struct.pack(">I", 99_999_999) + b"xx")
        check("impossible length -> 400", r.status_code == 400, r.text[:120])

        r = await c.post("/ask", content=struct.pack(">I", 5000) + PHOTO)
        check("truncated body -> 400", r.status_code == 400, r.text[:120])

        # An iPhone m4a: "ftyp" sits at byte 4, not at the start. With a
        # startswith it ended up wrapped in a WAV header it should not have.
        sent.clear()
        use(respond)
        M4A = (struct.pack(">I", 28) + b"ftypM4A " + b"\x00" * 2000)
        r = await c.post("/ask", content=frame(PHOTO, M4A))
        check("an m4a gets 200", r.status_code == 200, r.text[:120])
        body = sent[0].content
        check("the m4a is sent as-is, unwrapped",
              b"RIFF" not in body[:400] and b'filename="voice.m4a"' in body)
        check("for an m4a the duration is not invented",
              "x-bonsai-audio-secs" not in r.headers
              and r.headers["x-bonsai-audio-bytes"] == str(len(M4A)))

        # The glasses' flow: the photo uploads, the "Diga'm" plays and only
        # then does the mic start. In between the body is silent and the
        # request must not fall over because of it.
        async def with_pause():
            yield struct.pack(">I", len(PHOTO)) + PHOTO
            await asyncio.sleep(0.8)          # the "Diga'm" clip
            yield PCM

        r = await c.post("/ask?deviceId=amb-pausa", content=with_pause())
        check("a pause between the photo and the mic breaks nothing",
              r.status_code == 200, r.text[:150])
        check("and the resize happens while waiting",
              r.headers.get("x-bonsai-resize-wait-ms") == "0",
              r.headers.get("x-bonsai-resize-wait-ms"))

        # Mic gone completely silent: drop the connection, do not hang.
        previous_cap = main.ASK_SILENCE
        main.ASK_SILENCE = 0.4

        async def goes_silent():
            yield struct.pack(">I", len(PHOTO)) + PHOTO
            await asyncio.sleep(5)            # the firmware has hung
            yield PCM

        t = time.perf_counter()
        r = await c.post("/ask?deviceId=mut", content=goes_silent())
        took = time.perf_counter() - t
        main.ASK_SILENCE = previous_cap
        check("a mic that goes silent -> 408 and no hang",
              r.status_code == 408 and took < 3,
              f"{r.status_code} in {took:.1f} s")

        r = await c.post("/ask?audioFormat=flac", content=frame(PHOTO, PCM))
        check("made-up format -> 400 before reading anything",
              r.status_code == 400, r.text[:120])

        r = await c.post("/ask?micRate=16000",
                         content=frame(PHOTO, b"\x11\x22" * 16000 * 31))
        check("31 s of audio -> 413", r.status_code == 413, r.text[:120])

        # Empty transcript: the mic gave nothing usable.
        sent.clear()

        def silent(request):
            if "transcriptions" in str(request.url):
                return httpx.Response(200, json={"text": "   "})
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

        use(silent)
        r = await c.post("/ask", content=frame(PHOTO, PCM))
        check("nothing understood -> 422", r.status_code == 422, r.text[:140])
        # 8 = the good one, the overlapped one, the m4a, the paused one, the
        # silent one, the 3 ms one, the 31 s one and this one. The ones that
        # fail before the photo is complete (bad frame, made-up format) leave
        # no row.
        check("the photo is stored anyway",
              len(memory.list_captures()) == 8, str(len(memory.list_captures())))

        # 429 from whisper's quota
        def out_of_quota(request):
            return httpx.Response(429, text="Please try again in 12.7s")

        use(out_of_quota)
        r = await c.post("/ask", content=frame(PHOTO, PCM))
        check("quota exhausted -> 429 with Retry-After",
              r.status_code == 429 and r.headers.get("retry-after") == "13",
              f"{r.status_code} {r.headers.get('retry-after')}")

    use(respond)
    await real_socket()

    print()
    print(f"{len(failures)} failures" if failures else "ALL GOOD, 0 tokens spent")
    return 1 if failures else 0


sys.exit(asyncio.run(main_test()))
