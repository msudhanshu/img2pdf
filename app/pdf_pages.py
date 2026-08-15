"""
Rasterise pages of an existing PDF so they can be packed like any other image.

The converter's whole size story (compress harder, then move pages to the next
PDF) only works on image bytes, so a selected PDF is turned into one image per
page up front. Text-ish pages are kept lossless as PNG; pages that are really
photographs go out as high-quality JPEG, which is what the compression ladder
would end up producing anyway.

Rendering uses pypdfium2 (PDFium, no external binaries).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from app import config

try:  # pypdfium2 is optional; without it the app still converts plain images.
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - depends on install
    pdfium = None  # type: ignore[assignment]


class PdfRenderUnavailableError(RuntimeError):
    """Raised when a PDF is selected but pypdfium2 is not installed."""


def is_available() -> bool:
    return pdfium is not None


def _require_pdfium() -> None:
    if pdfium is None:
        raise PdfRenderUnavailableError(
            "Including PDFs needs pypdfium2. Install it with:\n"
            "    pip install pypdfium2"
        )


def page_count(pdf_path: Path | str) -> int:
    """Number of pages in a PDF (0 when it cannot be opened)."""
    _require_pdfium()
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception:  # noqa: BLE001 - corrupt/encrypted file is not fatal
        return 0
    try:
        return len(document)
    finally:
        document.close()


def _looks_like_line_art(page: Image.Image) -> bool:
    """
    True for text/vector pages, where PNG is both smaller and cleaner than JPEG.

    Scanned photos and images embedded in the PDF have thousands of distinct
    colours and fail this test immediately, so the check stays cheap.
    """
    probe = page.resize((256, 256), Image.BILINEAR)
    return probe.getcolors(maxcolors=192) is not None


def _save_page(page: Image.Image, destination_stem: Path) -> Path:
    if _looks_like_line_art(page):
        destination = destination_stem.with_suffix(".png")
        page.save(destination, format="PNG", optimize=True)
        return destination
    destination = destination_stem.with_suffix(".jpg")
    page.convert("RGB").save(
        destination, format="JPEG", quality=config.PDF_RENDER_JPEG_QUALITY, subsampling=0
    )
    return destination


def render_pdf_pages(
    pdf_path: Path | str,
    out_dir: Path | str,
    stem: str,
    dpi: float = config.PDF_RENDER_DPI,
) -> list[Path]:
    """
    Render every page of ``pdf_path`` into ``out_dir`` and return the files in order.

    ``stem`` prefixes the written files, so pages from different PDFs never
    collide and the page order stays visible on disk.
    """
    _require_pdfium()
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    document = pdfium.PdfDocument(str(pdf_path))
    try:
        scale = float(dpi) / 72.0
        rendered: list[Path] = []
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
            finally:
                page.close()
            rendered.append(_save_page(image, out_path / f"{stem}_p{index + 1:04d}"))
            image.close()
        return rendered
    finally:
        document.close()
