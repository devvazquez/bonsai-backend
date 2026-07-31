# Bonsai Backend — notas para Claude

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
   (3,1 MB) gasta ~35.000 tokens por petición: seis pruebas y el día está quemado. La
   misma foto a 896 px son 64 KB, unas **10 veces menos tokens**.
2. **Una sola llamada por prueba**, no un barrido de idiomas y prompts. Cada variante es
   otra factura de tokens.
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

## Pendiente (visto probando, sin arreglar)

- `main.py:159`: un timeout de `httpx` tiene `str(e)` vacío, así que el error que llega al
  cliente es `"Fallo al describir la imagen: "` sin más. Incluir el tipo de excepción.
- `groq_vision.py:101`: manda `data:image/jpeg` siempre, aunque la imagen sea PNG.
- Reducir la imagen en el servidor antes de llamar a Groq (Pillow, lado largo 896 px), así
  vale igual para la ESP32 y para la app web y no depende de que el cliente se acuerde.
