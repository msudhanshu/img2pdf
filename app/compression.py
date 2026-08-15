"""Image compression used before PDF packing.

Compressors must not change pixel dimensions. Swap in a stronger
implementation by passing any ``ImageCompressor`` to the converter.

Default stack (all local, no uploads):
- JPEG: Pillow encode, then MozJPEG lossless optimize
- PNG: oxipng lossless first (when installed), then JPEG if more size is needed
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from pathlib import Path

from PIL import Image, ImageOps

from app import config

try:
    import mozjpeg_lossless_optimization as mozjpeg
except ImportError:  # pragma: no cover - optional at runtime
    mozjpeg = None

try:
    import oxipng
except ImportError:  # pragma: no cover - no wheel on some Python versions
    oxipng = None

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
PNG_EXTENSIONS = {".png"}


class ImageCompressor(ABC):
    """Compress an image file without changing its width or height."""

    @abstractmethod
    def compress(self, source_path: Path) -> bytes:
        """Return compressed image bytes (JPEG or PNG) for PDF embedding."""

    @abstractmethod
    def strengthen(self) -> bool:
        """Increase compression. Return False if already at the strongest setting."""

    @abstractmethod
    def reset(self) -> None:
        """Return to the mildest compression setting."""

    @property
    @abstractmethod
    def label(self) -> str:
        """Short description of the current settings (for status text)."""


def _to_rgb_or_gray(image: Image.Image) -> Image.Image:
    if image.mode == "L":
        return image
    if image.mode == "RGB":
        return image.convert("RGB")
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _load_oriented(path: Path) -> Image.Image:
    with Image.open(path) as img:
        oriented = ImageOps.exif_transpose(img)
        prepared = _to_rgb_or_gray(oriented)
        return prepared.copy()


def _is_bitonal(image: Image.Image) -> bool:
    return image.mode == "L" and image.getcolors(maxcolors=4) is not None


def _mozjpeg_optimize(jpeg_bytes: bytes) -> bytes:
    if mozjpeg is None:
        return jpeg_bytes
    try:
        optimized = mozjpeg.optimize(jpeg_bytes)
    except Exception:
        return jpeg_bytes
    return optimized if optimized and len(optimized) <= len(jpeg_bytes) else jpeg_bytes


def encode_jpeg(image: Image.Image, quality: int) -> bytes:
    """Lossy JPEG at the given quality. Dimensions are unchanged."""
    buffer = io.BytesIO()
    save_kwargs: dict = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "progressive": True,
    }
    if image.mode == "RGB":
        save_kwargs["subsampling"] = 2  # 4:2:0, typical photo compression
    image.save(buffer, **save_kwargs)
    return _mozjpeg_optimize(buffer.getvalue())


def encode_png_lossless(path: Path, image: Image.Image) -> bytes:
    """Lossless PNG. Prefers oxipng; falls back to Pillow."""
    source_bytes: bytes | None = None
    if path.suffix.lower() in PNG_EXTENSIONS:
        source_bytes = path.read_bytes()

    if oxipng is not None and source_bytes is not None:
        try:
            kwargs = {"level": 4}
            strip = getattr(oxipng, "StripChunks", None)
            if strip is not None:
                kwargs["strip"] = strip.all()
            optimized = oxipng.optimize_from_memory(source_bytes, **kwargs)
            if optimized:
                return bytes(optimized)
        except Exception:
            pass

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class AdaptiveImageCompressor(ImageCompressor):
    """JPEG + PNG compressor: lossless first, then JPEG quality steps.

    Dimensions are never changed. Plug this (or a subclass) into
    ``convert_images_to_pdfs(..., compressor=...)``.
    """

    def __init__(
        self,
        jpeg_qualities: tuple[int, ...] | None = None,
    ) -> None:
        qualities = jpeg_qualities or tuple(
            range(
                config.INITIAL_JPEG_QUALITY - config.QUALITY_STEP,
                config.MIN_JPEG_QUALITY - 1,
                -config.QUALITY_STEP,
            )
        )
        # 0 = lossless PNG / JPEG q=INITIAL, then 75, 65, ... MIN
        self._jpeg_qualities = qualities
        # 0 = lossless/native, 1..n = jpeg quality ladder
        self._stage = 0

    def compress(self, source_path: Path) -> bytes:
        path = Path(source_path)
        image = _load_oriented(path)
        if self._stage == 0:
            return self._lossless(path, image)
        quality = self._jpeg_qualities[self._stage - 1]
        if _is_bitonal(image):
            # 1-bit scans stay PNG; JPEG ringing would hurt text.
            return encode_png_lossless(path, image)
        return encode_jpeg(image, quality)

    def _lossless(self, path: Path, image: Image.Image) -> bytes:
        suffix = path.suffix.lower()
        if suffix in PNG_EXTENSIONS or _is_bitonal(image):
            return encode_png_lossless(path, image)
        if suffix in JPEG_EXTENSIONS:
            # Bake EXIF orientation, then MozJPEG-optimize a high-quality JPEG.
            return encode_jpeg(image, config.INITIAL_JPEG_QUALITY)
        # BMP / TIFF / WebP: start on the JPEG ladder's mildest setting.
        return encode_jpeg(image, config.INITIAL_JPEG_QUALITY)

    def strengthen(self) -> bool:
        if self._stage >= len(self._jpeg_qualities):
            return False
        self._stage += 1
        return True

    def reset(self) -> None:
        self._stage = 0

    @property
    def label(self) -> str:
        if self._stage == 0:
            extras = []
            if oxipng is not None:
                extras.append("oxipng")
            if mozjpeg is not None:
                extras.append("mozjpeg")
            suffix = f" [{', '.join(extras)}]" if extras else ""
            return f"lossless / JPEG q={config.INITIAL_JPEG_QUALITY}{suffix}"
        return f"JPEG quality={self._jpeg_qualities[self._stage - 1]}"


class JpegQualityCompressor(ImageCompressor):
    """Simpler compressor: JPEG quality steps only (original dimensions kept)."""

    def __init__(
        self,
        initial_quality: int = config.INITIAL_JPEG_QUALITY,
        min_quality: int = config.MIN_JPEG_QUALITY,
        quality_step: int = config.QUALITY_STEP,
    ) -> None:
        self._initial_quality = initial_quality
        self._min_quality = min_quality
        self._quality_step = quality_step
        self._quality = initial_quality

    def compress(self, source_path: Path) -> bytes:
        image = _load_oriented(Path(source_path))
        return encode_jpeg(image, self._quality)

    def strengthen(self) -> bool:
        next_quality = self._quality - self._quality_step
        if next_quality < self._min_quality:
            return False
        self._quality = next_quality
        return True

    def reset(self) -> None:
        self._quality = self._initial_quality

    @property
    def label(self) -> str:
        return f"JPEG quality={self._quality}"


def default_compressor() -> ImageCompressor:
    """Factory used by the converter unless a custom compressor is injected."""
    return AdaptiveImageCompressor()
