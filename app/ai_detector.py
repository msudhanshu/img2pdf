"""
Optional CNN document segmentation, used as the primary boundary locator.

The model is U²-Net (small variant, 4.4 MB, Apache-2.0) run through **OpenCV's
own DNN module** — deliberately, because it means no extra Python dependency:
``onnxruntime`` has no wheels for recent Python versions, while cv2.dnn ships
with the OpenCV that the scanner already needs. Inference is ~0.2 s on CPU.

The model predicts "the salient object in this photo", which for a document
photo is the document. Its mask is coarse, so it is not used as the crop
directly: the scanner turns it into candidate quadrilaterals and still scores
them on real image evidence. If the model is missing, fails to download, or
produces nothing usable, the classical detector takes over.

The weights are downloaded once, on first use, to a per-user cache. Nothing is
uploaded — inference is entirely local.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from app import config

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - depends on install
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


ProgressCallback = Callable[[str], None]

MODEL_FILENAME = "u2netp.onnx"
MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
MODEL_SHA256 = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
MODEL_BYTES = 4574861

_INPUT_SIZE = 320
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

_net = None  # cached cv2.dnn.Net
_download_error: str | None = None  # remembered so a failed fetch is tried once


class ModelUnavailableError(RuntimeError):
    """The AI model could not be loaded or downloaded."""


def cache_dir() -> Path:
    """Where downloaded weights live (override with IMG2PDF_MODEL_DIR)."""
    override = os.environ.get("IMG2PDF_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".img2pdf" / "models"


def _search_paths() -> list[Path]:
    """Bundled copies win over the download cache, so a packaged .exe works offline."""
    paths = [cache_dir() / MODEL_FILENAME]
    bundled = getattr(sys, "_MEIPASS", None)  # PyInstaller one-file extraction dir
    if bundled:
        paths.insert(0, Path(bundled) / MODEL_FILENAME)
    paths.insert(0, Path(__file__).resolve().parent.parent / "models" / MODEL_FILENAME)
    return paths


def model_path() -> Path | None:
    """Path to an already-present model file, or None."""
    for candidate in _search_paths():
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def is_model_available() -> bool:
    return model_path() is not None


def is_available() -> bool:
    """AI detection can run right now (OpenCV present and weights on disk)."""
    return cv2 is not None and is_model_available()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_model(progress: ProgressCallback | None = None) -> Path:
    """
    Fetch the weights once into the cache directory. Returns the model path.

    The download is verified against a pinned SHA-256 before it is installed,
    so a truncated or tampered file is never loaded.
    """
    existing = model_path()
    if existing is not None:
        return existing

    def report(message: str) -> None:
        if progress:
            progress(message)

    destination = cache_dir() / MODEL_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    report(f"Downloading document model ({MODEL_BYTES / 1e6:.1f} MB, one time)...")

    temp_file = Path(tempfile.mkstemp(prefix="u2netp_", suffix=".part")[1])
    try:
        request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "img2pdf"})
        with urllib.request.urlopen(request, timeout=120) as response:
            with temp_file.open("wb") as handle:
                shutil.copyfileobj(response, handle)

        actual = _sha256(temp_file)
        if actual != MODEL_SHA256:
            raise ModelUnavailableError(
                "Downloaded model failed its checksum; it was not installed."
            )
        shutil.move(str(temp_file), destination)
    except ModelUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - network/IO problems are all "no model"
        raise ModelUnavailableError(f"Could not download the model: {exc}") from exc
    finally:
        temp_file.unlink(missing_ok=True)

    report("Model ready.")
    return destination


def ensure_model(
    allow_download: bool = True, progress: ProgressCallback | None = None
) -> Path:
    global _download_error
    existing = model_path()
    if existing is not None:
        return existing
    if not allow_download:
        raise ModelUnavailableError("The AI model is not installed.")
    if _download_error is not None:
        # Already failed once this session (offline, blocked, disk full). Don't
        # re-try for every page — fall straight back to the classical detector.
        raise ModelUnavailableError(_download_error)
    try:
        return download_model(progress)
    except ModelUnavailableError as exc:
        _download_error = str(exc)
        raise


def _load_net(path: Path):
    global _net
    if _net is None:
        if cv2 is None:
            raise ModelUnavailableError("OpenCV is not installed.")
        try:
            _net = cv2.dnn.readNetFromONNX(str(path))
        except Exception as exc:  # noqa: BLE001
            raise ModelUnavailableError(f"Could not load the model: {exc}") from exc
    return _net


def segment(
    bgr: np.ndarray,
    allow_download: bool = True,
    progress: ProgressCallback | None = None,
) -> np.ndarray:
    """
    Return a 0/255 document mask the same size as ``bgr``.

    Raises ModelUnavailableError when the model cannot be used at all, so the
    caller can fall back to the classical detector.
    """
    if cv2 is None:
        raise ModelUnavailableError("OpenCV is not installed.")

    net = _load_net(ensure_model(allow_download, progress))

    blob = cv2.dnn.blobFromImage(
        bgr, 1.0 / 255.0, (_INPUT_SIZE, _INPUT_SIZE), swapRB=True, crop=False
    )
    mean = np.array(_MEAN, dtype=np.float32).reshape(1, 3, 1, 1)
    std = np.array(_STD, dtype=np.float32).reshape(1, 3, 1, 1)
    blob = ((blob - mean) / std).astype(np.float32)

    net.setInput(blob)
    prediction = net.forward()

    saliency = np.asarray(prediction).reshape(_INPUT_SIZE, _INPUT_SIZE).astype(np.float32)
    spread = float(saliency.max() - saliency.min())
    if spread < 1e-6:
        raise ModelUnavailableError("Model returned an empty prediction.")
    saliency = (saliency - saliency.min()) / spread

    height, width = bgr.shape[:2]
    full = cv2.resize(saliency, (width, height), interpolation=cv2.INTER_LINEAR)
    return (full > config.SCAN_AI_MASK_THRESHOLD).astype(np.uint8) * 255
