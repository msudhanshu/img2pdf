"""
Document scanner: auto-crop, perspective correction, deskew and "scanned" look.

Detection is segmentation-first, because contour fishing alone invents
boundaries on the photos people actually take (an ID page filling the frame,
a certificate on a dark table):

1. Model the background from the image border ring, so "document" means
   "the region in the middle that does not look like the surroundings".
2. Turn that mask into candidate quadrilaterals (mask outline, contour
   approximation), and score every candidate on real evidence: gradient
   support along each side, and contrast between the inside and the outside.
3. Only warp when a candidate is actually supported. When the document runs off
   the frame there is no closed boundary, so the crop falls back to the mask's
   bounding box — and when nothing is trustworthy, the frame is kept as is.

Clean-up is deliberately gentle: shading is removed additively from the L
channel only, so colour documents (ID cards, stamps, photos) keep their colour
and their midtones instead of being bleached.

Standalone preview:

    python -m app.scanner input.jpg output.jpg --mode auto --debug debug.jpg
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app import config

try:  # OpenCV/NumPy are optional; without them the app still converts unscanned images.
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - depends on install
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


SCAN_MODES = ("auto", "color", "gray", "bw")


class ScannerUnavailableError(RuntimeError):
    """Raised when scanning is requested but OpenCV is not installed."""


@dataclass
class ScanOptions:
    """User-facing knobs for the scan pipeline."""

    enabled: bool = config.DEFAULT_SCAN_ENABLED
    mode: str = config.DEFAULT_SCAN_MODE
    auto_crop: bool = True
    deskew: bool = True
    sharpen: bool = True

    def __post_init__(self) -> None:
        if self.mode not in SCAN_MODES:
            raise ValueError(
                f"mode must be one of {', '.join(SCAN_MODES)} (got {self.mode!r})"
            )


@dataclass
class ScanReport:
    """What the pipeline actually did to one image — used by the crop preview."""

    cropped: bool = False
    quad: "np.ndarray | None" = None
    crop_kind: str = "none"  # none | perspective | bounds
    detail: str = ""
    skew_deg: float = 0.0
    mode: str = ""


def is_available() -> bool:
    return cv2 is not None


def _require_cv2() -> None:
    if cv2 is None:
        raise ScannerUnavailableError(
            "Document scanning needs OpenCV. Install it with:\n"
            "    pip install opencv-python-headless numpy"
        )


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------


def _order_corners(points: np.ndarray) -> np.ndarray:
    """Return the 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = points.reshape(4, 2).astype(np.float32)
    ordered = np.zeros((4, 2), dtype=np.float32)
    coord_sum = pts.sum(axis=1)
    coord_diff = np.diff(pts, axis=1).ravel()
    ordered[0] = pts[np.argmin(coord_sum)]  # top-left
    ordered[2] = pts[np.argmax(coord_sum)]  # bottom-right
    ordered[1] = pts[np.argmin(coord_diff)]  # top-right
    ordered[3] = pts[np.argmax(coord_diff)]  # bottom-left
    return ordered


def _corner_angles(quad: np.ndarray) -> list[float]:
    angles = []
    for i in range(4):
        v1 = quad[(i - 1) % 4] - quad[i]
        v2 = quad[(i + 1) % 4] - quad[i]
        norm = float(np.linalg.norm(v1) * np.linalg.norm(v2))
        if norm == 0:
            return [0.0]
        cosine = float(np.dot(v1, v2)) / norm
        angles.append(float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))))
    return angles


def _geometry_ok(quad: np.ndarray, shape: tuple[int, int]) -> bool:
    """Cheap sanity gates before spending effort on evidence."""
    height, width = shape
    area = float(cv2.contourArea(quad.astype(np.float32)))
    if area <= 0:
        return False
    coverage = area / float(height * width)
    if not config.SCAN_MIN_QUAD_AREA_RATIO <= coverage <= 0.999:
        return False

    # Corners may sit slightly outside the frame (a page cut off at the edge),
    # but a quad reaching far beyond the photo is a detection failure.
    margin_x = width * config.SCAN_CORNER_MARGIN_RATIO
    margin_y = height * config.SCAN_CORNER_MARGIN_RATIO
    if (
        quad[:, 0].min() < -margin_x
        or quad[:, 0].max() > width + margin_x
        or quad[:, 1].min() < -margin_y
        or quad[:, 1].max() > height + margin_y
    ):
        return False

    angles = _corner_angles(quad)
    if min(angles) < 55 or max(angles) > 125:
        return False

    side_lengths = [float(np.linalg.norm(quad[(i + 1) % 4] - quad[i])) for i in range(4)]
    if min(side_lengths) < 0.05 * max(height, width):
        return False
    aspect = max(side_lengths) / max(min(side_lengths), 1e-6)
    return aspect <= 8.0


def _on_frame_border(point_a: np.ndarray, point_b: np.ndarray, shape: tuple[int, int]) -> bool:
    """True when a whole side lies along the edge of the photo."""
    height, width = shape
    tolerance = max(3.0, 0.01 * max(height, width))
    for axis, limit in ((0, width), (1, height)):
        if point_a[axis] <= tolerance and point_b[axis] <= tolerance:
            return True
        if point_a[axis] >= limit - tolerance and point_b[axis] >= limit - tolerance:
            return True
    return False


# --------------------------------------------------------------------------
# Evidence: does this quad sit on a real document boundary?
# --------------------------------------------------------------------------


def _sample_line(image: np.ndarray, start: np.ndarray, end: np.ndarray, count: int) -> np.ndarray:
    """Bilinear-ish samples along a line, clipped to the image."""
    height, width = image.shape[:2]
    ts = np.linspace(0.0, 1.0, count, dtype=np.float32)[:, None]
    points = start[None, :] * (1 - ts) + end[None, :] * ts
    xs = np.clip(points[:, 0], 0, width - 1).astype(np.int32)
    ys = np.clip(points[:, 1], 0, height - 1).astype(np.int32)
    return image[ys, xs]


def _quad_evidence(
    quad: np.ndarray, gradient: np.ndarray, lightness: np.ndarray
) -> tuple[float, str]:
    """
    Score a quad on measurable support. Returns ``(score, reason)``; score <= 0 rejects.

    Two independent signals per side:
    * gradient support — the fraction of the side lying on a real image edge
    * step contrast — the L difference between just inside and just outside
    Sides that coincide with the photo's own border are treated as neutral,
    since a document running off the frame has no visible edge there.
    """
    shape = gradient.shape[:2]
    if not _geometry_ok(quad, shape):
        return 0.0, "geometry"

    diagonal = float(np.hypot(*shape))
    offset = max(2.0, diagonal * 0.004)
    samples = 96
    center = quad.mean(axis=0)

    supports: list[float] = []
    contrasts: list[float] = []
    border_sides = 0

    for i in range(4):
        start, end = quad[i], quad[(i + 1) % 4]
        if _on_frame_border(start, end, shape):
            border_sides += 1
            continue

        # Outward normal for this side.
        edge = end - start
        length = float(np.linalg.norm(edge))
        if length < 1e-6:
            return 0.0, "degenerate side"
        normal = np.array([-edge[1], edge[0]], dtype=np.float32) / length
        midpoint = (start + end) / 2.0
        if float(np.dot(normal, midpoint - center)) < 0:
            normal = -normal

        # Gradient support: the strongest edge response within a small band.
        band = [
            _sample_line(gradient, start + normal * d, end + normal * d, samples)
            for d in (-offset, 0.0, offset)
        ]
        along_edge = np.max(np.stack(band), axis=0)
        threshold = max(12.0, float(np.percentile(gradient, 92)))
        supports.append(float((along_edge > threshold).mean()))

        inside = _sample_line(
            lightness, start - normal * offset * 2, end - normal * offset * 2, samples
        ).astype(np.float32)
        outside = _sample_line(
            lightness, start + normal * offset * 2, end + normal * offset * 2, samples
        ).astype(np.float32)
        contrasts.append(float(np.abs(np.median(inside) - np.median(outside))))

    if border_sides == 4:
        return 0.0, "quad is just the photo border"

    support = float(np.mean(supports)) if supports else 0.0
    weakest = float(np.min(supports)) if supports else 0.0
    contrast = float(np.mean(contrasts)) if contrasts else 0.0

    if support < config.SCAN_MIN_EDGE_SUPPORT:
        return 0.0, f"weak edges (support {support:.2f})"
    if weakest < config.SCAN_MIN_EDGE_SUPPORT * 0.4:
        return 0.0, f"one side unsupported ({weakest:.2f})"
    if contrast < config.SCAN_MIN_EDGE_CONTRAST:
        return 0.0, f"no step at the border (contrast {contrast:.1f})"

    area = float(cv2.contourArea(quad.astype(np.float32)))
    coverage = area / float(shape[0] * shape[1])
    score = (
        2.0 * support
        + min(contrast / 60.0, 1.0)
        + 0.6 * coverage
        + 0.15 * border_sides  # a page running off-frame is normal, not suspicious
    )
    return score, f"support={support:.2f} contrast={contrast:.0f} cover={coverage:.2f}"


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------


def _document_mask(bgr: np.ndarray) -> np.ndarray:
    """
    Foreground mask built from a background model of the photo's border ring.

    The surroundings (desk, floor, table) touch the frame edge; the document
    does not look like them. This is far more stable than hunting for contours.
    """
    height, width = bgr.shape[:2]
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)

    ring = max(2, int(min(height, width) * 0.04))
    border = np.concatenate(
        [
            lab[:ring].reshape(-1, 3),
            lab[-ring:].reshape(-1, 3),
            lab[:, :ring].reshape(-1, 3),
            lab[:, -ring:].reshape(-1, 3),
        ]
    )
    median = np.median(border, axis=0)
    spread = np.median(np.abs(border - median), axis=0) * 1.4826
    spread = np.maximum(spread, 4.0)

    distance = np.sqrt((((lab - median) / spread) ** 2).sum(axis=2))
    mask = (distance > config.SCAN_BACKGROUND_SIGMA).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (max(3, width // 100) | 1, max(3, height // 100) | 1)
    )
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)

    # Keep the blob covering the middle of the frame (that is what was photographed).
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return mask
    center_slice = labels[
        int(height * 0.35) : int(height * 0.65), int(width * 0.35) : int(width * 0.65)
    ]
    center_labels = center_slice[center_slice > 0]
    if center_labels.size:
        values, counts = np.unique(center_labels, return_counts=True)
        chosen = int(values[np.argmax(counts)])
    else:
        chosen = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))

    blob = np.where(labels == chosen, 255, 0).astype(np.uint8)
    # Fill interior holes so dark print inside the page does not punch the mask.
    filled = blob.copy()
    flood = np.zeros((blob.shape[0] + 2, blob.shape[1] + 2), np.uint8)
    cv2.floodFill(filled, flood, (0, 0), 255)
    return blob | cv2.bitwise_not(filled)


def _quads_from_contour(contour: np.ndarray) -> list[np.ndarray]:
    quads: list[np.ndarray] = []
    perimeter = cv2.arcLength(contour, True)
    if perimeter <= 0:
        return quads
    for epsilon_ratio in (0.01, 0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quads.append(_order_corners(approx.astype(np.float32)))
    hull = cv2.convexHull(contour)
    hull_perimeter = cv2.arcLength(hull, True)
    for epsilon_ratio in (0.02, 0.04, 0.07):
        approx = cv2.approxPolyDP(hull, epsilon_ratio * hull_perimeter, True)
        if len(approx) == 4:
            quads.append(_order_corners(approx.astype(np.float32)))
    quads.append(_order_corners(cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32)))
    return quads


def _candidate_quads(bgr: np.ndarray, mask: np.ndarray) -> list[np.ndarray]:
    """Quad hypotheses from the foreground mask and from classic edge contours."""
    candidates: list[np.ndarray] = []

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:3]:
        candidates.extend(_quads_from_contour(contour))

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    smooth = cv2.bilateralFilter(cv2.GaussianBlur(gray, (5, 5), 0), 9, 60, 60)
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    median = float(np.median(smooth))
    for low, high in ((max(0, 0.66 * median), min(255, 1.33 * median)), (50, 150), (25, 90)):
        edges = cv2.Canny(smooth, int(low), int(high))
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, close_kernel)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:4]:
            candidates.extend(_quads_from_contour(contour))

    return candidates


def _mask_bounds_quad(mask: np.ndarray) -> np.ndarray | None:
    """Axis-aligned crop around the detected document, with a small safety margin."""
    height, width = mask.shape[:2]
    points = cv2.findNonZero(mask)
    if points is None:
        return None
    x, y, w, h = cv2.boundingRect(points)
    if w * h < config.SCAN_MIN_QUAD_AREA_RATIO * height * width:
        return None
    pad = int(max(height, width) * 0.005)
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(width, x + w + pad)
    y1 = min(height, y + h + pad)
    return np.array(
        [[x0, y0], [x1 - 1, y0], [x1 - 1, y1 - 1], [x0, y1 - 1]], dtype=np.float32
    )


def find_document_quad(bgr: np.ndarray) -> np.ndarray | None:
    """
    Find the page corners in a BGR image (top-left, top-right, bottom-right, bottom-left).

    Returns None when nothing is trustworthy — the caller then keeps the frame.
    """
    return _detect(bgr)[0]


def _detect(bgr: np.ndarray) -> tuple[np.ndarray | None, str, str]:
    """Detection core. Returns ``(quad_in_full_res, kind, detail)``."""
    _require_cv2()
    height, width = bgr.shape[:2]
    scale = config.SCAN_DETECT_LONG_EDGE_PX / float(max(height, width))
    if scale < 1.0:
        work = cv2.resize(
            bgr, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
        )
    else:
        scale = 1.0
        work = bgr

    lab = cv2.cvtColor(work, cv2.COLOR_BGR2LAB)
    lightness = lab[:, :, 0]
    blurred = cv2.GaussianBlur(lightness, (5, 5), 0)
    gradient = cv2.magnitude(
        cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3),
    )

    mask = _document_mask(work)
    best_quad: np.ndarray | None = None
    best_score = 0.0
    best_detail = ""

    for quad in _candidate_quads(work, mask):
        score, detail = _quad_evidence(quad, gradient, lightness)
        if score > best_score:
            best_score, best_quad, best_detail = score, quad, detail

    if best_quad is not None:
        return best_quad / scale, "perspective", best_detail

    # No supported quadrilateral: the document probably runs off the frame.
    # Cropping to the foreground's bounding box is the safe move.
    bounds = _mask_bounds_quad(mask)
    if bounds is not None:
        coverage = cv2.contourArea(bounds) / float(mask.shape[0] * mask.shape[1])
        if coverage <= config.SCAN_MAX_BOUNDS_COVERAGE:
            return bounds / scale, "bounds", f"bounding box (cover={coverage:.2f})"
        return None, "none", "document fills the frame; nothing to crop"

    return None, "none", "no document found"


def four_point_transform(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Warp the quad to a head-on rectangle, sized from its longest edges."""
    tl, tr, br, bl = quad
    width = int(round(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))))
    height = int(round(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))))
    width = max(width, 1)
    height = max(height, 1)

    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(quad.astype(np.float32), destination)
    return cv2.warpPerspective(
        bgr,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _crop_to_bounds(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Plain rectangular crop (used when the page runs off the frame)."""
    height, width = bgr.shape[:2]
    x0 = int(max(0, np.floor(quad[:, 0].min())))
    y0 = int(max(0, np.floor(quad[:, 1].min())))
    x1 = int(min(width, np.ceil(quad[:, 0].max())))
    y1 = int(min(height, np.ceil(quad[:, 1].max())))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return bgr
    return bgr[y0:y1, x0:x1]


# --------------------------------------------------------------------------
# Skew
# --------------------------------------------------------------------------


def estimate_skew_angle(bgr: np.ndarray) -> float:
    """
    Residual rotation of the text baselines, in degrees (positive = clockwise).

    Uses the median angle of near-horizontal Hough segments. Returns 0.0 when
    there is not enough evidence, so a bad estimate can never wreck a page.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    scale = 1000.0 / float(max(gray.shape))
    if scale < 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 50, 150)
    # Smear characters into word/line blobs so lines of text read as long segments.
    edges = cv2.dilate(
        edges, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)), iterations=1
    )

    min_length = max(40, int(gray.shape[1] * 0.25))
    segments = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 720,
        threshold=80,
        minLineLength=min_length,
        maxLineGap=12,
    )
    if segments is None:
        return 0.0

    angles: list[float] = []
    # OpenCV 4 returns (N, 1, 4); OpenCV 5 returns (N, 4).
    for x1, y1, x2, y2 in np.asarray(segments).reshape(-1, 4):
        angle = np.degrees(np.arctan2(float(y2 - y1), float(x2 - x1)))
        if angle > 90:
            angle -= 180
        elif angle < -90:
            angle += 180
        if abs(angle) <= config.SCAN_MAX_DESKEW_DEG:
            angles.append(angle)

    if len(angles) < 5:
        return 0.0
    return float(np.median(angles))


def rotate_image(bgr: np.ndarray, angle: float) -> np.ndarray:
    """Rotate about the centre, expanding the canvas so nothing is clipped."""
    if abs(angle) < config.SCAN_MIN_DESKEW_DEG:
        return bgr
    height, width = bgr.shape[:2]
    center = (width / 2.0, height / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_width = int(height * sin + width * cos)
    new_height = int(height * cos + width * sin)
    matrix[0, 2] += new_width / 2.0 - center[0]
    matrix[1, 2] += new_height / 2.0 - center[1]
    return cv2.warpAffine(
        bgr,
        matrix,
        (new_width, new_height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


# --------------------------------------------------------------------------
# "Scanned page" rendering
# --------------------------------------------------------------------------


def _shading(lightness: np.ndarray) -> np.ndarray:
    """Low-frequency illumination of the page (shadows, vignetting, uneven light)."""
    small_side = max(32, int(max(lightness.shape) * 0.06)) | 1
    # Closing on a coarse scale removes print but keeps the lighting envelope.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (small_side, small_side))
    envelope = cv2.morphologyEx(lightness, cv2.MORPH_CLOSE, kernel)
    return cv2.GaussianBlur(envelope, (0, 0), sigmaX=max(8.0, max(lightness.shape) * 0.02))


def _flatten_lightness(lightness: np.ndarray, strength: float) -> np.ndarray:
    """
    Even out lighting *additively*, so local contrast and colour survive.

    Dividing by the background (the usual trick) bleaches dense colour documents;
    subtracting the low-frequency shading only removes the lighting gradient.
    """
    if strength <= 0:
        return lightness
    shading = _shading(lightness).astype(np.float32)
    target = float(np.percentile(shading, 80))
    corrected = lightness.astype(np.float32) + strength * (target - shading)
    return np.clip(corrected, 0, 255).astype(np.uint8)


def _gentle_levels(gray: np.ndarray) -> np.ndarray:
    """Whiten paper and deepen ink, with a capped gain so nothing gets crushed."""
    low = float(np.percentile(gray, 2))
    high = float(np.percentile(gray, 98))
    if high - low < 20:
        return gray
    gain = min(255.0 / (high - low), config.SCAN_MAX_CONTRAST_GAIN)
    scaled = (gray.astype(np.float32) - low) * gain
    return np.clip(scaled, 0, 255).astype(np.uint8)


def _unsharp(image: np.ndarray, amount: float) -> np.ndarray:
    blurred = cv2.GaussianBlur(image, (0, 0), sigmaX=1.2)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


def _to_bw(gray: np.ndarray) -> np.ndarray:
    """Near-bitonal text rendering with local thresholding."""
    block = max(15, int(max(gray.shape) * 0.02) | 1)
    block = min(block, 61)
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block,
        config.SCAN_BW_THRESHOLD_C,
    )
    # Drop isolated speckles from paper grain / sensor noise.
    return cv2.medianBlur(binary, 3)


def image_statistics(bgr: np.ndarray) -> dict[str, float]:
    """Cheap descriptors used to decide what kind of page this is."""
    small = cv2.resize(bgr, (256, 256), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1].astype(np.float32)
    value = hsv[:, :, 2].astype(np.float32)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    lit = value > 60
    colour_ratio = float((saturation[lit] > 55).mean()) if lit.any() else 0.0
    return {
        "colour_ratio": colour_ratio,
        "bright_ratio": float((gray > 200).mean()),
        "mid_ratio": float(((gray > 90) & (gray < 205)).mean()),
    }


def resolve_mode(bgr: np.ndarray, mode: str) -> str:
    """
    Turn ``auto`` into the concrete mode this image will actually use.

    Biased towards keeping the image intact: black & white is only chosen for
    pages that really are plain ink on paper. ID cards, stamps, photos and
    anything with midtones stay in colour, because thresholding destroys them.
    """
    if mode != "auto":
        return mode
    stats = image_statistics(bgr)
    if stats["colour_ratio"] > config.SCAN_AUTO_COLOR_RATIO:
        return "color"
    if stats["mid_ratio"] > config.SCAN_AUTO_MIDTONE_RATIO:
        return "gray"  # photographic shading — thresholding would destroy it
    if stats["bright_ratio"] > config.SCAN_AUTO_PAPER_RATIO:
        return "bw"
    return "gray"


def enhance(bgr: np.ndarray, mode: str, sharpen: bool = True) -> np.ndarray:
    """
    Apply the scanned look. Returns BGR for colour, single-channel otherwise.
    """
    mode = resolve_mode(bgr, mode)

    if mode == "color":
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        lightness, a_chan, b_chan = cv2.split(lab)
        lightness = _flatten_lightness(lightness, config.SCAN_COLOR_FLATTEN_STRENGTH)
        lightness = _gentle_levels(lightness)
        result = cv2.cvtColor(cv2.merge((lightness, a_chan, b_chan)), cv2.COLOR_LAB2BGR)
        return _unsharp(result, config.SCAN_SHARPEN_AMOUNT) if sharpen else result

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if mode == "gray":
        flat = _flatten_lightness(gray, config.SCAN_COLOR_FLATTEN_STRENGTH)
        flat = _gentle_levels(flat)
        return _unsharp(flat, config.SCAN_SHARPEN_AMOUNT) if sharpen else flat

    # Black & white: flatten hard first, since thresholding wants even lighting.
    flat = _flatten_lightness(gray, config.SCAN_BW_FLATTEN_STRENGTH)
    return _to_bw(flat)


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------


def scan_array_detailed(
    bgr: np.ndarray, options: ScanOptions | None = None
) -> tuple[np.ndarray, np.ndarray, ScanReport]:
    """
    Run the pipeline and return ``(geometry_only, enhanced, report)``.

    ``geometry_only`` is the page after crop/deskew with the original pixels
    untouched — that is the image to look at when judging the crop itself,
    separately from the clean-up.
    """
    _require_cv2()
    opts = options or ScanOptions()
    report = ScanReport()

    geometry = bgr
    if opts.auto_crop:
        quad, kind, detail = _detect(bgr)
        report.crop_kind = kind
        report.detail = detail
        if quad is not None:
            report.quad = quad
            report.cropped = True
            geometry = (
                four_point_transform(bgr, quad)
                if kind == "perspective"
                else _crop_to_bounds(bgr, quad)
            )

    if opts.deskew:
        # After a successful warp the page is already square-on, so only a small
        # residual (text vs. page edges) is worth correcting.
        angle = estimate_skew_angle(geometry)
        if report.crop_kind == "perspective":
            angle = angle if abs(angle) <= 3.0 else 0.0
        geometry = rotate_image(geometry, angle)
        report.skew_deg = angle

    report.mode = resolve_mode(geometry, opts.mode)
    enhanced = enhance(geometry, report.mode, sharpen=opts.sharpen)
    return geometry, enhanced, report


def scan_array(bgr: np.ndarray, options: ScanOptions | None = None) -> np.ndarray:
    """Run the full pipeline on a BGR array. Returns BGR or single-channel."""
    return scan_array_detailed(bgr, options)[1]


def draw_detected_boundary(bgr: np.ndarray, quad: np.ndarray | None, note: str = "") -> np.ndarray:
    """Copy of the photo with the detected page outline and corners drawn on it."""
    _require_cv2()
    overlay = bgr.copy()
    thickness = max(2, int(max(bgr.shape[:2]) / 400))
    if quad is not None:
        cv2.polylines(overlay, [quad.astype(np.int32)], True, (0, 0, 255), thickness * 2)
        for index, (x, y) in enumerate(quad.astype(int)):
            cv2.circle(overlay, (int(x), int(y)), thickness * 4, (0, 255, 255), -1)
            cv2.putText(
                overlay,
                str(index + 1),
                (int(x) + thickness * 5, int(y)),
                cv2.FONT_HERSHEY_SIMPLEX,
                thickness * 0.6,
                (0, 255, 255),
                thickness,
            )
    if note:
        cv2.putText(
            overlay,
            note,
            (thickness * 5, thickness * 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            thickness * 0.55,
            (0, 0, 255) if quad is None else (0, 160, 0),
            thickness,
        )
    return overlay


def _pil_to_bgr(image: Image.Image) -> np.ndarray:
    rgb = image.convert("RGB")
    return cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)


def _array_to_pil(array: np.ndarray) -> Image.Image:
    if array.ndim == 2:
        return Image.fromarray(array, mode="L")
    return Image.fromarray(cv2.cvtColor(array, cv2.COLOR_BGR2RGB))


def scan_image(image: Image.Image, options: ScanOptions | None = None) -> Image.Image:
    """Scan a PIL image. Grayscale/BW results come back as mode ``L``."""
    _require_cv2()
    result = scan_array(_pil_to_bgr(image), options)
    scanned = _array_to_pil(result)
    if getattr(image, "info", None) and "dpi" in image.info:
        scanned.info["dpi"] = image.info["dpi"]
    return scanned


def load_upright(path: Path | str) -> Image.Image:
    """Open an image with the phone's EXIF rotation already applied."""
    from PIL import ImageOps

    with Image.open(path) as img:
        return ImageOps.exif_transpose(img)


def scan_file(path: Path | str, options: ScanOptions | None = None) -> Image.Image:
    """Scan an image file, honouring EXIF orientation from phone cameras."""
    _require_cv2()
    return scan_image(load_upright(path), options)


def scan_file_detailed(
    path: Path | str, options: ScanOptions | None = None
) -> tuple[Image.Image, Image.Image, Image.Image, ScanReport]:
    """
    Scan a file and return everything needed to judge the result:
    ``(outline_overlay, geometry_only, enhanced, report)``.
    """
    _require_cv2()
    bgr = _pil_to_bgr(load_upright(path))
    geometry, enhanced, report = scan_array_detailed(bgr, options)
    note = f"{report.crop_kind}: {report.detail}"
    outline = draw_detected_boundary(bgr, report.quad, note)
    return (
        _array_to_pil(outline),
        _array_to_pil(geometry),
        _array_to_pil(enhanced),
        report,
    )


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Auto-crop and clean up a document photo.")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--mode", choices=SCAN_MODES, default=config.DEFAULT_SCAN_MODE)
    parser.add_argument("--no-crop", action="store_true", help="Skip boundary detection")
    parser.add_argument("--no-deskew", action="store_true")
    parser.add_argument("--no-sharpen", action="store_true")
    parser.add_argument(
        "--debug", metavar="PATH", help="Write a copy with the detected quad drawn on it"
    )
    args = parser.parse_args()

    _require_cv2()
    options = ScanOptions(
        mode=args.mode,
        auto_crop=not args.no_crop,
        deskew=not args.no_deskew,
        sharpen=not args.no_sharpen,
    )

    outline, _geometry, enhanced, report = scan_file_detailed(args.input, options)
    enhanced.save(args.output)
    print(
        f"Wrote {args.output} (mode={report.mode}, crop={report.crop_kind}, "
        f"skew={report.skew_deg:+.2f}deg) — {report.detail}"
    )
    if args.debug:
        outline.save(args.debug)
        print(f"Detection overlay written to {args.debug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
