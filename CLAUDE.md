# Bonsai Backend — notas para Claude

## La API va versionada: `/api/v1`

Las rutas se declaran en un `APIRouter` (`main.api`) y al final de `main.py` se
monta con `prefix=API_PREFIX`.

**No hay alias sin prefijo**: `/look` y `/health` a secas dan 404. Los hubo un
rato, por si quedaba algún dispositivo con el firmware viejo, pero no queda
ninguno y mantener dos caminos a lo mismo solo da pie a que uno se quede atrás.

Las páginas no se versionan: `/provar` y `/admin` se abren en un navegador, no
son API. Cuelgan de `app` directamente y no del router.

Al añadir un endpoint nuevo, `@api.post(...)` y no `@app.post(...)`, o solo
existirá en las rutas viejas. Hay una comprobación en `tests/test_api.py` (bloque
«4 ter») que verifica el prefijo, el alias y que el esquema no se duplique.

## Un proveedor de visión y uno de voz, y ya está

`vision.py` habla con Groq y `tts.py` sintetiza con Piper. No hay dónde elegir:
**no existen los campos `provider` ni `tts`** en las peticiones, ni las variables
`VISION_PROVIDER` / `TTS_PROVIDER`.

Los hubo. Había un `gemini_vision.py` y un edge-tts detrás de una capa que
repartía, y se quitaron los dos porque ya estaba decidido con datos cuál ganaba:

- **Groq** frente a Gemini: 552 ms de visión (551-554) frente a 844 ms (649-937)
  con la misma imagen, y sobre todo mucho más regular.
- **Piper** frente a edge-tts: 205 ms de mediana frente a 1.320 ms con texto
  nuevo, sin red y sin caché ajena que engañe al medir.

Lo que se perdía manteniéndolos era una capa de reparto de una sola rama y dos
caminos donde uno se queda desfasado. Lo que se pierde de verdad es la vía de
escape del 429 de Groq: **8.000 tokens/minuto son unas 3 fotos por minuto a
896 px**, y antes se desarrollaba con Gemini para no pelearse con eso. Ahora
toca esperar el reset o pagar; ver «Cuota de Groq» aquí abajo.

`groq_vision.py` también desapareció: su contenido está dentro de `vision.py`,
que ahora es «el módulo de visión + el cliente HTTP compartido». `stt.py` y
`tts.py` siguen haciendo `from .vision import get_client`. Ojo: `tts.py` hace ese
import **dentro de una función** (para bajar las voces), así que no basta con
mirar las cabeceras de los ficheros.

Antes de medir o probar cualquier cosa, `python scripts/bench_latency.py --selftest`:
comprueba el código sin gastar un solo token.

## Cuota de Groq: no la gastes

La cuenta está en el plan gratuito y **el límite es por organización, no por API key**
(documentado en https://console.groq.com/docs/rate-limits: *"Rate limits apply at the
organization level, not individual users"*). Crear una key nueva **no** reinicia nada:
hay que esperar el reset o pasar a un plan de pago.

Los límites que se agotan primero, medidos con `qwen/qwen3.6-27b`:

- **TPD (tokens/día): 200.000** — es el que duele, tarda ~24 h en reiniciarse.
- **TPM (tokens/minuto): 8.000** — salta constantemente si las imágenes van grandes.
- RPD (peticiones/día): 1.000 — sobra de largo.

Se ven en vivo en las cabeceras `x-ratelimit-*` de cualquier respuesta de Groq.

### Reglas al probar

1. **Reduce SIEMPRE la imagen antes de mandarla a Groq.** Una foto de móvil de 4032×3024
   (3,1 MB) gasta del orden de 50.000 tokens por petición: seis pruebas y el día está
   quemado. La misma foto a 896 px son 64 KB y **2.656 tokens** (cifra dicha por el
   propio error 429 de Groq: `Requested 2656`), unas 20 veces menos.
2. **Una sola llamada por prueba**, no un barrido de idiomas y prompts. Cada variante es
   otra factura de tokens. `scripts/bench_latency.py` obliga a esto: sin `--yes` no llama a
   nadie, y aborta si el plan pasa de 20.000 tokens estimados.
3. Para probar TTS, memoria, el panel o `/health` **no hace falta Groq**: usa `/speak`,
   `/memory`, `/admin` y `/health`, que no tocan el proveedor de visión.

Medido con la imagen reducida a 896 px: visión ~1,1 s. Con la original de 3,1 MB: ~3,7 s.
Reducir es más rápido *y* más barato.

## Cómo levantar el proyecto en local

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
GROQ_API_KEY=... .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

### Dónde está cada cosa

El código está en el paquete `app/` y se arranca con **`app.main:app`**, no
`main:app`. Dentro de `app/` los imports son relativos (`from . import vision`).

```
app/       main, vision (Groq), stt, tts (Piper), images, memory, panel, audios, static/
tests/     test_api.py (automático, 0 tokens) · smoke.py (manual, gasta cuota)
scripts/   bench_latency.py, generate_audios.py, run-local.sh, run-local.ps1
```

`tests/` y `scripts/` se ejecutan desde la raíz del repositorio
(`python tests/test_api.py`); los dos se ponen la raíz en el `sys.path` ellos
solos, y hay un `conftest.py` en la raíz para que `pytest` haga lo mismo.

Ojo con `tts.py`: hace `from . import vision` **dentro de una función**, para
bajar las voces reutilizando el cliente HTTP compartido. No basta con mirar las
cabeceras de los ficheros al buscar dependencias.

### El código va en inglés; el README y `/provar`, no

Identificadores, comentarios y docstrings, todo en inglés, y los comentarios de
**1 o 2 líneas**: dicen por qué, no qué. El razonamiento largo vive aquí y en
`docs/BENCHMARKS.md`, que es su sitio; repetirlo en el código solo daba dos
copias que se separan.

Se quedan en catalán/castellano dos cosas, a propósito: el **README**, que lo lee
una persona, y las **cadenas de `/provar`**, que también. El panel `/admin`
(`panel.py`) sí está traducido entero, interfaz incluida.

Lo que **no** se traduce nunca, aunque suene raro en un fichero en inglés:

- Cabeceras `X-Bonsai-*` y `X-API-Token`.
- Campos JSON (`deviceId`, `audioFormat`, `sampleRate`, `maxSide`, `micRate`…).
- Rutas, incluidas `/provar` y su alias `/probar`.
- Nombres de variables de entorno. **Cuidado con esta trampa**: el nombre Python
  sí se traduce y el de entorno no, así que `ASK_SILENCE` lee
  `ASK_SILENCE_TIMEOUT_SECONDS`, `ASK_MAX_SECONDS` lee `ASK_MAX_AUDIO_SECONDS` y
  `ASK_MAX_IMAGE` lee `ASK_MAX_IMAGE_BYTES`.
- El esquema de SQLite: renombrar una tabla o una columna es una migración sobre
  datos vivos de la VPS.
- Los ids de voz de Piper (`ca_ES-upc_ona-medium`) y los códigos `ca`/`es`/`en`.

### `clips` ahora se llama `audios`

Para que coincida con el firmware, que ya les llama `Audio::DefaultAudios`.
Cambió el módulo (`audios.py`), las funciones, **la ruta** (`/clips` → `/audios`,
`/clips/{id}` → `/audios/{audio_id}`), la clave de la respuesta (`clips` →
`audios`) y la cabecera (`X-Bonsai-Clip` → `X-Bonsai-Audio`). Las dos últimas son
contrato con el dispositivo: hay que actualizar los dos lados a la vez.

**Los ids no se tocan** (`no_wifi`, `start_talking`, `first_boot`,
`missing_config`): el firmware los pide por nombre.

Y `scripts/generate_audios.py` ya no tiene su propia tabla de frases; lee
`app/audios.py`, que es la misma que sirve la API. Antes la frase del «Digue'm»
estaba escrita en dos sitios.

**La voz de Piper NO está en el repositorio**: se baja sola la primera vez (63 MB).
Si estás corriendo `tests/test_api.py` en una máquina limpia, ojo con esto: el test cambia
el cliente HTTP compartido por uno de mentira, y si la descarga cae dentro de ese
cambio se guarda la respuesta falsa de Groq como si fuera el `.onnx`, y luego peta
con un `KeyError: 'num_symbols'` que no dice nada. Por eso el test baja la voz
**antes** de poner el transporte falso, y `ensure_voice` rechaza cualquier descarga
de menos de 1 KB.

## Configuración más rápida de `/look` (medido)

Imagen reducida a 896 px e `pcm16` a 16 kHz: **primer byte de audio a ~1.030 ms**.

Cosas que NO cambian la latencia, comprobadas para no volver a probarlas:

- **El formato de audio.** El audio tarda 349-391 ms en salir sea `pcm16` a 22 kHz o
  `mulaw` a 8 kHz. Elige por ancho de banda, no por velocidad.
- Bajar de 896 a 672 px en Groq (536 ms frente a 552 ms, dentro del ruido).

## Pendiente

- **`/admin` no se monta.** `panel.build()` no lo llama nadie desde `main.py`, y
  la propia función tampoco hace el `ui.run_with(app)` que NiceGUI necesita para
  colgarse de FastAPI. O sea que la ruta da 404 siempre, tenga o no `ADMIN_PASSWORD`.
  Viene de antes de la limpieza; el resto de esta nota describe cómo debería quedar.

Descartado ya con datos: **streaming de visión hacia el TTS**. Medido con `--mode ttft`,
el primer token llega a 1.246 ms y la frase completa a 1.303 ms: 57 ms de diferencia. No
merece la pena. El cuello de botella es el TTS.

**Se usa Piper con `ca_ES-upc_ona-medium`**: 205 ms de mediana, y sin caché que engañe
porque sintetiza de cero cada vez. Una petición completa con foto pasó de 2.575 ms a
1.170 ms.

### La voz no se pide: la decide el idioma

Los endpoints reciben `lang` y la voz sale de un diccionario en el código,
`tts.VOICES`. **No hay parámetro `voice`, ni `/voices`, ni `descargar_voces.py`**:
se quitaron los tres a la vez porque
elegir voz por petición no lo usa nadie (el firmware manda un idioma) y obligaba
a validar en el endpoint algo que ya está decidido en el servidor.

Cambiar la voz de un idioma es escribir otro nombre del repositorio de Piper en
ese diccionario. **La ruta de descarga se deduce del nombre** (`_hf_path`:
`ca_ES-upc_ona-medium` → `ca/ca_ES/upc_ona/medium`), así que no hay una segunda
tabla que mantener al lado, que es lo que había antes.

Si la voz no está en disco se baja sola (`tts.ensure_voice`, con un
candado por voz para que dos peticiones simultáneas no bajen 63 MB dos veces).
Medido: 13,1 s la primera petición en castellano, 53 ms la siguiente.

Ojo con el orden: **la descarga va antes de leer el `sample_rate` del modelo**.
Sin el `.onnx.json` no se sabe a qué frecuencia habla la voz, y suponer 22050
cuando es de 16 kHz (upc_pau-x_low) haría que sonara acelerada.

Un idioma que no esté en el diccionario da un 400 diciendo cuáles hay, en vez de
contestar en catalán por lo bajo.

**En catalán solo hay tres voces** en rhasspy/piper-voices, comprobado a mano
contra el repositorio: `upc_ona-medium` (la de por defecto, 63 MB, 215 ms),
`upc_ona-x_low` (20 MB) y `upc_pau-x_low` (voz masculina, 28 MB, 145 ms). No
existen `low` ni `high` de ona ni una `medium` de pau: dan 404.

El formato va en `audioFormat` y son `pcm16`, `mulaw` o `wav`. El WAV de una frase
pesa 247 KB, que importa por BLE: para el I2S, `pcm16` o `mulaw` en crudo.

### `/speak` acepta GET además de POST

Porque un navegador pide el audio con `<audio src="...">`, y eso es **siempre un GET**. El
reproductor de `/docs` hace justo eso: aunque el POST haya devuelto el audio, monta un
`<source src="/api/v1/speak?...">` y vuelve a pedirlo. Con POST-only se comía un 405 y se
quedaba en `0:00` sin sonar, con el audio del POST descargado al lado.

Encaja sin forzar nada: `/speak` no cambia nada en el servidor, todo va en la query y
pedirlo dos veces da lo mismo. Van **dos decoradores** (`@api.get` y `@api.post`) y no un
`api_route(methods=[...])`, porque con los dos métodos en una ruta FastAPI genera el mismo
`operationId` y avisa de que está duplicado.

`/look` y `/ask` siguen siendo POST-only, y ahí no molesta: el cuerpo es la foto.

### El esquema dice que la respuesta es audio, no JSON

Sin `responses=`/`response_class=`, FastAPI apunta `application/json` en el esquema de los
tres endpoints que hablan, que devuelven audio. `_RESPUESTA_AUDIO` lo arregla y se pasa a
`/look`, `/ask` y `/speak`.

### El WAV va entero, no troceado, y por un motivo

`tts.wav_header` sin `data_bytes` pone 0xFFFFFFFF en las longitudes, que es lo
único que se puede hacer si se responde sobre la marcha. Al ESP32 le da igual, pero **un
reproductor no puede calcular la duración y enseña 0:00 sin sonar**: se vio probando
`/speak` desde `/docs`, con un cuerpo de 18 KB de audio de verdad que ningún `<audio>`
quería tocar.

Así que con `wav` se junta todo antes de responder y la cabecera lleva las longitudes
reales (y un `Content-Length`). No se pierde streaming donde importa: el I2S se lleva
`pcm16` o `mulaw`, que siguen troceados, y quien pide `wav` es un navegador, que necesita
el fichero entero igualmente. Con Piper son los ~205 ms de la síntesis.

Efecto secundario en `/provar`: para `wav`, «primer byte» y «audio completo» pasan a ser
casi el mismo número. No es que haya empeorado nada, es que antes el primer byte era una
cabecera que no servía.

## `/ask`: la foto y la pregunta dicha en voz alta

Mismo camino que `/look` pero con voz por delante. El cuerpo va en crudo,
`[4 bytes de longitud][foto JPEG][audio]`, y admite `Transfer-Encoding: chunked`
sin `Content-Length`, que es como lo mandará el firmware.

**El cuerpo se lee en dos tiempos a propósito** (`_Trama.foto()` y
`_Trama.audio()`). Si se lee de una vez —o con `await request.body()`— la foto
no se guarda hasta el final y mandar en trozos no sirve de nada. La primera
versión lo hacía mal y solo se vio al medirlo con sockets a pelo: la foto se
guardaba a los 3,01 s, clavada al final de la frase. Ahora, 0,01 s.

Y ojo con lo que se solapa: **la subida, no la transcripción**. Whisper
necesita el audio entero, así que no empieza hasta que se cierra la petición.

Medido de verdad (foto de una mochila + 5 s de pregunta en m4a): Whisper 295 ms,
visión 1.158-1.278 ms, **primer byte de audio a 1.897 ms** con la foto ya a
896 px, y 2.685 ms si llega de 12 MP (reducirla cuesta 703 ms, más que
transcribir). O sea que `/ask` suma ~870 ms sobre los ~1.030 ms de `/look`.

Cuatro cosas que conviene no volver a averiguar:

1. **Whisper NO gasta la cuota de texto de Groq.** Se factura por segundos de
   audio, así que probar `/ask` no te deja sin `/look`. Aun así el tope de
   30 s (`ASK_MAX_AUDIO_SECONDS`) está por algo: un micro que se quede abierto
   sí puede quemar la cuota de audio del día.
2. **La clave es la misma `GROQ_API_KEY`** para transcribir y para ver: sin ella,
   `/ask` devuelve 500.
3. **Al PCM del micro hay que ponerle cabecera WAV con las longitudes de
   verdad.** No sirve `tts.wav_header`, que pone 0xFFFFFFFF porque
   responde sobre la marcha: Whisper rechaza un WAV con longitudes imposibles.
   Por eso hay una `stt.wav_header` aparte.
4. **El m4a no empieza por su firma**: los 4 primeros bytes son el tamaño de la
   caja y `ftyp` viene detrás, así que un `startswith` no lo detecta y el
   fichero acababa envuelto en una cabecera WAV que no le tocaba. Es lo que
   graba un iPhone, o sea que salta a la primera prueba con audio de verdad.
   El MP3 sin ID3 (sync 0xFF Ex) NO se detecta a propósito: esos dos bytes
   salen a menudo en PCM en crudo y confundirlos sería peor.

Antes de la foto y la pregunta se le dan por dichos dos turnos —
`user: Hey Bonsai!` / `assistant: Diga’m!` (`vision.VOICE_PREAMBLE`)— para que
conteste como quien sigue una conversación. `/look` no los lleva.

**No son inventados: es lo que pasa de verdad.** El firmware detecta "Hey
Bonsai", hace la foto y, mientras sube, suena por el altavoz un clip grabado
que dice "Diga'm". Por eso van en `ASK_WAKE_PHRASE`/`ASK_WAKE_REPLY`: si se
cambia el clip de las gafas hay que cambiarlas o le estaremos contando al
modelo una conversación que no ha ocurrido.

El clip **no está en el repositorio a propósito**: el dispositivo se lo baja al
primer arranque con `/speak?text=...&audioFormat=pcm16&sampleRate=16000` y lo
guarda en la SD (16 KB, 0,52 s). Así siempre es la voz que hay puesta en el
servidor y no hay dos copias que se separen. `scripts/generate_audios.py` lo deja en
`assets/` si lo quieres a mano desde el ordenador, pero esa carpeta está en el
.gitignore. Ojo: Piper mete ruido aleatorio en cada síntesis, así que dos
generaciones no dan ficheros idénticos al byte.

Ese flujo deja al servidor esperando con la foto en la mano mientras suena el
clip y la persona habla, así que **la foto se reduce en ese hueco**, en un hilo
aparte. Con una de 12 MP son 703 ms que dejan de estar en el camino crítico.
`X-Bonsai-Resize-Wait-Ms` dice lo que se ha llegado a esperar de verdad:
normalmente 0.

El hueco entre la foto y la primera muestra también obliga a un timeout **por
trozo** y no total (`ASK_SILENCE_TIMEOUT_SECONDS`, 15 s): si el micro enmudece
del todo hay que soltar la conexión con un 408 en vez de dejarla abierta.

Para probarlo sin gastar nada: `python tests/test_api.py`. Sustituye el cliente
HTTP por uno de mentira y comprueba el payload de verdad (los dos turnos, la
cabecera WAV, el modelo). Ojo: `stt.py` hace `from .vision import get_client`,
así que parchear solo `vision.get_client` no le llega — hay que tocar los dos
módulos.

## Endpoints de imagen: `/look` y `/ask`

Solo hay estos dos. `/describe` se eliminó. Devolvía JSON con el audio en base64 (33 % más de bytes y algo que
descodificar) y solo tenía sentido si hubiera una app web en medio. No la hay: la ESP32
llama a la API directamente, y si algún día hay app será para configurar el dispositivo de
vez en cuando, no para que funcionen las gafas.

`/look` devuelve las muestras en crudo y en streaming, que es lo que quiere el I2S del
MAX98357A, y el texto va en la cabecera `X-Bonsai-Text` (base64, porque las cabeceras son
ASCII). El formato es `pcm16`, `mulaw` o `wav`.

**La respuesta va con `Transfer-Encoding: chunked`**, no con `Content-Length`:
es streaming, al empezar no se sabe cuánto audio habrá. O sea que cada trozo
lleva delante su tamaño en hexadecimal y un `\r\n`. Quien lo lea del socket a
mano (el ESP32) tiene que quitarlos antes de escribir al I2S o se oye un clic
por trozo. Comprobado pidiendo /speak con un socket a pelo: el cuerpo empieza
por `3fda\r\n`, no por las muestras.

Lo importante del streaming: el primer byte de audio sale a los 1.024-1.463 ms (casi todo
es la visión) y a partir de ahí el audio llega más rápido de lo que se escucha, así que la
descarga se solapa con la reproducción y **deja de sumar latencia**. Solo hay que ir más
rápido que el tiempo real:

- `pcm16` 22050 Hz: 44,1 KB/s en tiempo real
- `pcm16` 16000 Hz: 32,0 KB/s  ← el equilibrio razonable
- `mulaw` 16000 Hz: 16,0 KB/s
- `mulaw`  8000 Hz:  8,0 KB/s  ← si el WiFi va justo

No separes la visión y la voz en dos llamadas para ganar tiempo: son dos viajes de ida y
vuelta y sale peor. Yo lo sugerí una vez y estaba equivocado; lo que se quería (evitar el
base64 y sonar antes) lo da `/look` en una sola petición.

Ya arreglado: el timeout con `str(e)` vacío (ahora `vision.describe_error`) y el
`data:image/jpeg` fijo (ahora `vision.sniff_mime`).

## TTS catalán del BSC y Projecte AINA: mirado y descartado (por ahora)

Medido con la **misma locutora `ona`** que usa Piper y la misma frase, mismo Xeon:

- Piper `upc_ona` medium: **205 ms**, RTF 26x, modelo de 63 MB.
- Matxa v2 central + WaveNeXt (`BSC-LT/matxa-tts-v2-ca-central-graphemes`):
  **1.053 ms**, RTF 5,8x, modelo de 271 MB.

Cinco veces más lento. Suena mejor (Matcha-TTS con flow matching, 47 voces), pero para
las gafas manda la latencia. Se mantiene Piper.

Cómo ejecutarlo si hace falta: el Space `BSC-LT/matxa-tts-v2` lleva los ONNX con el
vocoder ya incluido, y se corre con onnxruntime igual que Piper. Entradas: `x`,
`x_lengths`, `scales` (temperature, length_scale), `spks`. La salida útil es la segunda
(`hfwaveform`), float a 22050 Hz. Hace falta el módulo `text/` del Space para el cleaner
`catalan_text`, más `unidecode` y `num2words`.

Ojo con las licencias:

- v2 central: **Apache-2.0**, la única buena sin ataduras.
- v2 multiacento (balear, valencià, nord-occidental): **no comercial**, hay que licenciar
  con los locutores a través del BSC y La Fresca Produccions.
- Todas las v1 de projecte-aina y el StyleTTS2 catalán: **GPL-3.0**.

## Administrar la base de datos

Una sola: **`/admin`** (`panel.py`). Explorar cualquier tabla, crearlas, añadir columnas,
editar filas y ejecutar SQL a mano. Va con NiceGUI montado sobre el mismo FastAPI, así que
no hay contenedor ni puerto aparte.

Había además una página `/memoria` solo para los recuerdos; se eliminó porque `/admin` ya
hace lo mismo y mejor. Los recuerdos se siguen pudiendo tocar por API con `/memory`, que
es lo que usa el terminal de pruebas.

Antes de escribirlo se probaron sqlite-web, DbGate y Outerbase Studio. Outerbase se
descartó porque carga la interfaz desde `studio.outerbase.com` (sin internet te quedas
sin panel: no es autoalojado de verdad). DbGate llegó a estar en el compose, pero era
otro contenedor, otro puerto y otro login para lo mismo.

Dos cosas del panel que costaron encontrar:

1. **`/admin` solo existe si hay `ADMIN_PASSWORD`.** Es acceso SQL completo; sin
   contraseña la ruta no se monta y devuelve 404. La puerta va como middleware de
   Starlette y no como dependencia de FastAPI, porque el WebSocket de NiceGUI no pasa
   por las dependencias.
2. **NiceGUI habla por WebSocket en `/socket.io`.** Detrás de Nginx hacen falta
   `proxy_http_version 1.1` y las cabeceras `Upgrade`/`Connection`, o la página carga y
   se queda en blanco. Caddy lo hace solo.

Para probarlo no hace falta cuota de nadie: `ADMIN_PASSWORD=proves uvicorn app.main:app` y
Playwright contra `/admin`.
