import os
from pathlib import Path

import pcbnew
import wx


class KiCadLibraryManager(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "KiCad Library Manager"
        self.category = "Library"
        self.description = "Manage KiCad library ZIP imports/exports and project utilities."
        self.show_toolbar_button = True
        icon = Path(__file__).resolve().parents[1] / "resources" / "icon.png"
        if icon.exists():
            self.icon_file_name = str(icon)

    def Run(self):
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

            gui_wx.MainFrame()
        except Exception as exc:
            wx.MessageBox(
                f"Failed to start KiCad Library Manager:\n{exc}",
                "KiCad Library Manager",
                wx.OK | wx.ICON_ERROR,
            )


KiCadLibraryManager().register()
