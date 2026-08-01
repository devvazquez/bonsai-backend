"""Reducir la foto antes de mandarla al proveedor de visión.

Por qué en el servidor y no en el cliente: así vale igual para la ESP32 y para
la app web, y no depende de que cada cliente se acuerde. Lo que **no** ahorra
es la subida, que ya ha ocurrido cuando llega aquí.

Compensa con los dos proveedores, pero por motivos distintos, y los dos
motivos están medidos con una foto de 3024x4032:

- **Groq cobra por píxeles**: una foto de móvil son ~50.000 tokens y a 896 px
  son 2.656 (cifra que dio su propio error 429). Reducir es la diferencia
  entre poder usarlo y agotar la cuota del día en seis fotos.
- **Gemini cobra plano** (1.108 tokens medidos con `countTokens` tanto a
  256x170 como a 2400x1597), así que no ahorra ni un token... pero **sí ahorra
  1,3 segundos**: la visión pasó de 2.342 ms sin reducir a 988 ms a 896 px. Lo
  que se paga con la foto grande no son tokens, es el tiempo de subirla y
  procesarla.

Por eso está activado para los dos por defecto. Se puede afinar por proveedor
con IMAGE_RESIZE_FOR.
"""

from __future__ import annotations

import base64
import io
import os

# Lado largo al que se reduce. 896 px es el punto medido: por debajo se pierde
# detalle para leer carteles y por encima solo se pagan tokens y latencia.
MAX_SIDE = int(os.environ.get("IMAGE_MAX_SIDE", "896"))

# A qué proveedores se les reduce. A los dos: a Groq le baja la factura y a
# Gemini la latencia. Vacío = a ninguno.
RESIZE_FOR = tuple(
    p.strip().lower()
    for p in os.environ.get("IMAGE_RESIZE_FOR", "gemini,groq").split(",")
    if p.strip()
)

QUALITY = int(os.environ.get("IMAGE_JPEG_QUALITY", "80"))


def enabled_for(provider: str) -> bool:
    return MAX_SIDE > 0 and provider.lower() in RESIZE_FOR


def reducir(image_base64: str, max_side: int | None = None) -> tuple[str, dict]:
    """Devuelve (imagen_en_base64, info). Si no hay nada que hacer, la deja igual.

    Nunca revienta la petición: si Pillow no está o la imagen es rara, se
    manda la original. Quedarse sin describir por no poder redimensionar sería
    mucho peor que mandar una foto grande.
    """
    lado = MAX_SIDE if max_side is None else max_side
    info: dict = {"resized": False}
    if lado <= 0:
        return image_base64, info

    try:
        from PIL import Image, ImageOps
    except ImportError:
        info["error"] = "Pillow no está instalado"
        return image_base64, info

    try:
        crudo = base64.b64decode(image_base64, validate=False)
        im = Image.open(io.BytesIO(crudo))
        # exif_transpose porque las fotos de móvil vienen giradas: sin esto se
        # describiría la escena tumbada.
        im = ImageOps.exif_transpose(im)
        original = im.size

        if max(original) <= lado:
            info.update({"from": original, "reason": "ya es pequeña"})
            return image_base64, info

        im = im.convert("RGB")
        im.thumbnail((lado, lado))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=QUALITY, optimize=True)
        nuevo = buf.getvalue()

        info.update({
            "resized": True,
            "from": original,
            "to": im.size,
            "bytesFrom": len(crudo),
            "bytesTo": len(nuevo),
        })
        return base64.b64encode(nuevo).decode("ascii"), info
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return image_base64, info
