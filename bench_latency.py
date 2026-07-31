#!/usr/bin/env python3
"""Banco de pruebas de latencia del backend de Bonsai.

La latencia es lo que decide si las gafas se sienten inmediatas o torpes, así
que aquí se mide por partes: reducir la imagen, codificarla, la red, el modelo
de visión y el TTS. Sin desglose no se sabe dónde atacar.

CUIDADO CON LA CUOTA. Por defecto **no gasta nada**: enseña el plan y lo que
costaría. Hay que pasar `--yes` para que llame de verdad al proveedor.

    # Ni un token: comprueba el código y estima el coste
    python bench_latency.py --selftest
    python bench_latency.py --provider gemini

    # Gasta cuota: 1 petición por tamaño (lo mínimo para tener el dato)
    python bench_latency.py --provider gemini --image foto.jpg --yes

    # Time-to-first-token, para saber si merece la pena hacer streaming
    python bench_latency.py --provider gemini --image foto.jpg --mode ttft --yes
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import statistics
import sys
import time

import httpx

API_URL = os.environ.get("BONSAI_API_URL", "http://127.0.0.1:8080")
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
def tokens_estimados(provider: str, ancho: int, alto: int) -> int:
    """Coste aproximado en tokens de entrada de una imagen.

    Gemini cobra por baldosas de 768x768 a 258 tokens (y 258 fijos si cabe en
    384 px). Los modelos tipo Qwen-VL trocean en parches de 28 px. Ninguna de
    las dos cuentas es exacta, pero sirve para saber si una prueba cuesta 300
    tokens o 35.000, que es la diferencia que importa.
    """
    if provider == "gemini":
        if max(ancho, alto) <= 384:
            return 258
        baldosas = math.ceil(ancho / 768) * math.ceil(alto / 768)
        return 258 * max(1, baldosas)
    # Groq (Qwen-VL): la cuenta por parches de 28 px daba muy corto frente a
    # lo real, así que va calibrado con lo que dijo el propio error 429 de
    # Groq: "Requested 2656" para una imagen de 672x896 (602.112 px).
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
# Medición contra el servidor (/describe)
# --------------------------------------------------------------------------
def medir_servidor(
    cliente: httpx.Client, provider: str, var: dict, audio: bool, prompt: str | None
) -> dict:
    cuerpo = {
        "deviceId": DEVICE_ID,
        "image": var["b64"],
        "lang": "es",
        "audio": audio,
        "provider": provider,
    }
    if prompt:
        cuerpo["prompt"] = prompt

    cabeceras = {"Content-Type": "application/json"}
    if API_TOKEN:
        cabeceras["X-API-Token"] = API_TOKEN

    t0 = time.perf_counter()
    try:
        r = cliente.post(f"{API_URL}/describe", json=cuerpo, headers=cabeceras)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    total_ms = (time.perf_counter() - t0) * 1000

    if r.status_code == 429:
        espera = r.headers.get("Retry-After", "?")
        return {"error": f"429 cuota agotada (Retry-After: {espera}s)", "parar": True}
    if r.status_code >= 400:
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}

    d = r.json()
    t = d.get("timings") or {}
    servidor = sum(v for v in t.values() if isinstance(v, int))
    return {
        "total_ms": total_ms,
        "vision_ms": t.get("vision_ms", 0),
        "tts_ms": t.get("tts_ms", 0),
        "memoria_ms": t.get("memoria_ms", 0),
        # Lo que no es el servidor es subir la imagen y bajar el audio.
        "red_ms": max(0.0, total_ms - servidor),
        "texto": d.get("text", ""),
        "modelo": d.get("model", "?"),
    }


# --------------------------------------------------------------------------
# Time-to-first-token, hablando directamente con el proveedor
# --------------------------------------------------------------------------
SYSTEM_TTFT = (
    "Eres el asistente de visión de unas gafas. Contesta en 1 o 2 frases "
    "cortas, en castellano, sin preámbulos."
)
USER_TTFT = "¿Qué tengo delante? Dímelo en una o dos frases."


def medir_ttft(provider: str, var: dict) -> dict:
    """Mide cuánto tarda en llegar el primer trozo de texto.

    Interesa porque si el TTFT es mucho menor que el total, conviene ir
    mandando el texto al TTS a medida que llega en vez de esperar la frase
    entera: es la mayor reducción de latencia que le queda al proyecto.
    """
    import vision

    if provider == "gemini":
        import gemini_vision as mod

        clave = mod.api_key()
        url = f"{mod.BASE_URL}/models/{mod.MODEL}:streamGenerateContent?alt=sse"
        payload = mod._payload(var["b64"], SYSTEM_TTFT, USER_TTFT, True)
    else:
        import groq_vision as mod

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
                                "url": f"data:{vision.sniff_mime(var['b64'])};base64,{var['b64']}"
                            },
                        },
                    ],
                },
            ],
        }

    if not clave:
        return {"error": f"Sin API key para {provider}"}

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
                    fragmento = _texto_del_fragmento(provider, datos)
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


def _texto_del_fragmento(provider: str, datos: str) -> str:
    try:
        d = json.loads(datos)
    except json.JSONDecodeError:
        return ""
    if provider == "gemini":
        cands = d.get("candidates") or []
        if not cands:
            return ""
        partes = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p["text"] for p in partes if isinstance(p.get("text"), str))
    delta = (d.get("choices") or [{}])[0].get("delta") or {}
    return delta.get("content") or ""


# --------------------------------------------------------------------------
# Comprobaciones que no gastan ni un token
# --------------------------------------------------------------------------
def selftest() -> int:
    """Valida lo que se puede validar sin llamar a nadie."""
    import gemini_vision
    import groq_vision
    import vision

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

    print("\nElección de proveedor:")
    check("por defecto", vision.resolve(None), vision.DEFAULT_PROVIDER)
    check("explícito groq", vision.resolve("groq"), "groq")
    check("mayúsculas", vision.resolve("GEMINI"), "gemini")
    try:
        vision.resolve("openai")
        print("  ❌ proveedor inválido: no lanzó error")
        fallos.append("proveedor inválido aceptado")
    except ValueError:
        print("  ✅ proveedor inválido rechazado")

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
    check("groq texto", groq_vision._segundos_de_espera(r_groq), 759.024)
    check(
        "groq cabecera",
        groq_vision._segundos_de_espera(httpx.Response(429, headers={"retry-after": "30"}, text="x")),
        30.0,
    )
    r_gem = httpx.Response(
        429,
        text='{"error":{"details":[{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"27s"}]}}',
    )
    check("gemini retryDelay", gemini_vision._segundos_de_espera(r_gem), 27.0)

    print("\nPayload de Gemini:")
    p = gemini_vision._payload(png, "sistema", "usuario", True)
    check("thinkingLevel", p["generationConfig"]["thinkingLevel"], gemini_vision.THINKING_LEVEL)
    check("maxOutputTokens", p["generationConfig"]["maxOutputTokens"], 150)
    check("systemInstruction", p["systemInstruction"]["parts"][0]["text"], "sistema")
    check("mimeType", p["contents"][0]["parts"][1]["inlineData"]["mimeType"], "image/png")
    sin = gemini_vision._payload(png, "s", "u", False)
    check("sin thinkingLevel", "thinkingLevel" in sin["generationConfig"], False)

    print("\nRespuestas raras de Gemini:")
    check(
        "texto normal",
        gemini_vision._texto_de_la_respuesta(
            {"candidates": [{"content": {"parts": [{"text": " Un coche. "}]}}]}
        ),
        "Un coche.",
    )
    for nombre, data, aguja in (
        ("bloqueada", {"promptFeedback": {"blockReason": "SAFETY"}}, "bloqueó"),
        ("sin candidatos", {"candidates": []}, "candidato"),
        (
            "max tokens",
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]},
            "maxOutputTokens",
        ),
    ):
        try:
            gemini_vision._texto_de_la_respuesta(data)
            print(f"  ❌ {nombre}: no lanzó error")
            fallos.append(f"{nombre} no lanzó error")
        except RuntimeError as e:
            ok = aguja in str(e)
            print(f"  {'✅' if ok else '❌'} {nombre}: {e}")
            if not ok:
                fallos.append(f"{nombre}: el mensaje no menciona {aguja!r}")

    print("\nEstimación de tokens por imagen:")
    for prov in ("gemini", "groq"):
        for w, h in ((672, 896), (3024, 4032)):
            print(f"  {prov:7} {w}x{h}: ~{tokens_estimados(prov, w, h):,} tokens")

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
    p.add_argument("--provider", default="gemini", choices=["gemini", "groq", "both"])
    p.add_argument("--image", help="Ruta de la imagen de prueba")
    p.add_argument("--mode", default="server", choices=["server", "ttft"])
    p.add_argument("--repeat", type=int, default=1, help="Repeticiones (por defecto 1)")
    p.add_argument("--audio", action="store_true", help="Incluir el TTS en la medición")
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
    provs = ["gemini", "groq"] if args.provider == "both" else [args.provider]

    # --- Plan y coste antes de gastar nada -------------------------------
    print(f"\nPlan: modo {args.mode}, {args.repeat} repetición(es)")
    print(f"{'imagen':<26} {'KB':>7} {'tokens/petición estimados':>28}")
    print("-" * 64)
    coste = 0
    for v in vars_:
        est = {pr: tokens_estimados(pr, v["ancho"], v["alto"]) for pr in provs}
        coste += sum(est.values()) * args.repeat
        detalle = "  ".join(f"{pr}: ~{n:,}" for pr, n in est.items())
        print(f"{v['nombre']:<26} {v['bytes']/1024:>7.0f} {detalle:>28}")
    peticiones = len(vars_) * len(provs) * args.repeat
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
    resultados: dict[tuple[str, str], list[dict]] = {}
    parar = False
    with httpx.Client(timeout=120.0) as cliente:
        for prov in provs:
            for v in vars_:
                if parar:
                    break
                clave = (prov, v["nombre"])
                resultados[clave] = []
                for i in range(args.repeat):
                    if args.mode == "ttft":
                        r = medir_ttft(prov, v)
                    else:
                        r = medir_servidor(cliente, prov, v, args.audio, args.prompt)
                    if "error" in r:
                        print(f"⚠️  {prov} / {v['nombre']}: {r['error']}")
                        if r.get("parar"):
                            print("   Cuota agotada: se para para no insistir.")
                            parar = True
                        break
                    resultados[clave].append(r)
                    print(f"   {prov} / {v['nombre']} [{i+1}/{args.repeat}] ok")

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
        print(f"{'proveedor / imagen':<38} {'TTFT':>10} {'total':>10} {'trozos':>8}")
        print("-" * 78)
        for (prov, nombre), ms in utiles.items():
            print(f"{prov + ' / ' + nombre:<38} {med(ms,'ttft_ms'):>9.0f}ms "
                  f"{med(ms,'total_ms'):>9.0f}ms {med(ms,'trozos'):>8.0f}")
        print("-" * 78)
        peor = max(utiles.items(), key=lambda kv: med(kv[1], "total_ms"))
        ahorro = med(peor[1], "total_ms") - med(peor[1], "ttft_ms")
        print(f"\nMandando el texto al TTS en cuanto llega el primer token se "
              f"adelantarían\nhasta ~{ahorro:.0f} ms en {peor[0][0]} / {peor[0][1]}.")
    else:
        print("LATENCIA POR PARTES (medianas)")
        cab = f"{'proveedor / imagen':<38} {'total':>9} {'visión':>9} {'red':>8}"
        if args.audio:
            cab += f" {'tts':>8}"
        print(cab)
        print("-" * 78)
        for (prov, nombre), ms in utiles.items():
            fila = (f"{prov + ' / ' + nombre:<38} {med(ms,'total_ms'):>8.0f}ms "
                    f"{med(ms,'vision_ms'):>8.0f}ms {med(ms,'red_ms'):>7.0f}ms")
            if args.audio:
                fila += f" {med(ms,'tts_ms'):>7.0f}ms"
            print(fila)
        print("-" * 78)
        mejor = min(utiles.items(), key=lambda kv: med(kv[1], "total_ms"))
        print(f"\nMás rápido: {mejor[0][0]} / {mejor[0][1]} "
              f"({med(mejor[1],'total_ms'):.0f} ms)")
        for v in vars_:
            if v["resize_ms"]:
                print(f"Reducir a {v['nombre']}: {v['resize_ms']:.0f} ms de CPU "
                      f"(+{v['encode_ms']:.0f} ms de base64)")

    print("\nTextos devueltos:")
    for (prov, nombre), ms in utiles.items():
        if ms and ms[0].get("texto"):
            print(f"  [{prov} / {nombre}] {ms[0]['texto'][:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
