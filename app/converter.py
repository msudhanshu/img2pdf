"""Chunk images into PDFs with size-aware resize retries."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import img2pdf
from PIL import Image

from app import config

ProgressCallback = Callable[[str], None]


@dataclass
class ConvertResult:
    """Outcome of a conversion run."""

    output_paths: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pdf_count: int = 0
    image_count: int = 0


def is_image_path(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in config.IMAGE_EXTENSIONS


def file_created_time(path: Path) -> float:
    """Creation time when available (Windows st_ctime / macOS st_birthtime)."""
    stat = path.stat()
    return float(getattr(stat, "st_birthtime", stat.st_ctime))


def sort_by_created(paths: list[Path]) -> list[Path]:
    """Oldest created first; name as tie-breaker. Used for PDF grouping."""
    return sorted(paths, key=lambda p: (file_created_time(p), p.name.lower()))


def collect_images_from_folder(folder: Path) -> list[Path]:
    """Non-recursive listing of image-extension files only, sorted by created date."""
    if not folder.is_dir():
        return []
    images = [p.resolve() for p in folder.iterdir() if is_image_path(p)]
    return sort_by_created(images)


def normalize_image_paths(paths: list[Path | str]) -> list[Path]:
    """Keep valid images, drop duplicates, then sort by date created."""
    seen: set[Path] = set()
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path in seen or not is_image_path(path):
            continue
        seen.add(path)
        result.append(path)
    return sort_by_created(result)


def infer_source_output_dir(paths: list[Path]) -> Path:
    """Prefer the shared parent folder of selected images (source path)."""
    if not paths:
        raise ValueError("No image paths provided.")
    parents = [p.parent.resolve() for p in paths]
    counts: dict[Path, int] = {}
    for parent in parents:
        counts[parent] = counts.get(parent, 0) + 1
    # Most common parent; ties broken by path string for stability
    return max(counts.items(), key=lambda item: (item[1], str(item[0])))[0]


def chunk_paths(paths: list[Path], max_images: int) -> list[list[Path]]:
    if max_images < 1:
        raise ValueError("max_images must be at least 1")
    return [paths[i : i + max_images] for i in range(0, len(paths), max_images)]

def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in ("RGB", "L"):
        return image.convert("RGB")
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _encode_jpeg(image: Image.Image, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _prepare_jpeg_pages(
    image_paths: list[Path],
    scale: float,
    quality: int,
) -> list[bytes]:
    pages: list[bytes] = []
    for path in image_paths:
        with Image.open(path) as img:
            rgb = _to_rgb(img)
            if scale < 1.0:
                width = max(1, int(rgb.width * scale))
                height = max(1, int(rgb.height * scale))
                rgb = rgb.resize((width, height), Image.Resampling.LANCZOS)
            pages.append(_encode_jpeg(rgb, quality))
    return pages


def _build_pdf_bytes(jpeg_pages: list[bytes]) -> bytes:
    return img2pdf.convert(jpeg_pages)


def _min_scale_for_paths(image_paths: list[Path]) -> float:
    """Scale floor so the longest edge stays at or above MIN_LONG_EDGE_PX when possible."""
    max_long_edge = 0
    for path in image_paths:
        with Image.open(path) as img:
            max_long_edge = max(max_long_edge, max(img.size))
    if max_long_edge <= 0:
        return config.INITIAL_SCALE
    if max_long_edge <= config.MIN_LONG_EDGE_PX:
        return config.INITIAL_SCALE
    return config.MIN_LONG_EDGE_PX / max_long_edge


def convert_chunk_to_pdf(
    image_paths: list[Path],
    output_path: Path,
    max_bytes: int,
    progress: ProgressCallback | None = None,
) -> str | None:
    """
    Write one PDF for the given images, resizing in a loop until under max_bytes.

    Returns a warning string if the size floor was hit while still over limit, else None.
    """
    if not image_paths:
        raise ValueError("image_paths must not be empty")

    def report(message: str) -> None:
        if progress:
            progress(message)

    scale = config.INITIAL_SCALE
    quality = config.INITIAL_JPEG_QUALITY
    min_scale = _min_scale_for_paths(image_paths)
    warning: str | None = None
    attempt = 0

    while True:
        attempt += 1
        report(
            f"Building {output_path.name} (attempt {attempt}, "
            f"scale={scale:.2f}, quality={quality})..."
        )
        jpeg_pages = _prepare_jpeg_pages(image_paths, scale, quality)
        pdf_bytes = _build_pdf_bytes(jpeg_pages)

        if len(pdf_bytes) <= max_bytes:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(pdf_bytes)
            report(
                f"Saved {output_path.name} "
                f"({len(pdf_bytes) / (1024 * 1024):.2f} MB)"
            )
            return warning

        can_drop_quality = quality - config.QUALITY_STEP >= config.MIN_JPEG_QUALITY
        next_scale = scale * config.SCALE_FACTOR
        can_scale = next_scale >= min_scale - 1e-9

        if can_drop_quality:
            quality -= config.QUALITY_STEP
            continue
        if can_scale:
            scale = next_scale
            quality = config.INITIAL_JPEG_QUALITY
            continue

        # Floors reached; save best effort and warn.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(pdf_bytes)
        size_mb = len(pdf_bytes) / (1024 * 1024)
        limit_mb = max_bytes / (1024 * 1024)
        warning = (
            f"{output_path.name} is {size_mb:.2f} MB "
            f"(over {limit_mb:.2f} MB limit) after minimum resize/quality."
        )
        report(warning)
        return warning


def convert_images_to_pdfs(
    image_paths: list[Path | str],
    output_dir: Path | str | None = None,
    max_images_per_pdf: int = config.DEFAULT_MAX_IMAGES_PER_PDF,
    max_pdf_size_mb: float = config.DEFAULT_MAX_PDF_SIZE_MB,
    progress: ProgressCallback | None = None,
) -> ConvertResult:
    """
    Convert selected images into one or more size-capped PDFs.

    Images are sorted by date created before grouping into chunks of
    ``max_images_per_pdf``. PDFs are written into the source folder by default.
    """
    paths = normalize_image_paths(list(image_paths))
    if not paths:
        raise ValueError("No valid image files selected.")

    if output_dir is None:
        out_dir = infer_source_output_dir(paths)
    else:
        out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_pdf_size_mb * 1024 * 1024)
    if max_bytes < 1:
        raise ValueError("max_pdf_size_mb must be positive.")

    # Grouping order is creation date (already applied in normalize_image_paths).
    chunks = chunk_paths(paths, max_images_per_pdf)
    result = ConvertResult(image_count=len(paths), pdf_count=len(chunks))

    def report(message: str) -> None:
        if progress:
            progress(message)

    report(
        f"Converting {len(paths)} image(s) into {len(chunks)} PDF(s) "
        f"(grouped by date created) → {out_dir}"
    )

    for index, chunk in enumerate(chunks, start=1):
        output_path = out_dir / config.OUTPUT_NAME_PATTERN.format(part=index)
        warning = convert_chunk_to_pdf(
            chunk,
            output_path,
            max_bytes=max_bytes,
            progress=progress,
        )
        result.output_paths.append(output_path)
        if warning:
            result.warnings.append(warning)

    report("Done.")
    return result
