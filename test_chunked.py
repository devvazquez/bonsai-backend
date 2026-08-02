"""¿Se solapa de verdad la subida en trozos? Socket a pelo, como el firmware.

    python test_chunked.py     (sale con código 1 si no se solapa)

Manda la foto y luego 3 s de mustras a 100 ms por trozo, con
`Transfer-Encoding: chunked` y sin `Content-Length`, que es lo que hará la
XIAO con un WiFiClient. Comprueba que el servidor guarda la foto MIENTRAS
todavía se está hablando y no al final.

Esto estuvo mal: el cuerpo se leía de una vez y la foto no se guardaba hasta
que la petición acababa. Sin sockets a pelo no se veía, porque el resultado
era correcto igualmente — solo llegaba tarde.

Groq va simulado en localhost: 0 tokens.
"""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

CAPS = tempfile.mkdtemp(prefix="raw-")
DB = tempfile.mkdtemp(prefix="raw-db-") + "/bonsai.db"

open("/tmp/groq_fals.py", "w").write('''
from fastapi import FastAPI, Request
app = FastAPI()

@app.post("/openai/v1/audio/transcriptions")
async def stt(request: Request):
    await request.body()
    return {"text": "De quina marca es la motxilla?"}

@app.post("/openai/v1/chat/completions")
async def chat(request: Request):
    await request.body()
    return {"choices": [{"message": {"content": "Es una motxilla negra de FILA."}}]}
''')

entorno = {**os.environ, "GROQ_API_KEY": "x",
           "BONSAI_DB_PATH": DB, "BONSAI_CAPTURES_DIR": CAPS}

groq = subprocess.Popen(
    ["/home/user/bonsai-backend/.venv/bin/python", "-m", "uvicorn",
     "groq_fals:app", "--port", "8099"],
    cwd="/tmp", env=entorno, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

arranque = (
    "import sys, time; sys.path.insert(0, '/home/user/bonsai-backend');"
    "import stt, groq_vision, memory;"
    "_sc = memory.save_capture;"
    "memory.save_capture = lambda *a, **k: "
    "  (print('GUARDA', time.time(), flush=True), _sc(*a, **k))[1];"
    "stt.GROQ_STT_URL = 'http://127.0.0.1:8099/openai/v1/audio/transcriptions';"
    "groq_vision.GROQ_URL = 'http://127.0.0.1:8099/openai/v1/chat/completions';"
    "import uvicorn, main; uvicorn.run(main.app, host='127.0.0.1', port=8098, log_level='error')"
)
back = subprocess.Popen(["/home/user/bonsai-backend/.venv/bin/python", "-c", arranque],
                        cwd="/home/user/bonsai-backend", env=entorno,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

guardada = []
def lee_servidor():
    for linea in back.stdout:
        if linea.startswith("GUARDA"):
            guardada.append(float(linea.split()[1]))
threading.Thread(target=lee_servidor, daemon=True).start()

# Un JPEG cualquiera de ~100 KB: lo que importa aquí es cuándo llega, no qué es.
FOTO = (bytes.fromhex("ffd8ffe000104a464946000101000001000100 00") .replace(b" ", b"")
        + b"\x00" * 100_000)
SEGUNDOS, TROZO_MS, RATE = 3.0, 100, 16000
por_trozo = int(RATE * 2 * TROZO_MS / 1000)

# Esperar a que arranquen
for _ in range(120):
    try:
        socket.create_connection(("127.0.0.1", 8098), 1).close()
        socket.create_connection(("127.0.0.1", 8099), 1).close()
        break
    except OSError:
        time.sleep(0.5)
time.sleep(2)

s = socket.create_connection(("127.0.0.1", 8098))
s.sendall(
    b"POST /ask?deviceId=xiao&audioFormat=pcm16&sampleRate=16000&micRate=16000 HTTP/1.1\r\n"
    b"Host: 127.0.0.1:8098\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Transfer-Encoding: chunked\r\n\r\n"
)

def chunk(d):
    return f"{len(d):x}\r\n".encode() + d + b"\r\n"

T0 = time.time()
s.sendall(chunk(struct.pack(">I", len(FOTO)) + FOTO))
print(f"  [XIAO] {time.time()-T0:5.2f} s  foto enviada, empieza a hablar")

for _ in range(int(SEGUNDOS * 1000 / TROZO_MS)):
    time.sleep(TROZO_MS / 1000)
    s.sendall(chunk(b"\x11\x22" * (por_trozo // 2)))
fin_habla = time.time()
print(f"  [XIAO] {fin_habla-T0:5.2f} s  deja de hablar")
s.sendall(b"0\r\n\r\n")

resp = b""
while b"\r\n\r\n" not in resp:
    resp += s.recv(4096)
primer_byte = time.time()
cab = resp.split(b"\r\n\r\n")[0].decode(errors="replace")
s.close()

time.sleep(0.5)
print()
print(" ", cab.splitlines()[0])
if guardada:
    d = guardada[0] - T0
    print(f"  foto guardada en el servidor a los .. {d:5.2f} s")
    print(f"  deja de hablar a los ................ {fin_habla-T0:5.2f} s")
    print(f"  cabeceras de vuelta a los ........... {primer_byte-T0:5.2f} s")
    print()
    bien = d < fin_habla - T0 - 0.3
    if bien:
        print(f"  => Se guarda {fin_habla-T0-d:.1f} s ANTES de acabar de hablar: SÍ se solapa.")
    else:
        print("  => NO se solapa: el servidor no ve nada hasta que el cuerpo acaba.")
else:
    print("  el servidor no llegó a guardar nada")
    bien = False

back.terminate(); groq.terminate()
sys.exit(0 if bien else 1)
