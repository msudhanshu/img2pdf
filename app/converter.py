"""Chunk images into PDFs with compression, then fewer pages if needed."""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import img2pdf

from app import config
from app.compression import ImageCompressor, default_compressor
from app.scanner import ScanOptions, scan_file, scan_file_detailed

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


def _compressed_pages(
    image_paths: list[Path],
    compressor: ImageCompressor,
) -> list[bytes]:
    return [compressor.compress(path) for path in image_paths]


@contextmanager
def _scanned_sources(
    paths: list[Path],
    scan_options: ScanOptions | None,
    report: ProgressCallback,
) -> Iterator[list[Path]]:
    """
    Yield paths to scanned copies of ``paths`` (auto-cropped, deskewed, cleaned).

    Scanning is done once up front and cached in a temp folder, because the
    compression loop re-encodes the same page many times. Order is preserved,
    and any page that fails to scan falls back to its original file.
    """
    if scan_options is None or not scan_options.enabled:
        yield paths
        return

    with tempfile.TemporaryDirectory(prefix="img2pdf_scan_") as temp_dir:
        scan_dir = Path(temp_dir)
        scanned: list[Path] = []
        for index, path in enumerate(paths, start=1):
            report(f"Scanning {path.name} ({index}/{len(paths)})...")
            try:
                page = scan_file(path, scan_options)
            except Exception as exc:  # noqa: BLE001 - one bad page must not fail the run
                report(f"Could not scan {path.name} ({exc}); using the original.")
                scanned.append(path)
                continue

            # Bitonal pages go through PNG so the temp copy adds no JPEG ringing.
            bitonal = page.mode == "L" and (page.getcolors(maxcolors=4) is not None)
            if bitonal:
                destination = scan_dir / f"{index:04d}.png"
                page.save(destination, format="PNG", optimize=True)
            else:
                destination = scan_dir / f"{index:04d}.jpg"
                page.save(
                    destination,
                    format="JPEG",
                    quality=config.SCAN_INTERMEDIATE_JPEG_QUALITY,
                    subsampling=0,
                )
            scanned.append(destination)
        yield scanned


@dataclass
class CropPreviewResult:
    """Outcome of a crop-preview run (no PDFs written)."""

    output_dir: Path | None = None
    output_paths: list[Path] = field(default_factory=list)
    image_count: int = 0
    cropped_count: int = 0
    warnings: list[str] = field(default_factory=list)


def export_crop_previews(
    image_paths: list[Path | str],
    output_dir: Path | str | None = None,
    scan_options: ScanOptions | None = None,
    progress: ProgressCallback | None = None,
) -> CropPreviewResult:
    """
    Write scan results to a ``temp_crop`` folder so the crop can be checked by eye.

    Three files per photo, no PDF involved:

    * ``<name>_1_outline.jpg`` – the original with the detected page outline drawn
    * ``<name>_2_crop.jpg``    – cropped and deskewed, original pixels otherwise
    * ``<name>_3_scan.jpg``    – the final cleaned-up page as it would enter the PDF
    """
    paths = normalize_image_paths(list(image_paths))
    if not paths:
        raise ValueError("No valid image files selected.")

    options = scan_options or ScanOptions()
    options = ScanOptions(
        enabled=True,  # the preview always scans, whatever the checkbox says
        mode=options.mode,
        auto_crop=options.auto_crop,
        deskew=options.deskew,
        sharpen=options.sharpen,
    )

    if output_dir is None:
        out_dir = infer_source_output_dir(paths) / config.SCAN_PREVIEW_DIR_NAME
    else:
        out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    result = CropPreviewResult(output_dir=out_dir, image_count=len(paths))

    def report(message: str) -> None:
        if progress:
            progress(message)

    report(f"Writing crop previews for {len(paths)} image(s) → {out_dir}")

    for index, path in enumerate(paths, start=1):
        report(f"Cropping {path.name} ({index}/{len(paths)})...")
        try:
            outline, geometry, enhanced, scan_report = scan_file_detailed(path, options)
        except Exception as exc:  # noqa: BLE001 - one bad page must not fail the run
            warning = f"Could not scan {path.name}: {exc}"
            report(warning)
            result.warnings.append(warning)
            continue

        stem = f"{index:02d}_{path.stem}"
        for suffix, image in (
            ("1_outline", outline),
            ("2_crop", geometry),
            ("3_scan", enhanced),
        ):
            destination = out_dir / f"{stem}_{suffix}.jpg"
            image.convert("L" if image.mode == "L" else "RGB").save(
                destination, format="JPEG", quality=92
            )
            result.output_paths.append(destination)

        if scan_report.cropped:
            result.cropped_count += 1
        else:
            warning = f"No page boundary found in {path.name} (kept the full frame)."
            report(warning)
            result.warnings.append(warning)

    report(
        f"Crop preview done: {result.cropped_count}/{len(paths)} auto-cropped → {out_dir}"
    )
    return result


def _build_pdf_bytes(pages: list[bytes]) -> bytes:
    return img2pdf.convert(pages)


def _write_pdf(output_path: Path, pdf_bytes: bytes) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(pdf_bytes)


def convert_images_to_pdfs(
    image_paths: list[Path | str],
    output_dir: Path | str | None = None,
    max_images_per_pdf: int = config.DEFAULT_MAX_IMAGES_PER_PDF,
    max_pdf_size_mb: float = config.DEFAULT_MAX_PDF_SIZE_MB,
    progress: ProgressCallback | None = None,
    compressor: ImageCompressor | None = None,
    scan_options: ScanOptions | None = None,
) -> ConvertResult:
    """
    Convert selected images into one or more size-capped PDFs.

    Images are sorted by date created. When ``scan_options`` is enabled each
    photo is first auto-cropped to the document boundary, deskewed and cleaned
    up to look scanned. Each PDF then starts with up to ``max_images_per_pdf``
    pages. Compression is tried first (original dimensions kept). If the PDF is
    still over the size limit at maximum compression, the last image is moved
    to the next PDF and the loop repeats.
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
    if max_images_per_pdf < 1:
        raise ValueError("max_images_per_pdf must be at least 1.")

    compressor = compressor or default_compressor()
    result = ConvertResult(image_count=len(paths))

    def report(message: str) -> None:
        if progress:
            progress(message)

    report(
        f"Converting {len(paths)} image(s) (grouped by date created) → {out_dir}"
    )

    with _scanned_sources(paths, scan_options, report) as sources:
        _pack_into_pdfs(
            sources=sources,
            out_dir=out_dir,
            max_images_per_pdf=max_images_per_pdf,
            max_bytes=max_bytes,
            compressor=compressor,
            report=report,
            result=result,
        )

    result.pdf_count = len(result.output_paths)
    report("Done.")
    return result


def _pack_into_pdfs(
    sources: list[Path],
    out_dir: Path,
    max_images_per_pdf: int,
    max_bytes: int,
    compressor: ImageCompressor,
    report: ProgressCallback,
    result: ConvertResult,
) -> None:
    """Fill PDFs in order: compress harder first, then drop pages to the next PDF."""
    remaining = list(sources)
    part = 1

    while remaining:
        candidate_count = min(max_images_per_pdf, len(remaining))
        compressor.reset()
        output_path = out_dir / config.OUTPUT_NAME_PATTERN.format(part=part)

        while True:
            candidate = remaining[:candidate_count]
            report(
                f"Building {output_path.name} "
                f"({len(candidate)} image(s), {compressor.label})..."
            )
            pdf_bytes = _build_pdf_bytes(_compressed_pages(candidate, compressor))
            size_mb = len(pdf_bytes) / (1024 * 1024)

            if len(pdf_bytes) <= max_bytes:
                _write_pdf(output_path, pdf_bytes)
                report(f"Saved {output_path.name} ({size_mb:.2f} MB)")
                result.output_paths.append(output_path)
                remaining = remaining[candidate_count:]
                part += 1
                break

            if compressor.strengthen():
                continue

            if candidate_count > 1:
                candidate_count -= 1
                compressor.reset()
                report(
                    f"{output_path.name} still over limit after max compression; "
                    f"trying {candidate_count} image(s)..."
                )
                continue

            limit_mb = max_bytes / (1024 * 1024)
            warning = (
                f"{output_path.name} is {size_mb:.2f} MB "
                f"(over {limit_mb:.2f} MB limit) after maximum compression "
                f"of a single image."
            )
            _write_pdf(output_path, pdf_bytes)
            report(warning)
            result.output_paths.append(output_path)
            result.warnings.append(warning)
            remaining = remaining[1:]
            part += 1
            break
