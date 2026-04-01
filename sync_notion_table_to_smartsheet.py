#!/usr/bin/env python3
"""
Sync Notion database to Smartsheet.

Reads rows from a Notion database and pushes them to a Smartsheet sheet.

Usage:
    # Use default IDs
    python sync_notion_table_to_smartsheet.py

    # Override database/sheet IDs
    python sync_notion_table_to_smartsheet.py --database-id <database_id> --sheet-id <sheet_id>

Environment Variables:
    NOTION_API_TOKEN        - Notion API token
    SMARTSHEET_ACCESS_TOKEN - Smartsheet API token
"""

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx
import smartsheet
from dotenv import load_dotenv


# Column mapping: Notion table column → Smartsheet column
COLUMN_MAPPING = {
    "name": "name",
    "Lab flow": "Lab flow",
    "date": "date",
    "operator": "operater",  # Note: Smartsheet has typo "operater"
    "tags": "tags",
    "workcell": "workcell",
    "#of plates": "# of plates",
}


class NotionDatabaseFetcher:
    """Fetch data from Notion databases."""

    def __init__(self, api_key: Optional[str] = None):
        load_dotenv()
        self.api_key = api_key or os.getenv("NOTION_API_TOKEN")
        if not self.api_key:
            raise ValueError(
                "Notion API token required. Set NOTION_API_TOKEN env var or pass api_key."
            )

        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(headers=self._headers, timeout=60.0)

    def _extract_property_value(self, prop: Dict[str, Any]) -> str:
        """Extract plain text value from a Notion property."""
        prop_type = prop.get("type")

        if prop_type == "title":
            texts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in texts)

        elif prop_type == "rich_text":
            texts = prop.get("rich_text", [])
            return "".join(t.get("plain_text", "") for t in texts)

        elif prop_type == "number":
            val = prop.get("number")
            return str(val) if val is not None else ""

        elif prop_type == "select":
            select = prop.get("select")
            return select.get("name", "") if select else ""

        elif prop_type == "multi_select":
            items = prop.get("multi_select", [])
            return ", ".join(item.get("name", "") for item in items)

        elif prop_type == "date":
            date = prop.get("date")
            if date:
                start = date.get("start", "")
                return start
            return ""

        elif prop_type == "checkbox":
            return "Yes" if prop.get("checkbox") else "No"

        elif prop_type == "url":
            return prop.get("url", "")

        elif prop_type == "email":
            return prop.get("email", "")

        elif prop_type == "phone_number":
            return prop.get("phone_number", "")

        else:
            # Unsupported type, return empty string
            return ""

    def fetch_database_data(
        self, database_id: str
    ) -> Tuple[List[str], List[Dict[str, str]]]:
        """
        Fetch all pages from a Notion database.

        Args:
            database_id: Notion database ID

        Returns:
            Tuple of (column_names, rows) where:
            - column_names: List of property names
            - rows: List of row data, each row is a dict {property_name: value}

        Raises:
            ValueError: If database not found or empty
        """
        try:
            all_pages = []
            has_more = True
            start_cursor = None

            # Paginate through all results
            while has_more:
                payload = {}
                if start_cursor:
                    payload["start_cursor"] = start_cursor

                response = self._client.post(
                    f"https://api.notion.com/v1/databases/{database_id}/query",
                    json=payload
                )
                response.raise_for_status()
                data = response.json()

                all_pages.extend(data.get("results", []))
                has_more = data.get("has_more", False)
                start_cursor = data.get("next_cursor")

            if not all_pages:
                raise ValueError("Database is empty (no pages found)")

            # Extract column names from first page's properties
            first_page = all_pages[0]
            column_names = list(first_page.get("properties", {}).keys())

            # Extract row data
            rows = []
            for page in all_pages:
                row_data = {}
                properties = page.get("properties", {})

                for prop_name, prop_value in properties.items():
                    row_data[prop_name] = self._extract_property_value(prop_value)

                rows.append(row_data)

            return column_names, rows

        except httpx.HTTPStatusError as e:
            raise ValueError(f"Error fetching database: {e}")


class NotionDatabaseToSmartsheet:
    """Sync Notion databases to Smartsheet sheets."""

    def __init__(
        self,
        notion_api_key: Optional[str] = None,
        smartsheet_api_key: Optional[str] = None,
    ):
        load_dotenv()

        self.notion = NotionDatabaseFetcher(api_key=notion_api_key)

        self.smartsheet_token = smartsheet_api_key or os.getenv(
            "SMARTSHEET_ACCESS_TOKEN"
        )
        if not self.smartsheet_token:
            raise ValueError(
                "Smartsheet API token required. Set SMARTSHEET_ACCESS_TOKEN env var."
            )

        self.smart = smartsheet.Smartsheet(self.smartsheet_token)
        self.smart.errors_as_exceptions(True)

    def sync_database_to_sheet(
        self,
        database_id: str,
        sheet_id: int,
        column_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, int]:
        """
        Sync a Notion database to a Smartsheet sheet.

        Args:
            database_id: Notion database ID
            sheet_id: Smartsheet sheet ID
            column_mapping: Optional custom mapping {notion_col: smartsheet_col}

        Returns:
            Dictionary with counts: added, errors
        """
        # Fetch database data
        print(f"Fetching database data from {database_id}...")
        column_names, rows = self.notion.fetch_database_data(database_id)
        print(f"Found {len(column_names)} columns, {len(rows)} rows")
        print(f"Columns: {column_names}")

        # Use provided mapping or default
        col_map = column_mapping or COLUMN_MAPPING

        # Validate column mapping
        missing_cols = []
        for notion_col in col_map.keys():
            if notion_col not in column_names:
                missing_cols.append(notion_col)

        if missing_cols:
            print(f"⚠️  Warning: Expected columns not found in database: {missing_cols}")
            print(f"Available columns: {column_names}")

        # Get Smartsheet column IDs
        print(f"Fetching Smartsheet schema...")
        sheet = self.smart.Sheets.get_sheet(sheet_id)
        ss_column_map = {col.title: col.id for col in sheet.columns}

        # Validate Smartsheet columns exist
        missing_ss_cols = []
        for ss_col in col_map.values():
            if ss_col not in ss_column_map:
                missing_ss_cols.append(ss_col)

        if missing_ss_cols:
            raise ValueError(
                f"Smartsheet columns not found: {missing_ss_cols}. "
                f"Available columns: {list(ss_column_map.keys())}"
            )

        # Build rows to add
        rows_to_add = []
        errors = 0

        for row_idx, row_data in enumerate(rows):
            try:
                cells = []

                # Map each column
                for notion_col, ss_col in col_map.items():
                    # Get value from row dict
                    value = row_data.get(notion_col, "").strip()
                    if value:  # Only add non-empty cells
                        cell = smartsheet.models.Cell()
                        cell.column_id = ss_column_map[ss_col]
                        cell.value = value
                        cells.append(cell)

                if cells:  # Only add row if it has data
                    row = smartsheet.models.Row()
                    row.to_bottom = True
                    row.cells = cells
                    rows_to_add.append(row)
                else:
                    print(f"Skipping empty row {row_idx + 1}")

            except Exception as e:
                print(f"Error processing row {row_idx + 1}: {e}")
                errors += 1

        # Add rows to Smartsheet
        stats = {"added": 0, "errors": errors}

        if rows_to_add:
            print(f"Adding {len(rows_to_add)} rows to Smartsheet...")
            batch_size = 500
            for i in range(0, len(rows_to_add), batch_size):
                batch = rows_to_add[i : i + batch_size]
                try:
                    self.smart.Sheets.add_rows(sheet_id, batch)
                    stats["added"] += len(batch)
                    print(f"  Added rows {i + 1} to {i + len(batch)}")
                except Exception as e:
                    print(f"Error adding batch: {e}")
                    stats["errors"] += len(batch)

        print(
            f"Sync complete: {stats['added']} added, {stats['errors']} errors"
        )
        return stats


def main(args: Optional[List[str]] = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="sync_notion_table_to_smartsheet",
        description="Sync Notion database to Smartsheet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use default IDs (database: 25c1a62c8a83801da570ff60bdad4bfa, sheet: 8970283403661188)
  python sync_notion_table_to_smartsheet.py

  # Override with custom IDs
  python sync_notion_table_to_smartsheet.py --database-id abc123 --sheet-id 1234567890

Environment Variables:
  NOTION_API_TOKEN        Notion API token
  SMARTSHEET_ACCESS_TOKEN Smartsheet API token

Default IDs:
  Database ID: 25c1a62c8a83801da570ff60bdad4bfa
  Sheet ID:    8970283403661188

Getting Custom IDs:
  Database ID: From Notion URL - the ID in notion.so/DATABASE_ID
  Sheet ID:    From Smartsheet URL - the ID in app.smartsheet.com/sheets/SHEET_ID
        """,
    )

    parser.add_argument(
        "--database-id",
        metavar="ID",
        default="25c1a62c8a83801da570ff60bdad4bfa",
        help="Notion database ID (default: 25c1a62c8a83801da570ff60bdad4bfa)",
    )

    parser.add_argument(
        "--sheet-id",
        metavar="ID",
        type=int,
        default=8970283403661188,
        help="Smartsheet sheet ID (default: 8970283403661188)",
    )

    parser.add_argument(
        "--notion-key",
        metavar="KEY",
        help="Notion API token (overrides env var)",
    )

    parser.add_argument(
        "--smartsheet-key",
        metavar="KEY",
        help="Smartsheet API token (overrides env var)",
    )

    parsed = parser.parse_args(args)

    try:
        syncer = NotionDatabaseToSmartsheet(
            notion_api_key=parsed.notion_key,
            smartsheet_api_key=parsed.smartsheet_key,
        )

        stats = syncer.sync_database_to_sheet(
            database_id=parsed.database_id,
            sheet_id=parsed.sheet_id,
        )

        print(f"\n✓ Sync successful!")
        print(f"  Rows added: {stats['added']}")
        if stats['errors'] > 0:
            print(f"  Errors: {stats['errors']}")
            return 1

        return 0

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except smartsheet.exceptions.ApiError as e:
        print(f"Smartsheet API Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
