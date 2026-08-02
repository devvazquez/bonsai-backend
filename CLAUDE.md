# Bonsai Backend — notas para Claude

## Proveedor de visión: hay dos

`vision.py` es la capa común y elige entre `groq_vision.py` (por defecto) y
`gemini_vision.py`. Se cambia con `VISION_PROVIDER` o, por petición, con el campo
`provider` de `/look`.

**Groq es el de por defecto** porque es el más rápido y sobre todo el más regular: 552 ms
de visión (551-554) frente a los 844 ms de Gemini (649-937), con la misma imagen.

Su pega es la cuota: 8.000 tokens/minuto son unas 3 fotos por minuto a 896 px. Para
desarrollar sin pelearse con el 429, `VISION_PROVIDER=gemini` o `"provider":"gemini"` en
la petición.

Antes de medir o probar cualquier cosa, `python bench_latency.py --selftest`:
comprueba el código sin gastar un solo token.

### Gemini: dos cosas que se aprendieron a base de medir

1. **`thinkingLevel` va anidado en `thinkingConfig`.** Suelto en
   `generationConfig` la API responde 400. Y no es opcional: con `medium` el
   modelo gasta ~140 tokens pensando, agota los 150 de `maxOutputTokens` y
   devuelve la frase cortada (`finishReason: MAX_TOKENS`). Con `minimal`, 838 ms
   y respuesta completa.
2. **El tamaño de la imagen no cambia el coste en Gemini.** Medido con
   `countTokens` (que es gratis): 1.108 tokens tanto a 256x170 como a 2400x1597.
   El precio lo pone `mediaResolution` (LOW 286 / MEDIUM 577 / HIGH 1.133). Esto
   es al revés que en Groq, que cobra por píxeles.

Para contar tokens sin gastar cuota de generación:
`POST /v1beta/models/<modelo>:countTokens`.

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
   otra factura de tokens. `bench_latency.py` obliga a esto: sin `--yes` no llama a
   nadie, y aborta si el plan pasa de 20.000 tokens estimados.
3. Para probar TTS, memoria, el panel o `/health` **no hace falta Groq**: usa `/speak`,
   `/memory`, `/admin` y `/health`, que no tocan el proveedor de visión.

Medido con la imagen reducida a 896 px: visión ~1,1 s. Con la original de 3,1 MB: ~3,7 s.
Reducir es más rápido *y* más barato.

## Cómo levantar el proyecto en local

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
GROQ_API_KEY=... .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8080
```

En sesiones de Claude Code on the web el proxy intercepta el TLS con una CA propia y
`edge-tts` (que usa `aiohttp` con `certifi`) falla con `CERTIFICATE_VERIFY_FAILED`. Se
arregla apuntando el bundle de certifi del venv a la CA del proxy:

```sh
cp /root/.ccr/ca-bundle.crt .venv/lib/python3.11/site-packages/certifi/cacert.pem
```

Esto es cosa del sandbox, no del proyecto: en la VPS con Docker no aplica.

## Configuración más rápida de `/look` (medido)

`VISION_PROVIDER=groq`, imagen reducida a 896 px e `pcm16` a 16 kHz: **primer byte de
audio a ~1.030 ms**. Groq gana a Gemini con la misma imagen (552 ms de visión frente a
844 ms) y además es mucho más regular: 551-554 ms frente a 649-937 ms.

El precio de Groq es la cuota: 8.000 tokens/minuto son unas **3 fotos por minuto** a
896 px. Para desarrollar sin pelearse con el 429, Gemini; para la latencia, Groq.

Cosas que NO cambian la latencia, comprobadas para no volver a probarlas:

- **El formato de audio.** El audio tarda 349-391 ms en salir sea `pcm16` a 22 kHz o
  `mulaw` a 8 kHz. Elige por ancho de banda, no por velocidad.
- Bajar de 896 a 672 px en Groq (536 ms frente a 552 ms, dentro del ruido).

## Pendiente

- El **429 de Gemini** sigue sin verificarse contra una respuesta real (nunca se agotó la
  cuota): el parseo del `retryDelay` viene de la documentación.

Descartado ya con datos: **streaming de visión hacia el TTS**. Medido con `--mode ttft`,
el primer token llega a 1.246 ms y la frase completa a 1.303 ms: 57 ms de diferencia. No
merece la pena. El cuello de botella es el TTS.

## Al medir edge-tts: Microsoft cachea por texto y voz

**Nunca midas edge-tts con una frase que ya hayas sintetizado antes**, ni en otra
ejecución: la caché es del servidor de Microsoft y persiste. Medido con 8 frases nuevas
frente a las mismas 8 repetidas:

- Texto nuevo (uso real): primer trozo mediana 1.092 ms, completo 1.320 ms (cola a 2.089).
- Texto repetido: primer trozo 262 ms, completo 430 ms.

Son 3,1x. Yo mismo me colé una vez dando 509 ms como "sin caché" cuando esa frase ya se
había sintetizado cuatro veces antes. Usa frases nuevas y cada una una sola vez.

**Ya decidido: se usa Piper con `ca_ES-upc_ona-medium`** (`TTS_PROVIDER=piper`, el de por
defecto). 205 ms de mediana frente a los 1.320 ms de edge-tts con texto nuevo, y sin caché
que engañe porque sintetiza de cero cada vez. Una petición completa con foto pasó de
2.575 ms a 1.170 ms. edge-tts sigue disponible con `TTS_PROVIDER=edge` o `"tts":"edge"`.

Ojo: **Piper devuelve WAV y edge-tts MP3**. El formato va en `audioFormat`, no lo des por
hecho. El WAV pesa 247 KB frente a 66 KB de la misma frase en MP3, que importa por BLE.

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
2. **La clave es la misma `GROQ_API_KEY`.** Aunque pongas `VISION_PROVIDER=gemini`,
   `/ask` necesita Groq para transcribir y devuelve 500 sin ella.
3. **Al PCM del micro hay que ponerle cabecera WAV con las longitudes de
   verdad.** No sirve `piper_tts.cabecera_wav`, que pone 0xFFFFFFFF porque
   responde sobre la marcha: Whisper rechaza un WAV con longitudes imposibles.
   Por eso hay una `stt.cabecera_wav` aparte.
4. **El m4a no empieza por su firma**: los 4 primeros bytes son el tamaño de la
   caja y `ftyp` viene detrás, así que un `startswith` no lo detecta y el
   fichero acababa envuelto en una cabecera WAV que no le tocaba. Es lo que
   graba un iPhone, o sea que salta a la primera prueba con audio de verdad.
   El MP3 sin ID3 (sync 0xFF Ex) NO se detecta a propósito: esos dos bytes
   salen a menudo en PCM en crudo y confundirlos sería peor.

Antes de la foto y la pregunta se le dan por dichos dos turnos —
`user: Hey Bonsai!` / `assistant: Diga’m!` (`vision.PREAMBULO_VEU`)— para que
conteste como quien sigue una conversación. `/look` no los lleva.

**No son inventados: es lo que pasa de verdad.** El firmware detecta "Hey
Bonsai", hace la foto y, mientras sube, suena por el altavoz un clip grabado
que dice "Diga'm". Por eso van en `ASK_WAKE_PHRASE`/`ASK_WAKE_REPLY`: si se
cambia el clip de las gafas hay que cambiarlas o le estaremos contando al
modelo una conversación que no ha ocurrido.

El clip vive en `assets/digam-16k.pcm` (16 KB, 0,52 s, pcm16 a 16 kHz) y se
copia a la SD de las gafas al primer arranque. Lo genera `generar_clips.py`
con el propio Piper, para que sea la misma voz que las respuestas y no se note
el salto; también se puede bajar con
`/speak?text=...&audioFormat=pcm16&sampleRate=16000`, que es lo que conviene si
algún día cambia la voz del servidor. Ojo con dos cosas: `*.wav` está en el
.gitignore y hay una excepción `!assets/*.wav`, y Piper mete ruido aleatorio en
cada síntesis, así que regenerar no da un fichero idéntico al byte.

Ese flujo deja al servidor esperando con la foto en la mano mientras suena el
clip y la persona habla, así que **la foto se reduce en ese hueco**, en un hilo
aparte. Con una de 12 MP son 703 ms que dejan de estar en el camino crítico.
`X-Bonsai-Resize-Wait-Ms` dice lo que se ha llegado a esperar de verdad:
normalmente 0.

El hueco entre la foto y la primera muestra también obliga a un timeout **por
trozo** y no total (`ASK_SILENCE_TIMEOUT_SECONDS`, 15 s): si el micro enmudece
del todo hay que soltar la conexión con un 408 en vez de dejarla abierta.

Para probarlo sin gastar nada: `python test_ask.py`. Sustituye el cliente HTTP
por uno de mentira y comprueba el payload de verdad (los dos turnos, la
cabecera WAV, el modelo). Ojo: `stt.py` y `groq_vision.py` hacen
`from vision import get_client`, así que parchear solo `vision.get_client` no
les llega — hay que tocar los tres módulos.

## Endpoints de imagen: `/look` y `/ask`

Solo hay estos dos. `/describe` se eliminó. Devolvía JSON con el audio en base64 (33 % más de bytes y algo que
descodificar) y solo tenía sentido si hubiera una app web en medio. No la hay: la ESP32
llama a la API directamente, y si algún día hay app será para configurar el dispositivo de
vez en cuando, no para que funcionen las gafas.

`/look` devuelve las muestras en crudo y en streaming, que es lo que quiere el I2S del
MAX98357A, y el texto va en la cabecera `X-Bonsai-Text` (base64, porque las cabeceras son
ASCII). Con `"tts":"edge"` devuelve MP3; con Piper, `pcm16`, `mulaw` o `wav`.

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

Descartado: el **TTS de Gemini**. Soporta catalán y suena bien, pero 5.354, 7.727 y
11.320 ms medidos. Inservible aquí.

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

Para probarlo no hace falta cuota de nadie: `ADMIN_PASSWORD=proves uvicorn main:app` y
Playwright contra `/admin`.
