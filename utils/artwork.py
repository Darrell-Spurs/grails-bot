"""Loading artwork. Five renderers each carried their own copy of this.

Deliberately limited to fetching and squaring: callers keep their own resize
size and resampling filter, because those differ between renderers and changing
them would change the pictures.

No project imports, so every rendering module can use it without a cycle.
"""
import os
from io import BytesIO

import requests
from PIL import Image

# A stalled CDN would otherwise hold a worker thread for as long as it likes.
DEFAULT_TIMEOUT = 15


def fetch_image(url_or_path, timeout=DEFAULT_TIMEOUT):
    """An image as RGB, from a URL or -- for the Sour Patch fallback -- a local path.

    A failed download raises requests' HTTPError. Decoding the error page used
    to raise "cannot identify image file" instead, which said nothing about the
    URL; either way the caller sees an exception, so nothing downstream changes.
    """
    if url_or_path and os.path.exists(url_or_path):
        return Image.open(url_or_path).convert("RGB")
    response = requests.get(url_or_path, timeout=timeout)
    response.raise_for_status()
    return Image.open(BytesIO(response.content)).convert("RGB")


def center_square(image):
    """Crop the longer side so the image is square, keeping the centre."""
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))
