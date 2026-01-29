import os
import sys
from pathlib import Path

import pcbnew


class KiCadLibraryManager(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "KiCad Library Manager"
        self.category = "Library"
        self.description = "Manage KiCad library ZIP imports/exports and project utilities."
        self.show_toolbar_button = True
        icon = _find_icon_path()
        if icon:
            self.icon_file_name = str(icon)

    def Run(self):
        _ensure_vendor_path()
        try:
            import wx
        except Exception as exc:
            print(f"KiCad Library Manager: wxPython not available: {exc}")
            return
        if _focus_existing_window(wx):
            return

        board = pcbnew.GetBoard()
        project_dir = None
        if board:
            board_path = board.GetFileName()
            if board_path:
                project_dir = Path(board_path).resolve().parent

        if not project_dir:
            wx.MessageBox(
                "No PCB project is loaded. Open a board and try again.",
                "KiCad Library Manager",
                wx.OK | wx.ICON_ERROR,
            )
            return

        os.environ["KLM_PROJECT_DIR"] = str(project_dir)
        os.environ["KLM_BASE_PATH"] = str(project_dir)

        try:
            from . import gui_wx

            if hasattr(gui_wx, "get_or_create_main_frame"):
                gui_wx.get_or_create_main_frame()
            else:
                gui_wx.MainFrame()
        except Exception as exc:
            wx.MessageBox(
                f"Failed to start KiCad Library Manager:\n{exc}",
                "KiCad Library Manager",
                wx.OK | wx.ICON_ERROR,
            )


def _find_icon_path():
    here = Path(__file__).resolve()
    # Package layout: <root>/plugins/kicad_library_manager/__init__.py
    pkg_icon = here.parents[2] / "resources" / "icon.png"
    if pkg_icon.exists():
        return pkg_icon

    # Installed PCM layout: <user>/.../3rdparty/plugins/<id>/kicad_library_manager
    identifier = "com_github_ihysol_kicad-library-manager"
    for parent in here.parents:
        if parent.name.lower() == "3rdparty":
            installed_icon = parent / "resources" / identifier / "icon.png"
            if installed_icon.exists():
                return installed_icon
            break

    return None


def _ensure_vendor_path():
    plugin_dir = Path(__file__).resolve().parent
    plugin_path = str(plugin_dir)
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    vendor_dir = plugin_dir / "vendor"
    if vendor_dir.exists():
        vendor_path = str(vendor_dir)
        if vendor_path not in sys.path:
            sys.path.insert(0, vendor_path)


def _focus_existing_window(wx_mod) -> bool:
    """Bring an existing GUI window to the foreground if it's already open."""
    try:
        for win in wx_mod.GetTopLevelWindows():
            try:
                title = win.GetTitle()
            except Exception:
                continue
            if "KiCad Library Manager" in title:
                try:
                    win.Show(True)
                    win.Iconize(False)
                    win.Raise()
                    win.RequestUserAttention()
                except Exception:
                    pass
                return True
    except Exception:
        return False
    return False


KiCadLibraryManager().register()
