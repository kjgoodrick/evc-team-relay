"""Bounded raster verification. Call from an admitted worker, never the event loop.

Only sanitized, orientation-normalized pixels leave this module. Originals,
metadata, animation and unsupported/active formats are not stored or served.
"""

import hashlib
from dataclasses import dataclass
from io import BytesIO

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
THUMBNAIL_EDGE = 512
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


@dataclass(frozen=True)
class Rendition:
    data: bytes
    mime: str
    width: int
    height: int

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class VerifiedPicture:
    source_mime: str
    content: Rendition
    thumbnail: Rendition


class LimitedBuffer(BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, value):
        if self.tell() + len(value) > self.limit:
            raise HTTPException(413, {"code": "picture_output_too_large"})
        return super().write(value)


def encode(image, *, jpeg=False, limit=MAX_BYTES):
    with LimitedBuffer(limit) as output:
        image.save(
            output,
            format="JPEG" if jpeg else "PNG",
            **({"quality": 90} if jpeg else {"compress_level": 3}),
        )
        return Rendition(output.getvalue(), "image/jpeg" if jpeg else "image/png", *image.size)


def verify(data: bytes) -> VerifiedPicture:
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, {"code": "picture_too_large"})
    try:
        with Image.open(BytesIO(data), formats=list(FORMATS)) as source:
            # JPEG's factory can identify an MPO container despite an explicit
            # JPEG allowlist. Check the resulting decoder's actual format too.
            if source.format not in FORMATS:
                raise HTTPException(415, {"code": "invalid_picture"})
            if source.width * source.height > MAX_PIXELS:
                raise HTTPException(413, {"code": "picture_too_many_pixels"})
            source_mime = FORMATS[source.format]
            source.verify()
        # verify() does not decode pixels; reopen and force the actual decode.
        with Image.open(BytesIO(data), formats=list(FORMATS)) as source:
            if getattr(source, "is_animated", False):
                raise HTTPException(415, {"code": "animated_picture_unsupported"})
            source.load()
            ImageOps.exif_transpose(source, in_place=True)
            has_alpha = "A" in source.getbands() or "transparency" in source.info
            with source.convert("RGBA" if has_alpha else "RGB") as pixels:
                # Conversion may copy EXIF/ICC/XMP/comments into info. Never
                # pass those to an encoder, including implicit PNG EXIF writes.
                pixels.info.clear()
                content = encode(pixels, jpeg=source_mime == "image/jpeg" and not has_alpha)
                pixels.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE), Image.Resampling.LANCZOS)
                thumbnail = encode(pixels, limit=MAX_THUMBNAIL_BYTES)
                return VerifiedPicture(source_mime, content, thumbnail)
    except Image.DecompressionBombError:
        raise HTTPException(413, {"code": "picture_too_many_pixels"}) from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise HTTPException(415, {"code": "invalid_picture"}) from None
