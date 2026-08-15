# Image to PDF (Windows 10)

Desktop app that combines selected photos into one or more PDFs:

- Choose a **single file**, **multiple files**, or a **whole folder**
- Folder mode picks image extensions (jpg, jpeg, png, webp, bmp, tif, tiff) **and PDFs**
- **Existing PDFs can be included**: every page of a selected PDF becomes a page of the output (see below)
- Files are **sorted by date created**, then grouped into PDFs of at most **20** pages
- Extra pages spill into the next PDF (`images_part_01.pdf`, `images_part_02.pdf`, …)
- PDFs are written into a **`rocket_pdf_output` folder created inside the source folder**
- Each PDF is kept under a size limit (default **10 MB**) by **compressing** JPEG and PNG (dimensions stay the same). If it still does not fit, fewer pages go into that PDF.
- Optional **Scan documents** mode turns phone photos of paper into scan-like pages: auto-crop to the document, perspective + skew correction, shadow removal

---

## Including existing PDFs

PDFs can sit in the selection next to photos — pick them with **Add File(s)**, or
just choose a folder and every PDF in it is selected along with the images.

Each page of a selected PDF is **rendered to an image** (150 dpi, via PDFium)
before packing. That is what keeps one promise intact: the size limit still
applies to the final PDF, because rendered pages go through the same compression
ladder as the photos, and are moved to the next part file when a part will not
fit. Text-ish pages are kept lossless (PNG); pages that are really photographs go
out as high-quality JPEG.

Two consequences worth knowing:

- Text in an included PDF becomes **image text** — not selectable, not searchable.
- **Scan documents** never runs on PDF pages (they are already flat scans, so
  boundary detection could only damage them). Photos in the same batch are still
  scanned normally.

A PDF that cannot be opened (corrupt or password-protected) is skipped with a
warning; the rest of the batch still converts.

**Requirement:** this needs `pypdfium2`. It is in `requirements.txt`; if it is
missing, PDFs are refused at selection time and image conversion still works.

```bash
pip install pypdfium2
```

---

## Scan documents (auto-crop, deskew, clean up)

Tick **Scan documents** before converting and every photo goes through a
document-scanner pipeline first (OpenCV — no cloud, no model downloads):

1. **Background model** — the photo's border ring is sampled to learn what the
   surroundings look like (desk, floor, table). The document is the region in
   the middle that does *not* look like them. This is far steadier than hunting
   for contours, which invents boundaries on close-up shots.
2. **Candidate quadrilaterals** — from the foreground mask and from classic edge
   contours, at several approximation tolerances.
3. **Evidence check** — each candidate must earn the crop: a measured fraction of
   every side has to lie on a real image edge, and there must be a lightness step
   between the inside and the outside. Sides that run along the photo's own
   border are treated as neutral, since a page shot close-up has no visible edge
   there.
4. **Crop** — a supported quadrilateral is perspective-warped flat. When the
   document runs off the frame there is no closed boundary, so it falls back to
   the foreground's bounding box. When nothing is trustworthy, **the frame is
   kept as is** — a photo is never wrecked by a bad detection.
5. **Deskew** — residual rotation is measured from the text baselines (Hough
   segments) and corrected, up to ±15°.
6. **Clean-up** — uneven lighting is removed *additively* from the lightness
   channel only, so colour documents (ID cards, stamps, photos) keep their colour
   and midtones instead of being bleached. Levels are then stretched with a
   capped gain.

Roughly 0.6 s for a 2 MP photo, 1.8 s for a 7 MP one.

**AI detection (default on)**

The boundary is located by a CNN first — U²-Net (4.6 MB, Apache-2.0), run through
**OpenCV's own DNN module**, so there is no extra Python dependency (`onnxruntime`
has no wheels for recent Python versions). Inference is ~0.2 s per photo on CPU
and entirely local: nothing is uploaded.

The model's mask is coarse, so it is never used as the crop directly — it feeds
the same candidate/evidence machinery described above. The classical detector
takes over whenever the model is missing, fails to download, or produces
something unconvincing, and the crop-preview outline labels which one ran
(`[ai]` or `[classic]`).

The weights download once, on first use, into `~/.img2pdf/models/` (override with
`IMG2PDF_MODEL_DIR`), verified against a pinned SHA-256. To pre-install them —
or to bundle them into the `.exe` so it works offline — drop the file at
`models/u2netp.onnx`:

```bash
mkdir -p models && curl -L -o models/u2netp.onnx \
  https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx
```

Untick **AI detection** in the UI to force the classical detector.

**Modes**

| Mode | Result |
|------|--------|
| `auto` | Picks per image; colour wins easily, `bw` only for plain ink-on-paper pages (default) |
| `color` | Colour kept, lighting flattened, mild sharpening |
| `gray` | Grayscale scan (smaller files) |
| `bw` | Near-bitonal black & white — smallest, best for plain text. Do not use for ID cards or photos |

**Requirement:** scanning needs OpenCV. It is in `requirements.txt`; if it is
missing the checkbox is disabled and normal conversion still works.

```bash
pip install opencv-python-headless numpy
```

**Command line (single image, useful for tuning):**

```bash
python -m app.scanner photo.jpg scanned.jpg --mode auto
python -m app.scanner photo.jpg scanned.jpg --debug boundary.jpg   # see the detected outline
```

## Crop preview

**Crop preview** (next to Convert) scans the selected photos and writes the
results to a `temp_crop` folder inside the source folder — no PDF, nothing
overwritten — so the crop can be checked by eye before converting.

Three files per photo:

| File | Shows |
|------|-------|
| `*_1_outline.jpg` | The original with the detected outline, corners, and the reason drawn on it |
| `*_2_crop.jpg` | Cropped and deskewed, original pixels otherwise — judge the **crop** here |
| `*_3_scan.jpg` | The final cleaned-up page as it would enter the PDF — judge the **quality** here |

The outline image also labels which path ran: `perspective` (a real boundary was
found and flattened), `bounds` (page ran off the frame, so a straight crop), or
`none` (nothing trustworthy, frame kept).

------|--------|
| `auto` | Colour pages stay colour, plain text pages become black & white (default) |
| `color` | Colour kept, lighting flattened and sharpened |
| `gray` | Grayscale scan (smaller files) |
| `bw` | Near-bitonal black & white — smallest, best for plain text |

**Requirement:** scanning needs OpenCV. It is in `requirements.txt`; if it is
missing the checkbox is disabled and normal conversion still works.

```bash
pip install opencv-python-headless numpy
```

**Command line (single image, useful for tuning):**

```bash
python -m app.scanner photo.jpg scanned.jpg --mode auto
python -m app.scanner photo.jpg scanned.jpg --debug boundary.jpg   # see the detected page outline
```

---

## End users (recommended): single-click `.exe`

No Python and no terminal.

1. Get `Img2PDF.exe` (build it once on a Windows PC, or download the GitHub Actions artifact — see below).
2. Copy `Img2PDF.exe` anywhere (Desktop, USB drive, etc.).
3. **Double-click** `Img2PDF.exe` to open the app.

That is the whole “install”: one file, double-click to run. Windows may show a SmartScreen prompt the first time (“Windows protected your PC”) because the file is not code-signed — choose **More info → Run anyway**.

First launch of a one-file `.exe` can take a few seconds while Windows unpacks it.

### Build the `.exe` on a Windows PC (one-time)

1. Install [Python 3.11+](https://www.python.org/downloads/) and check **Add python.exe to PATH**.
2. Copy this project folder onto the Windows machine.
3. **Double-click** [`build_windows.bat`](build_windows.bat).
4. When it finishes, Explorer opens `dist\`. Use:

   `dist\Img2PDF.exe`

Share that single file with other Windows 10 PCs.

### Or build via GitHub Actions (no local Windows needed)

If this repo is on GitHub:

1. Push a tag like `v1.0.0`, or run the **Build Windows EXE** workflow manually (**Actions → Build Windows EXE → Run workflow**).
2. Download the **Img2PDF-windows** artifact — it contains `Img2PDF.exe`.

---

## Developers: run from source

### macOS (Homebrew Python)

Homebrew Python does not include Tk by default. Install it once:

```bash
brew install python-tk@3.14
```

Then recreate the venv and run:

```bash
cd img2pdf
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app
```

### Windows / general

```bat
cd img2pdf
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app
```

Or: `python run_app.py`

## Usage

1. Click **Add File**, **Add Files**, or **Add Folder**.
2. Folder selection loads the images **and PDFs** from that folder (non-recursive) and replaces the list.
3. The list is shown sorted by date created (oldest first) — that same order is used for PDF grouping.
4. Set **Max pages / PDF** and **Max PDF size (MB)** if needed. A selected PDF counts as one page per page it contains.
5. Tick **Scan documents** (and pick a mode) to auto-crop/deskew photos of paper.
6. Optionally click **Crop preview** first to check the crop in a `temp_crop` folder.
7. Click **Convert**. Output PDFs are saved in a `rocket_pdf_output` folder inside the source folder, which opens when the run finishes.

If even one page at maximum compression is still over the size limit, that PDF is written anyway and a warning is shown.

## Project layout

```
app/
  main.py              # CustomTkinter UI
  converter.py         # Created-date sort, compress-then-split packing, PDF writer
  compression.py       # ImageCompressor interface + JPEG/PNG (MozJPEG, oxipng)
  pdf_pages.py         # Renders pages of a selected PDF to images (PDFium)
  scanner.py           # Document scanner: background model, evidence-scored crop, deskew, clean up
  ai_detector.py       # Optional U^2-Net segmentation via cv2.dnn (downloads weights once)
  config.py            # Defaults
run_app.py             # App entry (used by the .exe)
build_windows.bat      # Double-click on Windows to build Img2PDF.exe
Img2PDF.spec           # PyInstaller config (windowed, one-file)
requirements.txt
requirements-build.txt
```

## Defaults

| Setting | Default |
|---------|---------|
| Max pages / PDF | 20 |
| Max PDF size | 10 MB |
| Folder scan | Non-recursive, image extensions + `.pdf` |
| Grouping order | Date created (oldest first) |
| Output location | `rocket_pdf_output/` inside the source folder |
| PDF page rendering | 150 dpi |
| Size strategy | Compress JPEG/PNG first (no resize); if still over limit, fewer pages per PDF |

## JPEG and PNG compression

Both formats are compressed **on your machine** (no uploads). Pixel size is not changed.

| Source | What happens |
|--------|----------------|
| **JPEG** | Re-encoded (quality 85 → 45 if needed), EXIF stripped, then [MozJPEG](https://pypi.org/project/mozjpeg-lossless-optimization/) lossless optimize |
| **PNG** | Lossless [oxipng](https://pypi.org/project/pyoxipng/) first (when installed). If the PDF is still too large, converted to JPEG at decreasing quality |
| **BMP / TIFF / WebP** | Same JPEG quality ladder |

**Rough size drop** (typical photos, vs the original file):

| Setting | JPEG (already compressed camera file) | PNG photo | PNG screenshot / scan |
|---------|----------------------------------------|-----------|------------------------|
| Lossless / quality 85 | often **10–30%** (EXIF + MozJPEG); sometimes little extra if the JPEG is already tight | **70–90%** after JPEG conversion | oxipng **15–35%** lossless; JPEG conversion can be more but may soften text |
| Quality 75 | about **20–40%** | **80–90%+** | similar |
| Quality 45 (strongest) | about **40–70%**, with visible softness | **85–95%** | similar |

These are typical ranges, not guarantees. A JPEG that is already small (WhatsApp, social apps) may barely shrink until quality is dropped a lot. If the PDF is still over the limit after quality 45, the app puts fewer images in that PDF.
| Scan documents | Off |
| Scan mode | `auto` (colour unless the page is plain ink on paper) |
| AI detection | On (falls back to classical when unsure or offline) |
| Crop preview output | `temp_crop/` inside the source folder |
| Max deskew angle | 15 deg |