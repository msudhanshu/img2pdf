"""Default settings for the image-to-PDF converter."""

# Chunking
DEFAULT_MAX_IMAGES_PER_PDF = 20

# Size limit (bytes derived from MB in the UI)
DEFAULT_MAX_PDF_SIZE_MB = 10.0

# Image formats accepted by the UI and folder scan
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}

# JPEG quality compressor (dimensions are never changed)
INITIAL_JPEG_QUALITY = 85
MIN_JPEG_QUALITY = 40
QUALITY_STEP = 10

# Document scanner (auto-crop / deskew / scanned look)
DEFAULT_SCAN_ENABLED = False
DEFAULT_SCAN_MODE = "auto"  # auto | color | gray | bw
# Boundary detection runs on a copy scaled to this long edge (speed + stability)
SCAN_DETECT_LONG_EDGE_PX = 1000
# A detected page must cover at least this fraction of the frame to be trusted
SCAN_MIN_QUAD_AREA_RATIO = 0.20
# How far outside the photo a corner may sit before the quad is nonsense
SCAN_CORNER_MARGIN_RATIO = 0.03
# Foreground = pixels this many robust deviations away from the border colour
SCAN_BACKGROUND_SIGMA = 3.0
# Evidence a quad must show: fraction of each side lying on a real edge,
# and the lightness step across the border
SCAN_MIN_EDGE_SUPPORT = 0.45
SCAN_MIN_EDGE_CONTRAST = 6.0
# Above this coverage the document fills the frame, so cropping is pointless
SCAN_MAX_BOUNDS_COVERAGE = 0.97
# Rotation limits for the deskew step
SCAN_MAX_DESKEW_DEG = 15.0
SCAN_MIN_DESKEW_DEG = 0.15
# Clean-up strength. Flattening is additive, so these stay conservative:
# colour documents must keep their colour and midtones.
SCAN_COLOR_FLATTEN_STRENGTH = 0.7
SCAN_BW_FLATTEN_STRENGTH = 1.0
SCAN_MAX_CONTRAST_GAIN = 1.6
SCAN_SHARPEN_AMOUNT = 0.35
# Bias of the adaptive threshold in "bw" mode (higher = cleaner but thinner text)
SCAN_BW_THRESHOLD_C = 15
# "auto" mode: colour wins easily; bw only for plain ink-on-paper pages
SCAN_AUTO_COLOR_RATIO = 0.02
SCAN_AUTO_MIDTONE_RATIO = 0.35
SCAN_AUTO_PAPER_RATIO = 0.45
# Quality of the intermediate scanned page handed to the PDF size loop
SCAN_INTERMEDIATE_JPEG_QUALITY = 95
# Folder (inside the source folder) for the "Crop preview" button's output
SCAN_PREVIEW_DIR_NAME = "temp_crop"

# Output naming (written into the source folder)
OUTPUT_NAME_PATTERN = "images_part_{part:02d}.pdf"

# UI
APP_TITLE = "Image to PDF"
WINDOW_SIZE = "720x560"
