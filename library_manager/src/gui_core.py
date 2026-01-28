"""
Backend helpers for the KiCad Library Manager.
This module is UI-agnostic and exposes utilities for:
- config persistence
- logging setup hook
- ZIP scanning/status classification
- process/purge execution
- symbol export validation
- DRC rule updates
- convenience helpers for opening folders
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from sexpdata import Symbol, loads

try:
    # Preferred: real project environment (package import)
    from .library_manager import (
        INPUT_ZIP_FOLDER,
        PROJECT_FOOTPRINT_LIB,
        PROJECT_SYMBOL_LIB,
        export_symbols,
        get_existing_main_symbols,
        process_zip,
        purge_zip_contents,
    )
except ImportError:
    try:
        # Fallback: direct import for standalone runs
        from library_manager import (
            INPUT_ZIP_FOLDER,
            PROJECT_FOOTPRINT_LIB,
            PROJECT_SYMBOL_LIB,
            export_symbols,
            get_existing_main_symbols,
            process_zip,
            purge_zip_contents,
        )
    except ImportError:  # pragma: no cover - fallback for isolated editing
        INPUT_ZIP_FOLDER = Path.cwd() / "klm_data" / "library_input"
        PROJECT_SYMBOL_LIB = Path.cwd() / "ProjectSymbols.kicad_sym"
        PROJECT_FOOTPRINT_LIB = Path.cwd() / "ProjectFootprints.pretty"

        def get_existing_main_symbols() -> Set[str]:
            return set()

        def process_zip(*args, **kwargs):  # type: ignore
            raise RuntimeError("library_manager not available")

        def purge_zip_contents(*args, **kwargs):  # type: ignore
            raise RuntimeError("library_manager not available")

        def export_symbols(*args, **kwargs):  # type: ignore
            raise RuntimeError("library_manager not available")

# =========================
# Logging
# =========================
logger = logging.getLogger("kicad_library_manager")
APP_VERSION = "v1.2a"


def ensure_logger(handler: logging.Handler | None = None, level: int = logging.INFO) -> None:
    """Attach a handler once (useful for GUI log sinks) and set level."""
    logger.setLevel(level)
    if handler and not any(isinstance(h, handler.__class__) for h in logger.handlers):
        logger.addHandler(handler)
    logger.propagate = False


# =========================
# Config persistence
# =========================
if getattr(__import__("sys"), "frozen", False):
    CONFIG_FILE = Path(__import__("sys").executable).resolve().parent / "gui_config.json"
else:
    CONFIG_FILE = Path(__file__).parent / "gui_config.json"

RENAME_ASSETS_KEY = "rename_assets_default"
USE_SYMBOLNAME_KEY = "use_symbol_name_as_ref"
SHOW_LOG_KEY = "show_log"
AUTO_BORDER_KEY = "auto_border_on_generate"
BORDER_MARGIN_KEY = "auto_border_margin_px"
RENDER_PRESET_KEY = "render_preset_idx"


def load_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault(RENAME_ASSETS_KEY, False)
    cfg.setdefault(USE_SYMBOLNAME_KEY, False)
    cfg.setdefault(SHOW_LOG_KEY, True)
    cfg.setdefault(AUTO_BORDER_KEY, True)
    cfg.setdefault(BORDER_MARGIN_KEY, 20)
    cfg.setdefault(RENDER_PRESET_KEY, 1)
    return cfg


def save_config(config: Dict[str, Any]) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=4), encoding="utf-8")
    except Exception as e:
        logger.error(f"Could not save configuration to {CONFIG_FILE}: {e}")


# =========================
# Symbols / library helpers
# =========================
SUB_PART_PATTERN = re.compile(r"_\d(_\d)+$|_\d$")


def list_project_symbols() -> List[str]:
    """Return unique main symbols from ProjectSymbols.kicad_sym."""
    if not PROJECT_SYMBOL_LIB.exists():
        return []
    try:
        sexp = loads(PROJECT_SYMBOL_LIB.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[ERROR] reading symbol library: {e}")
        return []

    symbols: List[str] = []
    for el in sexp[1:]:
        if isinstance(el, list) and len(el) > 1 and str(el[0]) == "symbol":
            name = str(el[1])
            base = SUB_PART_PATTERN.sub("", name)
            if base not in symbols:
                symbols.append(base)
    return symbols


def update_existing_symbols_cache() -> Set[str]:
    try:
        existing = get_existing_main_symbols()
        logger.debug(f"Loaded {len(existing)} existing symbols.")
        return existing
    except Exception as e:
        logger.error(f"Failed to load existing symbols: {e}")
        return set()


# =========================
# ZIP scanning
# =========================
def _classify_zip(zip_path: Path, existing_symbols: Set[str]) -> Dict[str, Any]:
    """Inspect one ZIP and return status metadata for the UI."""
    row = {
        "path": zip_path,
        "name": zip_path.name,
        "status": "NEW",
        "tooltip": "No KiCad symbols found.",
    }
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            sym_files = [n for n in zf.namelist() if n.lower().endswith(".kicad_sym")]
            fp_files = [n for n in zf.namelist() if n.lower().endswith(".kicad_mod")]
            model_files = [n for n in zf.namelist() if n.lower().endswith(".stp")]

            if not sym_files:
                row["status"] = "MISSING_SYMBOL"
                row["tooltip"] = "ZIP does not contain any .kicad_sym files (symbols)."
                return row

            if not fp_files:
                row["status"] = "MISSING_FOOTPRINT"
                row["tooltip"] = (
                    f"Contains {len(sym_files)} symbol file(s) but no .kicad_mod footprints."
                )
                return row

            for existing in existing_symbols:
                if existing and existing.lower() in zip_path.stem.lower():
                    row["status"] = "PARTIAL"
                    row["tooltip"] = (
                        f"Contains symbols (e.g. '{existing}') already in library. "
                        "Unchecked by default."
                    )
                    return row

            row["status"] = "NEW"
            row["tooltip"] = (
                f"Contains {len(sym_files)} symbol file(s), "
                f"{len(fp_files)} footprint(s), {len(model_files)} model(s)."
            )
            return row

    except Exception as e:
        row["status"] = "ERROR"
        row["tooltip"] = f"Could not scan ZIP: {e}"
        return row


def scan_zip_folder(folder: Path) -> List[Dict[str, Any]]:
    """Return status rows for all .zip files in a folder."""
    if not folder.exists() or not folder.is_dir():
        logger.error(f"Folder not found at '{folder}'.")
        return []
    existing_symbols = update_existing_symbols_cache()
    zip_paths = sorted(p for p in folder.glob("*.zip") if p.is_file())
    return [_classify_zip(p, existing_symbols) for p in zip_paths]


# =========================
# CLI call (process/purge)
# =========================
def process_archives(
    paths: List[Path], *, is_purge: bool, rename_assets: bool, use_symbol_name: bool
) -> bool:
    """Process or purge selected ZIPs; returns success."""
    if not paths:
        logger.error("No ZIP files selected for action.")
        return False

    action_name = "PURGE" if is_purge else "PROCESS"
    logger.info(f"--- Initiating {action_name} for {len(paths)} file(s) ---")

    success = True
    for z in paths:
        logger.info(f"--- {action_name} {z.name} ---")
        try:
            if is_purge:
                purge_zip_contents(z)
            else:
                process_zip(z, rename_assets=rename_assets, use_symbol_name=use_symbol_name)
            logger.info(f"{z.name} done.")
        except Exception as e:
            logger.error(f"[FAIL] {z.name} failed: {e}")
            success = False

    logger.info(f"--- {action_name} COMPLETE ---")
    return success


# =========================
# Export logic
# =========================
def _load_symbol_footprints() -> Dict[str, str]:
    """Map symbol -> footprint field from project symbol lib."""
    try:
        sexp = loads(PROJECT_SYMBOL_LIB.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[FAIL] Could not read symbol library: {e}")
        return {}

    symbol_footprints: Dict[str, str] = {}
    for el in sexp[1:]:
        if isinstance(el, list) and len(el) > 1 and str(el[0]) == "symbol":
            sym_name = str(el[1])
            footprint_field = None
            for item in el:
                if (
                    isinstance(item, list)
                    and len(item) >= 3
                    and str(item[0]) == "property"
                    and str(item[1]) == "Footprint"
                ):
                    footprint_field = str(item[2])
                    break
            if footprint_field:
                symbol_footprints[sym_name] = footprint_field
    return symbol_footprints


def export_symbols_with_checks(selected_symbols: List[str]) -> Tuple[bool, List[Path]]:
    """
    Validate footprints/3D models for selected symbols, then export.
    Returns (success, export_paths).
    """
    if not selected_symbols:
        logger.warning("No symbols selected for export.")
        return False, []

    symbol_footprints = _load_symbol_footprints()
    valid_symbols: List[str] = []
    missing_footprints: List[str] = []
    missing_models: List[str] = []

    for sym in selected_symbols:
        footprint_name = None
        for candidate in (sym, f"LIB_{sym}"):
            if candidate in symbol_footprints:
                footprint_name = symbol_footprints[candidate]
                break
        if not footprint_name:
            missing_footprints.append(sym)
            continue

        footprint_basename = footprint_name.split(":")[-1]
        found_fp = None
        for fp in PROJECT_FOOTPRINT_LIB.rglob("*.kicad_mod"):
            if fp.stem == footprint_basename:
                found_fp = fp
                break

        if not found_fp:
            missing_footprints.append(sym)
            continue

        model_files: List[Path] = []
        try:
            for raw_line in found_fp.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if line.startswith("(model "):
                    segment = line.split("(model", 1)[1]
                    segment = segment.split(")", 1)[0].strip().strip('"')
                    expanded = os.path.expandvars(segment)
                    expanded = (
                        expanded.replace("${KICAD7_3DMODEL_DIR}", "3d_models")
                        .replace("${KICAD6_3DMODEL_DIR}", "3d_models")
                        .replace("${KICAD8_3DMODEL_DIR}", "3d_models")
                    )
                    model_files.append(Path(expanded))
        except Exception:
            pass

        resolved_models: List[Path] = []
        for m in model_files:
            if m.is_absolute() and m.exists():
                resolved_models.append(m)
            else:
                test_path = (PROJECT_FOOTPRINT_LIB.parent / m).resolve()
                if test_path.exists():
                    resolved_models.append(test_path)
                else:
                    missing_models.append(str(m))

        logger.info(
            f"Found {len(resolved_models)} 3D file(s) for {sym}: "
            + ", ".join(m.name for m in resolved_models)
        )
        valid_symbols.append(sym)

    if missing_footprints:
        logger.warning(f"Missing footprints for: {', '.join(missing_footprints)}")
    if missing_models:
        logger.warning(f"Missing 3D models: {', '.join(missing_models)}")
    if not valid_symbols:
        logger.error("No valid symbols found (missing or unresolved footprints).")
        return False, []

    export_paths = export_symbols(valid_symbols)
    if export_paths:
        logger.info(f"Exported {len(export_paths)} ZIP file(s) successfully.")
        outdir = export_paths[0].parent if hasattr(export_paths[0], "parent") else None
        if outdir:
            logger.info(f"Output directory: {outdir}")
        else:
            logger.warning("Could not determine output directory.")
        return True, export_paths

    logger.error("[FAIL] Export returned no files.")
    return False, []


# =========================
# DRC updater
# =========================
def update_drc_rules() -> bool:
    """
    Auto-select correct .kicad_dru template based on copper layer count
    and copy it to Project.kicad_dru.
    """
    try:
        cwd = Path.cwd()
        pcb = None
        for parent in [cwd] + list(cwd.parents):
            hits = list(parent.glob("*.kicad_pcb"))
            if hits:
                pcb = hits[0]
                break
        if not pcb:
            logger.error("[ERROR] No .kicad_pcb file found.")
            return False
        logger.info(f"Found PCB file: {pcb.name}")

        with pcb.open("r", encoding="utf-8") as f:
            sexpr = loads(f.read())

        layers_block = None
        for e in sexpr:
            if isinstance(e, list) and e and e[0] == Symbol("layers"):
                layers_block = e
                break
        if not layers_block:
            logger.error("[ERROR] No (layers ...) block found in PCB file.")
            return False

        copper_layers = [
            layer
            for layer in layers_block[1:]
            if isinstance(layer, list)
            and len(layer) > 1
            and str(layer[1]).endswith(".Cu")
        ]
        layer_count = len(copper_layers)
        logger.info(f"Detected {layer_count} copper layers.")

        dru_template_dir = None
        preferred_roots = [
            Path(__file__).resolve().parent,
        ]
        for parent in preferred_roots + [cwd] + list(cwd.parents):
            cand = parent / "dru_templates"
            if cand.exists() and cand.is_dir():
                dru_template_dir = cand
                break
        if not dru_template_dir:
            logger.error("[ERROR] No 'dru_templates' folder found.")
            logger.error("[INFO] Ensure dru_templates is bundled with the plugin package or present near the project.")
            return False

        src = next(
            (fp for fp in dru_template_dir.glob(f"dru_{layer_count}_layer.kicad_dru")),
            None,
        )
        if not src or not src.exists():
            logger.error(f"[ERROR] No template found for {layer_count} layers.")
            return False

        dst = None
        for parent in [cwd] + list(cwd.parents):
            hits = list(parent.glob("Project.kicad_dru"))
            if hits:
                dst = hits[0]
                break
        if not dst:
            dst = Path.cwd() / "Project.kicad_dru"

        shutil.copyfile(src, dst)
        logger.info(f"Applied {src.name} -> {dst.name}")
        logger.info("[SUCCESS] DRC updated successfully.")
        return True

    except Exception as e:
        logger.error(f"[FAIL] DRC update failed: {e}")
        return False


# =========================
# Folder helpers
# =========================
def open_folder_in_explorer(path: Path) -> None:
    """Open a folder in the OS file explorer."""
    try:
        if not path.exists() or not path.is_dir():
            logger.error(f"Folder path does not exist: {path}")
            return
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        logger.info(f"Opened folder in explorer: {path}")
    except Exception as e:
        logger.error(f"Failed to open folder in explorer: {e}")


def open_output_folder() -> None:
    """Open /library_output in system explorer."""
    output_folder = INPUT_ZIP_FOLDER.parent / "library_output"
    output_folder.mkdir(parents=True, exist_ok=True)
    open_folder_in_explorer(output_folder)
