"""Reducir la foto antes de mandarla al modelo de visión.

Por qué en el servidor y no en el cliente: así vale igual para la ESP32 y para
la app web, y no depende de que cada cliente se acuerde. Lo que **no** ahorra
es la subida, que ya ha ocurrido cuando llega aquí.

Groq cobra por píxeles: medido con una foto de 3024x4032, son ~50.000 tokens, y
a 896 px son 2.656 (cifra que dio su propio error 429). Reducir es la diferencia
entre poder usarlo y agotar la cuota del día en seis fotos.
"""

from __future__ import annotations

import base64
import io
import os

# Lado largo al que se reduce. 896 px es el punto medido: por debajo se pierde
# detalle para leer carteles y por encima solo se pagan tokens y latencia.
MAX_SIDE = int(os.environ.get("IMAGE_MAX_SIDE", "896"))

# IMAGE_MAX_SIDE=0 desactiva la reducción.
ENABLED = MAX_SIDE > 0

QUALITY = int(os.environ.get("IMAGE_JPEG_QUALITY", "80"))


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
