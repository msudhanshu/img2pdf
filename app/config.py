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

# Resize / quality loop
INITIAL_JPEG_QUALITY = 85
MIN_JPEG_QUALITY = 40
QUALITY_STEP = 10
INITIAL_SCALE = 1.0
SCALE_FACTOR = 0.85
MIN_LONG_EDGE_PX = 800

# Output naming (written into the source folder)
OUTPUT_NAME_PATTERN = "images_part_{part:02d}.pdf"

# UI
APP_TITLE = "Image to PDF"
WINDOW_SIZE = "720x560"