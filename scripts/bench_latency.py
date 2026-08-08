#!/usr/bin/env python3
"""Banco de pruebas de latencia del backend de Bonsai.

La latencia es lo que decide si las gafas se sienten inmediatas o torpes, así
que aquí se mide por partes: reducir la imagen, codificarla, la red, el modelo
de visión y el TTS. Sin desglose no se sabe dónde atacar.

CUIDADO CON LA CUOTA. Por defecto **no gasta nada**: enseña el plan y lo que
costaría. Hay que pasar `--yes` para que llame de verdad al proveedor.

    # Ni un token: comprueba el código y estima el coste
    python bench_latency.py --selftest
    python bench_latency.py --image foto.jpg

    # Gasta cuota: 1 petición por tamaño (lo mínimo para tener el dato)
    python bench_latency.py --image foto.jpg --yes

    # Time-to-first-token, para saber si merece la pena hacer streaming
    python bench_latency.py --image foto.jpg --mode ttft --yes
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

API_URL = os.environ.get("BONSAI_API_URL", "http://127.0.0.1:8080")
# Prefijo de versión de la API (el mismo API_PREFIX de main.py).
API_PREFIX = os.environ.get("BONSAI_API_PREFIX", "/api/v1")
API_TOKEN = os.environ.get("BONSAI_API_TOKEN", "")
DEVICE_ID = "bench"

# Lados largos a comparar. 896 px es el que interesa: por debajo se pierde
# detalle para leer texto, por encima solo se pagan tokens y latencia.
TAMANOS = (672, 896, 1568)

# Tope de tokens que el banco puede gastar de una tirada. Es una red para no
# repetir lo de quemar los 200.000 tokens del día en seis pruebas.
PRESUPUESTO_POR_DEFECTO = 20_000


# --------------------------------------------------------------------------
# Estimación de tokens (aproximada, para decidir si merece la pena lanzarlo)
# --------------------------------------------------------------------------
def tokens_estimados(ancho: int, alto: int) -> int:
    """Coste aproximado en tokens de entrada de una imagen.

    Groq cobra proporcionalmente a los píxeles. La cuenta está calibrada con lo
    que dijo su propio error 429, "Requested 2656" para 672x896 (602.112 px),
    no con la documentación, que no cuadraba.
    """
    return round(ancho * alto / 227) or 1


# --------------------------------------------------------------------------
# Preparación de la imagen
# --------------------------------------------------------------------------
def variantes(path: str, tamanos: tuple[int, ...]) -> list[dict]:
    """Devuelve la imagen original y sus versiones reducidas, ya en base64.

    El tiempo de reducir y de codificar se mide aquí porque también cuenta:
    forma parte de lo que espera la persona.
    """
    crudo = open(path, "rb").read()

    t0 = time.perf_counter()
    b64 = base64.b64encode(crudo).decode()
    encode_ms = (time.perf_counter() - t0) * 1000

    try:
        from PIL import Image, ImageOps
    except ImportError:
        print("⚠️  Sin Pillow: solo se mide la imagen original.")
        print("   Instálalo con: pip install pillow\n")
        return [
            {
                "nombre": "original",
                "b64": b64,
                "bytes": len(crudo),
                "ancho": 0,
                "alto": 0,
                "resize_ms": 0.0,
                "encode_ms": encode_ms,
            }
        ]

    import io

    original = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    salida = [
        {
            "nombre": f"original {original.width}x{original.height}",
            "b64": b64,
            "bytes": len(crudo),
            "ancho": original.width,
            "alto": original.height,
            "resize_ms": 0.0,
            "encode_ms": encode_ms,
        }
    ]

    for lado in tamanos:
        if max(original.size) <= lado:
            continue
        t0 = time.perf_counter()
        im = original.copy()
        im.thumbnail((lado, lado))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80, optimize=True)
        crudo_r = buf.getvalue()
        resize_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        b64_r = base64.b64encode(crudo_r).decode()
        enc_ms = (time.perf_counter() - t0) * 1000

        salida.append(
            {
                "nombre": f"{lado} px ({im.width}x{im.height})",
                "b64": b64_r,
                "bytes": len(crudo_r),
                "ancho": im.width,
                "alto": im.height,
                "resize_ms": resize_ms,
                "encode_ms": enc_ms,
            }
        )
    return salida


# --------------------------------------------------------------------------
# Medición contra el servidor (/look)
# --------------------------------------------------------------------------
def medir_servidor(cliente: httpx.Client, var: dict, prompt: str | None) -> dict:
    """Mide /look, que es el único endpoint de imagen que hay.

    Lo interesante aquí es el **primer byte de audio**: es cuando el ESP32
    podría empezar a sonar. El total importa mucho menos, porque a partir del
    primer byte la descarga se solapa con la reproducción.
    """
    cuerpo = {
        "deviceId": DEVICE_ID,
        "image": var["b64"],
        "lang": "es",
        "audioFormat": "pcm16",
        "sampleRate": 16000,
    }
    if prompt:
        cuerpo["prompt"] = prompt

    cabeceras = {"Content-Type": "application/json"}
    if API_TOKEN:
        cabeceras["X-API-Token"] = API_TOKEN

    t0 = time.perf_counter()
    primero_ms, n = None, 0
    try:
        with cliente.stream(
            "POST", f"{API_URL}{API_PREFIX}/look", json=cuerpo, headers=cabeceras
        ) as r:
            if r.status_code == 429:
                r.read()
                espera = r.headers.get("Retry-After", "?")
                return {"error": f"429 cuota agotada (Retry-After: {espera}s)",
                        "parar": True}
            if r.status_code >= 400:
                r.read()
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            cab = r.headers
            for trozo in r.iter_bytes():
                if trozo:
                    if primero_ms is None:
                        primero_ms = (time.perf_counter() - t0) * 1000
                    n += len(trozo)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    total_ms = (time.perf_counter() - t0) * 1000

    vision = int(cab.get("x-bonsai-vision-ms", 0))
    reducir = int(cab.get("x-bonsai-resize-ms", 0))
    return {
        "total_ms": total_ms,
        "primer_audio_ms": primero_ms or total_ms,
        "vision_ms": vision,
        "reducir_ms": reducir,
        # Lo que no es visión ni reducir es sintetizar y mover los bytes.
        "audio_ms": max(0.0, (primero_ms or total_ms) - vision - reducir),
        "bytes": n,
        "texto": base64.b64decode(cab.get("x-bonsai-text", "")).decode(
            "utf-8", "replace"),
        "modelo": cab.get("x-bonsai-model", "?"),
    }


# --------------------------------------------------------------------------
# Time-to-first-token, hablando directamente con el proveedor
# --------------------------------------------------------------------------
SYSTEM_TTFT = (
    "Eres el asistente de visión de unas gafas. Contesta en 1 o 2 frases "
    "cortas, en castellano, sin preámbulos."
)
USER_TTFT = "¿Qué tengo delante? Dímelo en una o dos frases."


def medir_ttft(var: dict) -> dict:
    """Mide cuánto tarda en llegar el primer trozo de texto.

    Interesa porque si el TTFT es mucho menor que el total, conviene ir
    mandando el texto al TTS a medida que llega en vez de esperar la frase
    entera. Ya está medido y descartado: 1.246 ms frente a 1.303 ms.
    """
    from app import vision as mod

    clave = mod.api_key()
    url = mod.GROQ_URL
    payload = {
        "model": mod.MODEL,
        "temperature": 0.4,
        "max_completion_tokens": 150,
        "reasoning_effort": "none",
        "reasoning_format": "hidden",
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_TTFT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": USER_TTFT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mod.sniff_mime(var['b64'])};base64,{var['b64']}"
                        },
                    },
                ],
            },
        ],
    }

    if not clave:
        return {"error": "Sin GROQ_API_KEY"}

    t0 = time.perf_counter()
    ttft_ms = None
    trozos = 0
    texto = []
    try:
        with httpx.Client(timeout=60.0) as c:
            with c.stream(
                "POST", url, json=payload, headers=mod.auth_headers(clave)
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    if r.status_code == 429:
                        return {"error": "429 cuota agotada", "parar": True}
                    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
                for linea in r.iter_lines():
                    if not linea.startswith("data:"):
                        continue
                    datos = linea[5:].strip()
                    if not datos or datos == "[DONE]":
                        continue
                    fragmento = _texto_del_fragmento(datos)
                    if not fragmento:
                        continue
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    trozos += 1
                    texto.append(fragmento)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    total_ms = (time.perf_counter() - t0) * 1000
    if ttft_ms is None:
        return {"error": "No llegó ningún token"}
    return {
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "trozos": trozos,
        "texto": "".join(texto).strip(),
    }


def _texto_del_fragmento(datos: str) -> str:
    try:
        d = json.loads(datos)
    except json.JSONDecodeError:
        return ""
    delta = (d.get("choices") or [{}])[0].get("delta") or {}
    return delta.get("content") or ""


# --------------------------------------------------------------------------
# Comprobaciones que no gastan ni un token
# --------------------------------------------------------------------------
def selftest() -> int:
    """Valida lo que se puede validar sin llamar a nadie."""
    from app import vision

    fallos = []

    def check(nombre: str, obtenido, esperado) -> None:
        ok = obtenido == esperado
        print(f"  {'✅' if ok else '❌'} {nombre}: {obtenido!r}")
        if not ok:
            fallos.append(f"{nombre}: esperaba {esperado!r}, salió {obtenido!r}")

    print("Formato de imagen (se detecta, ya no va 'jpeg' fijo):")
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode()
    jpg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()
    webp = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 ").decode()
    gif = base64.b64encode(b"GIF89a" + b"\x00" * 40).decode()
    check("PNG", vision.sniff_mime(png), "image/png")
    check("JPEG", vision.sniff_mime(jpg), "image/jpeg")
    check("WEBP", vision.sniff_mime(webp), "image/webp")
    check("GIF", vision.sniff_mime(gif), "image/gif")
    check("basura -> jpeg", vision.sniff_mime("!!!"), "image/jpeg")

    print("\nMensajes de error (el bug del timeout con str(e) vacío):")
    check(
        "timeout sin mensaje",
        vision.describe_error(httpx.ReadTimeout("")),
        "ReadTimeout",
    )
    check("error normal", vision.describe_error(ValueError("roto")), "roto")

    print("\nLectura del rato de espera en un 429:")
    r_groq = httpx.Response(
        429, text='{"error":{"message":"Please try again in 12m39.024s"}}'
    )
    check("del texto", vision._segundos_de_espera(r_groq), 759.024)
    check(
        "de la cabecera",
        vision._segundos_de_espera(httpx.Response(429, headers={"retry-after": "30"}, text="x")),
        30.0,
    )

    print("\nEstimación de tokens por imagen (Groq cobra por píxeles):")
    for w, h in ((672, 896), (3024, 4032)):
        print(f"  {w}x{h}: ~{tokens_estimados(w, h):,} tokens")

    if fallos:
        print(f"\n❌ {len(fallos)} fallo(s):")
        for f in fallos:
            print(f"   - {f}")
        return 1
    print("\n✅ Todo correcto (0 tokens gastados)")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Mide la latencia del backend. Por defecto NO gasta cuota.",
    )
    p.add_argument("--image", help="Ruta de la imagen de prueba")
    p.add_argument("--mode", default="server", choices=["server", "ttft"])
    p.add_argument("--repeat", type=int, default=1, help="Repeticiones (por defecto 1)")
    p.add_argument("--prompt", help="Pregunta concreta en vez de la descripción")
    p.add_argument("--sizes", default=",".join(str(t) for t in TAMANOS))
    p.add_argument("--only-small", action="store_true",
                   help="Solo el tamaño más pequeño: lo más barato en cuota")
    p.add_argument("--budget", type=int, default=PRESUPUESTO_POR_DEFECTO,
                   help=f"Tope de tokens estimados (por defecto {PRESUPUESTO_POR_DEFECTO})")
    p.add_argument("--yes", action="store_true",
                   help="Confirma que puede gastar cuota. Sin esto solo enseña el plan.")
    p.add_argument("--selftest", action="store_true",
                   help="Comprueba el código sin llamar a nadie (0 tokens)")
    args = p.parse_args()

    if args.selftest:
        return selftest()

    if not args.image:
        print("Falta --image. Ejemplo:\n  python bench_latency.py --image foto.jpg --yes")
        print("\nO comprueba el código sin gastar nada:\n  python bench_latency.py --selftest")
        return 2
    if not os.path.isfile(args.image):
        print(f"No encuentro la imagen: {args.image}")
        return 2

    tamanos = tuple(int(s) for s in args.sizes.split(",") if s.strip())
    vars_ = variantes(args.image, tamanos)
    if args.only_small:
        vars_ = [min(vars_, key=lambda v: v["bytes"])]

    # --- Plan y coste antes de gastar nada -------------------------------
    print(f"\nPlan: modo {args.mode}, {args.repeat} repetición(es)")
    print(f"{'imagen':<26} {'KB':>7} {'tokens/petición estimados':>28}")
    print("-" * 64)
    coste = 0
    for v in vars_:
        est = tokens_estimados(v["ancho"], v["alto"])
        coste += est * args.repeat
        print(f"{v['nombre']:<26} {v['bytes']/1024:>7.0f} {'~' + f'{est:,}':>28}")
    peticiones = len(vars_) * args.repeat
    print("-" * 64)
    print(f"Total: {peticiones} petición(es), ~{coste:,} tokens de entrada estimados")

    if coste > args.budget:
        print(f"\n⛔ Pasa del presupuesto ({args.budget:,} tokens). Opciones:")
        print("   --only-small          solo la imagen reducida")
        print("   --sizes 896           un único tamaño")
        print(f"   --budget {coste}   subir el tope a conciencia")
        return 1
    if not args.yes:
        print("\nEnsayo: no se ha gastado nada. Añade --yes para medir de verdad.")
        return 0

    # --- Medición --------------------------------------------------------
    resultados: dict[str, list[dict]] = {}
    parar = False
    with httpx.Client(timeout=120.0) as cliente:
        for v in vars_:
            if parar:
                break
            nombre = v["nombre"]
            resultados[nombre] = []
            for i in range(args.repeat):
                if args.mode == "ttft":
                    r = medir_ttft(v)
                else:
                    r = medir_servidor(cliente, v, args.prompt)
                if "error" in r:
                    print(f"⚠️  {nombre}: {r['error']}")
                    if r.get("parar"):
                        print("   Cuota agotada: se para para no insistir.")
                        parar = True
                    break
                resultados[nombre].append(r)
                print(f"   {nombre} [{i+1}/{args.repeat}] ok")

    # --- Resultados ------------------------------------------------------
    utiles = {k: v for k, v in resultados.items() if v}
    if not utiles:
        print("\nSin mediciones válidas.")
        return 1

    def med(muestras: list[dict], campo: str) -> float:
        vals = [m[campo] for m in muestras if campo in m]
        return statistics.median(vals) if vals else 0.0

    print("\n" + "=" * 78)
    if args.mode == "ttft":
        print("TIME TO FIRST TOKEN (medianas)")
        print(f"{'imagen':<38} {'TTFT':>10} {'total':>10} {'trozos':>8}")
        print("-" * 78)
        for nombre, ms in utiles.items():
            print(f"{nombre:<38} {med(ms,'ttft_ms'):>9.0f}ms "
                  f"{med(ms,'total_ms'):>9.0f}ms {med(ms,'trozos'):>8.0f}")
        print("-" * 78)
        peor = max(utiles.items(), key=lambda kv: med(kv[1], "total_ms"))
        ahorro = med(peor[1], "total_ms") - med(peor[1], "ttft_ms")
        print(f"\nMandando el texto al TTS en cuanto llega el primer token se "
              f"adelantarían\nhasta ~{ahorro:.0f} ms en {peor[0]}.")
    else:
        print("LATENCIA POR PARTES (medianas)")
        print(f"{'imagen':<34} {'reducir':>9} {'visión':>9} "
              f"{'audio':>9} {'1er byte':>10} {'KB':>7}")
        print("-" * 82)
        for nombre, ms in utiles.items():
            print(f"{nombre:<34} {med(ms,'reducir_ms'):>8.0f}ms "
                  f"{med(ms,'vision_ms'):>8.0f}ms {med(ms,'audio_ms'):>8.0f}ms "
                  f"{med(ms,'primer_audio_ms'):>9.0f}ms {med(ms,'bytes')/1024:>7.0f}")
        print("-" * 82)
        mejor = min(utiles.items(), key=lambda kv: med(kv[1], "primer_audio_ms"))
        print(f"\nMás rápido al primer byte de audio: {mejor[0]} "
              f"({med(mejor[1],'primer_audio_ms'):.0f} ms)")
        for v in vars_:
            if v["resize_ms"]:
                print(f"Reducir a {v['nombre']}: {v['resize_ms']:.0f} ms de CPU "
                      f"(+{v['encode_ms']:.0f} ms de base64)")

    print("\nTextos devueltos:")
    for nombre, ms in utiles.items():
        if ms and ms[0].get("texto"):
            print(f"  [{nombre}] {ms[0]['texto'][:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
