# KiCad Library Manager (wxPython)

wxPython-based GUI and CLI for importing, purging, and exporting component ZIPs into this KiCad template.

---

## Features

* Import component `.zip` archives into project symbol/footprint/3D libraries
* Drag-and-drop ZIP support with status checks (missing symbol/footprint detection)
* Purge previously imported parts
* Export project symbols with footprint/3D validation
* Delete selected symbols plus linked footprints/3D models
* DRC rules updater based on PCB layer count
* Generate top/bottom board images via `kicad-cli` with crop frame + presets
* Save board previews to `klm_data/Docs/img`
* BOM generation (via `kicad-cli`), BOM parsing, and Mouser Auto Order workflow
* GUI-first (`library_manager/src/gui_wx.py`) with a companion CLI (`library_manager/src/cli_main.py`)

---

## Usage

### Install as KiCad plugin (recommended)

**Option A: Add repo URL**

1. In KiCad, open **Plugin and Content Manager**.
2. Add this repository URL in the repositories list: `https://ihysol.github.io/kicad-library-manager/repository.json`
3. Click **Update** and install the plugin.
4. Restart KiCad.

**Option B: Install from file**

1. Download the ZIP from the releases page: `https://github.com/Ihysol/kicad-library-manager/releases`
2. In KiCad, open **Plugin and Content Manager**.
3. Click **Install from File** and select the downloaded ZIP.
4. Restart KiCad.

### Run from source (optional)

* Install dependencies and run the wx GUI:
  ```bash
  pip install -r library_manager/src/requirements.txt
  python library_manager/src/gui_wx.py
  ```
* Or run the CLI:
  ```bash
  python library_manager/src/cli_main.py process path\\to\\part.zip --use-symbol-name
  python library_manager/src/cli_main.py purge path\\to\\part.zip
  python library_manager/src/cli_main.py export --symbols U1 U2
  ```

### Workflow

1. Open a board in the PCB Editor and launch the plugin.
2. Drop or select `.zip` files into `klm_data/library_input` (created next to the `.kicad_pro`).
3. Pick ZIPs to import and press **PROCESS / IMPORT**.
3. Use **PURGE / DELETE** to remove a part before re-importing.
4. Switch to **Export Project Symbols** to export selected symbols into ZIPs.
5. DRC tab applies the correct `.kicad_dru` template based on PCB layer count.

---

## Folder Structure (project-local)

All runtime data lives under `klm_data/` next to your `.kicad_pro`:

* **klm_data/library_input/** - Folder for `.zip` files or component definitions to be imported.
* **klm_data/library_output/** - Destination for exported ZIPs.
* **klm_data/imgs/** - Generated board images.
* **library_manager/dru_templates/** - DRC templates consumed by the DRC tab.

## Credits

* [Darexsh](https://github.com/Darexsh) - tester, debugger, and creative input

## Build the executable

```
# Windows:
pyinstaller --onefile --name kicad_library_manager --noconsole library_manager/src/gui_wx.py

# Linux:
pyinstaller --onefile --name kicad_library_manager --noconsole library_manager/src/gui_wx.py
```

