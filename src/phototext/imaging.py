from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()


class ImageReadError(Exception):
    pass


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


DEFAULT_MAX_PIXELS = 357_913_941


def _bomb_check(im: Image.Image, max_pixels: int) -> None:
    """Refuse images above the pixel budget before any decode happens."""
    width, height = im.size
    if max_pixels and width * height > max_pixels:
        raise ImageReadError(
            f"possible decompression bomb: {width}x{height} = {width * height} "
            f"pixels exceeds max_image_pixels ({max_pixels})"
        )


def prepare_image(
    path: Path,
    max_edge: int = 1024,
    jpeg_quality: int = 85,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> bytes:
    try:
        with Image.open(path) as im:
            _bomb_check(im, max_pixels)
            return _encode(im, max_edge, jpeg_quality)
    except ImageReadError:
        raise
    except Exception as first_error:
        converted = _sips_to_jpeg(path)
        if converted is None:
            raise ImageReadError(f"{type(first_error).__name__}: {first_error}") from first_error
        try:
            with Image.open(converted) as im:
                _bomb_check(im, max_pixels)
                return _encode(im, max_edge, jpeg_quality)
        except ImageReadError:
            raise
        except Exception as second_error:
            raise ImageReadError(f"{type(second_error).__name__}: {second_error}") from second_error
        finally:
            converted.unlink(missing_ok=True)


TILE_OVERLAP = 0.08


def prepare_tiles(
    path: Path,
    max_edge: int = 1024,
    jpeg_quality: int = 85,
    overlap: float = TILE_OVERLAP,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> list[bytes]:
    """Four overlapping quadrant JPEGs, cut from the ORIGINAL resolution.

    Each tile is downscaled to max_edge independently, so a dense photo gets
    roughly twice the linear resolution per tile compared to the whole-image
    pass. Order: top-left, top-right, bottom-left, bottom-right.
    """
    try:
        with Image.open(path) as im:
            _bomb_check(im, max_pixels)
            return _encode_tiles(im, max_edge, jpeg_quality, overlap)
    except ImageReadError:
        raise
    except Exception as first_error:
        converted = _sips_to_jpeg(path)
        if converted is None:
            raise ImageReadError(f"{type(first_error).__name__}: {first_error}") from first_error
        try:
            with Image.open(converted) as im:
                _bomb_check(im, max_pixels)
                return _encode_tiles(im, max_edge, jpeg_quality, overlap)
        except ImageReadError:
            raise
        except Exception as second_error:
            raise ImageReadError(f"{type(second_error).__name__}: {second_error}") from second_error
        finally:
            converted.unlink(missing_ok=True)


def _encode_tiles(
    im: Image.Image, max_edge: int, jpeg_quality: int, overlap: float
) -> list[bytes]:
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    width, height = im.size
    tile_w = min(width, max(1, round(width * (0.5 + overlap))))
    tile_h = min(height, max(1, round(height * (0.5 + overlap))))
    boxes = [
        (0, 0, tile_w, tile_h),
        (width - tile_w, 0, width, tile_h),
        (0, height - tile_h, tile_w, height),
        (width - tile_w, height - tile_h, width, height),
    ]
    return [_encode(im.crop(box), max_edge, jpeg_quality) for box in boxes]


def crop_jpeg(
    path: Path,
    box: tuple[int, int, int, int],
    max_edge: int = 512,
    jpeg_quality: int = 85,
    margin: float = 0.2,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> bytes:
    """JPEG crop of a region (x, y, w, h) in original-image pixels.

    A little margin is added around the box so the crop keeps hair, chin,
    and shoulders — enough context to re-recognize a face. Coordinates are
    relative to the EXIF-oriented image (what a browser displays).
    """
    try:
        with Image.open(path) as im:
            _bomb_check(im, max_pixels)
            return _encode_crop(im, box, max_edge, jpeg_quality, margin)
    except ImageReadError:
        raise
    except Exception as first_error:
        converted = _sips_to_jpeg(path)
        if converted is None:
            raise ImageReadError(f"{type(first_error).__name__}: {first_error}") from first_error
        try:
            with Image.open(converted) as im:
                _bomb_check(im, max_pixels)
                return _encode_crop(im, box, max_edge, jpeg_quality, margin)
        except ImageReadError:
            raise
        except Exception as second_error:
            raise ImageReadError(f"{type(second_error).__name__}: {second_error}") from second_error
        finally:
            converted.unlink(missing_ok=True)


def _encode_crop(
    im: Image.Image,
    box: tuple[int, int, int, int],
    max_edge: int,
    jpeg_quality: int,
    margin: float,
) -> bytes:
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    width, height = im.size
    x, y, w, h = box
    x, y = max(0, int(x)), max(0, int(y))
    w, h = max(1, int(w)), max(1, int(h))
    dx, dy = int(w * margin), int(h * margin)
    left = max(0, x - dx)
    top = max(0, y - dy)
    right = min(width, x + w + dx)
    bottom = min(height, y + h + dy)
    if right <= left or bottom <= top:
        raise ImageReadError(f"crop box {box} is outside the {width}x{height} image")
    return _encode(im.crop((left, top, right, bottom)), max_edge, jpeg_quality)


def test_image_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 40, 40)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _encode(im: Image.Image, max_edge: int, jpeg_quality: int) -> bytes:
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / longest
        im = im.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()


def _sips_to_jpeg(path: Path) -> Path | None:
    sips = shutil.which("sips")
    if sips is None:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="phototext-sips-")
    os.close(fd)
    try:
        result = subprocess.run(
            [sips, "-s", "format", "jpeg", str(path), "--out", tmp],
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        Path(tmp).unlink(missing_ok=True)
        return None
    try:
        ok = result.returncode == 0 and Path(tmp).stat().st_size > 0
    except OSError:
        ok = False
    if not ok:
        Path(tmp).unlink(missing_ok=True)
        return None
    return Path(tmp)
