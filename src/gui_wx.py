import csv
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import multiprocessing as mp
import wx
import wx.dataview as dv
from sexpdata import Symbol, loads

from gui_core import (
    APP_VERSION,
    AUTO_BORDER_KEY,
    BORDER_MARGIN_KEY,
    RENDER_PRESET_KEY,
    USE_SYMBOLNAME_KEY,
    SHOW_LOG_KEY,
    export_symbols_with_checks,
    list_project_symbols,
    load_config,
    open_folder_in_explorer,
    open_output_folder,
    process_archives,
    save_config,
    scan_zip_folder,
    update_drc_rules,
)
from library_manager import INPUT_ZIP_FOLDER, PROJECT_DIR

# ===============================
# Logging
# ===============================
import logging

# --- Ensure sys.stdout/stderr exist (important for PyInstaller GUI builds) ---
if not hasattr(sys, "stdout") or sys.stdout is None:
    class DevNull:
        def write(self, *_): pass
        def flush(self): pass
    sys.stdout = DevNull()
    sys.stderr = DevNull()

logger = logging.getLogger("kicad_library_manager")
logger.setLevel(logging.DEBUG)

# --- Formatter with auto-clean for duplicate prefixes ---
class CleanFormatter(logging.Formatter):
    def format(self, record):
        msg = record.getMessage()
        # Strip manually embedded tags like [INFO] [DEBUG]
        for tag in ("[INFO]", "[DEBUG]", "[WARN]", "[ERROR]", "[OK]"):
            msg = msg.replace(tag, "").strip()
        record.message = msg
        return super().format(record)

formatter = CleanFormatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

# --- Console handler (for debug mode or when running via python gui_wx.py) ---
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# --- Remove broken handlers (from PyInstaller --noconsole builds) ---
for h in list(logger.handlers):
    if isinstance(h, logging.StreamHandler) and getattr(h.stream, "write", None) is None:
        logger.removeHandler(h)

# --- Do not propagate logs to root (prevents duplication) ---
logger.propagate = False


# --- Shared icon helper for buttons ---
ICON_SIZE = (16, 16)
def set_button_icon(btn: wx.Button, art_id, position=wx.RIGHT):
    """Attach a standard wx art bitmap to a button when available."""
    bmp = wx.ArtProvider.GetBitmap(art_id, wx.ART_BUTTON, ICON_SIZE)
    if bmp.IsOk():
        btn.SetBitmap(bmp, position)


# --- GUI Log handler (later attached to wx.TextCtrl in MainFrame) ---
class WxGuiLogHandler(logging.Handler):
    """Custom logging handler that forwards logs into wx TextCtrl."""
    def __init__(self, gui_frame):
        super().__init__()
        self.gui_frame = gui_frame

    def emit(self, record):
        try:
            msg = formatter.format(record)
            wx.CallAfter(self.gui_frame.append_log, msg)
        except Exception:
            pass


# ===============================
# drag & drop feature
# ===============================

class ZipFileDropTarget(wx.FileDropTarget):
    """Handles drag-and-drop of .zip archives onto the ZIP list."""
    def __init__(self, parent_frame):
        super().__init__()
        self.parent = parent_frame

    def OnDropFiles(self, x, y, filenames):
        import shutil

        dropped = []
        target_dir = self.parent.current_folder
        target_dir.mkdir(parents=True, exist_ok=True)

        for f in filenames:
            p = Path(f)
            if p.is_file() and p.suffix.lower() == ".zip":
                target = target_dir / p.name
                try:
                    if target != p:
                        shutil.copy2(p, target)
                    dropped.append(target)
                except Exception as e:
                    wx.CallAfter(self.parent.append_log, f"[ERROR] Failed to copy {p.name}: {e}")

        if dropped:
            wx.CallAfter(self.parent.append_log, f"[OK] Added {len(dropped)} ZIP archive(s).")
            wx.CallAfter(self.parent.refresh_zip_list_async)

        return True


# ===============================
# multiprocessing helpers
# ===============================
def _scan_zip_folder_worker(folder_str: str, queue) -> None:
    from pathlib import Path
    from gui_core import scan_zip_folder
    try:
        rows = scan_zip_folder(Path(folder_str))
        queue.put(("ok", rows))
    except Exception as e:
        queue.put(("error", str(e)))


def _process_archives_worker(paths, is_purge, rename_assets, use_symbol_name, queue) -> None:
    from pathlib import Path
    from gui_core import process_archives
    try:
        ok = process_archives(
            [Path(p) for p in paths],
            is_purge=is_purge,
            rename_assets=rename_assets,
            use_symbol_name=use_symbol_name,
        )
        queue.put(("ok", ok))
    except Exception as e:
        queue.put(("error", str(e)))


def _list_symbols_worker(queue) -> None:
    from gui_core import list_project_symbols
    try:
        symbols = list_project_symbols()
        queue.put(("ok", symbols))
    except Exception as e:
        queue.put(("error", str(e)))


def _export_symbols_worker(selected_symbols, queue) -> None:
    from gui_core import export_symbols_with_checks
    try:
        success, export_paths = export_symbols_with_checks(selected_symbols)
        queue.put(("ok", success, [str(p) for p in export_paths]))
    except Exception as e:
        queue.put(("error", str(e)))


def _delete_symbols_worker(selected_symbols, queue) -> None:
    try:
        from library_manager import PROJECT_SYMBOL_LIB, PROJECT_FOOTPRINT_LIB, PROJECT_3D_DIR
        from sexpdata import loads, dumps

        deleted_syms = deleted_fp = deleted_3d = 0
        linked_footprints = set()

        with open(PROJECT_SYMBOL_LIB, "r", encoding="utf-8") as f:
            sym_data = loads(f.read())

        new_sym_data = [sym_data[0]]
        for el in sym_data[1:]:
            if not (isinstance(el, list) and len(el) > 1 and str(el[0]) == "symbol"):
                new_sym_data.append(el)
                continue

            sym_name = str(el[1])
            if sym_name in selected_symbols:
                for item in el:
                    if (
                        isinstance(item, list)
                        and len(item) >= 3
                        and str(item[0]) == "property"
                        and str(item[1]) == "Footprint"
                    ):
                        fp_name = str(item[2]).split(":")[-1]
                        linked_footprints.add(fp_name)
                deleted_syms += 1
                continue
            new_sym_data.append(el)

        if deleted_syms:
            with open(PROJECT_SYMBOL_LIB, "w", encoding="utf-8") as f:
                f.write(dumps(new_sym_data, pretty_print=True))

        for fp_name in linked_footprints:
            fp_path = PROJECT_FOOTPRINT_LIB / f"{fp_name}.kicad_mod"
            if fp_path.exists():
                fp_path.unlink()
                deleted_fp += 1
            stp_path = PROJECT_3D_DIR / f"{fp_name}.stp"
            if stp_path.exists():
                stp_path.unlink()
                deleted_3d += 1

        queue.put(("ok", deleted_syms, deleted_fp, deleted_3d))
    except Exception as e:
        queue.put(("error", str(e)))


def _update_drc_worker(queue) -> None:
    from gui_core import update_drc_rules
    try:
        ok = update_drc_rules()
        queue.put(("ok", ok))
    except Exception as e:
        queue.put(("error", str(e)))


def _find_kicad_cli_path() -> str | None:
    kicad_cli = shutil.which("kicad-cli")
    if kicad_cli:
        return kicad_cli

    if os.name == "nt":
        candidates = []
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_key)
            if not base:
                continue
            kicad_root = Path(base) / "KiCad"
            if not kicad_root.exists():
                continue
            for bin_path in kicad_root.glob("*\\bin\\kicad-cli.exe"):
                candidates.append(bin_path)
        if candidates:
            return str(sorted(candidates)[-1])
    return None


def _subprocess_no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def _normalize_bom_headers_inplace(bom_path: str) -> None:
    try:
        with open(bom_path, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            fieldnames = reader[0].keys() if reader else []
    except Exception:
        return

    if not fieldnames:
        return

    fieldnames = list(fieldnames)
    mapped = ["Qty" if h == "Quantity" else h for h in fieldnames]
    if mapped == fieldnames:
        return

    try:
        with open(bom_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=mapped)
            writer.writeheader()
            for row in reader:
                writer.writerow({new: row.get(old, "") for new, old in zip(mapped, fieldnames)})
    except Exception:
        return


def _generate_bom_worker(schematic_path: str, bom_path: str, queue) -> None:
    try:
        kicad_cli = _find_kicad_cli_path()
        if not kicad_cli:
            queue.put(("error", "kicad-cli not found. Please add it to PATH or install KiCad."))
            return

        fields = "Reference,Value,Footprint,MNR,LCSC,${QUANTITY}"
        labels = "Reference,Value,Footprint,MNR,LCSC,Qty"
        cmd = [
            kicad_cli,
            "sch",
            "export",
            "bom",
            schematic_path,
            "--output",
            bom_path,
            "--fields",
            fields,
            "--labels",
            labels,
            "--exclude-dnp",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, **_subprocess_no_window_kwargs())
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip() or "Unbekannter Fehler"
            queue.put(("error", f"BOM-Export fehlgeschlagen: {err}"))
            return

        _normalize_bom_headers_inplace(bom_path)
        queue.put(("ok", bom_path))
    except Exception as e:
        queue.put(("error", str(e)))


def _parse_bom_worker(bom_path: str, group_by_field: str | None, queue) -> None:
    try:
        from mouser_integration import BOMHandler
        handler = BOMHandler()
        data = handler.process_bom_file(bom_path, group_by_field=group_by_field)
        queue.put(("ok", data))
    except Exception as e:
        queue.put(("error", str(e)))


def _submit_order_worker(data_for_order: dict, queue) -> None:
    import io
    import contextlib
    import time as _time
    try:
        import mouser_integration as mouser
        client = mouser.MouserOrderClient()

        class QueueWriter:
            def write(self, s):
                if s is None:
                    return
                for line in str(s).splitlines():
                    if line.strip():
                        queue.put(("log", line))
            def flush(self): pass

        with contextlib.redirect_stdout(QueueWriter()), contextlib.redirect_stderr(QueueWriter()):
            attempts = 0
            success = False
            for attempts in range(mouser.API_TIMEOUT_MAX_RETRIES):
                queue.put(("log", f"Attempt {attempts+1}/{mouser.API_TIMEOUT_MAX_RETRIES}"))
                try:
                    ok = client.order_parts_from_data_array(dict(data_for_order))
                except Exception as e:
                    ok = False
                    queue.put(("log", f"Exception during order attempt: {e}"))
                if ok:
                    success = True
                    break
                if attempts < mouser.API_TIMEOUT_MAX_RETRIES - 1:
                    queue.put(("log", f"Attempt failed. Retrying in {mouser.API_TIMEOUT_SLEEP_S} s..."))
                    _time.sleep(mouser.API_TIMEOUT_SLEEP_S)
            queue.put(("log", f"[INFO] Final result: Attempts -> {attempts+1}, Success -> {success}"))
        queue.put(("ok", success))
    except Exception as e:
        queue.put(("error", str(e)))


# ===============================
# board preview panel (image + crop overlay)
# ===============================
class BoardPreviewPanel(wx.Panel):
    """Panel that draws a scaled bitmap and interactive crop rectangle overlay."""
    def __init__(self, parent, on_crop_change=None, on_select=None, on_reset=None):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self._bmp = None
        self._scaled = None
        self._last_size = wx.Size(0, 0)
        self._crop = (0, 100, 0, 100)  # x0, x1, y0, y1 (percent)
        self._image_rect = None  # wx.Rect of scaled bitmap in panel coords
        self._drag_mode = None  # "move" or edges like "l","r","t","b","lt","rb",...
        self._drag_anchor = None  # (x,y) at drag start
        self._crop_at_drag = None
        self._on_crop_change = on_crop_change
        self._on_select = on_select
        self._on_reset = on_reset
        self._loading = False
        self._loading_text = "Rendering..."
        self._loading_progress = None
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_resize)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_mouse_move)
        self.Bind(wx.EVT_RIGHT_UP, self._on_right_up)

    def set_bitmap(self, bmp: wx.Bitmap | None):
        self._bmp = bmp if (bmp and bmp.IsOk()) else None
        self._scaled = None
        self.Refresh()

    def set_crop(self, crop: tuple[int, int, int, int]):
        self._crop = crop
        self.Refresh()

    def get_crop(self) -> tuple[int, int, int, int]:
        return self._crop

    def set_loading(self, loading: bool, text: str | None = None, progress: int | None = None):
        self._loading = loading
        if text:
            self._loading_text = text
        if progress is not None:
            self._loading_progress = max(0, min(100, int(progress)))
        if not loading:
            self._loading_progress = None
        self.Refresh()

    def _on_resize(self, event):
        event.Skip()
        self._scaled = None
        self.Refresh()

    def _get_scaled_bitmap(self) -> wx.Bitmap | None:
        if not self._bmp:
            return None
        size = self.GetClientSize()
        if size.width < 10 or size.height < 10:
            return None
        if self._scaled and self._last_size == size:
            return self._scaled
        img = self._bmp.ConvertToImage()
        w, h = img.GetWidth(), img.GetHeight()
        scale = min(size.width / w, size.height / h)
        new_w = max(int(w * scale), 1)
        new_h = max(int(h * scale), 1)
        img = img.Scale(new_w, new_h, wx.IMAGE_QUALITY_HIGH)
        self._scaled = wx.Bitmap(img)
        self._last_size = size
        return self._scaled

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()

        bmp = self._get_scaled_bitmap()
        if bmp:
            panel_size = self.GetClientSize()
            x = (panel_size.width - bmp.GetWidth()) // 2
            y = (panel_size.height - bmp.GetHeight()) // 2
            self._image_rect = wx.Rect(x, y, bmp.GetWidth(), bmp.GetHeight())
            dc.DrawBitmap(bmp, x, y, True)

            # draw crop rectangle
            x0, x1, y0, y1 = self._crop
            rect_x0 = x + int(bmp.GetWidth() * x0 / 100)
            rect_x1 = x + int(bmp.GetWidth() * x1 / 100)
            rect_y0 = y + int(bmp.GetHeight() * y0 / 100)
            rect_y1 = y + int(bmp.GetHeight() * y1 / 100)
            rect_w = max(rect_x1 - rect_x0, 1)
            rect_h = max(rect_y1 - rect_y0, 1)
            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.SetPen(wx.Pen(wx.Colour(220, 50, 50), 2))
            dc.DrawRectangle(rect_x0, rect_y0, rect_w, rect_h)

        if self._loading:
            panel_size = self.GetClientSize()
            dc.SetBrush(wx.Brush(wx.Colour(0, 0, 0, 80)))
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.DrawRectangle(0, 0, panel_size.width, panel_size.height)
            dc.SetTextForeground(wx.Colour(255, 255, 255))
            dc.DrawLabel(self._loading_text, wx.Rect(0, 0, panel_size.width, panel_size.height), alignment=wx.ALIGN_CENTER)
            if self._loading_progress is not None:
                bar_w = max(panel_size.width // 2, 120)
                bar_h = 10
                x = (panel_size.width - bar_w) // 2
                y = (panel_size.height // 2) + 18
                dc.SetBrush(wx.Brush(wx.Colour(255, 255, 255, 80)))
                dc.SetPen(wx.Pen(wx.Colour(255, 255, 255, 120)))
                dc.DrawRectangle(x, y, bar_w, bar_h)
                fill_w = int(bar_w * self._loading_progress / 100)
                dc.SetBrush(wx.Brush(wx.Colour(80, 200, 120, 200)))
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.DrawRectangle(x, y, fill_w, bar_h)

    def _on_left_down(self, event):
        if not self._image_rect:
            return
        pos = event.GetPosition()
        if self._on_select:
            self._on_select()
        if not self._image_rect.Contains(pos):
            return
        self._drag_mode = self._hit_test(pos)
        if not self._drag_mode:
            return
        self._drag_anchor = (pos.x, pos.y)
        self._crop_at_drag = self._crop
        self.CaptureMouse()

    def _on_left_up(self, event):
        if self.HasCapture():
            self.ReleaseMouse()
        self._drag_mode = None
        self._drag_anchor = None
        self._crop_at_drag = None

    def _on_mouse_move(self, event):
        if not self._drag_mode or not self._drag_anchor or not self._crop_at_drag:
            return
        if not self._image_rect:
            return
        pos = event.GetPosition()
        new_crop = self._compute_crop_from_drag(pos)
        if new_crop and self._on_crop_change:
            self._on_crop_change(new_crop)

    def _on_right_up(self, event):
        if self._on_reset:
            self._on_reset()

    def _hit_test(self, pos) -> str | None:
        rect = self._image_rect
        if not rect:
            return None
        x0, x1, y0, y1 = self._crop
        left = rect.x + int(rect.width * x0 / 100)
        right = rect.x + int(rect.width * x1 / 100)
        top = rect.y + int(rect.height * y0 / 100)
        bottom = rect.y + int(rect.height * y1 / 100)
        margin = 6

        near_left = abs(pos.x - left) <= margin
        near_right = abs(pos.x - right) <= margin
        near_top = abs(pos.y - top) <= margin
        near_bottom = abs(pos.y - bottom) <= margin

        if near_left and near_top:
            return "lt"
        if near_right and near_top:
            return "rt"
        if near_left and near_bottom:
            return "lb"
        if near_right and near_bottom:
            return "rb"
        if near_left:
            return "l"
        if near_right:
            return "r"
        if near_top:
            return "t"
        if near_bottom:
            return "b"

        if left < pos.x < right and top < pos.y < bottom:
            return "move"
        return None

    def _compute_crop_from_drag(self, pos) -> tuple[int, int, int, int] | None:
        rect = self._image_rect
        if not rect:
            return None
        x0, x1, y0, y1 = self._crop_at_drag
        min_size = 2  # percent

        def clamp(v, lo=0, hi=100):
            return max(min(v, hi), lo)

        # convert mouse to percent
        px = clamp(int((pos.x - rect.x) * 100 / rect.width))
        py = clamp(int((pos.y - rect.y) * 100 / rect.height))

        mode = self._drag_mode
        if mode == "move":
            dx = int((pos.x - self._drag_anchor[0]) * 100 / rect.width)
            dy = int((pos.y - self._drag_anchor[1]) * 100 / rect.height)
            nx0 = clamp(x0 + dx)
            nx1 = clamp(x1 + dx)
            ny0 = clamp(y0 + dy)
            ny1 = clamp(y1 + dy)
            # keep size
            if nx1 - nx0 < min_size:
                nx1 = nx0 + min_size
            if ny1 - ny0 < min_size:
                ny1 = ny0 + min_size
            if nx1 > 100:
                nx0 -= nx1 - 100
                nx1 = 100
            if ny1 > 100:
                ny0 -= ny1 - 100
                ny1 = 100
            if nx0 < 0:
                nx1 -= nx0
                nx0 = 0
            if ny0 < 0:
                ny1 -= ny0
                ny0 = 0
            return (nx0, nx1, ny0, ny1)

        nx0, nx1, ny0, ny1 = x0, x1, y0, y1
        if "l" in mode:
            nx0 = clamp(min(px, nx1 - min_size))
        if "r" in mode:
            nx1 = clamp(max(px, nx0 + min_size))
        if "t" in mode:
            ny0 = clamp(min(py, ny1 - min_size))
        if "b" in mode:
            ny1 = clamp(max(py, ny0 + min_size))
        return (nx0, nx1, ny0, ny1)




# ===============================
# Main GUI Frame
# ===============================
class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=f"KiCad Library Manager (wxPython) - {APP_VERSION}", size=(1120, 800))
        self.current_folder = INPUT_ZIP_FOLDER.resolve()
        self.zip_rows = []
        self._zip_busy = False
        self._zip_scan_timer = None
        self._zip_process_timer = None
        self._sym_busy = False
        self._sym_list_timer = None
        self._sym_export_timer = None
        self._sym_delete_timer = None
        self._drc_busy = False
        self._drc_timer = None
        self.InitUI()
        self._configure_logger()
        self.Centre()
        self.Show()

        # Kick off background load so the frame appears immediately
        threading.Thread(target=self._post_init_load, daemon=True).start()

    def _post_init_load(self):
        """Load config and initial data without blocking the UI thread."""
        cfg = load_config()
        try:
            zip_rows = scan_zip_folder(self.current_folder)
        except Exception as e:
            logger.error(f"Failed to scan ZIP folder: {e}")
            zip_rows = []
        try:
            symbols = list_project_symbols()
        except Exception as e:
            logger.error(f"Failed to load project symbols: {e}")
            symbols = []

        wx.CallAfter(self._apply_initial_data, cfg, zip_rows, symbols)

    def _apply_initial_data(self, cfg, zip_rows, symbols):
        self.chk_use_symbol_name.SetValue(cfg.get(USE_SYMBOLNAME_KEY, False))
        self.refresh_zip_list(rows=zip_rows)
        self.refresh_symbol_list(symbols=symbols)

    def _configure_logger(self):
        """Attach GUI log handler once."""
        if not any(isinstance(h, WxGuiLogHandler) for h in logger.handlers):
            gui_handler = WxGuiLogHandler(self)
            gui_handler.setFormatter(
                logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
            )
            logger.addHandler(gui_handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # prevent double-printing via root logger
    # ---------- Layout ----------
    def InitUI(self):
        panel = wx.Panel(self)
        self.panel = panel
        vbox = wx.BoxSizer(wx.VERTICAL)

        # --- Project opener ---
        self.project_file = self._find_project_file()
        proj_label = (
            f"Project: {self.project_file}"
            if self.project_file
            else "Project: not found"
        )
        proj_box = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_open_project = wx.Button(panel, label="Open KiCad Project in New Window")
        self.btn_open_project.Enable(bool(self.project_file))
        set_button_icon(self.btn_open_project, wx.ART_NORMAL_FILE)
        self.lbl_project = wx.StaticText(panel, label=proj_label)
        proj_box.Add(self.btn_open_project, 0, wx.RIGHT, 8)
        proj_box.Add(self.lbl_project, 0, wx.ALIGN_CENTER_VERTICAL)
        vbox.Add(proj_box, 0, wx.EXPAND | wx.ALL, 8)
        self.log_popup = None
        self.log_popup_ctrl = None

        # --- Tabs ---
        self.notebook = wx.Notebook(panel)
        self.tab_zip = wx.Panel(self.notebook)
        self.tab_symbol = wx.Panel(self.notebook)
        self.tab_drc = wx.Panel(self.notebook)
        self.tab_board = wx.Panel(self.notebook)
        self.tab_mouser = MouserAutoOrderTab(self.notebook, log_callback=self.append_log)
        self.notebook.AddPage(self.tab_zip, "Import ZIP Archives")
        self.notebook.AddPage(self.tab_symbol, "Export Project Symbols")
        self.notebook.AddPage(self.tab_drc, "DRC Manager")
        self.notebook.AddPage(self.tab_board, "Generate Images from PCB")
        self.notebook.AddPage(self.tab_mouser, "Mouser Auto Order")
        vbox.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)

        # --- ZIP tab content ---
        self.zip_vbox = wx.BoxSizer(wx.VERTICAL)

        # === Folder selection (ZIP tab only) ===
        box1 = wx.StaticBox(self.tab_zip, label="Select Archive Folder")
        s1 = wx.StaticBoxSizer(box1, wx.VERTICAL)
        h_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_select = wx.Button(self.tab_zip, label="Select ZIP Folder")
        self.btn_open = wx.Button(self.tab_zip, label="Open ZIP Folder")
        set_button_icon(self.btn_select, wx.ART_NEW_DIR)
        set_button_icon(self.btn_open, wx.ART_FOLDER_OPEN)
        h_buttons.Add(self.btn_select, 0, wx.RIGHT, 8)
        h_buttons.Add(self.btn_open, 0)
        self.current_folder_txt = wx.StaticText(
            self.tab_zip, label="Current Folder: (Initializing...)"
        )
        s1.Add(h_buttons, 0, wx.BOTTOM, 5)
        s1.Add(self.current_folder_txt, 0, wx.TOP, 2)
        self.zip_vbox.Add(s1, 0, wx.EXPAND | wx.ALL, 8)

        # === Top controls ===
        h_zip_top = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_zips = wx.Button(self.tab_zip, label="Refresh ZIPs")
        set_button_icon(self.btn_refresh_zips, wx.ART_REDO)
        self.chk_master_zip = wx.CheckBox(self.tab_zip, label="Select All")
        h_zip_top.Add(self.btn_refresh_zips, 0, wx.RIGHT, 8)
        h_zip_top.Add(self.chk_master_zip, 0, wx.ALIGN_CENTER_VERTICAL)
        self.zip_vbox.Add(h_zip_top, 0, wx.BOTTOM, 5)

        # === ZIP file list ===
        self.zip_file_list = dv.DataViewListCtrl(
            self.tab_zip,
            style=dv.DV_ROW_LINES | dv.DV_VERT_RULES | dv.DV_SINGLE
        )
        self.zip_col_include = self.zip_file_list.AppendToggleColumn("Include", width=80)
        self.zip_col_include.SetAlignment(wx.ALIGN_CENTER)
        self.zip_col_name = self.zip_file_list.AppendTextColumn("Archive Name", width=300)
        self.zip_col_status = self.zip_file_list.AppendIconTextColumn("Status", width=250, align=wx.ALIGN_LEFT)
        self.zip_col_delete = self.zip_file_list.AppendTextColumn("Delete", width=80, align=wx.ALIGN_CENTER)
        self.zip_file_list.SetDropTarget(ZipFileDropTarget(self))
        self.zip_file_list.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, self.on_zip_checkbox_changed)
        self.zip_file_list.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, self.on_zip_delete_clicked)
        
        # dynamically resize columns to always fill 100%
        self.zip_file_list.Bind(wx.EVT_SIZE, self.on_resize_zip_columns)        
        self.zip_vbox.Add(wx.StaticText(self.tab_zip, label="ZIP Archives:"), 0, wx.BOTTOM, 5)
        self.zip_vbox.Add(self.zip_file_list, 1, wx.EXPAND | wx.BOTTOM, 5)

        # === Option checkbox ===
        self.chk_use_symbol_name = wx.CheckBox(
            self.tab_zip,
            label="Use symbol name as footprint and 3D model name"
        )
        self.zip_vbox.Add(self.chk_use_symbol_name, 0, wx.TOP | wx.BOTTOM, 5)

        # === Process / Purge buttons ===
        self.btn_process = wx.Button(self.tab_zip, label="PROCESS / IMPORT")
        self.btn_purge = wx.Button(self.tab_zip, label="PURGE / DELETE")
        set_button_icon(self.btn_process, wx.ART_GO_FORWARD)
        set_button_icon(self.btn_purge, wx.ART_DELETE)
        h_zip_btns = wx.BoxSizer(wx.HORIZONTAL)
        h_zip_btns.Add(self.btn_process, 0, wx.RIGHT, 8)
        h_zip_btns.Add(self.btn_purge, 0)
        self.zip_vbox.Add(h_zip_btns, 0, wx.TOP, 5)

        self.tab_zip.SetSizer(self.zip_vbox)

        # --- Symbol tab content ---
        self.sym_vbox = wx.BoxSizer(wx.VERTICAL)
        
        h_sym_top = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh_symbols = wx.Button(self.tab_symbol, label="Refresh Symbols")
        set_button_icon(self.btn_refresh_symbols, wx.ART_REDO)
        self.chk_master_symbols = wx.CheckBox(self.tab_symbol, label="Select All")
        
        # --- fix ghost button / layout artifact ---
        self.btn_refresh_symbols.SetMinSize((150, -1))
        self.chk_master_symbols.SetMinSize((100, -1))
        self.btn_refresh_symbols.SetWindowStyleFlag(wx.BU_EXACTFIT)

        h_sym_top.Add(self.btn_refresh_symbols, 0, wx.RIGHT, 8)
        h_sym_top.Add(self.chk_master_symbols, 0, wx.ALIGN_CENTER_VERTICAL)
        self.sym_vbox.Add(h_sym_top, 0, wx.BOTTOM, 5)
        
        self.symbol_list = dv.DataViewListCtrl(self.tab_symbol, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES)
        self.sym_col_include = self.symbol_list.AppendToggleColumn("Include", width=80)
        self.sym_col_include.SetAlignment(wx.ALIGN_CENTER)
        self.sym_col_name = self.symbol_list.AppendTextColumn("Symbol", width=300)
        self.sym_vbox.Add(wx.StaticText(self.tab_symbol, label="Project Symbols:"), 0, wx.BOTTOM, 5)
        self.sym_vbox.Add(self.symbol_list, 1, wx.EXPAND | wx.BOTTOM, 5)

        # --- Export + Orphan Delete Buttons ---
        self.btn_export = wx.Button(self.tab_symbol, label="EXPORT SELECTED")
        self.btn_open_output = wx.Button(self.tab_symbol, label="OPEN OUTPUT FOLDER")
        self.btn_delete_orphans = wx.Button(self.tab_symbol, label="DELETE SELECTED")
        set_button_icon(self.btn_export, wx.ART_FILE_SAVE_AS)
        set_button_icon(self.btn_open_output, wx.ART_FOLDER_OPEN)
        set_button_icon(self.btn_delete_orphans, wx.ART_DELETE)
        self.tab_symbol.Layout()
        self.btn_delete_orphans.SetForegroundColour(wx.RED)

        h_sym_btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_delete_selected = wx.Button(self.tab_symbol, label="DELETE SELECTED")
        set_button_icon(self.btn_delete_selected, wx.ART_DELETE)
        self.btn_delete_selected.SetForegroundColour(wx.RED)

        h_sym_btns.Add(self.btn_export, 0, wx.RIGHT, 8)
        h_sym_btns.Add(self.btn_open_output, 0, wx.RIGHT, 8)
        h_sym_btns.Add(self.btn_delete_selected, 0)
        self.sym_vbox.Add(h_sym_btns, 0, wx.TOP, 5)
        self.tab_symbol.SetSizer(self.sym_vbox)


        # --- DRC tab content ---
        self.drc_vbox = wx.BoxSizer(wx.VERTICAL)
        self.btn_drc = wx.Button(self.tab_drc, label="Update DRC Rules")
        set_button_icon(self.btn_drc, wx.ART_TICK_MARK)
        self.drc_vbox.Add(wx.StaticText(self.tab_drc, label="Auto-Apply DRC Rules Based on PCB Layer Count:"), 0, wx.BOTTOM, 5)
        self.drc_vbox.Add(self.btn_drc, 0, wx.BOTTOM, 5)
        self.tab_drc.SetSizer(self.drc_vbox)

        # --- Generate Images from PCB tab content ---
        self.board_vbox = wx.BoxSizer(wx.VERTICAL)

        self.board_controls = wx.BoxSizer(wx.HORIZONTAL)
        self.board_controls.Add(
            wx.StaticText(self.tab_board, label="Generate top/bottom board images from .kicad_pcb:"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            12,
        )
        self.btn_generate_board = wx.Button(self.tab_board, label="Generate from .kicad_pcb")
        set_button_icon(self.btn_generate_board, wx.ART_EXECUTABLE_FILE)
        self.btn_render_custom = wx.Button(self.tab_board, label="Crop to Frame")
        set_button_icon(self.btn_render_custom, wx.ART_EXECUTABLE_FILE)
        self.btn_save_board = wx.Button(self.tab_board, label="Save Previews")
        set_button_icon(self.btn_save_board, wx.ART_FILE_SAVE_AS)
        self._board_has_images = False
        self.board_controls.Add(self.btn_generate_board, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.board_controls.Add(self.btn_render_custom, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.board_controls.Add(self.btn_save_board, 0, wx.ALIGN_CENTER_VERTICAL)
        self.board_vbox.Add(self.board_controls, 0, wx.EXPAND | wx.ALL, 8)

        self.board_sizes_box = wx.StaticBoxSizer(wx.VERTICAL, self.tab_board, "Render Resolution")
        self.board_sizes = wx.FlexGridSizer(cols=4, vgap=4, hgap=8)
        self.board_sizes.AddGrowableCol(1, 0)
        self.board_sizes.AddGrowableCol(3, 0)

        self.board_sizes.Add(wx.StaticText(self.tab_board, label="Width"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_render_w = wx.TextCtrl(self.tab_board, value="1920", size=(70, -1))
        self.board_sizes.Add(self.txt_render_w, 0, wx.ALIGN_CENTER_VERTICAL)
        self.board_sizes.Add(wx.StaticText(self.tab_board, label="Height"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_render_h = wx.TextCtrl(self.tab_board, value="1080", size=(70, -1))
        self.board_sizes.Add(self.txt_render_h, 0, wx.ALIGN_CENTER_VERTICAL)
        self.board_sizes_box.Add(self.board_sizes, 0, wx.ALL, 6)

        self.preset_radio = wx.RadioBox(
            self.tab_board,
            label="Resolution Preset",
            choices=["720p", "1080p", "2K", "4K"],
            majorDimension=4,
            style=wx.RA_SPECIFY_COLS,
        )
        cfg = load_config()
        preset_idx = cfg.get(RENDER_PRESET_KEY, 1)
        if not isinstance(preset_idx, int):
            preset_idx = 1
        preset_idx = max(0, min(preset_idx, self.preset_radio.GetCount() - 1))
        self.preset_radio.SetSelection(preset_idx)
        preset_values = {
            0: (1280, 720),
            1: (1920, 1080),
            2: (2560, 1440),
            3: (3840, 2160),
        }
        if preset_idx in preset_values:
            w, h = preset_values[preset_idx]
            self._set_render_preset(w, h)

        self.chk_auto_border = wx.CheckBox(
            self.tab_board,
            label="Use auto-border placement",
        )
        self.chk_auto_border.SetValue(cfg.get(AUTO_BORDER_KEY, True))
        self.btn_border_margin_dec = wx.Button(self.tab_board, label="-10", size=(50, -1))
        self.btn_border_margin_inc = wx.Button(self.tab_board, label="+10", size=(50, -1))
        self.txt_border_margin = wx.TextCtrl(
            self.tab_board,
            value=str(cfg.get(BORDER_MARGIN_KEY, 20)),
            size=(60, -1),
            style=wx.TE_PROCESS_ENTER,
        )

        self.border_margin_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.border_margin_sizer.Add(
            wx.StaticText(self.tab_board, label="Border margin (px)"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            8,
        )
        self.border_margin_sizer.Add(self.btn_border_margin_dec, 0, wx.RIGHT, 6)
        self.border_margin_sizer.Add(self.txt_border_margin, 0, wx.RIGHT, 6)
        self.border_margin_sizer.Add(self.btn_border_margin_inc, 0)

        self.border_settings_box = wx.StaticBoxSizer(wx.VERTICAL, self.tab_board, "Border Settings")
        self.border_settings_row = wx.BoxSizer(wx.HORIZONTAL)
        self.border_settings_row.Add(self.chk_auto_border, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        self.border_settings_row.Add(self.border_margin_sizer, 0, wx.ALIGN_CENTER_VERTICAL)
        self.border_settings_box.Add(self.border_settings_row, 0, wx.ALL, 6)
        self.board_sizes_row = wx.BoxSizer(wx.HORIZONTAL)
        self.board_sizes_row.Add(self.board_sizes_box, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self.board_sizes_row.Add(self.preset_radio, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        self.board_sizes_row.Add(self.border_settings_box, 0, wx.ALIGN_CENTER_VERTICAL)
        self.board_vbox.Add(self.board_sizes_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        wx.CallAfter(self._normalize_board_section_heights)

        self.lbl_crop_help = wx.StaticText(
            self.tab_board,
            label="Tip: Drag the red box to move/resize the crop frame. Right-click an image to auto-fit (or reset).",
        )
        self.lbl_crop_help.SetFont(wx.Font(wx.FontInfo().Bold().Italic()))
        self.lbl_crop_help.SetForegroundColour(wx.Colour(200, 80, 20))
        self.board_vbox.Add(self.lbl_crop_help, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.board_hbox = wx.BoxSizer(wx.HORIZONTAL)
        self.board_image_panel_top = BoardPreviewPanel(
            self.tab_board,
            on_crop_change=self._set_crop,
            on_select=lambda: self._set_active_preview("top"),
            on_reset=lambda: self._auto_crop_current_preview("top"),
        )
        self.board_image_panel_bottom = BoardPreviewPanel(
            self.tab_board,
            on_crop_change=self._set_crop,
            on_select=lambda: self._set_active_preview("bottom"),
            on_reset=lambda: self._auto_crop_current_preview("bottom"),
        )
        self.board_image_label_top = wx.StaticText(self.board_image_panel_top, label="")
        self.board_image_label_top.SetBackgroundColour(wx.Colour(240, 240, 240))
        self.board_image_label_top.SetForegroundColour(wx.Colour(60, 60, 60))
        self.board_image_label_bottom = wx.StaticText(self.board_image_panel_bottom, label="")
        self.board_image_label_bottom.SetBackgroundColour(wx.Colour(240, 240, 240))
        self.board_image_label_bottom.SetForegroundColour(wx.Colour(60, 60, 60))

        placeholder_top = self._make_placeholder_bitmap((520, 360), "Top image")
        placeholder_bottom = self._make_placeholder_bitmap((520, 360), "Bottom image")
        self.board_source_top = placeholder_top
        self.board_source_bottom = placeholder_bottom
        self._set_board_images(placeholder_top, placeholder_bottom)
        self._set_crop((0, 100, 0, 100))
        self.active_side = "top"
        self._set_active_preview("top")
        self._update_board_action_state()
        self.board_image_panel_top.Bind(wx.EVT_SIZE, self.on_board_image_resize)
        self.board_image_panel_bottom.Bind(wx.EVT_SIZE, self.on_board_image_resize)

        self.board_hbox.Add(self.board_image_panel_top, 1, wx.EXPAND | wx.ALL, 8)
        self.board_hbox.Add(self.board_image_panel_bottom, 1, wx.EXPAND | wx.ALL, 8)
        self.board_vbox.Add(self.board_hbox, 1, wx.EXPAND)
        self.tab_board.SetSizer(self.board_vbox)

        # --- Log output ---
        self.log_panel = wx.Panel(panel)
        log_box = wx.BoxSizer(wx.VERTICAL)
        log_header = wx.BoxSizer(wx.HORIZONTAL)
        log_header.Add(wx.StaticText(self.log_panel, label="Log:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.btn_toggle_log = wx.Button(self.log_panel, label="Open Log Window")
        set_button_icon(self.btn_toggle_log, wx.ART_LIST_VIEW)
        log_header.Add(self.btn_toggle_log, 0)
        log_box.Add(log_header, 0, wx.BOTTOM, 4)

        self.log_ctrl = wx.TextCtrl(self.log_panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.log_ctrl.SetMinSize((-1, 120))  # keep compact on main window
        log_box.Add(self.log_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.log_panel.SetSizer(log_box)
        vbox.Add(self.log_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)


        # --- Footer ---
        footer_box = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_author = wx.Button(panel, label="By: Ihysol (Tobias Gent)", style=wx.BU_EXACTFIT)
        self.btn_author.SetForegroundColour(wx.Colour(50, 50, 255))
        self.btn_author.SetBackgroundColour(panel.GetBackgroundColour())
        self.btn_author.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        footer_box.Add(self.btn_author, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 10)
        self.btn_issues = wx.Button(panel, label="Report Bug / Suggest Feature", style=wx.BU_EXACTFIT)
        self.btn_issues.SetForegroundColour(wx.Colour(50, 50, 255))
        self.btn_issues.SetBackgroundColour(panel.GetBackgroundColour())
        self.btn_issues.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        footer_box.Add(self.btn_issues, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 10)
        self.chk_show_log = wx.CheckBox(panel, label="Show log")
        self.lbl_version = wx.StaticText(panel, label=f"Version: {APP_VERSION}")
        footer_box.AddStretchSpacer(1)
        footer_box.Add(self.chk_show_log, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        footer_box.Add(self.lbl_version, 0, wx.ALIGN_CENTER_VERTICAL)
        vbox.Add(footer_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(vbox)
        # Apply persisted log visibility
        cfg = load_config()
        show_log = cfg.get(SHOW_LOG_KEY, True)
        self.chk_show_log.SetValue(show_log)
        self._apply_log_visibility(show_log, save_pref=False)

        # --- Bind events ---
        self.Bind(wx.EVT_BUTTON, lambda e: self.open_url("https://github.com/Ihysol"), self.btn_author)
        self.Bind(wx.EVT_BUTTON, lambda e: self.open_url("https://github.com/Ihysol/kicad-template"), self.btn_issues)
        self.Bind(wx.EVT_BUTTON, self.on_open_project, self.btn_open_project)
        self.Bind(wx.EVT_BUTTON, self.on_refresh_symbols, self.btn_refresh_symbols)
        self.symbol_list.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, self.on_symbol_item_toggled)
        self.Bind(wx.EVT_CHECKBOX, self.on_use_symbol_name_toggled, self.chk_use_symbol_name)
        self.Bind(wx.EVT_CHECKBOX, self.on_toggle_show_log, self.chk_show_log)
        self.Bind(wx.EVT_BUTTON, self.on_delete_selected, self.btn_delete_selected)


        self.Bind(wx.EVT_CHECKBOX, self.on_master_symbols_toggle, self.chk_master_symbols)
        self.Bind(wx.EVT_BUTTON, self.on_export, self.btn_export)
        self.Bind(wx.EVT_BUTTON, self.on_open_output, self.btn_open_output)
        self.Bind(wx.EVT_BUTTON, self.on_select_zip_folder, self.btn_select)
        self.Bind(wx.EVT_BUTTON, self.on_open_folder, self.btn_open)
        self.Bind(wx.EVT_BUTTON, self.on_process, self.btn_process)
        self.Bind(wx.EVT_BUTTON, self.on_purge, self.btn_purge)
        self.Bind(wx.EVT_BUTTON, self.on_drc_update, self.btn_drc)
        self.Bind(wx.EVT_BUTTON, self.on_refresh_zips, self.btn_refresh_zips)
        self.Bind(wx.EVT_CHECKBOX, self.on_master_zip_toggle, self.chk_master_zip)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_tab_changed)
        self.Bind(wx.EVT_BUTTON, self.on_toggle_log, self.btn_toggle_log)
        self.Bind(wx.EVT_BUTTON, self.on_generate_board_images, self.btn_generate_board)
        self.Bind(wx.EVT_BUTTON, self.on_generate_custom_board_images, self.btn_render_custom)
        self.Bind(wx.EVT_BUTTON, self.on_save_board_images, self.btn_save_board)
        self.Bind(wx.EVT_RADIOBOX, self.on_preset_changed, self.preset_radio)
        self.Bind(wx.EVT_CHECKBOX, self.on_auto_border_toggled, self.chk_auto_border)
        self.Bind(wx.EVT_BUTTON, self.on_border_margin_dec, self.btn_border_margin_dec)
        self.Bind(wx.EVT_BUTTON, self.on_border_margin_inc, self.btn_border_margin_inc)
        self.Bind(wx.EVT_TEXT_ENTER, self.on_border_margin_commit, self.txt_border_margin)
        self.txt_border_margin.Bind(wx.EVT_KILL_FOCUS, self.on_border_margin_commit)

    # ---------- Event handlers ----------
    def on_resize_zip_columns(self, event):
        """Keep ZIP list columns evenly split (33% each) when resized."""
        event.Skip()
        total_width = self.zip_file_list.GetClientSize().width
        toggle_col_width = 80  # keep the first checkbox column fixed
        usable_width = max(total_width - toggle_col_width, 0)

        # Split remaining width equally among the 3 visible columns
        col_width = usable_width // 3
        self.zip_file_list.GetColumn(1).SetWidth(col_width)  # Archive Name
        self.zip_file_list.GetColumn(2).SetWidth(col_width)  # Status
        self.zip_file_list.GetColumn(3).SetWidth(col_width)  # Delete

    
    def on_delete_selected(self, event):
        """Delete selected symbols (and linked footprints + 3D models)."""
        model = self.symbol_list.GetStore()
        total = model.GetCount()
        selected = [model.GetValueByRow(i, 1) for i in range(total) if model.GetValueByRow(i, 0)]

        if not selected:
            self.append_log("[WARN] No symbols selected for deletion.")
            return

        dlg = wx.MessageDialog(
            self,
            f"Delete {len(selected)} selected symbol(s) and their linked assets?",
            "Confirm Delete",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()
        self._start_delete_symbols(selected)


    
    def on_zip_delete_clicked(self, event):
        """Delete the ZIP file when double-clicking the Delete column."""
        item = event.GetItem()
        if not item.IsOk():
            return

        model = self.zip_file_list.GetStore()
        row = model.GetRow(item)
        if row < 0:
            return

        col = event.GetColumn()
        if col != 3:
            return

        if row >= len(self.zip_rows):
            return
        zip_path = Path(self.zip_rows[row].get("path", ""))

        dlg = wx.MessageDialog(
            self,
            f"Delete '{zip_path.name}' from the input folder?",
            "Confirm Delete",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()

        try:
            zip_path.unlink(missing_ok=True)
            self.append_log(f"[OK] Deleted {zip_path.name}")
        except Exception as e:
            self.append_log(f"[ERROR] Could not delete {zip_path.name}: {e}")
            return

        self.refresh_zip_list()
    
    def on_use_symbol_name_toggled(self, event):
        """Save preference to backend config."""
        value = self.chk_use_symbol_name.IsChecked()
        cfg = load_config()
        cfg[USE_SYMBOLNAME_KEY] = value
        save_config(cfg)
        state = "enabled" if value else "disabled"
        self.append_log(f"[INFO] 'Use symbol name as footprint/3D model' {state}.")

    def on_auto_border_toggled(self, event):
        """Persist auto-border setting for board image generation/reset."""
        value = self.chk_auto_border.IsChecked()
        cfg = load_config()
        cfg[AUTO_BORDER_KEY] = value
        save_config(cfg)
        state = "enabled" if value else "disabled"
        self.append_log(f"[INFO] Auto-border placement {state}.")

    def _set_border_margin_value(self, value: int):
        value = max(0, int(value))
        self.txt_border_margin.ChangeValue(str(value))
        cfg = load_config()
        cfg[BORDER_MARGIN_KEY] = value
        save_config(cfg)
        if self.chk_auto_border.IsChecked() and getattr(self, "_board_has_images", False):
            self._auto_crop_current_preview(self.active_side)

    def _get_border_margin_value(self) -> int:
        raw = self.txt_border_margin.GetValue().strip()
        if not raw.isdigit():
            return 0
        return max(0, int(raw))

    def on_border_margin_dec(self, event):
        self._set_border_margin_value(self._get_border_margin_value() - 10)

    def on_border_margin_inc(self, event):
        self._set_border_margin_value(self._get_border_margin_value() + 10)

    def on_border_margin_commit(self, event):
        if self.txt_border_margin.IsModified():
            self._set_border_margin_value(self._get_border_margin_value())
        event.Skip()
        
    def open_url(self, url):
        import webbrowser
        webbrowser.open_new_tab(url)

    def on_master_zip_toggle(self, event):
        """Select or deselect all checkboxes in the DataView list."""
        checked = self.chk_master_zip.IsChecked()
        model = self.zip_file_list.GetStore()
        total = model.GetCount()

        # Update all checkbox values
        for row in range(total):
            model.SetValueByRow(checked, row, 0)

        # Force the view to repaint (Windows quirk)
        self.zip_file_list.Refresh()

        # Update label
        self.chk_master_zip.SetLabel("Deselect All" if checked else "Select All")
        event.Skip()


    def on_zip_checkbox_changed(self, event):
        model = self.zip_file_list.GetStore()
        total = model.GetCount()
        checked = sum(1 for row in range(total) if model.GetValueByRow(row, 0))
        all_checked = checked == total and total > 0
        self.chk_master_zip.SetValue(all_checked)
        self.chk_master_zip.SetLabel("Deselect All" if all_checked else "Select All")
        if event:
            event.Skip()


    def on_symbol_item_toggled(self, event):
        model = self.symbol_list.GetStore()
        total = model.GetCount()
        checked = sum(1 for i in range(total) if model.GetValueByRow(i, 0))
        all_checked = checked == total and total > 0
        self.chk_master_symbols.SetValue(all_checked)
        self.chk_master_symbols.SetLabel("Deselect All" if all_checked else "Select All")
        if event:
            event.Skip()

    def on_refresh_symbols(self, event):
        self.refresh_symbol_list_async()

    def on_refresh_zips(self, event):
        self.refresh_zip_list_async()

    def on_master_symbols_toggle(self, event):
        checked = self.chk_master_symbols.IsChecked()
        model = self.symbol_list.GetStore()
        for i in range(model.GetCount()):
            model.SetValueByRow(checked, i, 0)
        self.symbol_list.Refresh()
        self.chk_master_symbols.SetLabel("Deselect All" if checked else "Select All")
        if event:
            event.Skip()

    def append_log(self, text):
        if "[FAIL]" in text or "[ERROR]" in text:
            self.log_ctrl.SetDefaultStyle(wx.TextAttr(wx.RED))
        elif "[OK]" in text or "[SUCCESS]" in text:
            self.log_ctrl.SetDefaultStyle(wx.TextAttr(wx.GREEN))
        elif "[WARN]" in text:
            self.log_ctrl.SetDefaultStyle(wx.TextAttr(wx.Colour(255, 165, 0)))
        else:
            self.log_ctrl.SetDefaultStyle(wx.TextAttr(wx.WHITE))
        self.log_ctrl.AppendText(text + "\n")
        self.log_ctrl.SetDefaultStyle(wx.TextAttr(wx.WHITE))
        if self.log_popup_ctrl:
            self.log_popup_ctrl.AppendText(text + "\n")

    def _log_ui(self, text: str):
        wx.CallAfter(self.append_log, text)

    def _set_board_busy(self, busy: bool):
        self._board_busy = busy
        self.btn_generate_board.Enable(not busy)
        self.btn_render_custom.Enable(not busy and self._board_has_images)
        self.btn_save_board.Enable(not busy and self._board_has_images)
        self.preset_radio.Enable(not busy)
        self.txt_render_w.Enable(not busy)
        self.txt_render_h.Enable(not busy)

    def _normalize_board_section_heights(self):
        """Align heights of the board settings subsections."""
        sections = [
            self.board_sizes_box.GetStaticBox(),
            self.preset_radio,
            self.border_settings_box.GetStaticBox(),
        ]
        max_h = 0
        for section in sections:
            try:
                _, h = section.GetBestSize()
                max_h = max(max_h, h)
            except Exception:
                pass
        if max_h <= 0:
            return
        max_h += 7
        for section in sections:
            try:
                section.SetMinSize((-1, max_h))
            except Exception:
                pass
        if hasattr(self, "board_sizes_row"):
            self.board_sizes_row.Layout()
        if hasattr(self, "board_vbox"):
            self.board_vbox.Layout()


    def _update_board_action_state(self):
        if getattr(self, "_board_busy", False):
            self.btn_render_custom.Enable(False)
            self.btn_save_board.Enable(False)
            return
        self.btn_render_custom.Enable(self._board_has_images)
        self.btn_save_board.Enable(self._board_has_images)

    def _set_board_has_images(self, has_images: bool):
        self._board_has_images = has_images
        self._update_board_action_state()

    def _set_zip_busy(self, busy: bool):
        self._zip_busy = busy
        controls = [
            self.btn_refresh_zips,
            self.btn_process,
            self.btn_purge,
            self.btn_select,
            self.btn_open,
            self.chk_master_zip,
            self.zip_file_list,
        ]
        for ctrl in controls:
            try:
                ctrl.Enable(not busy)
            except Exception:
                pass

    def _set_symbols_busy(self, busy: bool):
        self._sym_busy = busy
        controls = [
            self.btn_refresh_symbols,
            self.btn_export,
            self.btn_delete_selected,
            self.chk_master_symbols,
            self.symbol_list,
        ]
        for ctrl in controls:
            try:
                ctrl.Enable(not busy)
            except Exception:
                pass

    def refresh_symbol_list_async(self):
        if self._sym_busy:
            return
        self.append_log("[INFO] Refreshing project symbol list...")
        self._set_symbols_busy(True)
        ctx = mp.get_context("spawn")
        self._sym_list_queue = ctx.Queue()
        self._sym_list_process = ctx.Process(
            target=_list_symbols_worker,
            args=(self._sym_list_queue,),
        )
        self._sym_list_process.start()
        if not self._sym_list_timer:
            self._sym_list_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_sym_list_poll, self._sym_list_timer)
        self._sym_list_timer.Start(100)

    def _on_sym_list_poll(self, event):
        try:
            status, payload = self._sym_list_queue.get_nowait()
        except Exception:
            return
        self._sym_list_timer.Stop()
        if self._sym_list_process and self._sym_list_process.is_alive():
            self._sym_list_process.join(timeout=0)
        self._set_symbols_busy(False)
        if status == "ok":
            self.refresh_symbol_list(symbols=payload)
            self.append_log("[OK] Project symbol list refreshed.")
        else:
            self.append_log(f"[ERROR] Symbol refresh failed: {payload}")

    def _start_export_symbols(self, selected_symbols):
        if self._sym_busy:
            return
        self._set_symbols_busy(True)
        ctx = mp.get_context("spawn")
        self._sym_export_queue = ctx.Queue()
        self._sym_export_process = ctx.Process(
            target=_export_symbols_worker,
            args=(selected_symbols, self._sym_export_queue),
        )
        self._sym_export_process.start()
        if not self._sym_export_timer:
            self._sym_export_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_sym_export_poll, self._sym_export_timer)
        self._sym_export_timer.Start(200)

    def _on_sym_export_poll(self, event):
        try:
            status, success, _ = self._sym_export_queue.get_nowait()
        except Exception:
            return
        self._sym_export_timer.Stop()
        if self._sym_export_process and self._sym_export_process.is_alive():
            self._sym_export_process.join(timeout=0)
        self._set_symbols_busy(False)
        if status == "ok" and success:
            self.append_log("[OK] Export complete.")
        elif status == "ok":
            self.append_log("[FAIL] Export failed. See log for details.")
        else:
            self.append_log(f"[ERROR] Export failed: {success}")

    def _start_delete_symbols(self, selected_symbols):
        if self._sym_busy:
            return
        self._set_symbols_busy(True)
        ctx = mp.get_context("spawn")
        self._sym_delete_queue = ctx.Queue()
        self._sym_delete_process = ctx.Process(
            target=_delete_symbols_worker,
            args=(selected_symbols, self._sym_delete_queue),
        )
        self._sym_delete_process.start()
        if not self._sym_delete_timer:
            self._sym_delete_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_sym_delete_poll, self._sym_delete_timer)
        self._sym_delete_timer.Start(200)

    def _on_sym_delete_poll(self, event):
        try:
            status, deleted_syms, deleted_fp, deleted_3d = self._sym_delete_queue.get_nowait()
        except Exception:
            return
        self._sym_delete_timer.Stop()
        if self._sym_delete_process and self._sym_delete_process.is_alive():
            self._sym_delete_process.join(timeout=0)
        self._set_symbols_busy(False)
        if status == "ok":
            self.append_log(
                f"[INFO] Deleted {deleted_syms} symbols, {deleted_fp} footprints, {deleted_3d} 3D models."
            )
            self.refresh_symbol_list_async()
        else:
            self.append_log(f"[ERROR] Delete failed: {deleted_syms}")

    def _set_drc_busy(self, busy: bool):
        self._drc_busy = busy
        try:
            self.btn_drc.Enable(not busy)
        except Exception:
            pass

    def _start_drc_update(self):
        if self._drc_busy:
            return
        self._set_drc_busy(True)
        ctx = mp.get_context("spawn")
        self._drc_queue = ctx.Queue()
        self._drc_process = ctx.Process(target=_update_drc_worker, args=(self._drc_queue,))
        self._drc_process.start()
        if not self._drc_timer:
            self._drc_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_drc_poll, self._drc_timer)
        self._drc_timer.Start(200)

    def _on_drc_poll(self, event):
        try:
            status, payload = self._drc_queue.get_nowait()
        except Exception:
            return
        self._drc_timer.Stop()
        if self._drc_process and self._drc_process.is_alive():
            self._drc_process.join(timeout=0)
        self._set_drc_busy(False)
        if status == "ok" and payload:
            self.append_log("[OK] DRC updated successfully.")
        elif status == "ok":
            self.append_log("[FAIL] DRC update failed. See log for details.")
        else:
            self.append_log(f"[ERROR] DRC update failed: {payload}")

    def refresh_zip_list_async(self):
        """Scan ZIPs in a separate process and update the list."""
        if self._zip_busy:
            return
        self.append_log("[INFO] Scanning ZIP archives...")
        self._set_zip_busy(True)
        self._start_zip_scan()

    def _start_zip_scan(self):
        ctx = mp.get_context("spawn")
        self._zip_scan_queue = ctx.Queue()
        self._zip_scan_process = ctx.Process(
            target=_scan_zip_folder_worker,
            args=(str(self.current_folder), self._zip_scan_queue),
        )
        self._zip_scan_process.start()
        if not self._zip_scan_timer:
            self._zip_scan_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_zip_scan_poll, self._zip_scan_timer)
        self._zip_scan_timer.Start(100)

    def _on_zip_scan_poll(self, event):
        try:
            status, payload = self._zip_scan_queue.get_nowait()
        except Exception:
            return
        self._zip_scan_timer.Stop()
        if self._zip_scan_process and self._zip_scan_process.is_alive():
            self._zip_scan_process.join(timeout=0)
        self._set_zip_busy(False)
        if status == "ok":
            self.refresh_zip_list(rows=payload)
            self.append_log("[OK] ZIP archive list refreshed.")
        else:
            self.append_log(f"[ERROR] ZIP scan failed: {payload}")

    def _start_zip_process(self, paths, is_purge: bool, use_symbol_name: bool):
        if self._zip_busy:
            return
        self.append_log("[INFO] Processing ZIP archives in background...")
        self._set_zip_busy(True)
        ctx = mp.get_context("spawn")
        self._zip_process_queue = ctx.Queue()
        self._zip_process = ctx.Process(
            target=_process_archives_worker,
            args=(
                [str(p) for p in paths],
                is_purge,
                False,
                use_symbol_name,
                self._zip_process_queue,
            ),
        )
        self._zip_process.start()
        if not self._zip_process_timer:
            self._zip_process_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_zip_process_poll, self._zip_process_timer)
        self._zip_process_timer.Start(200)

    def _on_zip_process_poll(self, event):
        try:
            status, payload = self._zip_process_queue.get_nowait()
        except Exception:
            return
        self._zip_process_timer.Stop()
        if self._zip_process and self._zip_process.is_alive():
            self._zip_process.join(timeout=0)
        self._set_zip_busy(False)
        if status == "ok" and payload:
            self.append_log("[OK] Action complete. Refreshing lists...")
            self.refresh_zip_list_async()
            self.refresh_symbol_list()
        elif status == "ok":
            self.append_log("[FAIL] Action failed. See log for details.")
        else:
            self.append_log(f"[ERROR] Action failed: {payload}")

    def _load_and_set_board_images(self, top_path: str | None, bottom_path: str | None):
        try:
            bmp_top = wx.Bitmap(wx.Image(top_path)) if top_path else None
            bmp_bottom = wx.Bitmap(wx.Image(bottom_path)) if bottom_path else None
            self._set_board_images(bmp_top, bmp_bottom)
            if top_path:
                self.last_top_image_path = top_path
            if bottom_path:
                self.last_bottom_image_path = bottom_path
        except Exception as e:
            self.append_log(f"[WARN] Could not load PNG preview: {e}")

    def clear_log(self):
        self.log_ctrl.Clear()

    def on_select_zip_folder(self, event):
        dlg = wx.DirDialog(
            self,
            "Select Folder Containing ZIP Archives",
            str(self.current_folder),
            style=wx.DD_DIR_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            self.current_folder = Path(dlg.GetPath())
            self.refresh_zip_list_async()
        dlg.Destroy()

    def on_open_folder(self, event):
        open_folder_in_explorer(self.current_folder)

    def on_process(self, event):
        self.run_process_action(is_purge=False)

    def on_purge(self, event):
        self.run_process_action(is_purge=True)

    def on_export(self, event):
        selected_symbols = self.collect_selected_symbols_for_export()
        if not selected_symbols:
            self.append_log("[WARN] No symbols selected for export.")
            return
        self._start_export_symbols(selected_symbols)

    def on_open_output(self, event):
        open_output_folder()

    def on_drc_update(self, event):
        self._start_drc_update()

    def on_tab_changed(self, event):
        """Automatically refresh the correct list when switching tabs."""
        new_sel = self.notebook.GetSelection()
        if hasattr(self, "_last_tab") and new_sel == self._last_tab:
            event.Skip()
            return
        self._last_tab = new_sel
        sel = self.notebook.GetSelection()
        tab_label = self.notebook.GetPageText(sel)

        if "Import ZIP" in tab_label:
            self.refresh_zip_list_async()

        elif "Export Project" in tab_label:
            self.refresh_symbol_list_async()

        elif "DRC" in tab_label:
            self.append_log("[INFO] DRC Manager ready.")

        elif "Generate Images from PCB" in tab_label:
            self.append_log("[INFO] Generate Images from PCB ready.")

        elif "mouser" in tab_label.lower():
            self.append_log("[INFO] Mouser Auto Order ready.")
        event.Skip()

    def _find_project_file(self) -> Path | None:
        """Return the first .kicad_pro in PROJECT_DIR (if any)."""
        candidates = sorted(PROJECT_DIR.glob("*.kicad_pro"))
        return candidates[0] if candidates else None

    def _find_project_pcb(self) -> Path | None:
        """Return preferred PCB file (matching .kicad_pro stem if possible)."""
        proj_files = sorted(PROJECT_DIR.glob("*.kicad_pro"))
        if proj_files:
            preferred = PROJECT_DIR / f"{proj_files[0].stem}.kicad_pcb"
            if preferred.exists():
                return preferred
        fallback = PROJECT_DIR / "Project.kicad_pcb"
        if fallback.exists():
            return fallback
        top_level = sorted(PROJECT_DIR.glob("*.kicad_pcb"))
        if top_level:
            return top_level[0]
        nested = sorted(PROJECT_DIR.rglob("*.kicad_pcb"))
        if nested:
            return nested[0]
        return None

    def _count_copper_layers(self, pcb_path: Path) -> int | None:
        """Return number of copper layers in PCB file, or None on parse error."""
        try:
            with pcb_path.open("r", encoding="utf-8") as f:
                sexpr = loads(f.read())
        except Exception as e:
            self.append_log(f"[ERROR] Failed to parse PCB file: {e}")
            return None

        layers_block = None
        for e in sexpr:
            if isinstance(e, list) and e and e[0] == Symbol("layers"):
                layers_block = e
                break
        if not layers_block:
            return 0

        copper_layers = [
            layer
            for layer in layers_block[1:]
            if isinstance(layer, list)
            and len(layer) > 1
            and str(layer[1]).endswith(".Cu")
        ]
        return len(copper_layers)

    def _find_kicad_cli(self) -> str | None:
        """Locate kicad-cli via PATH or common install locations."""
        return _find_kicad_cli_path()

    def _find_svg_converter(self) -> tuple[str | None, str | None]:
        """
        Return (tool, mode) for converting SVG -> PNG.
        mode in {"magick", "rsvg", "inkscape"}.
        """
        magick = shutil.which("magick")
        if magick:
            return magick, "magick"
        rsvg = shutil.which("rsvg-convert")
        if rsvg:
            return rsvg, "rsvg"
        inkscape = shutil.which("inkscape")
        if inkscape:
            return inkscape, "inkscape"
        return None, None

    def _make_placeholder_bitmap(self, size, text: str) -> wx.Bitmap:
        width, height = size
        bmp = wx.Bitmap(width, height)
        dc = wx.MemoryDC(bmp)
        bg = wx.Colour(180, 180, 180)
        border = wx.Colour(140, 140, 140)
        text_col = wx.Colour(30, 30, 30)
        # Explicit fill to avoid platform defaults showing white.
        dc.SetBrush(wx.Brush(bg))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.DrawRectangle(0, 0, width, height)
        dc.SetPen(wx.Pen(border))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(0, 0, width, height)
        # Slight shadow + bold font for readability.
        font = self.GetFont()
        if font and font.IsOk():
            font.SetWeight(wx.FONTWEIGHT_BOLD)
            dc.SetFont(font)
        dc.SetTextForeground(wx.Colour(230, 230, 230))
        dc.DrawLabel(text, wx.Rect(0, 1, width, height), alignment=wx.ALIGN_CENTER)
        dc.SetTextForeground(text_col)
        dc.DrawLabel(text, wx.Rect(0, 0, width, height), alignment=wx.ALIGN_CENTER)
        dc.SelectObject(wx.NullBitmap)
        return bmp

    def _set_board_images(self, top_bmp: wx.Bitmap | None, bottom_bmp: wx.Bitmap | None):
        if top_bmp and top_bmp.IsOk():
            self.board_source_top = top_bmp
            self.board_image_panel_top.set_bitmap(top_bmp)
            self._update_image_label(self.board_image_label_top, top_bmp, self.board_image_panel_top)
        if bottom_bmp and bottom_bmp.IsOk():
            self.board_source_bottom = bottom_bmp
            self.board_image_panel_bottom.set_bitmap(bottom_bmp)
            self._update_image_label(self.board_image_label_bottom, bottom_bmp, self.board_image_panel_bottom)

    def _update_image_label(self, label: wx.StaticText, bmp: wx.Bitmap, panel: wx.Panel):
        label.SetLabel(f"{bmp.GetWidth()} x {bmp.GetHeight()} px")
        self._position_image_label(label, panel)

    def _position_image_label(self, label: wx.StaticText, panel: wx.Panel):
        padding = 6
        size = label.GetBestSize()
        panel_size = panel.GetClientSize()
        x = max(panel_size.width - size.width - padding, 0)
        y = max(panel_size.height - size.height - padding, 0)
        label.SetPosition((x, y))
        label.Raise()

    def on_board_image_resize(self, event):
        event.Skip()
        self._set_board_images(
            getattr(self, "board_source_top", None),
            getattr(self, "board_source_bottom", None),
        )
        self._position_image_label(self.board_image_label_top, self.board_image_panel_top)
        self._position_image_label(self.board_image_label_bottom, self.board_image_panel_bottom)

    def _get_crop_values(self) -> tuple[int, int, int, int]:
        return self.board_image_panel_top.get_crop()

    def _set_crop(self, crop: tuple[int, int, int, int]):
        self.board_image_panel_top.set_crop(crop)
        self.board_image_panel_bottom.set_crop(crop)

    def _set_active_preview(self, side: str):
        self.active_side = side
        active_color = wx.Colour(230, 240, 255)
        inactive_color = self.tab_board.GetBackgroundColour()
        if side == "top":
            self.board_image_panel_top.SetBackgroundColour(active_color)
            self.board_image_panel_bottom.SetBackgroundColour(inactive_color)
        else:
            self.board_image_panel_top.SetBackgroundColour(inactive_color)
            self.board_image_panel_bottom.SetBackgroundColour(active_color)
        self.board_image_panel_top.Refresh()
        self.board_image_panel_bottom.Refresh()

    def _set_render_preset(self, width: int, height: int):
        self.txt_render_w.SetValue(str(width))
        self.txt_render_h.SetValue(str(height))

    def on_preset_changed(self, event):
        idx = self.preset_radio.GetSelection()
        presets = {
            0: (1280, 720),
            1: (1920, 1080),
            2: (2560, 1440),
            3: (3840, 2160),
        }
        if idx in presets:
            w, h = presets[idx]
            self._set_render_preset(w, h)
        cfg = load_config()
        cfg[RENDER_PRESET_KEY] = idx
        save_config(cfg)

    def _auto_crop_from_alpha(self, img: wx.Image, margin_px: int = 20) -> tuple[int, int, int, int] | None:
        """Return crop rectangle in percent based on non-transparent pixels."""
        if not img.HasAlpha():
            return None
        w, h = img.GetWidth(), img.GetHeight()
        if w <= 0 or h <= 0:
            return None
        margin_px = max(0, min(int(margin_px), min(w, h) // 2))
        data = img.GetAlpha()
        if data is None:
            return None

        min_x, min_y = w, h
        max_x, max_y = -1, -1
        idx = 0
        for y in range(h):
            for x in range(w):
                if data[idx] > 0:
                    if x < min_x: min_x = x
                    if y < min_y: min_y = y
                    if x > max_x: max_x = x
                    if y > max_y: max_y = y
                idx += 1
        if max_x < min_x or max_y < min_y:
            return None

        min_x = max(min_x - margin_px, 0)
        min_y = max(min_y - margin_px, 0)
        max_x = min(max_x + margin_px, w - 1)
        max_y = min(max_y + margin_px, h - 1)

        x0 = int(min_x * 100 / w)
        x1 = int((max_x + 1) * 100 / w)
        y0 = int(min_y * 100 / h)
        y1 = int((max_y + 1) * 100 / h)
        x0 = max(0, min(100, x0))
        x1 = max(0, min(100, x1))
        y0 = max(0, min(100, y0))
        y1 = max(0, min(100, y1))
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, x1, y0, y1)

    def _auto_crop_current_preview(self, side: str):
        """Auto-fit the crop rectangle based on current preview image alpha."""
        if not self.chk_auto_border.IsChecked():
            self._set_crop((0, 100, 0, 100))
            return
        bmp = self.board_source_top if side == "top" else self.board_source_bottom
        if not bmp or not bmp.IsOk():
            self.append_log("[WARN] No preview image available to auto-fit.")
            return
        img = bmp.ConvertToImage()
        crop = self._auto_crop_from_alpha(img, margin_px=self._get_border_margin_value())
        if crop:
            self._set_crop(crop)
        else:
            self.append_log("[WARN] Auto-fit requires an image with transparency. Resetting to full frame.")
            self._set_crop((0, 100, 0, 100))

    def _parse_size_field(self, ctrl: wx.TextCtrl, label: str) -> int | None:
        raw = ctrl.GetValue().strip()
        if not raw.isdigit():
            self.append_log(f"[ERROR] {label} must be a positive integer.")
            return None
        val = int(raw)
        if val <= 0:
            self.append_log(f"[ERROR] {label} must be greater than 0.")
            return None
        return val

    def on_generate_custom_board_images(self, event):
        """Crop existing generated images using the current crop frame."""
        if getattr(self, "_board_busy", False):
            return
        self._set_board_busy(True)

        def worker():
            log = self._log_ui
            top = getattr(self, "last_top_image_path", None) or getattr(self, "base_top_image_path", None)
            bottom = getattr(self, "last_bottom_image_path", None) or getattr(self, "base_bottom_image_path", None)
            if not top or not bottom or not Path(top).exists() or not Path(bottom).exists():
                log("[ERROR] No generated PNGs available to crop. Click 'Generate Board Images' first.")
                wx.CallAfter(self._set_board_busy, False)
                return

            wx.CallAfter(self.board_image_panel_top.set_loading, True, "Cropping...", 0)
            wx.CallAfter(self.board_image_panel_bottom.set_loading, True, "Cropping...", 0)

            top_path = Path(top)
            bottom_path = Path(bottom)
            cropped_top = self._apply_crop_to_png(
                top_path,
                top_path.with_name(f"{top_path.stem}_crop{top_path.suffix}"),
                True,
                log_fn=log,
            )
            cropped_bottom = self._apply_crop_to_png(
                bottom_path,
                bottom_path.with_name(f"{bottom_path.stem}_crop{bottom_path.suffix}"),
                True,
                log_fn=log,
            )
            if not cropped_top or not cropped_bottom:
                wx.CallAfter(self.board_image_panel_top.set_loading, False)
                wx.CallAfter(self.board_image_panel_bottom.set_loading, False)
                wx.CallAfter(self._set_board_busy, False)
                return

            wx.CallAfter(self._load_and_set_board_images, str(cropped_top), str(cropped_bottom))
            wx.CallAfter(self.board_image_panel_top.set_loading, True, "Cropping...", 100)
            wx.CallAfter(self.board_image_panel_bottom.set_loading, True, "Cropping...", 100)
            wx.CallAfter(self.board_image_panel_top.set_loading, False)
            wx.CallAfter(self.board_image_panel_bottom.set_loading, False)
            log("[OK] Cropped previews updated.")
            wx.CallAfter(self._set_crop, (0, 100, 0, 100))
            wx.CallAfter(self._set_board_busy, False)

        threading.Thread(target=worker, daemon=True).start()

    def on_generate_board_images(self, event):
        """Generate top/bottom board images via kicad-cli (full size, no crop)."""
        if getattr(self, "_board_busy", False):
            return
        self._set_board_busy(True)
        threading.Thread(
            target=self._render_board_images,
            args=((2560, 1440), (2560, 1440), False),
            daemon=True,
        ).start()

    def on_save_board_images(self, event):
        """Save last generated images as board_preview_top/bottom.png."""
        top = getattr(self, "last_top_image_path", None)
        bottom = getattr(self, "last_bottom_image_path", None)
        if not top or not bottom or not Path(top).exists() or not Path(bottom).exists():
            self.append_log("[ERROR] No generated PNGs available to save.")
            return

        dst_dir = PROJECT_DIR.parent / "Docs" / "img"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_top = dst_dir / "board_preview_top.png"
        dst_bottom = dst_dir / "board_preview_bottom.png"
        try:
            shutil.copy2(top, dst_top)
            shutil.copy2(bottom, dst_bottom)
        except Exception as e:
            self.append_log(f"[ERROR] Failed to save previews: {e}")
            return
        self.append_log(f"[OK] Saved previews: {dst_top.name}, {dst_bottom.name}")

    def _apply_crop_to_png(
        self,
        src: Path,
        dst: Path,
        apply_crop: bool,
        log_fn=None,
    ) -> Path | None:
        if not src.exists():
            return None
        try:
            img = wx.Image(str(src))
        except Exception as e:
            if log_fn:
                log_fn(f"[WARN] Could not open image for cropping: {e}")
            else:
                self.append_log(f"[WARN] Could not open image for cropping: {e}")
            return None

        x0, x1, y0, y1 = self._get_crop_values()
        if not apply_crop:
            return src
        if (x0, x1, y0, y1) == (0, 100, 0, 100):
            return src

        w, h = img.GetWidth(), img.GetHeight()
        cx0 = int(w * x0 / 100)
        cx1 = int(w * x1 / 100)
        cy0 = int(h * y0 / 100)
        cy1 = int(h * y1 / 100)
        if cx1 <= cx0 or cy1 <= cy0:
            if log_fn:
                log_fn("[ERROR] Invalid crop rectangle.")
            else:
                self.append_log("[ERROR] Invalid crop rectangle.")
            return None

        cropped = img.GetSubImage(wx.Rect(cx0, cy0, cx1 - cx0, cy1 - cy0))
        if not cropped.IsOk():
            if log_fn:
                log_fn("[ERROR] Crop failed.")
            else:
                self.append_log("[ERROR] Crop failed.")
            return None
        cropped.SaveFile(str(dst), wx.BITMAP_TYPE_PNG)
        return dst

    def _render_board_images(self, top_size: tuple[int, int], bottom_size: tuple[int, int], apply_crop: bool):
        log = self._log_ui
        ui = wx.CallAfter
        def finish():
            ui(self.board_image_panel_top.set_loading, False)
            ui(self.board_image_panel_bottom.set_loading, False)
            ui(self._set_board_busy, False)

        pcb = self._find_project_pcb()
        if not pcb:
            log("[ERROR] No .kicad_pcb file found in project.")
            finish()
            return
        layer_count = self._count_copper_layers(pcb)
        if layer_count is None:
            finish()
            return
        if layer_count == 0:
            log("[ERROR] No copper layers found. No board to render.")
            finish()
            return

        kicad_cli = self._find_kicad_cli()
        if not kicad_cli:
            log("[ERROR] kicad-cli not found. Please add it to PATH or install KiCad.")
            finish()
            return

        if not hasattr(self, "board_temp_dir"):
            self.board_temp_dir = Path(tempfile.mkdtemp(prefix="kicad_board_"))
        output_dir = self.board_temp_dir
        top_svg = output_dir / f"{pcb.stem}_top.svg"
        bottom_svg = output_dir / f"{pcb.stem}_bottom.svg"
        top_png = output_dir / f"{pcb.stem}_top.png"
        bottom_png = output_dir / f"{pcb.stem}_bottom.png"

        log(f"[INFO] Exporting board images from {pcb.name} ...")

        def set_status(side: str, text: str, progress: int):
            panel = self.board_image_panel_top if side == "top" else self.board_image_panel_bottom
            ui(panel.set_loading, True, text, progress)

        set_status("top", "Preparing...", 2)
        set_status("bottom", "Preparing...", 2)

        # Prefer direct render (PNG). Fallback to SVG export if render is unavailable.
        cmd_render_top = [
            kicad_cli,
            "pcb",
            "render",
            "--output",
            str(top_png),
            "--width",
            str(top_size[0]),
            "--height",
            str(top_size[1]),
            "--side",
            "top",
            "--background",
            "transparent",
            "--quality",
            "high",
            str(pcb),
        ]
        cmd_render_bottom = [
            kicad_cli,
            "pcb",
            "render",
            "--output",
            str(bottom_png),
            "--width",
            str(bottom_size[0]),
            "--height",
            str(bottom_size[1]),
            "--side",
            "bottom",
            "--background",
            "transparent",
            "--quality",
            "high",
            str(pcb),
        ]

        results = {}

        def run_render(side: str, cmd):
            try:
                set_status(side, f"Preparing {side} render...", 10)
                set_status(side, f"Starting {side} render...", 20)
                res = subprocess.run(cmd, capture_output=True, text=True, **_subprocess_no_window_kwargs())
                results[side] = res
                set_status(side, f"Finishing {side} render...", 60)
            except Exception as e:
                results[side] = e

        t_top = threading.Thread(target=run_render, args=("top", cmd_render_top))
        t_bottom = threading.Thread(target=run_render, args=("bottom", cmd_render_bottom))
        t_top.start()
        t_bottom.start()
        t_top.join()
        t_bottom.join()

        res_top = results.get("top")
        res_bottom = results.get("bottom")
        if isinstance(res_top, Exception) or isinstance(res_bottom, Exception):
            err = res_top if isinstance(res_top, Exception) else res_bottom
            log(f"[ERROR] kicad-cli call failed: {err}")
            finish()
            return
        set_status("top", "Post-processing...", 75)
        set_status("bottom", "Post-processing...", 75)

        if res_top.returncode == 0 and res_bottom.returncode == 0 and top_png.exists():
            self.base_top_image_path = str(top_png)
            self.base_bottom_image_path = str(bottom_png)
            crop_top = self._apply_crop_to_png(
                top_png, output_dir / f"{pcb.stem}_top_crop.png", apply_crop, log_fn=log
            )
            crop_bottom = self._apply_crop_to_png(
                bottom_png, output_dir / f"{pcb.stem}_bottom_crop.png", apply_crop, log_fn=log
            )
            try:
                img_top = wx.Image(str(crop_top or top_png))
                if not apply_crop:
                    if self.chk_auto_border.IsChecked():
                        auto_crop = self._auto_crop_from_alpha(img_top, margin_px=self._get_border_margin_value())
                        if auto_crop:
                            ui(self._set_crop, auto_crop)
                ui(
                    self._load_and_set_board_images,
                    str(crop_top or top_png),
                    str(crop_bottom or bottom_png),
                )
                set_status("top", "Loading preview...", 95)
                set_status("bottom", "Loading preview...", 95)
                ui(self._set_board_has_images, True)
            except Exception as e:
                log(f"[WARN] Could not load PNG preview: {e}")
            if apply_crop and (crop_top or crop_bottom):
                log(f"[OK] Board images saved: {top_png.name}, {bottom_png.name} (cropped)")
            else:
                log(f"[OK] Board images saved: {top_png.name}, {bottom_png.name}")
            finish()
            return

        render_err = (res_top.stderr or res_top.stdout or "").strip()
        if render_err:
            log(f"[WARN] Render failed, falling back to SVG export: {render_err}")
            set_status("top", "Preparing SVG export...", 15)
            set_status("bottom", "Preparing SVG export...", 15)

        cmd_top = [
            kicad_cli,
            "pcb",
            "export",
            "svg",
            str(pcb),
            "--output",
            str(top_svg),
            "--layers",
            "F.Cu,F.SilkS,Edge.Cuts",
        ]
        cmd_bottom = [
            kicad_cli,
            "pcb",
            "export",
            "svg",
            str(pcb),
            "--output",
            str(bottom_svg),
            "--layers",
            "B.Cu,B.SilkS,Edge.Cuts",
        ]

        svg_results = {}

        def run_svg_export(side: str, cmd):
            try:
                set_status(side, "Starting SVG export...", 25)
                res = subprocess.run(cmd, capture_output=True, text=True, **_subprocess_no_window_kwargs())
                svg_results[side] = res
                set_status(side, "Finishing SVG export...", 55)
            except Exception as e:
                svg_results[side] = e

        t_svg_top = threading.Thread(target=run_svg_export, args=("top", cmd_top))
        t_svg_bottom = threading.Thread(target=run_svg_export, args=("bottom", cmd_bottom))
        t_svg_top.start()
        t_svg_bottom.start()
        t_svg_top.join()
        t_svg_bottom.join()

        res_top = svg_results.get("top")
        res_bottom = svg_results.get("bottom")
        if isinstance(res_top, Exception) or isinstance(res_bottom, Exception):
            err = res_top if isinstance(res_top, Exception) else res_bottom
            log(f"[ERROR] kicad-cli call failed: {err}")
            finish()
            return
        set_status("top", "SVG export done...", 70)
        set_status("bottom", "SVG export done...", 70)

        if res_top.returncode != 0:
            err = res_top.stderr.strip() or res_top.stdout.strip() or "Unknown error"
            log(f"[ERROR] Top image export failed: {err}")
            finish()
            return

        if res_bottom.returncode != 0:
            err = res_bottom.stderr.strip() or res_bottom.stdout.strip() or "Unknown error"
            log(f"[ERROR] Bottom image export failed: {err}")
            finish()
            return

        converter, mode = self._find_svg_converter()
        if converter and top_svg.exists() and bottom_svg.exists():
            log("[INFO] Converting SVGs to PNG for preview...")
            set_status("top", "Preparing conversion...", 75)
            set_status("bottom", "Preparing conversion...", 75)
            try:
                def run_convert(side: str, src_svg: Path, dst_png: Path):
                    set_status(side, "Converting...", 85)
                    if mode == "magick":
                        subprocess.run(
                            [converter, str(src_svg), str(dst_png)],
                            capture_output=True,
                            text=True,
                            **_subprocess_no_window_kwargs(),
                        )
                    elif mode == "rsvg":
                        subprocess.run(
                            [converter, str(src_svg), "-o", str(dst_png)],
                            capture_output=True,
                            text=True,
                            **_subprocess_no_window_kwargs(),
                        )
                    elif mode == "inkscape":
                        subprocess.run(
                            [converter, str(src_svg), "--export-type=png", f"--export-filename={dst_png}"],
                            capture_output=True,
                            text=True,
                            **_subprocess_no_window_kwargs(),
                        )

                t_conv_top = threading.Thread(target=run_convert, args=("top", top_svg, top_png))
                t_conv_bottom = threading.Thread(target=run_convert, args=("bottom", bottom_svg, bottom_png))
                t_conv_top.start()
                t_conv_bottom.start()
                t_conv_top.join()
                t_conv_bottom.join()
            except Exception as e:
                log(f"[WARN] SVG conversion failed: {e}")
            set_status("top", "Finishing conversion...", 92)
            set_status("bottom", "Finishing conversion...", 92)

        if top_png.exists():
            self.base_top_image_path = str(top_png)
            self.base_bottom_image_path = str(bottom_png)
            crop_top = self._apply_crop_to_png(
                top_png, output_dir / f"{pcb.stem}_top_crop.png", apply_crop, log_fn=log
            )
            crop_bottom = self._apply_crop_to_png(
                bottom_png, output_dir / f"{pcb.stem}_bottom_crop.png", apply_crop, log_fn=log
            )
            try:
                ui(
                    self._load_and_set_board_images,
                    str(crop_top or top_png),
                    str(crop_bottom or bottom_png),
                )
                set_status("top", "Loading preview...", 98)
                set_status("bottom", "Loading preview...", 98)
                ui(self._set_board_has_images, True)
            except Exception as e:
                log(f"[WARN] Could not load PNG preview: {e}")
        else:
            ui(
                self._set_board_images,
                self._make_placeholder_bitmap((520, 360), "Top SVG saved (no PNG preview)"),
                self._make_placeholder_bitmap((520, 360), "Bottom SVG saved (no PNG preview)"),
            )
            set_status("top", "Loading preview...", 98)
            set_status("bottom", "Loading preview...", 98)

        log(
            f"[OK] Board images saved: {top_svg.name}, {bottom_svg.name}"
            + (f" (PNG preview: {top_png.name}, {bottom_png.name})" if top_png.exists() else "")
        )
        finish()

    def on_toggle_log(self, event):
        """Open or focus a separate log window without resizing main layout."""
        if self.log_popup and self.log_popup.IsShown():
            self.log_popup.Raise()
            self.log_popup.Restore()
            return

        dlg = wx.Frame(self, title="Log", size=(900, 500))
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        ctrl = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        ctrl.SetValue(self.log_ctrl.GetValue())
        sizer.Add(ctrl, 1, wx.EXPAND | wx.ALL, 5)
        panel.SetSizer(sizer)
        dlg.Centre()

        def on_close(evt):
            self.log_popup = None
            self.log_popup_ctrl = None
            evt.Skip()

        dlg.Bind(wx.EVT_CLOSE, on_close)
        self.log_popup = dlg
        self.log_popup_ctrl = ctrl
        dlg.Show()

    def _apply_log_visibility(self, show: bool, save_pref: bool = True):
        """Show/hide inline log panel and save preference."""
        self.log_panel.Show(show)
        self.log_panel.SetMinSize((-1, 120 if show else 0))
        self.btn_toggle_log.Enable(show)
        if not show and self.log_popup:
            self.log_popup.Destroy()
            self.log_popup = None
            self.log_popup_ctrl = None
        # Re-layout immediately so other content expands/collapses without restart
        self.log_panel.Layout()
        if hasattr(self, "panel"):
            self.panel.Layout()
        self.Layout()
        self.SendSizeEvent()
        if save_pref:
            cfg = load_config()
            cfg[SHOW_LOG_KEY] = show
            save_config(cfg)

    def on_toggle_show_log(self, event):
        """Handle 'Show log' checkbox toggling."""
        show = self.chk_show_log.IsChecked()
        self._apply_log_visibility(show)

    def on_open_project(self, event):
        """Open the KiCad project file with the default application."""
        if not self.project_file or not self.project_file.exists():
            self.append_log("[ERROR] No .kicad_pro file found to open.")
            return
        try:
            if os.name == "nt":
                os.startfile(str(self.project_file))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.project_file)])
            else:
                subprocess.Popen(["xdg-open", str(self.project_file)])
            self.append_log(f"[OK] Opening project: {self.project_file.name}")
        except Exception as e:
            self.append_log(f"[ERROR] Could not open project: {e}")

    # ---------- Data helpers ----------
    def _make_status_icon(self, colour: wx.Colour, size=12):
        bmp = wx.Bitmap(size, size)
        dc = wx.MemoryDC(bmp)
        dc.SetBrush(wx.Brush(colour))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.DrawRectangle(0, 0, size, size)
        dc.SelectObject(wx.NullBitmap)
        return bmp

    def _make_delete_icon(self, size=16):
        bmp = wx.ArtProvider.GetBitmap(wx.ART_DELETE, wx.ART_MENU, (size, size))
        if bmp and bmp.IsOk():
            return bmp
        return self._make_status_icon(wx.Colour(220, 50, 50), size=min(size, 12))

    def refresh_zip_list(self, rows=None):
        """Scan current folder and rebuild ZIP DataView list."""
        self.current_folder.mkdir(parents=True, exist_ok=True)
        self.current_folder_txt.SetLabel(f"Current Folder: {self.current_folder}")
        if rows is None:
            rows = scan_zip_folder(self.current_folder)
        self.zip_rows = rows

        model = self.zip_file_list.GetStore()
        model.DeleteAllItems()

        status_styles = {
            "NEW": (wx.Colour(0, 255, 0), "NEW"),
            "PARTIAL": (wx.Colour(255, 160, 0), "IN PROJECT"),
            "MISSING_SYMBOL": (wx.Colour(255, 80, 80), "Missing Symbol (cannot import)"),
            "MISSING_FOOTPRINT": (wx.Colour(255, 80, 80), "Missing Footprint (cannot import)"),
            "ERROR": (wx.Colour(255, 80, 80), "ERROR"),
            "NONE": (wx.Colour(180, 180, 180), "MISSING"),
        }
        for row in self.zip_rows:
            raw_status = row.get("status", "")
            colour, text = status_styles.get(raw_status, (wx.Colour(180, 180, 180), raw_status or "-"))
            is_disabled = raw_status in ("MISSING_SYMBOL", "MISSING_FOOTPRINT")
            icontext = wx.dataview.DataViewIconText(f" {text}", self._make_status_icon(colour))
            model.AppendItem(
                [
                    not is_disabled and raw_status != "PARTIAL",
                    row.get("name", "unknown.zip"),
                    icontext,
                    "double-click to delete",
                ]
            )

        self.chk_master_zip.SetValue(False)
        self.chk_master_zip.SetLabel("Select All")
        self.zip_file_list.Refresh()

    def refresh_symbol_list(self, symbols=None):
        if symbols is None:
            symbols = list_project_symbols()
        self.symbol_list.GetStore().DeleteAllItems()
        for sym in symbols:
            self.symbol_list.GetStore().AppendItem([False, sym])
        self.chk_master_symbols.SetValue(False)
        self.chk_master_symbols.SetLabel("Select All")

    def collect_selected_symbols_for_export(self):
        model = self.symbol_list.GetStore()
        return [model.GetValueByRow(i, 1) for i in range(model.GetCount()) if model.GetValueByRow(i, 0)]

    def _get_selected_zip_paths(self) -> list[Path]:
        model = self.zip_file_list.GetStore()
        selected: list[Path] = []
        for i in range(model.GetCount()):
            if not model.GetValueByRow(i, 0):
                continue
            if i >= len(self.zip_rows):
                continue
            entry = self.zip_rows[i]
            status = entry.get("status", "")
            if status in ("MISSING_SYMBOL", "MISSING_FOOTPRINT"):
                self.append_log(f"[WARN] Skipping {entry.get('name','?')}: missing required files.")
                continue
            path = entry.get("path")
            if path:
                selected.append(Path(path))
        if not selected:
            self.append_log("[WARN] No valid ZIPs selected for import (must contain both symbol + footprint).")
        return selected

    def run_process_action(self, *, is_purge: bool):
        active_files = self._get_selected_zip_paths()
        if not active_files:
            return

        use_symbolname_as_ref = self.chk_use_symbol_name.GetValue()
        self._start_zip_process(
            active_files,
            is_purge=is_purge,
            use_symbol_name=use_symbolname_as_ref,
        )

class BOMFileDropTarget(wx.FileDropTarget):
    """Enable drag-and-drop of BOM CSV files onto the Mouser tab."""
    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    def OnDropFiles(self, x, y, filenames):
        for f in filenames:
            p = Path(f)
            if p.is_file() and p.suffix.lower() == ".csv":
                self.panel.current_bom_file = str(p)
                wx.CallAfter(self.panel.load_bom, str(p))
                wx.CallAfter(self.panel.log, f"[OK] Loaded BOM via drag-and-drop: {p.name}")
                return True
        wx.CallAfter(self.panel.log, "[WARN] Only CSV files are accepted for BOM import.")
        return False

class MouserAutoOrderTab(wx.Panel):
    """Tab to load BOM CSV, filter items and submit order via Mouser API (in-tab GUI)."""

    def __init__(self, parent, log_callback=None):
        """
        Initialize the panel with controls for loading BOM, selecting columns,
        setting multiplier, and submitting orders. Also sets up the data view.
        """
        super().__init__(parent)
        import mouser_integration as mouser  # heavy import: defer until tab creation
        self.mouser = mouser
        self.log_callback = log_callback
        self.bom_handler = self.mouser.BOMHandler() # BOM parsing utility
        self.order_client = self.mouser.MouserOrderClient() # Mouser order API client
        self.current_bom_file = None
        self.current_data_array = None
        self._final_overrides = set()
        self._suppress_qty_sync = False
        self._negative_extra_rows = set()
        self.temp_bom_dir = Path(tempfile.gettempdir()) / "kicad_mouser_bom"
        self.temp_bom_dir.mkdir(parents=True, exist_ok=True)
        self._mouser_busy = False
        self._bom_generate_timer = None
        self._bom_parse_timer = None
        self._order_timer = None

        s = wx.BoxSizer(wx.VERTICAL)

        # Top controls
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_generate_bom = wx.Button(self, label="Generate Project BOM")
        self.btn_generate_bom.SetToolTip(
            "Use kicad-cli to generate a BOM from the current project, then load it here."
        )
        self.btn_open_bom = wx.Button(self, label="Open BOM CSV...")
        self.btn_open_bom.SetToolTip("Open a BOM CSV file to load parts from.")
        self.btn_submit = wx.Button(self, label="Submit Cart to Mouser")
        self.btn_submit.SetToolTip(
            "Submit the current selection to Mouser shopping cart via API.\n\n"
            "Make sure that your API key is set in the environment variables and account details contains your address to show EUR currency in Logger."
        )
        
        # Set button icons
        set_button_icon(self.btn_generate_bom, wx.ART_REPORT_VIEW)
        set_button_icon(self.btn_open_bom, wx.ART_FOLDER_OPEN)
        set_button_icon(self.btn_submit, wx.ART_GO_DIR_UP)

        top.Add(self.btn_generate_bom, 1, wx.EXPAND | wx.RIGHT, 6)
        top.Add(self.btn_open_bom, 1, wx.EXPAND | wx.RIGHT, 6)
        sep = wx.StaticLine(self, style=wx.LI_VERTICAL)
        top.Add(sep, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        top.Add(self.btn_submit, 1, wx.EXPAND | wx.RIGHT, 6)
        sep2 = wx.StaticLine(self, style=wx.LI_VERTICAL)
        top.Add(sep2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        lbl_mnr_column = wx.StaticText(self, label="Mouser # column:")
        lbl_mnr_column.SetToolTip("Select which column in the BOM contains the Mouser Part Numbers.")
        top.Add(lbl_mnr_column, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.choice_mnr = wx.ComboBox(self, choices=[], style=wx.CB_READONLY)
        top.Add(self.choice_mnr, 0, wx.ALIGN_CENTER_VERTICAL)
        s.Add(top, 0, wx.ALL, 0)
        s.AddSpacer(10)

        # ---------- Configuration row: group-by + multiplier ----------
        cfg = wx.BoxSizer(wx.HORIZONTAL)
        lbl_group_by = wx.StaticText(self, label="Group by:")
        lbl_group_by.SetToolTip("Group BOM lines by the selected column before loading. Choose '(none)' to disable.")
        cfg.Add(lbl_group_by, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 0)
        self.choice_group = wx.ComboBox(self, choices=["Value", "(none)"], style=wx.CB_READONLY)
        self.choice_group.SetSelection(0)
        cfg.Add(self.choice_group, 0, wx.RIGHT, 12)

        lbl_multiplier = wx.StaticText(self, label="Multiplier:")
        lbl_multiplier.SetToolTip("Number of parts to order, e.g. 5: order 5x the BOM quantity for 5 PCBs.")
        cfg.Add(lbl_multiplier, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 0)
        self.multiplier = wx.SpinCtrl(self, min=1, max=1000, initial=5)
        cfg.Add(self.multiplier, 0, wx.RIGHT, 12)

        s.Add(cfg, 0, wx.ALL | wx.EXPAND, 0)
        s.AddSpacer(10)
        
        # ---------- Label + master toggle ----------
        header = wx.BoxSizer(wx.HORIZONTAL)
        self.label_dv_header = wx.StaticText(self, label="Components to order (check to include):")
        self.label_dv_header.SetToolTip("Checked items will be included in the order; uncheck to exclude specific lines.")
        header.Add(self.label_dv_header, 0, wx.ALIGN_CENTER_VERTICAL)
        header.AddStretchSpacer()
        self.chk_master_mouser = wx.CheckBox(self, label="Select All")
        self.chk_master_mouser.SetToolTip("Toggle all BOM lines on or off.")
        header.Add(self.chk_master_mouser, 0, wx.ALIGN_CENTER_VERTICAL)
        s.Add(header, 0, wx.LEFT | wx.RIGHT | wx.TOP, 0)

        # ---------- DataView for BOM rows ----------
        self.dv = dv.DataViewListCtrl(self, style=dv.DV_ROW_LINES | dv.DV_VERT_RULES)
        self.col_exclude = self.dv.AppendToggleColumn("Include", width=80) # Checkbox to include items
        self.col_exclude.SetAlignment(wx.ALIGN_CENTER)
        self.col_ref     = self.dv.AppendTextColumn("Reference", width=250)
        self.col_mnr     = self.dv.AppendTextColumn("Mouser Number", width=200)
        self.col_qty     = self.dv.AppendTextColumn("Per board Qty", width=120)
        self.col_extra   = self.dv.AppendTextColumn(
            "Extra Qty", width=90, mode=dv.DATAVIEW_CELL_EDITABLE
        )
        self.col_final   = self.dv.AppendTextColumn(
            "Final Qty", width=90, mode=dv.DATAVIEW_CELL_EDITABLE
        )
        self.COL_INCLUDE = 0
        self.COL_REF = 1
        self.COL_MNR = 2
        self.COL_QTY = 3
        self.COL_EXTRA = 4
        self.COL_FINAL = 5
        self.dv.SetDropTarget(BOMFileDropTarget(self))
        self.SetDropTarget(BOMFileDropTarget(self))
        s.Add(self.dv, 1, wx.EXPAND | wx.ALL, 0)

        self.SetSizer(s)

        # ---------- Bind events ----------
        self.btn_generate_bom.Bind(wx.EVT_BUTTON, self.on_generate_bom)
        self.btn_open_bom.Bind(wx.EVT_BUTTON, self.on_open_bom)
        self.btn_submit.Bind(wx.EVT_BUTTON, self.on_submit_order)
        self.choice_mnr.Bind(wx.EVT_COMBOBOX, self.on_mnr_changed)
        self.choice_group.Bind(wx.EVT_COMBOBOX, self.on_group_changed)
        self.Bind(wx.EVT_CHECKBOX, self.on_master_mouser_toggle, self.chk_master_mouser)
        self.dv.Bind(dv.EVT_DATAVIEW_ITEM_VALUE_CHANGED, self.on_dv_value_changed)
        self.dv.Bind(dv.EVT_DATAVIEW_ITEM_EDITING_DONE, self.on_bom_cell_edited)
        self.multiplier.Bind(wx.EVT_SPINCTRL, self.on_multiplier_changed)
        self.multiplier.Bind(wx.EVT_TEXT, self.on_multiplier_changed)
        self.dv.Bind(wx.EVT_LEFT_DOWN, self.on_dv_left_down)
        self._pending_edit = None

    # ---------- Logging helper ----------
    def log(self, text):
        """Log to panel and optionally forward to main logger."""
        if self.log_callback:
            try:
                self.log_callback(text)
            except Exception:
                pass

    def _log_ui(self, text: str):
        wx.CallAfter(self.log, text)

    def _set_mouser_busy(self, busy: bool):
        self._mouser_busy = busy
        controls = [
            self.btn_generate_bom,
            self.btn_open_bom,
            self.btn_submit,
            self.choice_mnr,
            self.choice_group,
            self.multiplier,
            self.chk_master_mouser,
            self.dv,
        ]
        for ctrl in controls:
            try:
                ctrl.Enable(not busy)
            except Exception:
                pass

    def _start_bom_generate(self, schematic_path: Path):
        if self._mouser_busy:
            return
        self._set_mouser_busy(True)
        bom_path = self.temp_bom_dir / f"{schematic_path.stem}_bom.csv"
        ctx = mp.get_context("spawn")
        self._bom_generate_queue = ctx.Queue()
        self._bom_generate_process = ctx.Process(
            target=_generate_bom_worker,
            args=(str(schematic_path), str(bom_path), self._bom_generate_queue),
        )
        self._bom_generate_process.start()
        if not self._bom_generate_timer:
            self._bom_generate_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_bom_generate_poll, self._bom_generate_timer)
        self._bom_generate_timer.Start(200)

    def _on_bom_generate_poll(self, event):
        try:
            status, payload = self._bom_generate_queue.get_nowait()
        except Exception:
            return
        self._bom_generate_timer.Stop()
        if self._bom_generate_process and self._bom_generate_process.is_alive():
            self._bom_generate_process.join(timeout=0)
        if status == "ok":
            self.current_bom_file = payload
            self._start_bom_parse(payload)
        else:
            self._log_ui(f"[ERROR] {payload}")
            self._set_mouser_busy(False)

    def _start_bom_parse(self, bom_path: str):
        if not self._mouser_busy:
            self._set_mouser_busy(True)
        self._log_ui(f"[INFO] Loading BOM: {bom_path}")
        ctx = mp.get_context("spawn")
        group_by = self._get_group_by_field()
        self._bom_parse_queue = ctx.Queue()
        self._bom_parse_process = ctx.Process(
            target=_parse_bom_worker,
            args=(bom_path, group_by, self._bom_parse_queue),
        )
        self._bom_parse_process.start()
        if not self._bom_parse_timer:
            self._bom_parse_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_bom_parse_poll, self._bom_parse_timer)
        self._bom_parse_timer.Start(200)

    def _on_bom_parse_poll(self, event):
        try:
            status, payload = self._bom_parse_queue.get_nowait()
        except Exception:
            return
        self._bom_parse_timer.Stop()
        if self._bom_parse_process and self._bom_parse_process.is_alive():
            self._bom_parse_process.join(timeout=0)
        if status == "ok":
            wx.CallAfter(self._apply_bom_data, payload)
            self._log_ui(f"[OK] BOM loaded: {Path(self.current_bom_file).name}")
        else:
            self._log_ui(f"[ERROR] BOM load failed: {payload}")
        self._set_mouser_busy(False)

    def _start_submit_order(self, data_for_order: dict):
        if self._mouser_busy:
            return
        self._set_mouser_busy(True)
        ctx = mp.get_context("spawn")
        self._order_queue = ctx.Queue()
        self._order_process = ctx.Process(
            target=_submit_order_worker,
            args=(data_for_order, self._order_queue),
        )
        self._order_process.start()
        if not self._order_timer:
            self._order_timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._on_order_poll, self._order_timer)
        self._order_timer.Start(200)

    def _on_order_poll(self, event):
        try:
            msg = self._order_queue.get_nowait()
        except Exception:
            return
        kind = msg[0]
        if kind == "log":
            self._log_ui(msg[1])
            return

        self._order_timer.Stop()
        if self._order_process and self._order_process.is_alive():
            self._order_process.join(timeout=0)
        if kind == "ok" and msg[1]:
            self._log_ui("[OK] Mouser order submitted successfully.")
        elif kind == "ok":
            self._log_ui("[FAIL] Mouser order failed. See log for details.")
        else:
            self._log_ui(f"[ERROR] Mouser order failed: {msg[1]}")
        self._set_mouser_busy(False)

    # ---------- Event handlers ----------
    def on_open_bom(self, evt):
        """Open file dialog to pick a BOM CSV file and load it."""
        with wx.FileDialog(self, "Open BOM CSV", wildcard="CSV files (*.csv)|*.csv",
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,) as fdg:
            if fdg.ShowModal() != wx.ID_OK:
                return
            path = fdg.GetPath()
            self.current_bom_file = path
            self._start_bom_parse(path)


    def on_generate_bom(self, evt):
        """Generate BOM from current KiCad project using kicad-cli, then open and load it."""
        schematic = self._find_project_schematic()
        if not schematic:
            self.log("[ERROR] Keine .kicad_sch Datei im Projekt gefunden.")
            return
        self.log(f"[INFO] Generiere BOM aus {schematic.name} ...")
        self._start_bom_generate(schematic)

    def _find_project_schematic(self) -> Path | None:
        """Return preferred schematic for BOM export (matching the .kicad_pro stem)."""
        proj_files = sorted(PROJECT_DIR.glob("*.kicad_pro"))
        if proj_files:
            preferred = PROJECT_DIR / f"{proj_files[0].stem}.kicad_sch"
            if preferred.exists():
                return preferred
        fallback = PROJECT_DIR / "Project.kicad_sch"
        if fallback.exists():
            return fallback
        top_level = sorted(PROJECT_DIR.glob("*.kicad_sch"))
        if top_level:
            return top_level[0]
        nested = sorted(PROJECT_DIR.rglob("*.kicad_sch"))
        if nested:
            return nested[0]
        return None

    def _find_kicad_cli(self) -> str | None:
        """Compatibility wrapper for older code paths."""
        return _find_kicad_cli_path()

    def _normalize_bom_headers(self, bom_path: Path):
        """Rename KiCad 'Quantity' column to 'Qty' for Mouser importer expectations."""
        try:
            with open(bom_path, newline="", encoding="utf-8") as f:
                reader = list(csv.DictReader(f))
                fieldnames = reader[0].keys() if reader else []
        except Exception as e:
            self.log(f"[WARN] BOM konnte nicht geprüft werden: {e}")
            return

        if not fieldnames:
            return

        fieldnames = list(fieldnames)
        mapped = ["Qty" if h == "Quantity" else h for h in fieldnames]
        if mapped == fieldnames:
            return

        try:
            with open(bom_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=mapped)
                writer.writeheader()
                for row in reader:
                    writer.writerow({new: row.get(old, "") for new, old in zip(mapped, fieldnames)})
        except Exception as e:
            self.log(f"[WARN] Konnte BOM-Header nicht anpassen: {e}")

    def load_bom(self, bom_path):
        """
        Load BOM CSV in a background process and apply results.
        """
        self.current_bom_file = bom_path
        self._start_bom_parse(bom_path)

    def _apply_bom_data(self, data):
        """
        Apply parsed BOM data to UI (choices + DataView).
        """
        try:
            headers = list(data.keys())
            if "Reference" not in headers or "Qty" not in headers:
                self.log("[ERROR] BOM missing required columns (Reference/Qty).")
                return

            choices = [h for h in headers if h not in ("Reference", "Qty")]
            if not choices:
                choices = [self.mouser.CSV_MOUSER_COLUMN_NAME]
            current_mnr = self.choice_mnr.GetValue()
            self.choice_mnr.Clear()
            self.choice_mnr.AppendItems(choices)
            default_idx = 0
            if self.mouser.CSV_MOUSER_COLUMN_NAME in choices:
                default_idx = choices.index(self.mouser.CSV_MOUSER_COLUMN_NAME)
            elif "Value" in choices:
                default_idx = choices.index("Value")
            if current_mnr in choices:
                self.choice_mnr.SetStringSelection(current_mnr)
            else:
                self.choice_mnr.SetSelection(default_idx)

            group_choices = ["(none)"] + [h for h in headers if h not in ("Reference", "Qty")]
            current_group = self.choice_group.GetValue()
            self.choice_group.Clear()
            self.choice_group.AppendItems(group_choices)
            if current_group in group_choices:
                self.choice_group.SetStringSelection(current_group)
            elif "Value" in group_choices:
                self.choice_group.SetStringSelection("Value")
            else:
                self.choice_group.SetStringSelection("(none)")

            self.multiplier.SetValue(5)

            self.current_data_array = data
            self._final_overrides.clear()

            self.dv.GetStore().DeleteAllItems()
            mnr_col = self.choice_mnr.GetValue() or choices[0]
            self.col_mnr.SetTitle(mnr_col)
            refs = data.get("Reference", [])
            mnrs = data.get(mnr_col, [""] * len(refs))
            qtys = data.get("Qty", [""] * len(refs))
            for ref, mnr, qty in zip(refs, mnrs, qtys):
                final_qty = self._compute_final_qty(qty, "0")
                self.dv.GetStore().AppendItem([True, str(ref), str(mnr), str(qty), "0", str(final_qty)])
            for row in range(self.dv.GetStore().GetCount()):
                self._enforce_final_qty_selection(row)
                self._update_extra_qty_style(row)

            if len(refs) == 0:
                self.log("[WARN] BOM contains no parts.")
            else:
                self.log(f"[OK] Loaded {len(refs)} BOM rows.")
            self._update_master_mouser_state()
        except Exception as e:
            self.log(f"[ERROR] Failed to apply BOM data: {e}")

    def on_mnr_changed(self, evt):
        """Update DataView column when user selects a different MNR column."""
        if not self.current_data_array:
            return

        mnr_col = self.choice_mnr.GetValue()
        self.col_mnr.SetTitle(mnr_col)
        
        store = self.dv.GetStore()
        store.DeleteAllItems()
        self._final_overrides.clear()

        refs = self.current_data_array.get("Reference", [])
        mnrs = self.current_data_array.get(mnr_col, [""] * len(refs))
        qtys = self.current_data_array.get("Qty", [""] * len(refs))

        for ref, mnr, qty in zip(refs, mnrs, qtys):
            final_qty = self._compute_final_qty(qty, "0")
            store.AppendItem([True, str(ref), str(mnr), str(qty), "0", str(final_qty)])
        for row in range(store.GetCount()):
            self._enforce_final_qty_selection(row)
            wx.CallAfter(self._update_extra_qty_style, row)
        self._update_master_mouser_state()

    def _get_group_by_field(self) -> str | None:
        value = self.choice_group.GetValue().strip() if self.choice_group else ""
        if not value or value.lower() == "(none)":
            return None
        return value

    def on_group_changed(self, evt):
        """Re-parse BOM using the selected grouping column."""
        if not self.current_bom_file:
            return
        self._start_bom_parse(self.current_bom_file)

    def _update_master_mouser_state(self):
        """Sync master checkbox label/value based on current rows."""
        store = self.dv.GetStore()
        total = store.GetCount()
        checked = sum(1 for row in range(total) if store.GetValueByRow(row, 0))
        all_checked = total > 0 and checked == total
        self.chk_master_mouser.SetValue(all_checked)
        self.chk_master_mouser.SetLabel("Deselect All" if all_checked else "Select All")

    def on_master_mouser_toggle(self, event):
        """Select or deselect all BOM rows."""
        checked = self.chk_master_mouser.IsChecked()
        store = self.dv.GetStore()
        for row in range(store.GetCount()):
            store.SetValueByRow(checked, row, 0)
        self.dv.Refresh()
        self.chk_master_mouser.SetLabel("Deselect All" if checked else "Select All")
        if event:
            event.Skip()

    def on_dv_value_changed(self, event):
        """Handle DataView value changes (include toggle + qty edits)."""
        if self._suppress_qty_sync:
            if event:
                event.Skip()
            return
        try:
            col = event.GetColumn()
        except Exception:
            col = None
        try:
            item = event.GetItem()
        except Exception:
            item = None
        row = self.dv.ItemToRow(item) if item else None
        if col == self.COL_INCLUDE:
            if row is not None:
                store = self.dv.GetStore()
                try:
                    include_now = bool(store.GetValueByRow(row, self.COL_INCLUDE))
                except Exception:
                    include_now = False
                if include_now:
                    base_qty = store.GetValueByRow(row, self.COL_QTY)
                    try:
                        base = int(str(base_qty).strip())
                    except Exception:
                        base = 0
                    try:
                        mult = int(self.multiplier.GetValue() or 1)
                    except Exception:
                        mult = 1
                    final_val = max(0, base * mult)
                    try:
                        current_final = int(str(store.GetValueByRow(row, self.COL_FINAL)).strip())
                    except Exception:
                        current_final = 0
                    if current_final <= 0 and final_val > 0:
                        self._suppress_qty_sync = True
                        store.SetValueByRow(str(final_val), row, self.COL_FINAL)
                        store.SetValueByRow("0", row, self.COL_EXTRA)
                        self._suppress_qty_sync = False
                        wx.CallAfter(self._update_extra_qty_style, row)
            self._update_master_mouser_state()
        elif col == self.COL_FINAL and row is not None:
            store = self.dv.GetStore()
            base_qty = store.GetValueByRow(row, self.COL_QTY)
            final_qty = store.GetValueByRow(row, self.COL_FINAL)
            try:
                base = int(str(base_qty).strip())
            except Exception:
                base = 0
            try:
                final_val = int(str(final_qty).strip())
            except Exception:
                final_val = 0
            if final_val < 0:
                final_val = 0
                self._suppress_qty_sync = True
                store.SetValueByRow(str(final_val), row, self.COL_FINAL)
                self._suppress_qty_sync = False
            try:
                mult = int(self.multiplier.GetValue() or 1)
            except Exception:
                mult = 1
            extra_val = final_val - (base * mult)
            self._suppress_qty_sync = True
            store.SetValueByRow(str(extra_val), row, self.COL_EXTRA)
            self._suppress_qty_sync = False
            self._enforce_final_qty_selection(row)
            wx.CallAfter(self._update_extra_qty_style, row)
        elif col == self.COL_EXTRA and row is not None:
            store = self.dv.GetStore()
            base_qty = store.GetValueByRow(row, self.COL_QTY)
            extra_qty = store.GetValueByRow(row, self.COL_EXTRA)
            self._suppress_qty_sync = True
            store.SetValueByRow(str(self._compute_final_qty(base_qty, extra_qty)), row, self.COL_FINAL)
            self._suppress_qty_sync = False
            self._enforce_final_qty_selection(row)
            wx.CallAfter(self._update_extra_qty_style, row)
        if event:
            event.Skip()

    def _compute_final_qty(self, base_qty, extra_qty):
        try:
            base = int(str(base_qty).strip())
        except Exception:
            base = 0
        try:
            extra = int(str(extra_qty).strip())
        except Exception:
            extra = 0
        try:
            mult = int(self.multiplier.GetValue() or 1)
        except Exception:
            mult = 1
        return max(0, base * mult + extra)

    def _enforce_final_qty_selection(self, row):
        store = self.dv.GetStore()
        try:
            final_val = int(str(store.GetValueByRow(row, self.COL_FINAL)).strip())
        except Exception:
            final_val = 0
        if final_val <= 0:
            try:
                currently_checked = bool(store.GetValueByRow(row, self.COL_INCLUDE))
            except Exception:
                currently_checked = False
            store.SetValueByRow(False, row, self.COL_INCLUDE)
            if currently_checked:
                self._update_master_mouser_state()

    def _update_extra_qty_style(self, row):
        store = self.dv.GetStore()
        try:
            extra_val = int(str(store.GetValueByRow(row, self.COL_EXTRA)).strip())
        except Exception:
            extra_val = 0
        was_negative = row in self._negative_extra_rows
        is_negative = extra_val < 0
        try:
            attr = dv.DataViewItemAttr()
            if is_negative:
                attr.SetColour(wx.Colour(200, 0, 0))
                attr.SetBackgroundColour(wx.Colour(255, 230, 230))
            else:
                attr.SetColour(wx.NullColour)
                attr.SetBackgroundColour(wx.NullColour)
            if hasattr(store, "SetAttrByRow"):
                store.SetAttrByRow(row, self.COL_EXTRA, attr)
            elif hasattr(store, "SetAttr"):
                store.SetAttr(row, self.COL_EXTRA, attr)
            elif hasattr(self.dv, "SetAttr"):
                self.dv.SetAttr(row, self.COL_EXTRA, attr)
            try:
                self.dv.RefreshRow(row)
            except Exception:
                self.dv.Refresh()
        except Exception:
            return
        if is_negative and not was_negative:
            try:
                ref = store.GetValueByRow(row, self.COL_REF)
            except Exception:
                ref = ""
            self._negative_extra_rows.add(row)
            self.log(f"[ERROR] Negative Extra Qty for {ref or 'row ' + str(row)}: {extra_val}")
        elif not is_negative and was_negative:
            self._negative_extra_rows.discard(row)

    def on_multiplier_changed(self, event):
        """Recalculate final qty for rows that are not manually overridden."""
        store = self.dv.GetStore()
        total = store.GetCount()
        self._suppress_qty_sync = True
        for row in range(total):
            base_qty = store.GetValueByRow(row, self.COL_QTY)
            extra_qty = store.GetValueByRow(row, self.COL_EXTRA)
            store.SetValueByRow(str(self._compute_final_qty(base_qty, extra_qty)), row, self.COL_FINAL)
            self._enforce_final_qty_selection(row)
        self._suppress_qty_sync = False
        if event:
            event.Skip()

    def on_bom_cell_edited(self, event):
        """Track manual overrides and recompute final qty when extra qty changes."""
        if self._suppress_qty_sync:
            if event:
                event.Skip()
            return
        try:
            item = event.GetItem()
        except Exception:
            item = None
        row = self.dv.ItemToRow(item) if item else None
        col = event.GetColumn()
        store = self.dv.GetStore()
        if row is None:
            if event:
                event.Skip()
            return
        if col == self.COL_FINAL:
            base_qty = store.GetValueByRow(row, self.COL_QTY)
            final_qty = store.GetValueByRow(row, self.COL_FINAL)
            try:
                base = int(str(base_qty).strip())
            except Exception:
                base = 0
            try:
                final_val = int(str(final_qty).strip())
            except Exception:
                final_val = 0
            if final_val < 0:
                final_val = 0
                self._suppress_qty_sync = True
                store.SetValueByRow(str(final_val), row, self.COL_FINAL)
                self._suppress_qty_sync = False
            try:
                mult = int(self.multiplier.GetValue() or 1)
            except Exception:
                mult = 1
            extra_val = final_val - (base * mult)
            self._suppress_qty_sync = True
            store.SetValueByRow(str(extra_val), row, self.COL_EXTRA)
            self._suppress_qty_sync = False
            self._enforce_final_qty_selection(row)
            wx.CallAfter(self._update_extra_qty_style, row)
        elif col == self.COL_EXTRA:
            base_qty = store.GetValueByRow(row, self.COL_QTY)
            extra_qty = store.GetValueByRow(row, self.COL_EXTRA)
            self._suppress_qty_sync = True
            store.SetValueByRow(str(self._compute_final_qty(base_qty, extra_qty)), row, self.COL_FINAL)
            self._suppress_qty_sync = False
            self._enforce_final_qty_selection(row)
            self._update_extra_qty_style(row)
        if event:
            event.Skip()

    def on_dv_left_down(self, event):
        """Start editing editable qty fields on single click."""
        pos = event.GetPosition()
        hit = self.dv.HitTest(pos)
        item = None
        col = None
        if isinstance(hit, tuple):
            if len(hit) >= 1:
                item = hit[0]
            if len(hit) >= 2:
                col = hit[1]
        if item and col in (self.COL_EXTRA, self.COL_FINAL):
            self._pending_edit = (item, col)
            event.Skip()
            wx.CallAfter(self._begin_pending_edit)
            return
        event.Skip()

    def _begin_pending_edit(self):
        if not self._pending_edit:
            return
        item, col = self._pending_edit
        self._pending_edit = None
        try:
            self.dv.EditItem(item, col)
        except Exception:
            pass

    # ---------- Data preparation ----------
    def collect_selected_for_order(self):
        """
        Collect checked DataView rows for ordering.
        Returns a dict compatible with MouserOrderClient.
        """
        store = self.dv.GetStore()
        refs, mnrs, qtys = [], [], []
        extras = []
        col_mnr_name = self.choice_mnr.GetValue() or self.mouser.CSV_MOUSER_COLUMN_NAME
        for row in range(store.GetCount()):
            include_flag = store.GetValueByRow(row, 0)
            if not include_flag:
                continue
            refs.append(store.GetValueByRow(row, 1))
            mnrs.append(store.GetValueByRow(row, 2))
            base_qty = store.GetValueByRow(row, self.COL_QTY)
            extra_qty = store.GetValueByRow(row, self.COL_EXTRA)
            final_qty = store.GetValueByRow(row, self.COL_FINAL)
            try:
                final_val = int(str(final_qty).strip())
            except Exception:
                final_val = self._compute_final_qty(base_qty, extra_qty)
            if final_val <= 0:
                continue
            qtys.append(str(final_val))
            extras.append("0")
        return {
            "Reference": refs,
            col_mnr_name: mnrs,
            "MNR_Column_Name": col_mnr_name,
            "Qty": qtys,
            "ExtraQty": extras,
            "Multiplier": 1,
        }

    def on_submit_order(self, evt):
        """Submit selected BOM items to Mouser cart using a background process."""
        if not self.current_data_array:
            self.log("[WARN] No BOM loaded.")
            return

        data_for_order = self.collect_selected_for_order()
        if not data_for_order["Reference"]:
            self.log("[WARN] No items selected for ordering.")
            return

        self._start_submit_order(data_for_order)
    

# ===============================
# wx.App entry
# ===============================
class KiCadApp(wx.App):
    def OnInit(self):
        self.frame = MainFrame()
        self.frame.Show()
        return True

if __name__ == "__main__":
    mp.freeze_support()
    app = KiCadApp(False)
    app.MainLoop()
