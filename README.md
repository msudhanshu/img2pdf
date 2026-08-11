# Image to PDF (Windows 10)

Desktop app that combines selected photos into one or more PDFs:

- Choose a **single file**, **multiple files**, or a **whole folder**
- Folder mode picks only image extensions (jpg, jpeg, png, webp, bmp, tif, tiff)
- Images are **sorted by date created**, then grouped into PDFs of at most **20** pages
- Extra images spill into the next PDF (`images_part_01.pdf`, `images_part_02.pdf`, …)
- PDFs are written into the **source folder** itself
- Each PDF is kept under a size limit (default **10 MB**) by resizing/recompressing in a loop

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

```bat
cd img2pdf
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m app.main
```

Or: `python run_app.py`

## Usage

1. Click **Add File**, **Add Files**, or **Add Folder**.
2. Folder selection loads only image files from that folder (non-recursive) and replaces the list.
3. The list is shown sorted by date created (oldest first) — that same order is used for PDF grouping.
4. Set **Max images / PDF** and **Max PDF size (MB)** if needed.
5. Click **Convert**. Output PDFs are saved next to the source images.

If a PDF cannot get under the size limit even at the minimum scale/quality floors, the app still writes the smallest result it can and shows a warning.

## Project layout

```
app/
  main.py              # CustomTkinter UI
  converter.py         # Created-date sort, chunking, resize loop, PDF writer
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
| Max images / PDF | 20 |
| Max PDF size | 10 MB |
| Folder scan | Non-recursive, image extensions only |
| Grouping order | Date created (oldest first) |
| Output location | Source folder of selected images |
| Resize strategy | Scale down + JPEG quality drop until under limit |
