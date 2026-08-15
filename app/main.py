"""CustomTkinter UI for browsing images and converting them to PDF."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app import config
from app.converter import (
    CropPreviewResult,
    collect_sources_from_folder,
    convert_images_to_pdfs,
    export_crop_previews,
    is_image_path,
    is_pdf_path,
    is_supported_path,
    resolve_output_dir,
    sort_by_created,
)
from app.crop_tab import ScannerTab
from app.pdf_pages import is_available as pdf_reader_available
from app.scanner import SCAN_MODES, ScanOptions, is_available as scanner_available
from app.ai_detector import is_model_available as ai_model_available


_IMAGE_FILETYPES = [
    (
        "Images and PDFs",
        "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.pdf "
        "*.JPG *.JPEG *.PNG *.WEBP *.BMP *.TIF *.TIFF *.PDF",
    ),
    ("All files", "*.*"),
]


class Img2PdfApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(config.APP_TITLE)
        self.geometry(config.WINDOW_SIZE)
        self.minsize(640, 480)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self._source_paths: list[Path] = []
        self._converting = False

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        """Top-level tabs. Each tab owns one feature; new ones just call ``add``."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._tabs = ctk.CTkTabview(self)
        self._tabs.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

        convert_tab = self._tabs.add("Convert to PDF")
        scanner_tab = self._tabs.add("Scanner")

        self._build_convert_tab(convert_tab)

        scanner_tab.grid_columnconfigure(0, weight=1)
        scanner_tab.grid_rowconfigure(0, weight=1)
        ScannerTab(scanner_tab).grid(row=0, column=0, sticky="nsew")

        self._tabs.set("Convert to PDF")

    def _build_convert_tab(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        toolbar.grid_columnconfigure(5, weight=1)

        ctk.CTkButton(toolbar, text="Add File", command=self._add_file).grid(
            row=0, column=0, padx=(0, 8), pady=8
        )
        ctk.CTkButton(toolbar, text="Add Files", command=self._add_files).grid(
            row=0, column=1, padx=(0, 8), pady=8
        )
        ctk.CTkButton(toolbar, text="Add Folder", command=self._add_folder).grid(
            row=0, column=2, padx=(0, 8), pady=8
        )
        ctk.CTkButton(toolbar, text="Remove Selected", command=self._remove_selected).grid(
            row=0, column=3, padx=(0, 8), pady=8
        )
        ctk.CTkButton(toolbar, text="Clear", command=self._clear_list).grid(
            row=0, column=4, padx=(0, 8), pady=8
        )

        list_frame = ctk.CTkFrame(parent)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=6)
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(1, weight=1)

        self._count_label = ctk.CTkLabel(
            list_frame, text="0 file(s) selected (sorted by date created)"
        )
        self._count_label.grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))

        self._listbox = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            activestyle="dotbox",
            highlightthickness=0,
            borderwidth=0,
        )
        self._listbox.grid(row=1, column=0, sticky="nsew", padx=(8, 0), pady=(0, 8))

        scrollbar = ctk.CTkScrollbar(list_frame, command=self._listbox.yview)
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(0, 8))
        self._listbox.configure(yscrollcommand=scrollbar.set)

        settings = ctk.CTkFrame(parent)
        settings.grid(row=2, column=0, sticky="ew", padx=12, pady=6)
        settings.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(settings, text="Max pages / PDF").grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 4)
        )
        self._max_images_entry = ctk.CTkEntry(settings, width=80)
        self._max_images_entry.insert(0, str(config.DEFAULT_MAX_IMAGES_PER_PDF))
        self._max_images_entry.grid(row=0, column=1, sticky="w", padx=8, pady=(8, 4))

        ctk.CTkLabel(settings, text="Max PDF size (MB)").grid(
            row=1, column=0, sticky="w", padx=8, pady=4
        )
        self._max_size_entry = ctk.CTkEntry(settings, width=80)
        self._max_size_entry.insert(0, str(config.DEFAULT_MAX_PDF_SIZE_MB))
        self._max_size_entry.grid(row=1, column=1, sticky="w", padx=8, pady=4)

        scan_row = ctk.CTkFrame(settings, fg_color="transparent")
        scan_row.grid(row=2, column=0, columnspan=3, sticky="ew", padx=4, pady=4)

        self._scan_var = ctk.BooleanVar(value=config.DEFAULT_SCAN_ENABLED)
        self._scan_check = ctk.CTkCheckBox(
            scan_row,
            text="Scan documents (auto-crop, deskew, clean up)",
            variable=self._scan_var,
            command=self._on_scan_toggled,
        )
        self._scan_check.grid(row=0, column=0, sticky="w", padx=4)

        self._scan_mode_menu = ctk.CTkOptionMenu(
            scan_row, values=list(SCAN_MODES), width=110
        )
        self._scan_mode_menu.set(config.DEFAULT_SCAN_MODE)
        self._scan_mode_menu.grid(row=0, column=1, sticky="w", padx=8)

        self._ai_var = ctk.BooleanVar(value=config.DEFAULT_SCAN_USE_AI)
        self._ai_check = ctk.CTkCheckBox(
            scan_row,
            text="AI detection",
            variable=self._ai_var,
            command=self._on_scan_toggled,
        )
        self._ai_check.grid(row=0, column=2, sticky="w", padx=8)

        self._scan_hint = ctk.CTkLabel(scan_row, text="", anchor="w", justify="left")
        self._scan_hint.grid(row=1, column=0, columnspan=3, sticky="w", padx=4, pady=(2, 0))

        if not scanner_available():
            self._scan_var.set(False)
            self._scan_check.configure(state="disabled")
            self._ai_check.configure(state="disabled")
            self._scan_hint.configure(
                text="Scanning needs OpenCV — install with: "
                "pip install opencv-python-headless numpy"
            )
        self._on_scan_toggled()

        ctk.CTkLabel(settings, text="Output folder").grid(
            row=3, column=0, sticky="w", padx=8, pady=(4, 8)
        )
        self._output_label = ctk.CTkLabel(
            settings,
            text=f"(a '{config.OUTPUT_DIR_NAME}' folder inside the source folder)",
            anchor="w",
            justify="left",
        )
        self._output_label.grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=(4, 8)
        )

        bottom = ctk.CTkFrame(parent)
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 12))
        bottom.grid_columnconfigure(0, weight=1)

        self._status_label = ctk.CTkLabel(
            bottom, text="Ready. Choose images or PDFs, or a whole folder.", anchor="w"
        )
        self._status_label.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        buttons = ctk.CTkFrame(bottom, fg_color="transparent")
        buttons.grid(row=1, column=0, sticky="e", padx=8, pady=(4, 8))

        self._crop_btn = ctk.CTkButton(
            buttons,
            text="Crop preview",
            command=self._start_crop_preview,
            fg_color="transparent",
            border_width=1,
        )
        self._crop_btn.grid(row=0, column=0, padx=(0, 8))
        if not scanner_available():
            self._crop_btn.configure(state="disabled")

        self._convert_btn = ctk.CTkButton(
            buttons, text="Convert", command=self._start_convert
        )
        self._convert_btn.grid(row=0, column=1)

    def _on_scan_toggled(self) -> None:
        enabled = bool(self._scan_var.get())
        self._scan_mode_menu.configure(state="normal" if enabled else "disabled")
        self._ai_check.configure(state="normal" if enabled else "disabled")
        if not scanner_available():
            return
        if not enabled:
            self._scan_hint.configure(
                text="Off: photos are packed into the PDF exactly as shot."
            )
            return
        if not self._ai_var.get():
            detector = "Classical detection (no model needed)."
        elif ai_model_available():
            detector = "AI detection, falling back to the classical one when unsure."
        else:
            detector = "AI model downloads once (4.6 MB) on first use."
        self._scan_hint.configure(
            text=f"auto = colour unless the page is plain ink on paper. {detector}"
        )

    def _scan_options(self) -> ScanOptions:
        return ScanOptions(
            enabled=bool(self._scan_var.get()),
            mode=self._scan_mode_menu.get(),
            use_ai=bool(self._ai_var.get()),
        )

    def _set_status(self, message: str) -> None:
        self._status_label.configure(text=message)

    def _update_output_label(self) -> None:
        if not self._source_paths:
            self._output_label.configure(
                text=f"(a '{config.OUTPUT_DIR_NAME}' folder inside the source folder)"
            )
            return
        self._output_label.configure(text=str(resolve_output_dir(self._source_paths)))

    def _refresh_list(self) -> None:
        self._source_paths = sort_by_created(self._source_paths)
        self._listbox.delete(0, tk.END)
        for path in self._source_paths:
            self._listbox.insert(tk.END, str(path))
        pdf_count = sum(1 for path in self._source_paths if is_pdf_path(path))
        suffix = f", {pdf_count} PDF(s)" if pdf_count else ""
        self._count_label.configure(
            text=(
                f"{len(self._source_paths)} file(s) selected{suffix} "
                "(sorted by date created)"
            )
        )
        self._update_output_label()

    def _add_paths(self, paths: list[Path], *, replace: bool = False) -> None:
        if replace:
            self._source_paths = []

        existing = set(self._source_paths)
        added = 0
        skipped_pdf = 0
        for path in paths:
            resolved = path.expanduser().resolve()
            if resolved in existing or not is_supported_path(resolved):
                continue
            if is_pdf_path(resolved) and not pdf_reader_available():
                skipped_pdf += 1
                continue
            self._source_paths.append(resolved)
            existing.add(resolved)
            added += 1
        self._refresh_list()

        if skipped_pdf:
            messagebox.showwarning(
                "PDFs skipped",
                f"{skipped_pdf} PDF file(s) were skipped because pypdfium2 is not "
                "installed. Install it with:\n\n    pip install pypdfium2",
            )
        if added:
            self._set_status(
                f"Added {added} file(s). PDF will be saved in "
                f"the '{config.OUTPUT_DIR_NAME}' folder."
            )
        else:
            self._set_status("No new files added.")

    def _add_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select an image or PDF file",
            filetypes=_IMAGE_FILETYPES,
        )
        if selected:
            self._add_paths([Path(selected)])

    def _add_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Select image or PDF files",
            filetypes=_IMAGE_FILETYPES,
        )
        if selected:
            self._add_paths([Path(p) for p in selected])

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select folder with images / PDFs")
        if not folder:
            return
        folder_path = Path(folder)
        sources = collect_sources_from_folder(folder_path)
        if not sources:
            messagebox.showinfo(
                "Nothing found",
                "No image or PDF files found in that folder "
                f"(extensions: {', '.join(sorted(config.SUPPORTED_EXTENSIONS))}).",
            )
            return
        # Whole-folder selection replaces the list and uses that folder as source.
        self._add_paths(sources, replace=True)
        self._set_status(
            f"Loaded {len(self._source_paths)} file(s) from folder. "
            f"PDF will be saved in: {folder_path.resolve() / config.OUTPUT_DIR_NAME}"
        )

    def _remove_selected(self) -> None:
        selection = list(self._listbox.curselection())
        if not selection:
            return
        for index in reversed(selection):
            del self._source_paths[index]
        self._refresh_list()
        self._set_status("Removed selected file(s).")

    def _clear_list(self) -> None:
        self._source_paths.clear()
        self._refresh_list()
        self._set_status("Cleared selection.")

    def _parse_settings(self) -> tuple[int, float]:
        try:
            max_images = int(self._max_images_entry.get().strip())
        except ValueError as exc:
            raise ValueError("Max pages / PDF must be a whole number.") from exc
        if max_images < 1:
            raise ValueError("Max pages / PDF must be at least 1.")

        try:
            max_size_mb = float(self._max_size_entry.get().strip())
        except ValueError as exc:
            raise ValueError("Max PDF size (MB) must be a number.") from exc
        if max_size_mb <= 0:
            raise ValueError("Max PDF size (MB) must be greater than 0.")

        return max_images, max_size_mb

    def _set_busy(self, busy: bool) -> None:
        self._converting = busy
        state = "disabled" if busy else "normal"
        self._convert_btn.configure(state=state)
        if scanner_available():
            self._crop_btn.configure(state=state)

    def _start_crop_preview(self) -> None:
        """Scan the selected images into a temp_crop folder, without making a PDF."""
        if self._converting:
            return
        # The preview is about boundary detection on photos, so PDF pages
        # (already flat) are left out of it.
        images = [path for path in self._source_paths if is_image_path(path)]
        if not images:
            messagebox.showwarning(
                "No images",
                "Choose at least one image file first "
                "(the crop preview does not apply to PDF pages).",
            )
            return

        self._set_busy(True)
        self._set_status("Starting crop preview...")
        thread = threading.Thread(
            target=self._crop_worker,
            args=(images, self._scan_options()),
            daemon=True,
        )
        thread.start()

    def _crop_worker(self, image_paths: list[Path], scan_options: ScanOptions) -> None:
        try:
            result = export_crop_previews(
                image_paths=image_paths,
                scan_options=scan_options,
                progress=lambda msg: self.after(0, self._set_status, msg),
            )
        except Exception as exc:  # noqa: BLE001 - show any failure in the UI
            self.after(0, self._on_crop_failed, str(exc))
            return
        self.after(0, self._on_crop_finished, result)

    def _on_crop_failed(self, error: str) -> None:
        self._set_busy(False)
        self._set_status("Crop preview failed.")
        messagebox.showerror("Crop preview failed", error)

    def _on_crop_finished(self, result: CropPreviewResult) -> None:
        self._set_busy(False)
        summary = (
            f"{result.cropped_count} of {result.image_count} image(s) auto-cropped."
        )
        self._set_status(f"Crop preview done. {summary}")

        detail = (
            f"{summary}\n\nWritten to:\n{result.output_dir}\n\n"
            "Per photo:\n"
            "  *_1_outline.jpg  detected page outline on the original\n"
            "  *_2_crop.jpg     cropped + deskewed, no clean-up\n"
            "  *_3_scan.jpg     final scanned look"
        )
        if result.warnings:
            detail += "\n\nWarnings:\n" + "\n".join(result.warnings[:10])
        messagebox.showinfo("Crop preview complete", detail)
        if result.output_dir:
            self._open_folder(result.output_dir)

    def _start_convert(self) -> None:
        if self._converting:
            return
        if not self._source_paths:
            messagebox.showwarning(
                "Nothing selected",
                "Choose a file, multiple files, or a folder first.",
            )
            return

        try:
            max_images, max_size_mb = self._parse_settings()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        output_dir = resolve_output_dir(self._source_paths)
        self._set_busy(True)
        self._set_status(f"Starting conversion → {output_dir}")

        thread = threading.Thread(
            target=self._convert_worker,
            args=(
                list(self._source_paths),
                output_dir,
                max_images,
                max_size_mb,
                self._scan_options(),
            ),
            daemon=True,
        )
        thread.start()

    def _convert_worker(
        self,
        image_paths: list[Path],
        output_dir: Path,
        max_images: int,
        max_size_mb: float,
        scan_options: ScanOptions,
    ) -> None:
        try:
            result = convert_images_to_pdfs(
                image_paths=image_paths,
                output_dir=output_dir,
                max_images_per_pdf=max_images,
                max_pdf_size_mb=max_size_mb,
                progress=lambda msg: self.after(0, self._set_status, msg),
                scan_options=scan_options,
            )
        except Exception as exc:  # noqa: BLE001 - show any conversion failure in UI
            self.after(0, self._on_convert_failed, str(exc))
            return
        self.after(0, self._on_convert_finished, result.output_paths, result.warnings)

    def _on_convert_failed(self, error: str) -> None:
        self._set_busy(False)
        self._set_status("Conversion failed.")
        messagebox.showerror("Conversion failed", error)

    def _on_convert_finished(
        self, output_paths: list[Path], warnings: list[str]
    ) -> None:
        self._set_busy(False)

        summary = (
            f"Created {len(output_paths)} PDF(s) in the "
            f"'{config.OUTPUT_DIR_NAME}' folder."
        )
        if warnings:
            summary += f" {len(warnings)} warning(s)."
        self._set_status(summary)

        detail = "\n".join(str(p) for p in output_paths)
        if warnings:
            detail += "\n\nWarnings:\n" + "\n".join(warnings)

        messagebox.showinfo("Conversion complete", detail)
        if output_paths:
            self._open_folder(output_paths[0].parent)

    def _open_folder(self, folder: Path) -> None:
        folder = folder.resolve()
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)], check=False)
            else:
                subprocess.run(["xdg-open", str(folder)], check=False)
        except OSError:
            pass

    def _on_close(self) -> None:
        if self._converting:
            if not messagebox.askyesno(
                "Quit",
                "Conversion is still running. Quit anyway?",
            ):
                return
        self.destroy()


def main() -> None:
    app = Img2PdfApp()
    app.mainloop()


if __name__ == "__main__":
    main()
