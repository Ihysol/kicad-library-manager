# mouser_integration.py
"""
Mouser integration for KiCad Library Manager GUI:
- BOM parsing
- Order preparation
- API requests (cart, items)
"""
import os
import requests
import uuid
import time
import glob
import json
import csv

BASE_URL = "https://api.mouser.com/api/v1.0"

# ===========================
# ICONS
# ===========================
ICON_OK = "✔"
ICON_FAIL = "✘"
ICON_INFO = "ℹ"
ICON_WARN = "⚠"
ICON_LOAD = "⟳"
ICON_BOX = "📦"

# ===========================
# USER VARIABLES
# ===========================
# Environment variable name for Mouser API key
ENVIRONMENT_VARIABLE_API_KEY_NAME = "MOUSER_API_KEY"

# CSV column names used in BOM files
CSV_MOUSER_COLUMN_NAME = "MNR"
CSV_QUANTITY_COLUMN_NAME = "Qty"
CSV_REFERENCE_COLUMN_NAME = "Reference"
CSV_DELIMITER = ","

# API retry settings
API_TIMEOUT_MAX_RETRIES = 10
API_TIMEOUT_SLEEP_S = 2

# Maximum length for displaying summarized Reference values
MAX_REFERENCE_DISPLAY_LENGTH = 21


# ===========================
# API REQUEST BASE CLASS
# ===========================
class MouserAPIRequest:
    """
    Base class for performing Mouser API requests.
    Handles GET/POST, error checking, and logging responses.
    """
    def __init__(self, operation, operations, body=None):
        self.operations = operations
        self.operation = operation
        self.method, url = self.operations.get(operation, (None, None))
        if self.method is None:
            raise ValueError(f"Unknown API operation: {operation}")
        api_key = os.getenv(ENVIRONMENT_VARIABLE_API_KEY_NAME)
        if not api_key:
            raise ValueError("Environment variable MOUSER_API_KEY not set!")
        self.url = f"{BASE_URL}{url}?apiKey={api_key}"
        self.body = body or {}
        self.response = None

    def run(self):
        """Execute the API request and print log messages."""
        print(f"\n-- API Request ({self.method}) {self.operation} --\n")
        print(f"{ICON_INFO}  URL: {self.url}\n")

        try:
            if self.method == "GET":
                self.response = requests.get(self.url)
                
            elif self.method == "POST":
                print(f"{ICON_LOAD} Sending JSON body ({len(str(self.body))} chars)...\n")
                self.response = requests.post(self.url, json=self.body)
                
                if self.response.status_code != 200:
                    print(f"{ICON_FAIL} HTTP ERROR: {self.response.status_code}")
                    print(f"{ICON_FAIL} Server Response:")
                    try:
                        err_data = self.response.json()
                        print(json.dumps(err_data, indent=2))
                    except json.JSONDecodeError:
                        txt = self.response.text
                        print(txt[:500] + ("..." if len(txt) > 500 else ""))
                    return False

            print(f"{ICON_INFO}  HTTP Status: {self.response.status_code}\n")
            return self._is_success()
        
        except requests.RequestException as e:
            print(f"{ICON_FAIL}  API ERROR: {e}")
            return False

    def get_response(self):
        """Return JSON response or raw text if JSON parsing fails."""
        if self.response is None:
            return {}
        try:
            return self.response.json()
        except Exception:
            return {"raw": self.response.text}

    def _is_success(self):
        """Check the API response for errors and print a summary."""
        data = self.get_response()

        if "Errors" in data and data["Errors"]:
            print("--- API RETURNED ERRORS ---")
            for err in data["Errors"]:
                print(f"{ICON_FAIL} {err}")
            return False

        print(f"{ICON_OK}  API returned no errors.\n")
        print(f"-------------------------------------\n")
        print("\n------ API Response Summary -----\n")

        # Log summary of cart data if available
        print(f"{ICON_BOX}  Cart Key:                      {data.get('CartKey', 'not provided')}")
        print(f"{ICON_INFO}  Currency Code:          {data.get('CurrencyCode', 'not provided')}")
        print(f"{ICON_INFO}  MerchandiseTotal:     {data.get('MerchandiseTotal', 'not provided')}")
        print(f"{ICON_INFO}  TotalItemCount:         {data.get('TotalItemCount', 'not provided')}")
        print(f"{ICON_INFO}  Additional Fees:         {data.get('additionalFeesTotal', 'not provided')}")

        print(f"-------------------------------------\n")
        return True


# ===========================
# CART REQUESTS
# ===========================
class MouserCartRequest(MouserAPIRequest):
    """
    Specific API request class for Mouser cart operations.
    Defines available operations like get, update, insert item, etc.
    """
    operations = {
        "get": ("GET", "/cart"),
        "update": ("POST", "/cart"),
        "insertitem": ("POST", "/cart/items/insert"),
        "updateitem": ("POST", "/cart/items/update"),
        "removeitem": ("POST", "/cart/item/remove"),
    }

    def __init__(self, operation, body=None):
        super().__init__(operation, self.operations, body)


# ===========================
# BOM HANDLING
# ===========================
class BOMHandler:
    """
    Handles BOM file reading, parsing, and preprocessing.
    Summarizes reference designators for easier display.
    """
    def __init__(self, target_headers=None, working_dir=None, group_by_field=None):
        self.target_headers = [] if target_headers is None else target_headers
        self.m_dir_path = working_dir or os.getcwd()
        self.group_by_field = group_by_field
        self.BOM_files = []
        self.data_array = {}

    def get_bom_files(self):
        """
        Return a list of CSV BOM files in the working directory.
        """
        self.BOM_files = glob.glob(os.path.join(self.m_dir_path, "*.csv"))
        print(f"{ICON_INFO}  Found {len(self.BOM_files)} CSV file(s) in directory.")
        return self.BOM_files

    @staticmethod
    def summerize_sorted_items(items):
        """
        Summarize sorted reference items into ranges (e.g., R1-R5, R7).
        """
        if not items:
            return ""
        def get_num(x):
            return int("".join(filter(str.isdigit, x)))
        ranges = []
        start = end = items[0]
        for i in range(1, len(items)):
            if (
                items[i][0] == items[i - 1][0]
                and get_num(items[i]) == get_num(items[i - 1]) + 1
            ):
                end = items[i]
            else:
                ranges.append(start if start == end else f"{start}-{end}")
                start = end = items[i]
        ranges.append(start if start == end else f"{start}-{end}")
        return ", ".join(ranges)

    @staticmethod
    def split_refs(raw):
        """Split reference strings by commas (with or without spaces)."""
        if not raw or raw == "/":
            return []
        return [p.strip() for p in str(raw).split(",") if p.strip()]

    def process_bom_file(self, bom_file, group_by_field=None):
        """
        Read a BOM CSV file, validate required columns, and convert into internal data array.
        Reference column is summarized and truncated for display.
        """
        print(f"{ICON_INFO}  Processing BOM file: {bom_file}\n")
        
        try:
            with open(bom_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
                headers = reader.fieldnames or []
                if not headers:
                    print(f"{ICON_FAIL}  No headers found in CSV.\n")
                    return {}

                # Check required columns
                required = [CSV_REFERENCE_COLUMN_NAME, CSV_QUANTITY_COLUMN_NAME]
                missing_required = [h for h in required if h not in headers]
                if missing_required:
                    print(f"{ICON_WARN} Missing required columns: {', '.join(missing_required)}\n")
                    return {}

                self.target_headers = headers
                rows = []
                for row in reader:
                    normalized = {}
                    for h in headers:
                        val = row.get(h, "")
                        if val is None or str(val).strip() == "":
                            val = "/"
                        normalized[h] = str(val).strip()
                    rows.append(normalized)
        except Exception as e:
            print(f"{ICON_FAIL}  Could not read CSV: {e}")
            return {}

        # Group rows by selected field (default: Value) and merge references/quantities.
        resolved_group = group_by_field if group_by_field is not None else self.group_by_field
        if resolved_group is None:
            resolved_group = "Value"
        if resolved_group in ("", "None", "(none)") or resolved_group not in headers:
            resolved_group = None

        if resolved_group:
            grouped = {}
            for row in rows:
                group_key = row.get(resolved_group, "/")
                if group_key not in grouped:
                    grouped[group_key] = []
                grouped[group_key].append(row)

            def _ref_sort_key(ref):
                prefix = ""
                digits = ""
                for ch in ref:
                    if ch.isdigit():
                        digits += ch
                    else:
                        if digits:
                            break
                        prefix += ch
                num = int(digits) if digits.isdigit() else 0
                return (prefix, num, ref)

            merged_rows = []
            for group_key, group_rows in grouped.items():
                if len(group_rows) == 1:
                    merged_rows.append(group_rows[0])
                    continue
                new_row = {}
                # Merge references
                all_refs = []
                for gr in group_rows:
                    all_refs.extend(self.split_refs(gr.get(CSV_REFERENCE_COLUMN_NAME, "")))
                all_refs = sorted(set(all_refs), key=_ref_sort_key)
                new_row[CSV_REFERENCE_COLUMN_NAME] = ", ".join(all_refs) if all_refs else "/"
                # Merge quantities
                total_qty = 0
                for gr in group_rows:
                    try:
                        total_qty += int(gr.get(CSV_QUANTITY_COLUMN_NAME, "0"))
                    except Exception:
                        pass
                new_row[CSV_QUANTITY_COLUMN_NAME] = str(total_qty) if total_qty > 0 else "0"
                # Preserve other columns (use first non-empty value)
                for h in headers:
                    if h in (CSV_REFERENCE_COLUMN_NAME, CSV_QUANTITY_COLUMN_NAME):
                        continue
                    val = next((r.get(h, "/") for r in group_rows if r.get(h, "/") not in ("", "/")), "/")
                    new_row[h] = val
                new_row[resolved_group] = group_key
                merged_rows.append(new_row)
            rows = merged_rows

        # Initialize data arrays from (possibly grouped) rows
        self.data_array = {h: [] for h in headers}
        for row in rows:
            for h in headers:
                self.data_array[h].append(row.get(h, "/"))

        # Summarize Reference column
        new_refs = []
        for r in self.data_array.get("Reference", []):
            items = self.split_refs(r)
            if items and items[0] != "":
                summarized = self.summerize_sorted_items(items)
                new_refs.append(summarized[:MAX_REFERENCE_DISPLAY_LENGTH])
            else:
                new_refs.append("")
        self.data_array["Reference"] = new_refs

        return self.data_array



# ===========================
# ORDER CLIENT
# ===========================
class MouserOrderClient:
    """
    Handles preparation of order items from BOM data array and sends them to Mouser cart.
    """
    def process_request(self, req_type, operation, body=None):
        """Dispatch API requests based on request type."""
        if req_type == "cart":
            return MouserCartRequest(operation, body).run()
        else:
            raise ValueError("Unknown request type")

    def order_parts_from_data_array(self, data_array):
        """
        Convert data array to Mouser order items, apply multiplier, and insert into cart.
        """
        print("\n------- Preparing order item -------\n")
        print(f"{ICON_INFO}  Preparing order items")
        multiplier = data_array.pop("Multiplier", 1)
        try:
            multiplier = int(multiplier)
        except Exception:
            multiplier = 1
        mnr_col_name = data_array.pop("MNR_Column_Name", CSV_MOUSER_COLUMN_NAME)
        items = []
        print(f"{ICON_INFO}  Using order multiplier: {multiplier}\n")

        refs = data_array.get(CSV_REFERENCE_COLUMN_NAME, [])
        qtys = data_array.get(CSV_QUANTITY_COLUMN_NAME, [])
        extras = data_array.pop("ExtraQty", [])
        mnrs = data_array.get(mnr_col_name, [])
        row_count = min(len(refs), len(qtys), len(mnrs))

        for idx in range(row_count):
            try:
                base_qty = int(qtys[idx])
            except Exception as e:
                print(f"{ICON_WARN} Skipping item {refs[idx] if idx < len(refs) else '?'} due to invalid quantity: {qtys[idx] if idx < len(qtys) else '?'} ({e})")
                continue
            extra_qty = 0
            if idx < len(extras):
                try:
                    extra_qty = int(extras[idx])
                except Exception:
                    extra_qty = 0
            items.append(
                {
                    "MouserPartNumber": mnrs[idx] if idx < len(mnrs) else "",
                    "Quantity": base_qty * multiplier + extra_qty,
                    "CustomerPartNumber": refs[idx] if idx < len(refs) else "",
                }
            )
        print(f"{ICON_INFO}  Prepared {len(items)} items for ordering.\n")
        print(f"-------------------------------------\n")
        if not items:
            print(f"{ICON_FAIL}  No order items prepared.\n")
            return False
        
        # Create cart payload and send to API
        body = {"CartKey": str(uuid.uuid4()), "CartItems": items}
        return self.process_request("cart", "insertitem", body)
