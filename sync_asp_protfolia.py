#!/usr/bin/env python3
"""
Sync Notion Projects DB to ASP Project Portfolio Smartsheet.

This script syncs data from Notion to an existing Smartsheet with a specific
column mapping. It matches rows by Project ID.

Usage:
    python sync_asp_portfolio.py preview [test|prod]  # Preview changes
    python sync_asp_portfolio.py sync [test|prod]     # Apply changes

    Default target is 'prod' if not specified.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import smartsheet
from notion_fetcher import NotionFetcher

# =============================================================================
# CONFIGURATION
# =============================================================================

# IDs (from .env)
NOTION_DB_ID = os.getenv("NOTION_PROJECTS_DB_ID")
SMARTSHEET_ASP_TEST_ID = os.getenv("SMARTSHEET_ASP_PORTFOLIO_TEST_ID")
SMARTSHEET_ASP_PROD_ID = os.getenv("SMARTSHEET_ASP_PORTFOLIO_PROD_ID")

if not NOTION_DB_ID:
    raise ValueError("NOTION_PROJECTS_DB_ID environment variable is required")


def get_smartsheet_id(target: str) -> int:
    """Get the Smartsheet ID based on target environment."""
    if target == "test":
        if not SMARTSHEET_ASP_TEST_ID:
            raise ValueError("SMARTSHEET_ASP_PORTFOLIO_TEST_ID environment variable is required")
        return int(SMARTSHEET_ASP_TEST_ID)
    else:  # prod
        if not SMARTSHEET_ASP_PROD_ID:
            raise ValueError("SMARTSHEET_ASP_PORTFOLIO_PROD_ID environment variable is required")
        return int(SMARTSHEET_ASP_PROD_ID)


# Will be set in main() based on command line argument
SMARTSHEET_ID = None

# Column mapping: Smartsheet column name -> Notion property name
# Only mapped columns will be synced; unmapped columns are left untouched
COLUMN_MAPPING = {
    "Global Priority": "Global Priority",
    "Phase": "Phase",
    "Project ID": "Project ID",
    "Status": "Status",
    "Health": "Health",
    "Decisions": "Decisions",
    "Charter (Program)": "Charter",  # Relation field
    "Project Name": "*Project",  # Title field in Notion
    "Description": "Description",
    "Start Date": "Start Date",
    "End Date": "End Date",
    "Update (Include risks and blockers)": "Update",
    "Wins": "Wins",
    "Project Lead": "Project Lead",
    "Tech Lead": "Technical Lead",
    "Team Members": "Team Members",
    "Department": "*Department",  # Relation column
    "Supported Tech Tiles": "Supported Tech Tiles",  # Relation field
    "Core Team": "Core Team",  # Checkbox field
}

# Which column to use for matching rows between systems
MATCH_COLUMN_SMARTSHEET = "Project ID"
MATCH_COLUMN_NOTION = "Project ID"

# Fallback match column when Project ID is missing
FALLBACK_MATCH_SMARTSHEET = "Project Name"
FALLBACK_MATCH_NOTION = "*Project"

# Value mappings for PICKLIST columns (Notion value -> Smartsheet value)
# If a Notion value isn't in the mapping, it's used as-is
VALUE_MAPPINGS = {
    "Phase": {
        "Phase 0": "0-Ideate",
        "Phase 1": "1-Plan",
        "Phase 2": "2-Build",
        "Complete": "3-Operate",  # Map Complete to 3-Operate
        "Cancelled": "N/A",       # Map Cancelled to N/A
        # MVP and N/A pass through as-is
    },
    "Status": {
        "Blocked - Delivery": "Blocked",
        # Other values (Active, Not Started, etc.) pass through as-is
    },
    "Department": {
        # Notion value -> Smartsheet value
        "Automation": "ASE",
        "Hardware & Controls Engineering": "HCE",
        "Facilities Engineering": "Facilities Eng.",
        "Life Sciences": "Life Science",
        "Physical Sciences": "Physical Science",
        # These pass through as-is (exact match):
        # ASP, Platform Science, Product, Software Product, Vacuum Synthesis,
        # Catalysis, Characterization, Corporate Development, Facilities Management,
        # IT & Security, LIMS Team, Next Generation Engineering, Porous Materials
    },
}


# =============================================================================
# SYNC LOGIC
# =============================================================================

# Cache for relation page titles (page_id -> title)
_relation_title_cache: dict = {}


def lookup_relation_titles(page_ids: list) -> list[str]:
    """Look up titles for relation page IDs, using cache."""
    import httpx
    
    titles = []
    ids_to_fetch = [pid for pid in page_ids if pid not in _relation_title_cache]
    
    # Fetch any uncached pages
    if ids_to_fetch:
        token = os.getenv("NOTION_API_TOKEN")
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
        }
        
        for page_id in ids_to_fetch:
            try:
                resp = httpx.get(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=headers,
                    timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    props = data.get("properties", {})
                    # Find the title property
                    for prop_val in props.values():
                        if prop_val.get("type") == "title":
                            title_items = prop_val.get("title", [])
                            title = "".join(t.get("plain_text", "") for t in title_items)
                            _relation_title_cache[page_id] = title.strip()
                            break
                    else:
                        _relation_title_cache[page_id] = page_id  # Fallback to ID
                else:
                    _relation_title_cache[page_id] = page_id  # Fallback to ID
            except Exception:
                _relation_title_cache[page_id] = page_id  # Fallback to ID
    
    # Build result from cache
    for page_id in page_ids:
        titles.append(_relation_title_cache.get(page_id, page_id))
    
    return titles


def format_notion_value(value, notion_type: str) -> str | None:
    """Format a Notion value for Smartsheet (text columns)."""
    if value is None:
        return None
    
    if notion_type == "date":
        # Return just the date string
        if isinstance(value, dict):
            return value.get("start")
        return str(value) if value else None
    
    if notion_type == "number":
        return value

    if notion_type == "checkbox":
        # Checkbox returns boolean - Smartsheet expects True/False
        return bool(value)

    if notion_type in ("select", "status"):
        if isinstance(value, dict):
            return value.get("name")
        return str(value) if value else None
    
    if notion_type == "multi_select":
        if isinstance(value, list):
            return ", ".join(v.get("name", str(v)) if isinstance(v, dict) else str(v) for v in value)
        return str(value) if value else None
    
    if notion_type == "people":
        if isinstance(value, list):
            names = []
            for person in value:
                if isinstance(person, dict):
                    # New format: dict with name, id, email
                    names.append(person.get("name") or person.get("email") or "")
                else:
                    names.append(str(person))
            return ", ".join(filter(None, names)) if names else None
        return str(value) if value else None
    
    if notion_type == "formula":
        if isinstance(value, dict):
            # Formula results have a type and value
            formula_type = value.get("type")
            if formula_type == "string":
                return value.get("string")
            elif formula_type == "number":
                return value.get("number")
            elif formula_type == "boolean":
                return str(value.get("boolean"))
            elif formula_type == "date":
                date_val = value.get("date", {})
                return date_val.get("start") if date_val else None
        return str(value) if value else None
    
    if notion_type == "relation":
        # Relation values are lists of page IDs - look up their titles
        if isinstance(value, list) and value:
            titles = lookup_relation_titles(value)
            # Note: value mappings for relations are applied separately
            # since they need to be split for MULTI_PICKLIST columns
            return titles if titles else None  # Return list, not joined string
        return None
    
    # Default: convert to string
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value) if value else None


def format_notion_people_for_contact(value) -> list | None:
    """Format Notion people for Smartsheet CONTACT_LIST columns.
    
    Returns a list of email dicts for objectValue, or None if empty.
    Expects value to be a list of dicts with 'name', 'id', and 'email' keys.
    """
    if not value or not isinstance(value, list):
        return None
    
    contacts = []
    for person in value:
        if isinstance(person, dict):
            email = person.get("email")
            if email:
                contacts.append({"email": email})
    
    return contacts if contacts else None


def get_notion_data():
    """Fetch and process data from Notion."""
    fetcher = NotionFetcher()
    schema = fetcher.get_database_schema(NOTION_DB_ID)
    records = fetcher.fetch_database(NOTION_DB_ID)
    
    print(f"Fetched {len(records)} records from Notion")
    
    # Build lookup by match column, falling back to Project Name if no Project ID
    notion_by_key = {}
    fallback_count = 0
    for record in records:
        key = record.get(MATCH_COLUMN_NOTION)
        if key:
            notion_by_key[str(key).strip()] = (record, schema)
        else:
            # Fall back to Project Name
            fallback_key = record.get(FALLBACK_MATCH_NOTION)
            if fallback_key:
                fallback_count += 1
                notion_by_key[str(fallback_key).strip()] = (record, schema)
    
    if fallback_count:
        print(f"  ({fallback_count} records matched by Project Name instead of Project ID)")
    
    return notion_by_key, schema


def get_smartsheet_data(client):
    """Fetch current Smartsheet data."""
    # Use level=2 to get accurate column types (SDK sometimes hides MULTI_CONTACT_LIST)
    sheet = client.Sheets.get_sheet(SMARTSHEET_ID, level=2)
    
    # Build column ID lookup
    col_id_to_name = {col.id: col.title for col in sheet.columns}
    col_name_to_id = {col.title: col.id for col in sheet.columns}
    
    # Track CONTACT_LIST and MULTI_CONTACT_LIST columns
    contact_columns = {col.title for col in sheet.columns 
                       if col.type in ("CONTACT_LIST", "MULTI_CONTACT_LIST")}
    
    # Track PICKLIST and MULTI_PICKLIST columns with their current options
    picklist_columns = {}
    for col in sheet.columns:
        # Get type as string (col.type is an EnumeratedValue object)
        col_type_str = str(col.type) if hasattr(col.type, '__str__') else col.type
        if col_type_str in ("PICKLIST", "MULTI_PICKLIST"):
            options = list(col.options) if col.options else []
            picklist_columns[col.title] = {
                "id": col.id,
                "type": col_type_str,  # Store as string for API calls
                "options": options
            }
    
    # Build row lookup by match column AND track linked cells
    match_col_id = col_name_to_id.get(MATCH_COLUMN_SMARTSHEET)
    fallback_col_id = col_name_to_id.get(FALLBACK_MATCH_SMARTSHEET)
    
    rows_by_key = {}
    linked_cells = {}  # {match_key: {column_name: sheet_name}}
    
    for row in sheet.rows:
        key = None
        fallback_key = None
        for cell in row.cells:
            if cell.column_id == match_col_id:
                key = str(cell.value).strip() if cell.value else None
            elif cell.column_id == fallback_col_id:
                fallback_key = str(cell.value).strip() if cell.value else None
        
        # Use Project ID if available, otherwise fall back to Project Name
        row_key = key or fallback_key
        
        if row_key:
            rows_by_key[row_key] = row
            
            # Check for linked cells (cross-sheet references)
            for cell in row.cells:
                has_link = hasattr(cell, 'link_in_from_cell') and cell.link_in_from_cell
                has_formula = hasattr(cell, 'formula') and cell.formula
                
                if has_link or has_formula:
                    col_name = col_id_to_name.get(cell.column_id)
                    if col_name:
                        if row_key not in linked_cells:
                            linked_cells[row_key] = {}
                        source = cell.link_in_from_cell.sheet_name if has_link else "formula"
                        linked_cells[row_key][col_name] = source
    
    print(f"Found {len(rows_by_key)} rows in Smartsheet")
    
    # Report linked cells
    total_linked = sum(len(cols) for cols in linked_cells.values())
    if total_linked > 0:
        print(f"Found {total_linked} linked cells that will be skipped during sync")
    
    return sheet, col_name_to_id, rows_by_key, contact_columns, picklist_columns, linked_cells


def collect_all_picklist_values(notion_data, notion_schema):
    """Collect all unique values that will be sent to each picklist column."""
    values_by_column = {}
    
    for key, notion_record in notion_data.items():
        record, schema = notion_record
        
        for ss_col, notion_prop in COLUMN_MAPPING.items():
            if notion_prop not in schema:
                continue
            
            notion_type = schema[notion_prop].get("type", "rich_text")
            raw_value = record.get(notion_prop)
            
            if raw_value is None:
                continue
            
            # Handle different types that map to picklists
            values = []
            
            if notion_type == "relation":
                # Relation -> MULTI_PICKLIST
                formatted = format_notion_value(raw_value, notion_type)
                if formatted:
                    for title in formatted:
                        if ss_col in VALUE_MAPPINGS and title in VALUE_MAPPINGS[ss_col]:
                            values.append(VALUE_MAPPINGS[ss_col][title])
                        else:
                            values.append(title)
            elif notion_type in ("select", "status"):
                # Select/Status -> PICKLIST
                formatted = format_notion_value(raw_value, notion_type)
                if formatted:
                    if ss_col in VALUE_MAPPINGS and formatted in VALUE_MAPPINGS[ss_col]:
                        values.append(VALUE_MAPPINGS[ss_col][formatted])
                    else:
                        values.append(formatted)
            
            # Add values to the set for this column
            if values:
                if ss_col not in values_by_column:
                    values_by_column[ss_col] = set()
                values_by_column[ss_col].update(values)
    
    return values_by_column


def ensure_picklist_options(client, picklist_columns, required_values):
    """Add any missing options to picklist columns.
    
    Args:
        client: Smartsheet client
        picklist_columns: Dict from get_smartsheet_data with column info
        required_values: Dict of column_name -> set of required values
    """
    for col_name, values in required_values.items():
        if col_name not in picklist_columns:
            continue
        
        col_info = picklist_columns[col_name]
        current_options = set(col_info["options"])
        missing = values - current_options
        
        if missing:
            print(f"  Adding {len(missing)} missing options to '{col_name}': {', '.join(sorted(missing))}")
            
            # Update column with all options (existing + new)
            new_options = list(current_options | missing)
            
            updated_col = smartsheet.models.Column()
            updated_col.type = col_info["type"]  # String type name, required when updating options
            updated_col.options = new_options
            
            try:
                result = client.Sheets.update_column(SMARTSHEET_ID, col_info["id"], updated_col)
                
                # Check if result is an error (SDK returns Error object instead of raising)
                if isinstance(result, smartsheet.models.Error):
                    print(f"    Warning: Could not update column options: {result.result.message}")
                elif hasattr(result, 'data') and result.data:
                    # Update our local cache
                    picklist_columns[col_name]["options"] = list(result.data.options) if result.data.options else new_options
                    print(f"    ✓ Added options successfully")
                else:
                    # Assume success if no error
                    picklist_columns[col_name]["options"] = new_options
                    print(f"    ✓ Added options successfully")
            except Exception as e:
                print(f"    Warning: Could not update column options: {e}")


def build_update_cells(notion_record, notion_schema, col_name_to_id, contact_columns, linked_cols=None):
    """Build cells to update for a Smartsheet row.
    
    Args:
        notion_record: Tuple of (record dict, schema dict)
        notion_schema: The Notion database schema
        col_name_to_id: Dict mapping Smartsheet column names to IDs
        contact_columns: Set of Smartsheet column names that are CONTACT_LIST type
        linked_cols: Optional dict of column names that are linked from other sheets
                     (these will be skipped to preserve the cross-sheet links)
    
    Note: Blank values in Notion will clear the corresponding Smartsheet cell.
    """
    record, schema = notion_record
    linked_cols = linked_cols or {}
    cells = []
    
    for ss_col, notion_prop in COLUMN_MAPPING.items():
        if ss_col not in col_name_to_id:
            continue  # Smartsheet column doesn't exist
        if notion_prop not in schema:
            continue  # Notion property doesn't exist
        if ss_col in linked_cols:
            continue  # Cell is linked from another sheet - don't overwrite
        
        notion_type = schema[notion_prop].get("type", "rich_text")
        raw_value = record.get(notion_prop)
        
        cell = smartsheet.models.Cell()
        cell.column_id = col_name_to_id[ss_col]
        
        # Handle CONTACT_LIST columns specially
        if ss_col in contact_columns and notion_type == "people":
            contacts = format_notion_people_for_contact(raw_value)
            if contacts is None:
                # Clear the cell if Notion value is empty
                cell.value = ""
            else:
                # Use dict format - Smartsheet SDK converts to proper object
                cell.object_value = {
                    "objectType": "MULTI_CONTACT",
                    "values": contacts  # Already a list of {"email": "..."} dicts
                }
        # Handle relation columns (like Department) - returns list of titles
        elif notion_type == "relation":
            formatted = format_notion_value(raw_value, notion_type)
            if formatted is None:
                # Clear the cell if Notion value is empty
                cell.value = ""
            else:
                # formatted is a list of titles - apply mappings to each
                mapped_values = []
                for title in formatted:
                    if ss_col in VALUE_MAPPINGS and title in VALUE_MAPPINGS[ss_col]:
                        mapped_values.append(VALUE_MAPPINGS[ss_col][title])
                    else:
                        mapped_values.append(title)
                
                # For MULTI_PICKLIST, use objectValue
                cell.object_value = {
                    "objectType": "MULTI_PICKLIST",
                    "values": mapped_values
                }
        else:
            formatted = format_notion_value(raw_value, notion_type)
            if formatted is None:
                # Clear the cell if Notion value is empty
                cell.value = ""
            else:
                # Apply value mapping if one exists for this column
                if ss_col in VALUE_MAPPINGS and formatted in VALUE_MAPPINGS[ss_col]:
                    formatted = VALUE_MAPPINGS[ss_col][formatted]
                
                cell.value = formatted
        
        cells.append(cell)
    
    return cells


def preview_sync(notion_data, ss_rows_by_key, col_name_to_id, notion_schema):
    """Preview what changes would be made."""
    updates = []
    adds = []
    
    for key, notion_record in notion_data.items():
        if key in ss_rows_by_key:
            updates.append(key)
        else:
            adds.append(key)
    
    print(f"\n{'='*60}")
    print("PREVIEW - No changes will be made")
    print(f"{'='*60}")
    print(f"\nRows to UPDATE: {len(updates)}")
    for key in updates[:10]:
        record, _ = notion_data[key]
        name = record.get("*Project", record.get("Project", "?"))
        print(f"  - {key}: {name}")
    if len(updates) > 10:
        print(f"  ... and {len(updates) - 10} more")
    
    print(f"\nRows to ADD: {len(adds)}")
    for key in adds[:10]:
        record, _ = notion_data[key]
        name = record.get("*Project", record.get("Project", "?"))
        print(f"  - {key}: {name}")
    if len(adds) > 10:
        print(f"  ... and {len(adds) - 10} more")
    
    print(f"\nColumn mapping ({len(COLUMN_MAPPING)} columns):")
    for ss_col, notion_prop in COLUMN_MAPPING.items():
        ss_exists = "✓" if ss_col in col_name_to_id else "✗"
        notion_exists = "✓" if notion_prop in notion_schema else "✗"
        print(f"  {ss_col} [{ss_exists}] <- {notion_prop} [{notion_exists}]")


def apply_sync(client, notion_data, ss_rows_by_key, col_name_to_id, contact_columns, linked_cells=None):
    """Apply sync changes to Smartsheet (updates existing rows and adds new ones).
    
    Args:
        linked_cells: Dict of {project_id: {column_name: source_sheet}} for cells
                      that are linked from other sheets and should not be overwritten.
    """
    linked_cells = linked_cells or {}
    updates = []
    adds = []
    
    # Separate updates from adds
    for key, notion_record in notion_data.items():
        # Get linked columns for this row (if any)
        row_linked_cols = linked_cells.get(key, {})
        cells = build_update_cells(notion_record, notion_record, col_name_to_id, contact_columns, row_linked_cols)
        
        if not cells:
            continue
            
        if key in ss_rows_by_key:
            # Update existing row
            ss_row = ss_rows_by_key[key]
            new_row = smartsheet.models.Row()
            new_row.id = ss_row.id
            new_row.cells = cells
            updates.append(new_row)
        else:
            # Add new row
            new_row = smartsheet.models.Row()
            new_row.to_bottom = True
            new_row.cells = cells
            adds.append((key, new_row))
    
    # Apply updates
    update_errors = 0
    if updates:
        print(f"\nUpdating {len(updates)} existing rows...")
        batch_size = 500
        for i in range(0, len(updates), batch_size):
            batch = updates[i:i + batch_size]
            try:
                result = client.Sheets.update_rows(SMARTSHEET_ID, batch)
                print(f"  Updated rows {i + 1} to {i + len(batch)}")
            except Exception as e:
                error_msg = str(e)
                # Try to extract meaningful error info
                if "CELL_VALUE_FAILS_VALIDATION" in error_msg:
                    print(f"  Warning: Some rows in batch {i + 1}-{i + len(batch)} had validation errors (picklist values may not exist)")
                else:
                    print(f"  Warning: Error updating rows {i + 1}-{i + len(batch)}: {error_msg[:200]}")
                update_errors += len(batch)
    
    # Add new rows
    add_errors = 0
    if adds:
        print(f"\nAdding {len(adds)} new rows...")
        batch_size = 500
        add_rows = [row for _, row in adds]
        for i in range(0, len(add_rows), batch_size):
            batch = add_rows[i:i + batch_size]
            try:
                result = client.Sheets.add_rows(SMARTSHEET_ID, batch)
                print(f"  Added rows {i + 1} to {i + len(batch)}")
            except Exception as e:
                error_msg = str(e)
                if "CELL_VALUE_FAILS_VALIDATION" in error_msg:
                    print(f"  Warning: Some rows in batch {i + 1}-{i + len(batch)} had validation errors")
                else:
                    print(f"  Warning: Error adding rows {i + 1}-{i + len(batch)}: {error_msg[:200]}")
                add_errors += len(batch)
    
    updated_count = len(updates) - update_errors
    added_count = len(adds) - add_errors
    
    if update_errors or add_errors:
        print(f"\n⚠ Sync complete with warnings. Updated {updated_count}/{len(updates)} rows, added {added_count}/{len(adds)} new rows.")
    else:
        print(f"\n✓ Sync complete! Updated {len(updates)} rows, added {len(adds)} new rows.")
    
    return {"updated": updated_count, "added": added_count, "update_errors": update_errors, "add_errors": add_errors}


def main():
    global SMARTSHEET_ID
    
    if len(sys.argv) < 2:
        print(__doc__)
        print("Please specify a mode: preview or sync")
        sys.exit(1)
    
    mode = sys.argv[1].lower()
    
    if mode not in ("preview", "sync"):
        print(f"Unknown mode: {mode}")
        print("Valid modes: preview, sync")
        sys.exit(1)
    
    # Parse target environment (default to prod)
    target = "prod"
    if len(sys.argv) >= 3:
        target = sys.argv[2].lower()
        if target not in ("test", "prod"):
            print(f"Unknown target: {target}")
            print("Valid targets: test, prod")
            sys.exit(1)
    
    SMARTSHEET_ID = get_smartsheet_id(target)
    
    print(f"Target: {target.upper()}")
    print(f"Notion DB: {NOTION_DB_ID}")
    print(f"Smartsheet: {SMARTSHEET_ID}")
    print()
    
    # Get data from both systems
    notion_data, notion_schema = get_notion_data()
    
    client = smartsheet.Smartsheet(os.getenv("SMARTSHEET_ACCESS_TOKEN"))
    sheet, col_name_to_id, ss_rows_by_key, contact_columns, picklist_columns, linked_cells = get_smartsheet_data(client)
    
    if contact_columns:
        print(f"Contact columns (will use email format): {', '.join(contact_columns)}")
    if picklist_columns:
        print(f"Picklist columns: {', '.join(picklist_columns.keys())}")
    print()
    
    # Collect all values we'll need to send to picklist columns
    required_values = collect_all_picklist_values(notion_data, notion_schema)
    
    # Ensure all required picklist options exist before syncing
    if required_values and mode == "sync":
        print("Checking picklist options...")
        ensure_picklist_options(client, picklist_columns, required_values)
        print()
    
    if mode == "preview":
        preview_sync(notion_data, ss_rows_by_key, col_name_to_id, notion_schema)
    else:
        apply_sync(client, notion_data, ss_rows_by_key, col_name_to_id, contact_columns, linked_cells)


if __name__ == "__main__":
    main()