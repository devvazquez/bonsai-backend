# Bonsai Backend — notas para Claude

## Proveedor de visión: hay dos

`vision.py` es la capa común y elige entre `gemini_vision.py` (por defecto) y
`groq_vision.py`. Se cambia con `VISION_PROVIDER` o, por petición, con el campo
`provider` de `/describe`.

Gemini es el de por defecto por la cuota: 250.000 tokens/minuto y 1.500
peticiones/día, frente a los 8.000/minuto y 200.000/día de Groq. Groq sigue
siendo el más rápido, pero con su cuota no se puede ni desarrollar.

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
3. **`"audio": false`** cuando solo estés comprobando la visión: ahorra el TTS (que es
   gratis, pero también 1-3 s de espera).
4. Para probar TTS, memoria o `/health` **no hace falta Groq**: usa esos endpoints y no
   toques `/describe`.

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

## Pendiente

- **Reducir la imagen en el servidor** antes de llamar al proveedor (Pillow, lado largo
  896 px), así vale igual para la ESP32 y para la app web y no depende de que el cliente
  se acuerde. Cuesta ~200 ms de CPU y ahorra ~2,6 s de latencia: es la mejora más
  rentable que queda.
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

## Para la ESP32-S3 se usa `/look`, no `/describe`

`/describe` mete el audio en base64 dentro del JSON: un 33 % más de bytes y algo que
descodificar en el microcontrolador. `/look` hace lo mismo en una sola petición pero
devuelve las muestras en crudo y en streaming, que es lo que quiere el I2S del MAX98357A.

Lo importante del streaming: el primer byte de audio sale a los 1.024-1.463 ms (casi todo
es la visión) y a partir de ahí el audio llega más rápido de lo que se escucha, así que la
descarga se solapa con la reproducción y **deja de sumar latencia**. Solo hay que ir más
rápido que el tiempo real:

- `pcm16` 22050 Hz: 44,1 KB/s en tiempo real
- `pcm16` 16000 Hz: 32,0 KB/s  ← el equilibrio razonable
- `mulaw` 16000 Hz: 16,0 KB/s
- `mulaw`  8000 Hz:  8,0 KB/s  ← si el WiFi va justo

No separes `/describe` y `/speak` para ganar tiempo: son dos viajes de ida y vuelta y sale
peor. Yo lo sugerí una vez y estaba equivocado; lo que se quería (evitar el base64 y sonar
antes) lo da `/look` en una sola petición.

Descartado: el **TTS de Gemini**. Soporta catalán y suena bien, pero 5.354, 7.727 y
11.320 ms medidos. Inservible aquí.

Ya arreglado: el timeout con `str(e)` vacío (ahora `vision.describe_error`) y el
`data:image/jpeg` fijo (ahora `vision.sniff_mime`).
