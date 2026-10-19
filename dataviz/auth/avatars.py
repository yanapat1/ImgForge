"""
Avatar images: whatever the person uploads is decoded with Pillow (so non-images are refused), turned upright
(EXIF orientation), centre-cropped to a square and re-encoded as a 256 px WebP. Re-encoding drops EXIF / GPS
data and anything else riding along in the file, and the server only ever serves image/webp.
"""

import io
import warnings

from PIL import Image, ImageOps

MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 40_000_000
SIZE = 256
MIME = 'image/webp'


class TooLarge(ValueError):
    pass


def process(data):
    """Upload bytes -> WebP bytes. TooLarge / ValueError with a human message."""
    if len(data) > MAX_BYTES:
        raise TooLarge('the image is larger than 5 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            w, h = im.size                          # header only: nothing decoded yet
            if w <= 0 or h <= 0:
                raise ValueError('not an image')
            if w * h > MAX_PIXELS:
                raise TooLarge('the image has more than 40 megapixels')
            if im.format == 'JPEG':
                im.draft('RGB', (SIZE * 2, SIZE * 2))   # decode at a reduced scale: much faster for photos
            im.load()
    except TooLarge:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise TooLarge('the image has too many pixels') from e
    except Exception as e:                      # noqa: BLE001 - anything Pillow cannot read is "not an image"
        raise ValueError('that file is not an image Pillow can read (use JPEG, PNG, WebP or GIF)') from e
    im = ImageOps.exif_transpose(im)
    im = im.convert('RGBA' if im.mode in ('RGBA', 'LA', 'P', 'PA') and _has_alpha(im) else 'RGB')
    w, h = im.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    im = im.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, 'WEBP', quality=85)
    return out.getvalue()


def _has_alpha(im):
    return im.mode in ('RGBA', 'LA', 'PA') or (im.mode == 'P' and 'transparency' in im.info)
