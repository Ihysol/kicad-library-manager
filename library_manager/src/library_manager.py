# SPDX-License-Identifier: BSD-3-Clause
# Refactored for clarity/minimal duplication. Runtime behavior preserved.

import os
import re
import sys
import json
import shutil
import zipfile
from uuid import uuid4
from pathlib import Path
from datetime import datetime
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False
from sexpdata import loads, dumps, Symbol

# ==========
# Logger
# ==========
import logging

logger = logging.getLogger("kicad_library_manager")

# ---------------------------------------------------------------------------------
# Constants / schema markers
# ---------------------------------------------------------------------------------

KICAD8_SCHEMA = 20221018
KICAD9_SCHEMA = 20240115
KICAD9_FOOTPRINT_SCHEMA = 20241229  # KiCad 9 .kicad_mod "version"

SUB_PART_PATTERN = re.compile(r"_\d(_\d)+$|_\d$")  # strip _1_1 / _2 etc.

# Only persist these properties when importing symbols
ALLOWED_SYMBOL_PROPERTIES = {
    "Value",
    "Reference",
    "Footprint",
    "Datasheet",
    "Description",
    "LCSC",
    "MNR",
}

# will be defined after environment bootstrap
PROJECT_DIR: Path
PROJECT_SYMBOL_LIB: Path
PROJECT_FOOTPRINT_LIB: Path
PROJECT_FOOTPRINT_LIB_NAME: str
PROJECT_3D_DIR: Path
INPUT_ZIP_FOLDER: Path
TEMP_MAP_FILE: Path

# ---------------------------------------------------------------------------------
# Small generic helpers
# ---------------------------------------------------------------------------------


def find_upward(target: str, start_path: Path) -> Path | None:
    """
    Walk upwards from start_path looking for either:
    - a folder with the exact name `target`, or
    - a file matching glob `target` (e.g. "*.kicad_pro").
    """
    for parent in [start_path] + list(start_path.parents):
        # Folder exact match
        candidate = parent / target
        if candidate.exists() and candidate.is_dir():
            return candidate
        # File glob
        matches = list(parent.glob(target))
        if matches:
            return matches[0]
    return None


def detect_project_version(start_path: Path) -> int:
    """
    Detect current project's KiCad 'schema version' to decide 8 vs 9 behavior.
    Order:
      1. Read (generator_version "X.Y") from nearest .kicad_sch
      2. Fallback to (version XXXXX) from .kicad_pro or .kicad_pcb
      3. Default KiCad 8 schema
    """

    def major_to_schema(ver_str: str) -> int:
        try:
            major = int(ver_str.split(".")[0])
        except Exception:
            return KICAD8_SCHEMA
        return KICAD9_SCHEMA if major >= 9 else KICAD8_SCHEMA

    sch_file = find_upward("*.kicad_sch", start_path)
    if sch_file and sch_file.exists():
        try:
            with open(sch_file, encoding="utf-8") as f:
                sch_data = loads(f.read())
            for node in sch_data:
                if (
                    isinstance(node, list)
                    and len(node) >= 2
                    and (
                        node[0] == Symbol("generator_version")
                        or node[0] == "generator_version"
                    )
                ):
                    ver = str(node[1]).strip('"')
                    schema = major_to_schema(ver)
                    logger.debug(f"Detectegenerator_version {ver} → schema {schema}")
                    return schema
        except Exception as e:
            logger.warning(
                f"Failed to parse {sch_file.name} for generator_version: {e}"
            )

    for pattern in ["*.kicad_pro", "*.kicad_pcb"]:
        proj_file = find_upward(pattern, start_path)
        if proj_file and proj_file.exists():
            try:
                with open(proj_file, encoding="utf-8") as f:
                    proj_data = loads(f.read())
                for node in proj_data:
                    if (
                        isinstance(node, list)
                        and len(node) >= 2
                        and (node[0] == Symbol("version") or node[0] == "version")
                    ):
                        ver_val = int(node[1])
                        logger.debug(
                            f"Detected version {ver_val} from {proj_file.name}"
                        )
                        return ver_val
            except Exception as e:
                logger.warning(f"Failed to parse {proj_file.name} for version: {e}")

    logger.warning("Could not detect KiCad version; defaulting to KiCad 8 (20221018).")
    return KICAD8_SCHEMA


def get_existing_main_symbols() -> set[str]:
    """Return the set of main symbol base names already in ProjectSymbols.kicad_sym."""
    return set(list_symbols_simple(PROJECT_SYMBOL_LIB, print_list=False))


def list_symbols_simple(sym_file: Path, print_list: bool = True) -> list[str]:
    """
    Return list of "main" symbol names from sym_file.
    Filters out sub-units like "_1_1".
    """
    if not sym_file.exists():
        if print_list:
            logger.warning(f"File not found: {sym_file.name}")
        return []

    try:
        with open(sym_file, "r", encoding="utf-8") as f:
            sexp_list = loads(f.read())
    except Exception as e:
        if print_list:
            logger.error(f"ERROR: Failed to parse S-expression in {sym_file.name}: {e}")
        return []

    symbols: list[str] = []
    for element in sexp_list[1:]:
        if (
            isinstance(element, list)
            and len(element) > 1
            and (element[0] == "symbol" or element[0] == Symbol("symbol"))
        ):
            symbol_name = str(element[1])
            if not SUB_PART_PATTERN.search(symbol_name):
                symbols.append(symbol_name)

    if print_list:
        logger.info(f"Found {len(symbols)} (main) symbols in {sym_file.name}:")
        logger.info(", ".join(symbols) if symbols else "No main symbols found.")

    return symbols


def find_sexp_element(sexp_list, target_tag: str):
    """Return the first S-expression (list) in sexp_list whose head == target_tag."""
    head_sym = Symbol(target_tag)
    for el in sexp_list:
        if isinstance(el, list) and el and (el[0] == target_tag or el[0] == head_sym):
            return el
    return None


def find_sexp_property(sexp_list, prop_name: str):
    """
    Return the first
        (property "<name>" "<value>" ...)
    in sexp_list with matching <name>.
    """
    prop_sym = Symbol("property")
    for el in sexp_list:
        if (
            isinstance(el, list)
            and len(el) > 2
            and (el[0] == "property" or el[0] == prop_sym)
        ):
            if str(el[1]) == prop_name:
                return el
    return None


# ---------------------------------------------------------------------------------
# Symbol conversion (8 <-> 9) helpers
# ---------------------------------------------------------------------------------


def _find_symbol_uuid_block(sym_node) -> int:
    """Return index of (uuid "...") in this symbol node, or -1."""
    for idx, child in enumerate(sym_node):
        if isinstance(child, list) and child and str(child[0]) == "uuid":
            return idx
    return -1


def _strip_children_by_head(sym_node, banned_heads: set[str]):
    """Return a copy of sym_node with any child whose head is in banned_heads removed."""
    cleaned = []
    for child in sym_node:
        if isinstance(child, list) and child:
            if str(child[0]) in banned_heads:
                continue
        cleaned.append(child)
    return cleaned


def convert_symbol_expr(sym_node, src_schema: int, dst_schema: int):
    """
    Convert one (symbol "NAME" ...) between KiCad 8/9 formats without touching geometry.

    8 → 9:
        - remove pin_names / pin_numbers
        - insert uuid if missing
    9 → 8:
        - remove uuid
    """
    if not (
        isinstance(sym_node, list)
        and len(sym_node) >= 2
        and str(sym_node[0]) == "symbol"
    ):
        return sym_node

    out = [c for c in sym_node]

    if src_schema == dst_schema:
        return out

    # 8 -> 9
    if src_schema < KICAD9_SCHEMA and dst_schema >= KICAD9_SCHEMA:
        out = _strip_children_by_head(out, {"pin_names", "pin_numbers"})
        if _find_symbol_uuid_block(out) == -1:
            if len(out) >= 2:
                out.insert(2, [Symbol("uuid"), str(uuid4())])
            else:
                out.append([Symbol("uuid"), str(uuid4())])
        return out

    # 9 -> 8
    if src_schema >= KICAD9_SCHEMA and dst_schema < KICAD9_SCHEMA:
        out = _strip_children_by_head(out, {"uuid"})
        return out

    return out


def _convert_symbol_recursive(sym_node, src_schema, dst_schema):
    """Recursively convert nested (symbol ...) blocks."""
    converted_top = convert_symbol_expr(sym_node, src_schema, dst_schema)
    out_children = []
    for child in converted_top:
        if isinstance(child, list) and child and str(child[0]) == "symbol":
            out_children.append(
                _convert_symbol_recursive(child, src_schema, dst_schema)
            )
        else:
            out_children.append(child)
    return out_children


# ---------------------------------------------------------------------------------
# Symbol normalization for project (KiCad 8 hide rules, property placement, etc.)
# ---------------------------------------------------------------------------------


def normalize_expr_for_project(expr, project_version: int):
    """
    Project-facing cleanup:
    - KiCad 8: strip unsupported stuff, make sure pin headers exist,
        hide/move non-Reference/Value properties to the right of the symbol.
    - KiCad 9: ensure (uuid ...) in symbols.
    Also ensures (version ...) matches 8/9 schema.
    """
    HIDDEN_OFFSET_MARGIN_X = 20  # mm to right of rightmost pin
    HIDDEN_OFFSET_STEP_Y = -2  # vertical spacing between properties
    HIDDEN_ROT = 0

    def deep_strip(e):
        """Remove KiCad9-only nodes that KiCad8 doesn't understand."""
        banned = {
            Symbol("uuid"),
            Symbol("extends"),
            Symbol("template"),
            Symbol("lib_id"),
            Symbol("style"),
            Symbol("parent"),
            Symbol("embedded_fonts"),
            Symbol("text_styles"),
        }
        if not isinstance(e, list):
            return e
        if e and e[0] in banned:
            return None
        cleaned = []
        for sub in e:
            sub_clean = deep_strip(sub)
            if sub_clean is not None:
                cleaned.append(sub_clean)
        return cleaned

    def strip_hide_flags(e):
        """Drop standalone (hide yes) before we rebuild effects/hide ourselves."""
        if not isinstance(e, list):
            return e
        cleaned = []
        for sub in e:
            if isinstance(sub, list):
                if len(sub) == 2 and str(sub[0]) == "hide" and str(sub[1]) == "yes":
                    continue
                sub = strip_hide_flags(sub)
                if sub is not None:
                    cleaned.append(sub)
            else:
                cleaned.append(sub)
        return cleaned

    def add_uuids(e):
        """Ensure each (symbol ...) block in KiCad 9 has (uuid ...)."""
        if isinstance(e, list):
            newnode = [add_uuids(x) for x in e]
            if (
                e
                and e[0] == Symbol("symbol")
                and not any(
                    isinstance(i, list) and i and i[0] == Symbol("uuid") for i in e
                )
            ):
                newnode.insert(2, [Symbol("uuid"), str(uuid4())])
            return newnode
        return e

    def ensure_pin_headers(sym):
        """KiCad 8: make sure each symbol has (pin_numbers ...) and (pin_names ...)."""
        if not (isinstance(sym, list) and sym and sym[0] == Symbol("symbol")):
            return sym

        has_pin_names = any(
            isinstance(i, list) and i and i[0] == Symbol("pin_names") for i in sym
        )
        has_pin_numbers = any(
            isinstance(i, list) and i and i[0] == Symbol("pin_numbers") for i in sym
        )

        insert_pos = 2 if len(sym) > 2 else len(sym)
        if not has_pin_numbers:
            sym.insert(insert_pos, [Symbol("pin_numbers"), Symbol("hide")])
            insert_pos += 1
        if not has_pin_names:
            sym.insert(
                insert_pos, [Symbol("pin_names"), [Symbol("offset"), 0], Symbol("hide")]
            )

        for i, sub in enumerate(sym):
            if isinstance(sub, list) and sub and sub[0] == Symbol("symbol"):
                sym[i] = ensure_pin_headers(sub)
        return sym

    def get_rightmost_pin_x(symbol_node):
        """Max pin X (for property placement offset)."""
        max_x = 0
        for child in symbol_node:
            if isinstance(child, list) and child and child[0] == Symbol("pin"):
                for elem in child:
                    if isinstance(elem, list) and elem and elem[0] == Symbol("at"):
                        try:
                            x = float(elem[1])
                            if x > max_x:
                                max_x = x
                        except Exception:
                            pass
        return max_x

    def fix_property_layout_recursive(
        node, rightmost_x=0, prop_index=[0], for_kicad8=False
    ):
        """
        For all properties except Reference/Value:
        - place them at x=right_of_symbol, y staggered by 2mm
        - hide them (KiCad8 style or KiCad9 style)
        """
        if not isinstance(node, list):
            return node

        if node and node[0] == Symbol("property"):
            name = str(node[1]).strip('"') if len(node) > 1 else ""
            if name not in ("Reference", "Value"):
                # wipe old at/hide/effects
                node[:] = [
                    x
                    for x in node
                    if not (
                        isinstance(x, list)
                        and x
                        and x[0] in (Symbol("at"), Symbol("hide"), Symbol("effects"))
                    )
                ]

                x_offset = rightmost_x + HIDDEN_OFFSET_MARGIN_X
                y_offset = HIDDEN_OFFSET_STEP_Y * prop_index[0]
                prop_index[0] += 1

                if for_kicad8:
                    node.append(
                        [
                            Symbol("effects"),
                            [Symbol("justify"), Symbol("left")],
                            [Symbol("hide")],
                        ]
                    )
                else:
                    node.append(
                        [
                            Symbol("effects"),
                            [Symbol("justify"), Symbol("left")],
                        ]
                    )
                    node.append([Symbol("hide"), Symbol("yes")])

                node.append([Symbol("at"), x_offset, y_offset, 0])

        for i, sub in enumerate(node):
            if isinstance(sub, list):
                node[i] = fix_property_layout_recursive(
                    sub, rightmost_x, prop_index, for_kicad8
                )
        return node

    def prune_properties(node):
        """Remove any (property ...) blocks not in the allowed whitelist."""
        if not isinstance(node, list):
            return node
        cleaned = []
        for child in node:
            if (
                isinstance(child, list)
                and child
                and child[0] == Symbol("property")
            ):
                name = str(child[1]).strip('"') if len(child) > 1 else ""
                if name not in ALLOWED_SYMBOL_PROPERTIES:
                    continue
            cleaned.append(prune_properties(child))
        return cleaned

    def ensure_property(sym_node, prop_name: str, default="~"):
        """Add missing property with a hidden default value for part metadata."""
        if not (
            isinstance(sym_node, list)
            and sym_node
            and sym_node[0] == Symbol("symbol")
        ):
            return

        existing = find_sexp_property(sym_node, prop_name)
        if existing:
            if len(existing) > 2 and str(existing[2]).strip() == "":
                existing[2] = default
            return

        new_prop = [
            Symbol("property"),
            prop_name,
            default,
            [Symbol("at"), 0, 0, 0],
            [
                Symbol("effects"),
                [Symbol("font"), [Symbol("size"), 1.27, 1.27]],
                [Symbol("justify"), Symbol("left")],
            ],
            [Symbol("hide"), Symbol("yes")],
        ]

        insert_pos = 2
        for idx, child in enumerate(sym_node):
            if isinstance(child, list) and child and child[0] == Symbol("property"):
                insert_pos = idx + 1
        sym_node.insert(insert_pos, new_prop)

    def add_default_part_properties(expr_root):
        """Ensure each main symbol has LCSC/MNR properties with default '~'."""
        if not isinstance(expr_root, list):
            return
        for el in expr_root:
            if isinstance(el, list) and el and el[0] == Symbol("symbol"):
                name = str(el[1]) if len(el) > 1 else ""
                if SUB_PART_PATTERN.search(name):
                    continue
                ensure_property(el, "MNR")
                ensure_property(el, "LCSC")

    expr = prune_properties(expr)
    add_default_part_properties(expr)

    # Main branch: KiCad 8 vs 9
    if project_version < KICAD9_SCHEMA:
        # KiCad 8
        expr = deep_strip(expr)
        expr = strip_hide_flags(expr)
        expr = ensure_pin_headers(expr)

        if isinstance(expr, list):
            for i, child in enumerate(expr):
                if isinstance(child, list) and child and child[0] == Symbol("symbol"):
                    rightmost_x = get_rightmost_pin_x(child)
                    expr[i] = fix_property_layout_recursive(
                        child,
                        rightmost_x,
                        prop_index=[0],
                        for_kicad8=True,
                    )

        # fix (version ...)
        found = False
        for i, e in enumerate(expr):
            if isinstance(e, list) and e and e[0] == Symbol("version"):
                expr[i][1] = KICAD8_SCHEMA
                found = True
                break
        if not found:
            expr.insert(1, [Symbol("version"), KICAD8_SCHEMA])

    else:
        # KiCad 9
        expr = add_uuids(expr)

        found = False
        for i, e in enumerate(expr):
            if isinstance(e, list) and e and e[0] == Symbol("version"):
                expr[i][1] = KICAD9_SCHEMA
                found = True
                break
        if not found:
            expr.insert(1, [Symbol("version"), KICAD9_SCHEMA])

    return expr


def ensure_project_symbol_header(project_sym_path: Path, project_version: int):
    """
    Ensure ProjectSymbols.kicad_sym has correct (version ...), (generator ...),
    and (generator_version ...).
    """
    if not project_sym_path.exists():
        return

    try:
        with open(project_sym_path, "r", encoding="utf-8") as f:
            sexpr = loads(f.read())
    except Exception as e:
        logger.warning(
            f"Failed to parse {project_sym_path.name} for header update: {e}"
        )
        return
    if not isinstance(sexpr, list) or not sexpr:
        return

    target_schema = KICAD9_SCHEMA if project_version >= KICAD9_SCHEMA else KICAD8_SCHEMA
    gen_ver = "9.0" if target_schema >= KICAD9_SCHEMA else "8.0"

    # remove any old version/generator/generator_version
    sexpr = [
        x
        for x in sexpr
        if not (
            isinstance(x, list)
            and x
            and str(x[0]) in ("version", "generator", "generator_version")
        )
    ]

    # insert updated header triplet at top after root tag
    sexpr.insert(1, [Symbol("version"), target_schema])
    sexpr.insert(2, [Symbol("generator"), "CSE-Manager"])
    sexpr.insert(3, [Symbol("generator_version"), gen_ver])

    try:
        with open(project_sym_path, "w", encoding="utf-8") as f:
            f.write(
                dumps(
                    sexpr,
                    wrap=Symbol("kicad_symbol_lib"),
                    pretty_print=True,
                )
            )
        logger.info(
            f"Updated header in {project_sym_path.name} → schema {target_schema}"
        )
    except Exception as e:
        logger.warning(
            f"Could not write updated header for {project_sym_path.name}: {e}"
        )


def append_symbols_from_file(src_sym_file: Path, rename_assets=False):
    """
    Appends symbols from a source KiCad library file to the project library.
    Automatically detects and converts between KiCad 8 and 9 symbol formats.
    Ensures correct wrapping, valid structure, and removes invalid top-level UUIDs.
    """

    def _flatten_lists(node):
        """Remove unnecessary single-item list wrappers in S-expressions."""
        if isinstance(node, list):
            if len(node) == 1 and isinstance(node[0], list):
                return _flatten_lists(node[0])
            return [_flatten_lists(n) for n in node]
        return node

    def _remove_top_uuid(node):
        """Remove top-level (uuid ...) entries under each (symbol ...) block."""
        if isinstance(node, list):
            if node and node[0] == Symbol("symbol"):
                node[:] = [
                    n
                    for n in node
                    if not (isinstance(n, list) and n and n[0] == Symbol("uuid"))
                ]
            for child in node:
                if isinstance(child, list):
                    _remove_top_uuid(child)

    existing_main_symbols = get_existing_main_symbols()
    footprint_map = {}
    if TEMP_MAP_FILE.exists():
        with open(TEMP_MAP_FILE, "r") as f:
            footprint_map = json.load(f)

    try:
        with open(src_sym_file, "r", encoding="utf-8") as f:
            src_sexp = loads(f.read())

        project_version = detect_project_version(PROJECT_DIR)
        src_sexp = normalize_expr_for_project(src_sexp, project_version)
        logger.info(f"Cleaned {src_sym_file.name} for KiCad schema {project_version}")

        # Automatically remove illegal UUIDs for KiCad 9
        if project_version >= KICAD9_SCHEMA:
            _remove_top_uuid(src_sexp)

    except FileNotFoundError:
        logger.error(f"[ERROR] Source file not found: {src_sym_file.name}")
        return False
    except Exception as e:
        logger.error(f"[ERROR] Parsing S-expression in {src_sym_file.name}: {e}")
        return False

    symbols_to_append = []
    appended_any = False

    # --- Collect new symbols ---
    for element in src_sexp[1:]:
        if (
            isinstance(element, list)
            and len(element) > 1
            and (element[0] == "symbol" or element[0] == Symbol("symbol"))
        ):
            symbol_name = str(element[1])
            base_name = SUB_PART_PATTERN.sub("", symbol_name)

            if base_name not in existing_main_symbols:
                # Fix footprint linkage
                raw_fp = None
                prop = find_sexp_property(element, "Footprint")
                if prop:
                    raw_fp = str(prop[2]).split(":")[-1]
                    link_name = base_name if rename_assets else raw_fp
                    prop[2] = f"{PROJECT_FOOTPRINT_LIB_NAME}:{link_name}"

                fp_elem = find_sexp_element(element, "footprint")
                if fp_elem and len(fp_elem) > 1:
                    name_only = str(fp_elem[1]).split(":")[-1]
                    link_name = base_name if rename_assets else name_only
                    fp_elem[1] = f"{PROJECT_FOOTPRINT_LIB_NAME}:{link_name}"
                    if not raw_fp:
                        raw_fp = name_only

                if raw_fp:
                    footprint_map[raw_fp] = base_name

                symbols_to_append.append(element)
                existing_main_symbols.add(base_name)
                appended_any = True
                logger.info(f"Appended symbol: {symbol_name}")
            else:
                logger.info(f"[SKIP] Symbol already exists: {symbol_name}")

    if not appended_any:
        logger.warning(f"No new symbols added from {src_sym_file.name}")
        return False

    # --- Save footprint map ---
    with open(TEMP_MAP_FILE, "w") as f:
        json.dump(footprint_map, f, indent=4)

    project_sym_path = PROJECT_SYMBOL_LIB
    new_file_content = None

    # --- If project file exists, append to it ---
    if project_sym_path.exists():
        try:
            with open(project_sym_path, "r", encoding="utf-8") as f:
                project_sexp = loads(f.read())

            if not (
                isinstance(project_sexp, list)
                and len(project_sexp) > 0
                and str(project_sexp[0]) == "kicad_symbol_lib"
            ):
                project_sexp = [Symbol("kicad_symbol_lib")] + project_sexp

            project_sexp.extend(symbols_to_append)
            project_sexp = _flatten_lists(project_sexp)
            if project_version >= KICAD9_SCHEMA:
                _remove_top_uuid(project_sexp)
            new_file_content = dumps(project_sexp, pretty_print=True, wrap=None)

        except Exception as e:
            logger.warning(f"Error modifying project library: {e}. Recreating file.")
            project_sym_path.unlink(missing_ok=True)
            new_file_content = None

    # --- If file doesn't exist, create new ---
    if not project_sym_path.exists() or new_file_content is None:
        target_schema = (
            KICAD9_SCHEMA
            if detect_project_version(PROJECT_DIR) >= KICAD9_SCHEMA
            else KICAD8_SCHEMA
        )
        gen_version = "9.0" if target_schema >= KICAD9_SCHEMA else "8.0"

        header = [
            ["version", target_schema],
            ["generator", "CSE-Manager"],
            ["generator_version", gen_version],
        ]

        full_sexp = [Symbol("kicad_symbol_lib")] + header + symbols_to_append
        full_sexp = _flatten_lists(full_sexp)
        if target_schema >= KICAD9_SCHEMA:
            _remove_top_uuid(full_sexp)
        new_file_content = dumps(full_sexp, pretty_print=True, wrap=None)

    # --- Write final library ---
    with open(project_sym_path, "w", encoding="utf-8") as f:
        f.write(new_file_content)

    ensure_project_symbol_header(project_sym_path, detect_project_version(PROJECT_DIR))
    return True


# ---------------------------------------------------------------------------------
# Footprint (.kicad_mod) conversion and localization
# ---------------------------------------------------------------------------------


def _downgrade_footprint_for_v8(node):
    """
    Strip KiCad 9+ only constructs so KiCad 8 accepts it.
    Removes e.g. uuid, tstamp, text_styles, embedded_fonts, keepout, etc.
    """
    banned = {
        "uuid",
        "tstamp",
        "locked",
        "text_styles",
        "embedded_fonts",
        "font",
        "model_uuid",
        "layerselection",
        "constraint",
        "outline_anchor",
        "keepout",
        "zone_connect",
        "solder_paste_ratio",
        "thermal_bridge_angle",
        "solder_mask_ratio",
    }

    if isinstance(node, list):
        head = str(node[0])
        if head in banned:
            return None
        cleaned = []
        for sub in node:
            sub_clean = _downgrade_footprint_for_v8(sub)
            if sub_clean is not None:
                cleaned.append(sub_clean)
        return cleaned
    return node


def _add_uuid_if_missing(node):
    """Add a (uuid ...) to a 'module' block when upgrading to KiCad 9."""
    if not isinstance(node, list):
        return node
    newnode = [_add_uuid_if_missing(x) for x in node]
    if (
        node
        and str(node[0]) == "module"
        and not any(isinstance(i, list) and i and i[0] == Symbol("uuid") for i in node)
    ):
        newnode.insert(2, [Symbol("uuid"), str(uuid4())])
    return newnode


def force_footprint_version(mod_text: str, dst_schema: int) -> str:
    """
    Convert a footprint string between KiCad 8 and KiCad 9:
    - Keep the root as (module "<name>") for both.
    - For KiCad 8 (dst < KICAD9_SCHEMA): strip 9-only nodes and set version 20221018.
    - For KiCad 9 (dst >= KICAD9_SCHEMA): add uuid if missing and set version 20241229.
    """
    try:
        sexp = loads(mod_text)
        # Some inputs come nested like [[module ...]]; flatten if so.
        if isinstance(sexp, list) and len(sexp) == 1 and isinstance(sexp[0], list):
            sexp = sexp[0]
    except Exception as e:
        logger.warning(f"Could not parse footprint: {e}")
        return mod_text

    # --- Force root: module "<name>" (never kicad_mod) ---
    if not sexp or str(sexp[0]) not in {"module", Symbol("module")}:
        sexp[0] = Symbol("module")
    # Ensure name string is present at index 1
    if len(sexp) < 2 or not isinstance(sexp[1], str):
        sexp.insert(1, "Unnamed_Footprint")

    if dst_schema < KICAD9_SCHEMA:
        # Downgrade to KiCad 8
        sexp = _downgrade_footprint_for_v8(sexp)
        has_version = False
        for sub in sexp:
            if isinstance(sub, list) and str(sub[0]) == "version":
                sub[1] = KICAD8_SCHEMA
                has_version = True
        if not has_version:
            sexp.insert(2, [Symbol("version"), KICAD8_SCHEMA])
        logger.info(f"Downgraded footprint '{sexp[1]}' → KiCad 8 (20221018)")
    else:
        # Upgrade to “KiCad 9 era” (still 'module' root)
        sexp = _add_uuid_if_missing(sexp)
        has_version = False
        for sub in sexp:
            if isinstance(sub, list) and str(sub[0]) == "version":
                sub[1] = KICAD9_FOOTPRINT_SCHEMA
                has_version = True
        if not has_version:
            sexp.insert(2, [Symbol("version"), KICAD9_FOOTPRINT_SCHEMA])
        logger.info(
            f"Upgraded footprint '{sexp[1]}' → KiCad 9 schema {KICAD9_FOOTPRINT_SCHEMA}"
        )

    # --- Serialize cleanly ---
    text = dumps(sexp, pretty_print=True, wrap=None)

    # Collapse redundant outer parentheses that sexpdata adds
    text = re.sub(r"\(\s+fp_", "(fp_", text)
    text = re.sub(r"\(\s+pad\s+", "(pad ", text)
    text = re.sub(r"\(\s+model\s+", "(model ", text)
    text = re.sub(r"\(\s+layer\s+", "(layer ", text)
    text = re.sub(r"\(\s+at\s+", "(at ", text)
    text = re.sub(r"\(\s+xyz\s+", "(xyz ", text)
    text = re.sub(r"\(\s+effects\s+", "(effects ", text)
    text = re.sub(r"\(\s+font\s+", "(font ", text)

    # Merge all fp_arc/fp_line/fp_text etc. blocks into single-line expressions
    def collapse_block(match):
        block = match.group(0)
        block = re.sub(r"\s*\n\s*", " ", block)  # collapse newlines
        block = re.sub(r"\s{2,}", " ", block)  # collapse double spaces
        return block.strip()

    text = re.sub(
        r"\(fp_(?:arc|line|text|poly|circle|rect)[^\)]*\)", collapse_block, text
    )
    text = re.sub(r"\(pad [^\)]*\)", collapse_block, text)
    text = re.sub(r"\(model [^\)]*\)", collapse_block, text)

    # Normalize spacing and close brackets
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\)\s+\)", "))", text)

    return text


def localize_3d_model_path(
    mod_file: Path, footprint_map: dict, mod_text: str | None = None
) -> str:
    """
    Rewrites (model "...") paths in `mod_text` (or on-disk file) to:
        ${KIPRJMOD}/3dmodels/<SymbolName>.stp
    Uses footprint_map[footprint_name] to figure out SymbolName.
    Returns new text.
    """
    footprint_name = mod_file.stem
    symbol_name = footprint_map.get(footprint_name, footprint_name)

    try:
        if mod_text is None:
            with open(mod_file, "r", encoding="utf-8") as f:
                mod_text = f.read()
        mod_sexp = loads(mod_text)
    except Exception as e:
        logger.warning(f"Could not parse {mod_file.name} for 3D localization: {e}")
        return mod_text

    modified = False
    for idx, elem in enumerate(mod_sexp):
        if isinstance(elem, list) and elem and str(elem[0]) == "model":
            new_path = f"${{KIPRJMOD}}/3dmodels/{symbol_name}.stp"
            if len(elem) > 1:
                mod_sexp[idx][1] = new_path
                modified = True

    return dumps(mod_sexp, pretty_print=True, wrap=None) if modified else mod_text


def rename_extracted_assets(
    tempdir: Path, footprint_map: dict, use_symbol_name: bool = False
) -> int:
    """
    Rename extracted footprints and 3D models according to the given mapping.
    If use_symbol_name=True, rename using the symbol base name instead.
    Returns the number of renamed files.
    """
    logger.info("rename_extracted_assets() called")
    logger.debug(f"use_symbol_name={use_symbol_name}, tempdir={tempdir}")
    if not footprint_map:
        logger.warning("footprint_map is empty")
    else:
        logger.debug(f"footprint_map has {len(footprint_map)} entries:")
    for k, v in footprint_map.items():
        logger.info(f"    {k} → {v}")

    rename_count = 0

    logger.debug("rename_extracted_assets() called")
    logger.debug(f"use_symbol_name={use_symbol_name}, tempdir={tempdir}")
    logger.debug(f"footprint_map has {len(footprint_map)} entries:")
    for k, v in footprint_map.items():
        logger.info(f"    {k} → {v}")

    # --- Footprints ---
    for mod_file in tempdir.rglob("*.kicad_mod"):
        stem = mod_file.stem
        new_name = mod_file.name
        symname = None

        if use_symbol_name:
            # find symbol name for this footprint name
            if stem in footprint_map:
                symname = footprint_map[stem]
        elif stem in footprint_map:
            # legacy rename mode
            symname = footprint_map[stem]

        if symname:
            new_name = f"{symname}.kicad_mod"
        else:
            logger.debug(f"No match for footprint {stem} in footprint_map")

        new_path = mod_file.with_name(new_name)
        if new_path != mod_file:
            try:
                mod_file.rename(new_path)
                rename_count += 1
                logger.info(f"Renamed footprint: {mod_file.name} → {new_path.name}")
            except Exception as e:
                logger.error(f"Error renaming footprint {mod_file.name}: {e}")

    # --- 3D Models ---
    for model_file in tempdir.rglob("*.stp"):
        stem = model_file.stem
        new_name = model_file.name
        symname = None

        if use_symbol_name:
            if stem in footprint_map:
                symname = footprint_map[stem]
        elif stem in footprint_map:
            symname = footprint_map[stem]

        if symname:
            new_name = f"{symname}{model_file.suffix}"
        else:
            logger.debug(f"No match for 3D model {stem} in footprint_map")

        new_path = model_file.with_name(new_name)
        if new_path != model_file:
            try:
                model_file.rename(new_path)
                rename_count += 1
                logger.info(f"Renamed 3D model: {model_file.name} → {new_path.name}")
            except Exception as e:
                logger.error(f"[FAIL] Error renaming 3D model {model_file.name}: {e}")

    if rename_count == 0:
        logger.warning(
            "No files were renamed — check footprint_map keys vs extracted file names:"
        )
    for mod in tempdir.rglob("*.kicad_mod"):
        logger.info(f"    found footprint file: {mod.name}")
    for stp in tempdir.rglob("*.stp"):
        logger.info(f"    found 3D model file: {stp.name}")
    else:
        logger.info(f"Renamed {rename_count} files total.")

    return rename_count


def fix_all_fp_arcs(text: str) -> str:
    """
    Normalize every (fp_arc ...) to KiCad 9 form:
      (fp_arc (start x y) (mid xm ym) (end x y) (layer <L>) (width <W>))
    Converts legacy (angle …) using proper sagitta geometry so 180° arcs don't collapse.
    """
    import re, math

    # --- helpers --------------------------------------------------------------
    def floats(s: str):
        return [float(x) for x in re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', s)]

    def rebuild_arc(block: str) -> str:
        # Extract pieces
        s_m = re.search(r'\(start\s+[^)]*\)', block)
        e_m = re.search(r'\(end\s+[^)]*\)', block)
        m_m = re.search(r'\(mid\s+[^)]*\)', block)
        a_m = re.search(r'\(angle\s+([^\)]+)\)', block)
        L_m = re.search(r'\(layer\s+([^) ]+)\)', block)
        W_m = re.search(r'\(width\s+([^) ]+)\)', block)

        if not (s_m and e_m):
            return block  # can't fix

        sx, sy = floats(s_m.group(0))[:2]
        ex, ey = floats(e_m.group(0))[:2]

        # If mid is missing but angle exists → compute with sagitta
        if (m_m is None) and a_m:
            ang_deg = float(a_m.group(1))
            # chord
            dx, dy = (ex - sx), (ey - sy)
            c = math.hypot(dx, dy)
            if c < 1e-12 or abs(ang_deg) < 1e-9:
                # degenerate: fall back to simple midpoint
                mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
            else:
                phi = math.radians(abs(ang_deg))  # central angle magnitude
                # radius & sagitta
                sin_half = math.sin(phi / 2.0)
                if abs(sin_half) < 1e-12:
                    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
                else:
                    R = c / (2.0 * sin_half)
                    s = R * (1.0 - math.cos(phi / 2.0))  # <-- correct sagitta
                    # chord midpoint
                    cx, cy = (sx + ex) / 2.0, (sy + ey) / 2.0
                    # unit normal (rotate chord by +90°)
                    nx, ny = (-dy / c, dx / c)
                    # angle sign picks which side of the chord
                    sign = -1.0 if ang_deg < 0 else 1.0
                    mx, my = cx + sign * s * nx, cy + sign * s * ny
            mid_str = f"(mid {mx:g} {my:g})"
            # remove (angle …) and insert (mid …)
            block = re.sub(r'\(angle\s+[^\)]+\)', '', block)
            # Insert mid next to start for stability; we'll reorder later
            block = block.replace(s_m.group(0), s_m.group(0) + " " + mid_str)

        # Ensure we have mid now; if still no mid, create simple midpoint
        if re.search(r'\(mid\s+[^)]*\)', block) is None:
            mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
            block = block.replace(s_m.group(0), s_m.group(0) + f" (mid {mx:g} {my:g})")

        # Remove any leftover (angle …)
        block = re.sub(r'\(angle\s+[^\)]+\)', '', block)

        # Re-extract final tokens for ordering
        s_m = re.search(r'\(start\s+[^)]*\)', block)
        m_m = re.search(r'\(mid\s+[^)]*\)', block)
        e_m = re.search(r'\(end\s+[^)]*\)', block)
        L = L_m.group(1) if L_m else 'F.SilkS'
        W = W_m.group(1) if W_m else '0.15'

        if not (s_m and m_m and e_m):
            return block  # something odd; leave unchanged

        return f"(fp_arc {s_m.group(0)} {m_m.group(0)} {e_m.group(0)} (layer {L}) (width {W}))"

    # --- find balanced (fp_arc …) blocks -------------------------------------
    def match_paren(s: str, pos: int) -> int:
        depth = 0
        i = pos
        while i < len(s):
            c = s[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    spans = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find('(fp_arc', i)
        if j == -1:
            break
        k = match_paren(text, j)
        if k == -1:
            break
        spans.append((j, k + 1))
        i = k + 1

    # rebuild from the end to keep indices stable
    out = []
    last = 0
    for a, b in spans:
        out.append(text[last:a])
        out.append(rebuild_arc(text[a:b]))
        last = b
    out.append(text[last:])
    new_text = ''.join(out)

    # tiny cleanups
    new_text = re.sub(r'\(\s+fp_arc', '(fp_arc', new_text)
    new_text = re.sub(r'\s+\)', ')', new_text)
    return new_text



# ---------------------------------------------------------------------------------
# ZIP import / purge / export
# ---------------------------------------------------------------------------------

def process_zip(zip_file, rename_assets: bool = False, use_symbol_name: bool = False):
    """
    Import one vendor ZIP:
    - extract
    - import/normalize symbols
    - rename assets if requested
    - convert footprints to project schema and localize model paths
    - copy .stp models
    """
    if use_symbol_name:
        logger.info(
            f"Using symbol name as footprint and 3D model name for {zip_file.name}"
        )

    # Prep temp extraction dir
    zip_file = Path(str(zip_file).strip()).resolve()
    tempdir = (INPUT_ZIP_FOLDER / "temp_extracted").resolve()
    if tempdir.exists():
        shutil.rmtree(tempdir)
    tempdir.mkdir(exist_ok=True)

    logger.debug(f"Importing ZIP: {zip_file}")
    logger.debug(f"Temporary extraction folder: {tempdir}")

    try:
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            zip_ref.extractall(tempdir)
    except Exception as e:
        logger.error(f"[FAIL] Error extracting ZIP file {zip_file.name}: {e}")
        return

    # Detect KiCad + 3D roots inside extraction
    all_dirs = [p for p in tempdir.rglob("*") if p.is_dir()]
    kicad_root = None
    model_root = None

    for d in all_dirs:
        if d.name.lower() == "kicad":
            kicad_root = d
        elif d.name.lower() == "3d":
            model_root = d

    if not kicad_root and not model_root:
        subs = [f for f in tempdir.iterdir() if f.is_dir()]
        if len(subs) == 1:
            nested_root = subs[0]
            logger.debug(f"Found nested folder: {nested_root.name}")
            for d in nested_root.rglob("*"):
                if d.is_dir() and d.name.lower() == "kicad":
                    kicad_root = d
                elif d.is_dir() and d.name.lower() == "3d":
                    model_root = d

    if not kicad_root:
        kicad_root = tempdir
        logger.warning("KiCad folder not found, using temp root.")
    if not model_root:
        model_root = tempdir
        logger.warning("3D folder not found, using temp root.")

    logger.debug(f"KiCad root detected: {kicad_root}")
    logger.debug(f"3D root detected: {model_root}")

    # --- symbols ---
    symbol_files = list(kicad_root.rglob("*.kicad_sym"))
    logger.debug(f"Found {len(symbol_files)} .kicad_sym files.")

    if not symbol_files:
        logger.error(f"[FAIL] No symbol files found in extracted ZIP {zip_file.name}.")
        shutil.rmtree(tempdir)
        return

    symbols_added = False
    for sym_file in symbol_files:
        logger.debug(f"Processing symbol file: {sym_file}")
        if append_symbols_from_file(
            sym_file, rename_assets=(rename_assets or use_symbol_name)
        ):
            symbols_added = True

    if not symbols_added and not TEMP_MAP_FILE.exists():
        logger.warning("No new symbols added — skipping footprints and 3D models.")
        shutil.rmtree(tempdir)
        return

    # load footprint_map after symbol import
    footprint_map = {}
    if TEMP_MAP_FILE.exists():
        with open(TEMP_MAP_FILE, "r") as f:
            footprint_map = json.load(f)
        logger.debug(f"Loaded footprint map with {len(footprint_map)} entries.")

    # --- rename assets in temp if desired ---
    if rename_assets or use_symbol_name:
        mode = "Symbol-name based" if use_symbol_name else "Default"
        logger.info(f"Renaming of Footprints/3D Models ENABLED ({mode}).")

        rename_count = rename_extracted_assets(
            tempdir, footprint_map, use_symbol_name=use_symbol_name
        )

        if rename_count > 0 and TEMP_MAP_FILE.exists():
            with open(TEMP_MAP_FILE, "r") as f:
                footprint_map = json.load(f)
        logger.info(f"Renamed {rename_count} assets.")

    # --- footprints ---
    project_version = detect_project_version(PROJECT_DIR)
    dst_schema = (
        KICAD8_SCHEMA
        if project_version < KICAD9_SCHEMA
        else KICAD9_FOOTPRINT_SCHEMA
    )

    for mod_file in kicad_root.rglob("*.kicad_mod"):
        dest = PROJECT_FOOTPRINT_LIB / mod_file.name
        if dest.exists():
            logger.warning(f'Skipped footprint "{mod_file.name}": already exists.')
            continue

        try:
            with open(mod_file, "r", encoding="utf-8") as f:
                mod_text = f.read()

            # detect source schema
            src_schema = KICAD8_SCHEMA
            mver = re.search(r"\(version\s+(\d+)\)", mod_text)
            if mver:
                try:
                    src_schema = int(mver.group(1))
                except ValueError:
                    pass

            # Normalize version/root
            if src_schema != dst_schema:
                mod_text = force_footprint_version(mod_text, dst_schema)

            # Fix arcs (KiCad 9 only)
            if dst_schema >= KICAD9_FOOTPRINT_SCHEMA:
                before = mod_text.count("(angle")
                mod_text = fix_all_fp_arcs(mod_text)
                after = mod_text.count("(angle")
                logger.info(f"{mod_file.name}: converted {before - after} arc(s)")

            # --- Localize 3D paths first ---
            mod_text = localize_3d_model_path(mod_file, footprint_map, mod_text)

            # --- Then reset 3D model offsets (center models) ---
            #   This ensures any large vendor offsets are zeroed
            offset_pattern = re.compile(
                r'\(at\s*\(\s*xyz\s+[-\d\.eE\s]+\)\)',
                flags=re.IGNORECASE
            )
            if offset_pattern.search(mod_text):
                mod_text = offset_pattern.sub('(at (xyz 0 0 0))', mod_text)
                logger.debug(f"{mod_file.name}: normalized 3D model offset to (0,0,0)")


            # Write final footprint
            with open(dest, "w", encoding="utf-8") as outf:
                outf.write(mod_text)

            logger.info(
                f'Added footprint "{mod_file.name}" '
                f'(KiCad {"8" if dst_schema < KICAD9_SCHEMA else "9"} schema).'
            )

        except Exception as e:
            logger.error(f"[FAIL] Error processing footprint {mod_file.name}: {e}")

    # --- 3D models ---
    copied_3d_count = 0
    for stp_file in model_root.rglob("*.stp"):
        dest_file = PROJECT_3D_DIR / stp_file.name
        if dest_file.exists():
            logger.warning(f'Skipped 3D model "{stp_file.name}" (already exists).')
            continue
        try:
            shutil.copy(stp_file, dest_file)
            copied_3d_count += 1
            logger.info(f'Copied 3D model "{stp_file.name}" → {PROJECT_3D_DIR.name}')
        except Exception as e:
            logger.error(f"[FAIL] Error copying 3D model {stp_file.name}: {e}")

    if copied_3d_count == 0:
        logger.warning("No new 3D models found or copied.")

    # cleanup temp + temp map
    shutil.rmtree(tempdir)
    if TEMP_MAP_FILE.exists():
        TEMP_MAP_FILE.unlink()

    logger.info(f"Finished importing {zip_file.name}")




def purge_zip_contents(zip_path: Path):
    """
    Delete from the project whatever the given vendor ZIP previously added:
    - symbols in ProjectSymbols.kicad_sym
    - .kicad_mod footprints in ProjectFootprints.pretty
    - .stp 3D models in /3dmodels
    """
    tempdir = INPUT_ZIP_FOLDER / "temp_extracted_purge"
    if tempdir.exists():
        shutil.rmtree(tempdir)
    tempdir.mkdir(exist_ok=True)

    logger.info(f"--- Purging contents of {zip_path.name} ---")

    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            all_zip_names = zip_ref.namelist()
    except Exception as e:
        logger.error(f"Error reading ZIP file {zip_path.name}: {e}")
        shutil.rmtree(tempdir)
        return

    symbols_to_delete = set()
    original_footprint_stems = set()
    original_stp_stems = set()

    # identify what needs to be purged
    for name in all_zip_names:
        name_path = Path(name)

        if name_path.suffix == ".kicad_sym":
            try:
                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extract(name, tempdir)
                extracted_sym_file = tempdir / name

                with open(extracted_sym_file, "r", encoding="utf-8") as f:
                    sexp_list = loads(f.read())

                for element in sexp_list[1:]:
                    if (
                        isinstance(element, list)
                        and len(element) > 1
                        and (element[0] == "symbol" or element[0] == Symbol("symbol"))
                    ):
                        sym_name = str(element[1])
                        base_name = SUB_PART_PATTERN.sub("", sym_name)
                        symbols_to_delete.add(base_name)

            except Exception as e:
                logger.error(f"Error processing symbol file {name} during purge: {e}")

        elif name_path.suffix == ".kicad_mod":
            original_footprint_stems.add(name_path.stem)

        elif name_path.suffix.lower() == ".stp":
            original_stp_stems.add(name_path.stem)

    # remove symbols from ProjectSymbols.kicad_sym
    if symbols_to_delete and PROJECT_SYMBOL_LIB.exists():
        logger.info(
            f"Attempting to delete {len(symbols_to_delete)} main symbols from {PROJECT_SYMBOL_LIB.name}..."
        )

        try:
            with open(PROJECT_SYMBOL_LIB, "r", encoding="utf-8") as f:
                project_sexp = loads(f.read())

            deleted_count = 0
            new_project_sexp = [project_sexp[0]]

            for element in project_sexp[1:]:
                if (
                    isinstance(element, list)
                    and len(element) > 1
                    and (element[0] == "symbol" or element[0] == Symbol("symbol"))
                ):
                    symbol_name = str(element[1])
                    base_name = SUB_PART_PATTERN.sub("", symbol_name)
                    if base_name in symbols_to_delete:
                        deleted_count += 1
                        continue
                new_project_sexp.append(element)

            if deleted_count > 0:
                with open(PROJECT_SYMBOL_LIB, "w", encoding="utf-8") as f:
                    f.write(dumps(new_project_sexp, pretty_print=True))
                logger.info(
                    f"Deleted {deleted_count} symbol block(s) corresponding to {len(symbols_to_delete)} main symbols."
                )
            else:
                logger.info("No matching symbols found for deletion.")

        except Exception as e:
            logger.error(f"ERROR during S-expression symbol deletion: {e}")

    # remove .kicad_mod footprints
    deleted_fp_count = 0
    stems_checked = set()
    stems_to_check = original_footprint_stems.union(symbols_to_delete)

    for stem in stems_to_check:
        fp_path = PROJECT_FOOTPRINT_LIB / (stem + ".kicad_mod")
        if fp_path.exists():
            fp_path.unlink()
            deleted_fp_count += 1

        stems_checked.add(stem)

    logger.info(
        f"Deleted {deleted_fp_count} footprints from {PROJECT_FOOTPRINT_LIB.name}."
    )

    # remove .stp models
    deleted_3d_count = 0
    stems_to_check = original_stp_stems.union(symbols_to_delete)
    for stem in stems_to_check:
        stp_path = PROJECT_3D_DIR / (stem + ".stp")
        if stp_path.exists():
            stp_path.unlink()
            deleted_3d_count += 1

    logger.info(
        f"Deleted {deleted_3d_count} 3D model files from {PROJECT_3D_DIR.name}."
    )

    shutil.rmtree(tempdir)


def export_symbols(selected_symbols: list[str]) -> list[Path]:
    """
    Export selected symbols (plus their footprints + 3D models) into
    library_output/LIB_<part>.zip with subfolders KiCad/ and 3D/.
    """
    import unicodedata

    export_paths: list[Path] = []

    try:
        if not selected_symbols:
            logger.error("[FAIL] No symbols provided for export.")
            return []

        if not PROJECT_SYMBOL_LIB.exists():
            logger.error("[FAIL] Project symbol library not found.")
            return []

        with open(PROJECT_SYMBOL_LIB, "r", encoding="utf-8") as f:
            sym_tree = loads(f.read())

        # map symbol -> "LIB:FootprintName"
        symbol_footprints: dict[str, str] = {}
        for el in sym_tree[1:]:
            if isinstance(el, list) and len(el) > 1 and str(el[0]) == "symbol":
                sym_name = str(el[1])
                footprint = None
                for item in el:
                    if (
                        isinstance(item, list)
                        and len(item) >= 3
                        and str(item[0]) == "property"
                        and str(item[1]) == "Footprint"
                    ):
                        footprint = str(item[2])
                        break
                if footprint:
                    symbol_footprints[sym_name] = footprint

        output_root = INPUT_ZIP_FOLDER.parent / "library_output"
        output_root.mkdir(parents=True, exist_ok=True)

        def normalize_name(s: str) -> str:
            return re.sub(r"[^A-Za-z0-9]", "", s).lower()

        for sym in selected_symbols:
            # find footprint ref either by exact symbol or LIB_symbol
            footprint_ref = None
            for name in [sym, f"LIB_{sym}"]:
                if name in symbol_footprints:
                    footprint_ref = symbol_footprints[name]
                    break

            if not footprint_ref:
                logger.warning(f"Symbol '{sym}' has no footprint assigned, skipping.")
                continue

            footprint_basename = footprint_ref.split(":")[-1]
            found_fp = None
            tgt_norm = normalize_name(footprint_basename)
            for fp in PROJECT_FOOTPRINT_LIB.rglob("*.kicad_mod"):
                if normalize_name(fp.stem) == tgt_norm:
                    found_fp = fp
                    break
            if not found_fp:
                logger.warning(
                    f"Footprint '{footprint_basename}' not found for {sym}, skipping."
                )
                continue

            # extract symbol definition
            single_symbol_sexpr = None
            for el in sym_tree[1:]:
                if (
                    isinstance(el, list)
                    and len(el) > 1
                    and str(el[0]) == "symbol"
                    and (str(el[1]) == sym or str(el[1]) == f"LIB_{sym}")
                ):
                    single_symbol_sexpr = el
                    break
            if not single_symbol_sexpr:
                logger.warning(
                    f"Symbol '{sym}' not found in {PROJECT_SYMBOL_LIB.name}."
                )
                continue

            # prep temp layout for this export
            part_name = sym
            zip_name = f"LIB_{part_name}.zip"
            part_folder = output_root / part_name
            kicad_folder = part_folder / "KiCad"
            model_folder = part_folder / "3D"

            if part_folder.exists():
                shutil.rmtree(part_folder)
            kicad_folder.mkdir(parents=True, exist_ok=True)
            model_folder.mkdir(parents=True, exist_ok=True)

            # write symbol file
            symbol_out = kicad_folder / f"{part_name}.kicad_sym"
            with open(symbol_out, "w", encoding="utf-8") as f:
                f.write(
                    "(kicad_symbol_lib (version 20211014) (generator CSE-Manager)\n"
                )
                f.write("  " + dumps(single_symbol_sexpr, pretty_print=True) + "\n)\n")

            # copy footprint
            fp_out = kicad_folder / found_fp.name
            shutil.copy2(found_fp, fp_out)

            # parse footprint to find model paths
            model_blocks = []
            collecting = False
            depth = 0
            current_block = []

            with open(found_fp, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not collecting and (
                        "(model" in line or line == "model" or line.endswith("model")
                    ):
                        collecting = True
                        depth = line.count("(") - line.count(")")
                        current_block = [line]
                        continue
                    if collecting:
                        current_block.append(line)
                        depth += line.count("(") - line.count(")")
                        if depth <= 0:
                            model_blocks.append(" ".join(current_block))
                            collecting = False
                            current_block = []

            resolved_models = []
            for block in model_blocks:
                try:
                    m = re.search(
                        r'["\']?([^"\']+\.stp)["\']?', block, flags=re.IGNORECASE
                    )
                    if not m:
                        logger.warning(
                            f"Could not extract model path from block: {block[:80]}..."
                        )
                        continue

                    raw_path = m.group(1).replace("\\", "/")
                    # normalize weird unicode / escapes
                    raw_path = unicodedata.normalize("NFKC", raw_path)
                    raw_path = raw_path.encode("ascii", "ignore").decode()

                    raw_path = os.path.expandvars(raw_path)
                    for env_var in (
                        "${KICAD7_3DMODEL_DIR}",
                        "${KICAD6_3DMODEL_DIR}",
                        "${KICAD8_3DMODEL_DIR}",
                    ):
                        raw_path = raw_path.replace(env_var, "3d_models")

                    model_path = Path(raw_path)
                    kiprojmod_root = PROJECT_SYMBOL_LIB.parent.parent

                    if "${KIPRJMOD}" in str(model_path):
                        model_path = Path(
                            str(model_path).replace("${KIPRJMOD}", str(kiprojmod_root))
                        )

                    if model_path.exists():
                        resolved_models.append(model_path)
                        logger.debug(f"Found 3D model for {sym}: {model_path}")
                    else:
                        # try relative to footprint dir
                        rel_model = (
                            PROJECT_FOOTPRINT_LIB.parent / model_path.name
                        ).resolve()
                        if rel_model.exists():
                            resolved_models.append(rel_model)
                            logger.debug(
                                f"Found relative 3D model for {sym}: {rel_model}"
                            )
                        else:
                            logger.warning(f"3D model not found: {model_path}")

                except Exception as e:
                    logger.warning(f"Failed to parse model block: {e}")

            for model_path in resolved_models:
                if model_path.exists():
                    shutil.copy2(model_path, model_folder / model_path.name)

            # zip up part_folder into LIB_<part>.zip (inside library_output)
            zip_path = output_root / zip_name
            shutil.make_archive(
                base_name=str(zip_path.with_suffix("")),
                format="zip",
                root_dir=output_root,
                base_dir=part_folder.name,
            )
            export_paths.append(zip_path)

            logger.info(f"Exported {zip_name}")
            shutil.rmtree(part_folder)

        if export_paths:
            logger.info(f"Created {len(export_paths)} ZIP file(s) in {output_root}")
        else:
            logger.warning("No ZIPs created.")
        logger.info(f"Output directory: {output_root}")
        return export_paths

    except Exception as e:
        logger.error(f"[FAIL] Export failed: {e}")
        return []


# ---------------------------------------------------------------------------------
# Environment bootstrap (must run at import time so GUI can call functions)
# ---------------------------------------------------------------------------------

load_dotenv()

env_project_dir = os.getenv("KLM_PROJECT_DIR")
env_base_path = os.getenv("KLM_BASE_PATH")
if env_project_dir:
    _base_path = Path(env_project_dir).resolve()
elif env_base_path:
    _base_path = Path(env_base_path).resolve()
else:
    _base_path = (
        Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    )

if env_project_dir:
    project_file = find_upward("*.kicad_pro", _base_path)
    if not project_file:
        raise RuntimeError("No KiCad project (*.kicad_pro) found in KLM_PROJECT_DIR.")
else:
    project_file = find_upward("*.kicad_pro", _base_path)
    if not project_file:
        raise RuntimeError("No KiCad project (*.kicad_pro) found.")

PROJECT_DIR = project_file.parent
PROJECT_SYMBOL_LIB = PROJECT_DIR / "symbols" / "ProjectSymbols.kicad_sym"
PROJECT_FOOTPRINT_LIB = PROJECT_DIR / "footprints" / "ProjectFootprints.pretty"
PROJECT_3D_DIR = PROJECT_DIR / "3dmodels"
PROJECT_FOOTPRINT_LIB_NAME = PROJECT_FOOTPRINT_LIB.stem

data_root_name = os.getenv("KLM_DATA_DIR", "klm_data")
KLM_DATA_DIR = PROJECT_DIR / data_root_name
input_folder_name = os.getenv("INPUT_ZIP_FOLDER", "library_input")
INPUT_ZIP_FOLDER = KLM_DATA_DIR / input_folder_name

TEMP_MAP_FILE = INPUT_ZIP_FOLDER / "footprint_to_symbol_map.json"

os.makedirs(PROJECT_SYMBOL_LIB.parent, exist_ok=True)
os.makedirs(PROJECT_FOOTPRINT_LIB, exist_ok=True)
os.makedirs(PROJECT_3D_DIR, exist_ok=True)
os.makedirs(KLM_DATA_DIR, exist_ok=True)
os.makedirs(INPUT_ZIP_FOLDER, exist_ok=True)
