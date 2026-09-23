"""Shrink the photo before sending it to the vision model.

On the server and not on the client so it works the same for the ESP32 and for
anything else, without every client having to remember. It does not save the
upload, which already happened by the time we get here.

Groq charges per pixel: a 3024x4032 photo is ~50.000 tokens and 2.656 at 896 px
(a figure its own 429 gave us). Shrinking is the difference between using it and
burning the day's quota in six photos.
"""

from __future__ import annotations

import base64
import io
import os

# Long side to shrink to. 896 px is the measured sweet spot: below it, signs
# stop being readable; above it, you only pay tokens and latency.
MAX_SIDE = int(os.environ.get("IMAGE_MAX_SIDE", "896"))

# IMAGE_MAX_SIDE=0 turns shrinking off.
ENABLED = MAX_SIDE > 0

QUALITY = int(os.environ.get("IMAGE_JPEG_QUALITY", "80"))

# The OV3660 frames come out soft and veiled: a lamp in shot lifts the blacks
# into grey haze and the whole picture loses contrast. Stretching the levels and
# a mild unsharp mask give the model back edges it can read, for a few tens of
# ms at 800x600. IMAGE_ENHANCE=0 turns it off.
ENHANCE = os.environ.get("IMAGE_ENHANCE", "1") != "0"


def _enhance(im):
    from PIL import ImageFilter, ImageOps
    # Per channel, so it also pulls out part of the magenta cast. The 1% cutoff
    # keeps a single blown lamp from deciding the white point.
    im = ImageOps.autocontrast(im, cutoff=1)
    return im.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=3))


def resize(image_base64: str, max_side: int | None = None) -> tuple[str, dict]:
    """Returns (image_in_base64, info). Leaves it alone if there is nothing to do.

    Never fails the request: if Pillow is missing or the image is odd, the
    original goes through. Not describing anything because we could not resize
    would be far worse than sending a big photo.
    """
    side = MAX_SIDE if max_side is None else max_side
    info: dict = {"resized": False}
    if side <= 0:
        return image_base64, info

    try:
        from PIL import Image, ImageOps
    except ImportError:
        info["error"] = "Pillow is not installed"
        return image_base64, info

    try:
        raw = base64.b64decode(image_base64, validate=False)
        im = Image.open(io.BytesIO(raw))
        # Phone photos come rotated; without exif_transpose we would describe
        # the scene lying on its side.
        im = ImageOps.exif_transpose(im)
        original = im.size

        if max(original) <= side and not ENHANCE:
            info.update({"from": original, "reason": "already small"})
            return image_base64, info

        im = im.convert("RGB")
        if max(original) > side:
            im.thumbnail((side, side))
            info["resized"] = True
        if ENHANCE:
            im = _enhance(im)
            info["enhanced"] = True
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=QUALITY, optimize=True)
        shrunk = buf.getvalue()

        info.update({
            "from": original,
            "to": im.size,
            "bytesFrom": len(raw),
            "bytesTo": len(shrunk),
        })
        return base64.b64encode(shrunk).decode("ascii"), info
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return image_base64, info
