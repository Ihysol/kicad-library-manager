# KiCad Library Manager (wxPython)

wxPython-based GUI and CLI for importing, purging, and exporting component ZIPs into this KiCad template.

---

## Features

* Import component `.zip` archives into project symbol/footprint libraries
* Purge previously imported parts
* Export project symbols (with footprint/3D validation)
* DRC template updater and Mouser Auto Order tab
* GUI-first (`gui_wx.py`) with a companion CLI (`cli_main.py`)

---

## Usage

* Install dependencies and run the wx GUI:
  ```bash
  pip install -r requirements.txt
  python gui_wx.py
  ```
* Or run the CLI:
  ```bash
  python cli_main.py process path\\to\\part.zip --use-symbol-name
  python cli_main.py purge path\\to\\part.zip
  python cli_main.py export --symbols U1 U2
  ```
* ZIPs are read from the `library_input` folder (or the folder you pick inside the GUI).

### Workflow

1. Drop or select `.zip` files into `library_input` (or choose a folder in the GUI).
2. Launch the GUI, pick ZIPs to import, and press **PROCESS / IMPORT**.
3. Use **PURGE / DELETE** to remove a part before re-importing.
4. Switch to **Export Project Symbols** to export selected symbols into ZIPs.
5. DRC tab applies the correct `.kicad_dru` template based on PCB layer count.

---

## Folder Structure

* **library_input/** – Folder for `.zip` files or component definitions to be imported (default source).
* **library_output/** – Destination for exported ZIPs.
* **dru_templates/** – DRC templates consumed by the DRC tab.

## Build the executable

```
# Windows:
pyinstaller --onefile --name kicad_library_manager --noconsole gui_wx.py

# Linux:
pyinstaller --onefile --name kicad_library_manager --noconsole gui_wx.py
```

