"""
KiCad Library Manager CLI
=========================
Unified CLI entry point for processing, purging, or exporting component ZIPs.

Usage examples:
    python cli_main.py process my_part.zip --rename-assets
    python cli_main.py purge my_part.zip
    python cli_main.py export --symbols U1 U2 U3
"""

import sys
import locale
import argparse
from pathlib import Path
import logging

# --- UTF-8 fix for Windows console ---
if sys.platform.startswith("win"):
    if locale.getpreferredencoding(False).lower() not in {"utf-8", "utf8"}:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

# --- Import project logic ---
from library_manager import (
    INPUT_ZIP_FOLDER,
    PROJECT_SYMBOL_LIB,
    process_zip,
    purge_zip_contents,
    export_symbols,
    list_symbols_simple,
)

# =========================================================
# Logging setup
# =========================================================
logger = logging.getLogger("kicad_library_manager")
logger.setLevel(logging.INFO)

# Only add console handler if not in GUI context
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    # Skip adding console handler if running under wx GUI
    if "wx" not in sys.modules:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
        )
        logger.addHandler(console_handler)

logger.propagate = False

# =========================================================
# CLI argument parser
# =========================================================
def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="KiCad Library Manager CLI",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "action",
        choices=["process", "purge", "export"],
        help='Action: "process" (import), "purge" (delete), or "export" (create ZIP)',
    )
    parser.add_argument(
        "--rename-assets",
        action="store_true",
        help="(Process only) Rename footprints and 3D models to match the symbol name.",
    )
    parser.add_argument(
        "--use-symbol-name",
        action="store_true",
        help="Use symbol name as footprint and 3D model name.",
    )
    parser.add_argument(
        "--input-folder",
        type=str,
        help=f"Folder containing ZIPs (default: {INPUT_ZIP_FOLDER})",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=[],
        help="Symbol names for export (used with 'export' action).",
    )
    parser.add_argument(
        "zip_files",
        nargs="*",
        type=str,
        help="ZIP files to process or purge. If empty, uses all ZIPs in input folder.",
    )

    return parser.parse_args(argv)


# =========================================================
# Main runner function
# =========================================================
def run_cli_action(
    action: str,
    zip_files=None,
    input_folder=None,
    rename_assets=False,
    use_symbol_name=False,
    symbols=None,
):
    """
    Unified entry point for CLI and GUI.
    Returns: (success: bool, output: str)
    """
    if zip_files is None:
        zip_files = []
    if symbols is None:
        symbols = []

    args = parse_arguments(
        [action]
        + (["--input-folder", str(input_folder)] if input_folder else [])
        + (["--rename-assets"] if rename_assets else [])
        + (["--use-symbol-name"] if use_symbol_name else [])
        + (["--symbols"] + symbols if symbols else [])
        + zip_files
    )

    success = True
    folder = Path(args.input_folder) if args.input_folder else INPUT_ZIP_FOLDER

    if args.action == "export":
        if not args.symbols:
            logger.error("No symbols specified for export.")
            return False, ""
        logger.info(f"--- EXPORTING {len(args.symbols)} SYMBOL(S) ---")
        export_symbols(args.symbols)
        logger.info("--- EXPORT COMPLETE ---")
        return True, ""

    # For process/purge
    zip_paths = [Path(z) for z in args.zip_files] if args.zip_files else list(folder.glob("*.zip"))
    if not zip_paths:
        logger.warning(f"No ZIP files found in '{folder}'.")
        list_symbols_simple(PROJECT_SYMBOL_LIB, print_list=True)
        return False, ""

    for z in zip_paths:
        print("")
        logger.info(f"--- {args.action.upper()} {z.name} ---")
        try:
            if args.action == "purge":
                purge_zip_contents(z)
            else:
                process_zip(z, rename_assets=args.rename_assets, use_symbol_name=args.use_symbol_name)
            logger.info(f"{z.name} done.")
        except Exception as e:
            logger.error(f"[FAIL] {z.name} failed: {e}")
            success = False

    print("")
    logger.info("--- FINAL SYMBOL LIST ---")
    list_symbols_simple(PROJECT_SYMBOL_LIB, print_list=True)
    return success, ""


# =========================================================
# CLI entry point
# =========================================================
def main():
    args = parse_arguments()
    success, _ = run_cli_action(
        action=args.action,
        zip_files=args.zip_files,
        input_folder=args.input_folder,
        rename_assets=args.rename_assets,
        use_symbol_name=args.use_symbol_name,
        symbols=args.symbols,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
