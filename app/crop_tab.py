"""Scanner tab: open one image, adjust the four page corners, crop in place."""

from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageTk

from app import config
from app.scanner import (
    crop_image_to_quad,
    detect_quad_in_image,
    is_available as scanner_available,
    load_upright,
)


_IMAGE_FILETYPES = [
    (
        "Images",
        "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff "
        "*.JPG *.JPEG *.PNG *.WEBP *.BMP *.TIF *.TIFF",
    ),
    ("All files", "*.*"),
]

_CORNER_LABELS = ("TL", "TR", "BR", "BL")
_HANDLE_RADIUS = 9
_GRAB_RADIUS = 22
# A freshly opened image starts with the corners on the frame itself: nothing is
# moved until the user asks for it with "Auto-detect".
_DEFAULT_INSET = 0.0


class ScannerTab(ctk.CTkFrame):
    """Interactive single-image cropper: auto-detect, drag corners, overwrite."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, fg_color="transparent")

        self._path: Path | None = None
        self._image: Image.Image | None = None
        # Corners in original image pixels, ordered top-left, top-right,
        # bottom-right, bottom-left.
        self._corners: list[list[float]] = []
        self._scale = 1.0
        self._offset = (0, 0)
        self._photo: ImageTk.PhotoImage | None = None
        self._drag_index: int | None = None

        self._build_ui()
        self._update_buttons()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(self)
        toolbar.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 6))
        toolbar.grid_columnconfigure(4, weight=1)

        self._open_btn = ctk.CTkButton(
            toolbar, text="Open Image", command=self._open_image, width=110
        )
        self._open_btn.grid(row=0, column=0, padx=(8, 8), pady=8)

        self._detect_btn = ctk.CTkButton(
            toolbar, text="Auto-detect", command=self._auto_detect, width=110
        )
        self._detect_btn.grid(row=0, column=1, padx=(0, 8), pady=8)

        self._reset_btn = ctk.CTkButton(
            toolbar,
            text="Full frame",
            command=self._reset_corners,
            width=110,
            fg_color="transparent",
            border_width=1,
        )
        self._reset_btn.grid(row=0, column=2, padx=(0, 8), pady=8)

        self._save_btn = ctk.CTkButton(
            toolbar, text="Crop & Save", command=self._save, width=110
        )
        self._save_btn.grid(row=0, column=5, padx=(0, 8), pady=8)

        canvas_frame = ctk.CTkFrame(self)
        canvas_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=6)
        canvas_frame.grid_columnconfigure(0, weight=1)
        canvas_frame.grid_rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            canvas_frame,
            background="#2b2b2b",
            highlightthickness=0,
            borderwidth=0,
        )
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self._canvas.bind("<Configure>", lambda _event: self._redraw())
        self._canvas.bind("<Button-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)

        self._status = ctk.CTkLabel(
            self, text="", anchor="w", justify="left"
        )
        self._status.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        if scanner_available():
            self._set_status(
                "Open an image, drag the four corners, then press Crop & Save."
            )
        else:
            self._set_status(
                "Scanning needs OpenCV — install with: "
                "pip install opencv-python-headless numpy"
            )

    def _set_status(self, message: str) -> None:
        self._status.configure(text=message)

    def _update_buttons(self) -> None:
        available = scanner_available()
        has_image = self._image is not None
        self._open_btn.configure(state="normal" if available else "disabled")
        state = "normal" if available and has_image else "disabled"
        for button in (self._detect_btn, self._reset_btn, self._save_btn):
            button.configure(state=state)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _open_image(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select an image to crop", filetypes=_IMAGE_FILETYPES
        )
        if not selected:
            return
        path = Path(selected).expanduser().resolve()
        if path.suffix.lower() not in config.IMAGE_EXTENSIONS:
            messagebox.showerror("Unsupported file", f"{path.name} is not an image.")
            return
        try:
            image = load_upright(path)
            image.load()
        except Exception as exc:  # noqa: BLE001 - any decode failure goes to the UI
            messagebox.showerror("Could not open image", str(exc))
            return

        self._path = path
        self._image = image
        self._photo = None
        self._drag_index = None
        self._corners = self._default_corners()
        self._update_buttons()
        self._redraw()
        self._set_status(
            f"{path.name} ({image.width} x {image.height}) — corners are on the full "
            "frame. Press Auto-detect, or drag them yourself."
        )

    def _default_corners(self) -> list[list[float]]:
        assert self._image is not None
        width, height = self._image.size
        dx = width * _DEFAULT_INSET
        dy = height * _DEFAULT_INSET
        return [
            [dx, dy],
            [width - 1 - dx, dy],
            [width - 1 - dx, height - 1 - dy],
            [dx, height - 1 - dy],
        ]

    def _reset_corners(self, redraw: bool = True) -> None:
        if self._image is None:
            return
        self._corners = self._default_corners()
        if redraw:
            self._redraw()
            self._set_status("Corners reset to the full frame.")

    def _auto_detect(self, quiet: bool = False) -> None:
        if self._image is None:
            return
        try:
            quad = detect_quad_in_image(self._image)
        except Exception as exc:  # noqa: BLE001 - detection is best-effort
            self._redraw()
            self._set_status(f"Auto-detect failed: {exc}")
            return

        name = self._path.name if self._path else "image"
        if quad is None:
            self._corners = self._default_corners()
            self._redraw()
            self._set_status(
                f"{name}: no page boundary detected — drag the corners yourself."
            )
            return

        self._corners = [[x, y] for x, y in quad]
        self._redraw()
        prefix = f"{name}: " if quiet else ""
        self._set_status(f"{prefix}page detected — drag any corner to adjust.")

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _redraw(self) -> None:
        self._canvas.delete("all")
        if self._image is None:
            return

        canvas_w = max(self._canvas.winfo_width(), 1)
        canvas_h = max(self._canvas.winfo_height(), 1)
        image_w, image_h = self._image.size
        self._scale = min(canvas_w / image_w, canvas_h / image_h)
        draw_w = max(int(image_w * self._scale), 1)
        draw_h = max(int(image_h * self._scale), 1)
        self._offset = ((canvas_w - draw_w) // 2, (canvas_h - draw_h) // 2)

        resized = self._image.convert("RGB").resize((draw_w, draw_h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        self._canvas.create_image(
            self._offset[0], self._offset[1], image=self._photo, anchor="nw"
        )

        if len(self._corners) != 4:
            return

        points = [self._to_canvas(x, y) for x, y in self._corners]
        flat = [value for point in points for value in point]
        self._canvas.create_polygon(
            flat, outline="#4aa3ff", fill="", width=2
        )
        for index, (cx, cy) in enumerate(points):
            self._canvas.create_oval(
                cx - _HANDLE_RADIUS,
                cy - _HANDLE_RADIUS,
                cx + _HANDLE_RADIUS,
                cy + _HANDLE_RADIUS,
                outline="#ffffff",
                fill="#4aa3ff",
                width=2,
            )
            self._canvas.create_text(
                cx,
                cy - _HANDLE_RADIUS - 10,
                text=_CORNER_LABELS[index],
                fill="#ffffff",
            )

    def _to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (self._offset[0] + x * self._scale, self._offset[1] + y * self._scale)

    def _to_image(self, cx: float, cy: float) -> tuple[float, float]:
        assert self._image is not None
        width, height = self._image.size
        x = (cx - self._offset[0]) / self._scale
        y = (cy - self._offset[1]) / self._scale
        return (min(max(x, 0.0), width - 1.0), min(max(y, 0.0), height - 1.0))

    # ------------------------------------------------------------------
    # Corner dragging
    # ------------------------------------------------------------------
    def _on_press(self, event: tk.Event) -> None:
        if self._image is None or len(self._corners) != 4:
            return
        best_index: int | None = None
        best_distance = float(_GRAB_RADIUS)
        for index, (x, y) in enumerate(self._corners):
            cx, cy = self._to_canvas(x, y)
            distance = ((cx - event.x) ** 2 + (cy - event.y) ** 2) ** 0.5
            if distance <= best_distance:
                best_distance = distance
                best_index = index
        self._drag_index = best_index

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_index is None:
            return
        x, y = self._to_image(event.x, event.y)
        self._corners[self._drag_index] = [x, y]
        self._redraw()

    def _on_release(self, _event: tk.Event) -> None:
        if self._drag_index is None:
            return
        label = _CORNER_LABELS[self._drag_index]
        x, y = self._corners[self._drag_index]
        self._drag_index = None
        self._set_status(f"{label} moved to ({x:.0f}, {y:.0f}).")

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------
    def _save(self) -> None:
        if self._image is None or self._path is None or len(self._corners) != 4:
            return

        quad = [(x, y) for x, y in self._corners]
        try:
            cropped = crop_image_to_quad(self._image, quad)
        except Exception as exc:  # noqa: BLE001 - show any warp failure in the UI
            messagebox.showerror("Crop failed", str(exc))
            return

        if not messagebox.askyesno(
            "Replace original?",
            f"{self._path.name} will be replaced with the cropped image "
            f"({cropped.width} x {cropped.height} px).\n\nContinue?",
        ):
            return

        try:
            self._write_in_place(cropped, self._path)
        except Exception as exc:  # noqa: BLE001 - show any write failure in the UI
            messagebox.showerror("Save failed", str(exc))
            return

        # Clear the canvas: the file on disk is already cropped, so leaving it
        # loaded only invites a second crop of an image that is done.
        saved_path = self._path
        self._clear()
        self._set_status(
            f"Saved cropped image ({cropped.width} x {cropped.height}) over "
            f"{saved_path}. Open another image to continue."
        )

    def _clear(self) -> None:
        """Drop the loaded image and reset the tab to its empty state."""
        self._path = None
        self._image = None
        self._corners = []
        self._photo = None
        self._drag_index = None
        self._canvas.delete("all")
        self._update_buttons()

    @staticmethod
    def _write_in_place(image: Image.Image, path: Path) -> None:
        """Write via a temp file next to the target, then swap it in."""
        suffix = path.suffix.lower()
        save_kwargs: dict[str, object] = {}
        if suffix in {".jpg", ".jpeg"}:
            image = image.convert("L" if image.mode == "L" else "RGB")
            save_kwargs = {"quality": config.SCAN_INTERMEDIATE_JPEG_QUALITY}

        temp_path = path.with_name(f".{path.stem}.tmp{path.suffix}")
        try:
            image.save(temp_path, **save_kwargs)
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
