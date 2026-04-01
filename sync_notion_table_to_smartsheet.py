#!/usr/bin/env python3
"""
Sync Notion simple table (inline table block) to Smartsheet.

Reads rows from a Notion table block on a page and pushes them to a Smartsheet sheet.

Usage:
    # Use default IDs
    python sync_notion_table_to_smartsheet.py

    # Override page/sheet IDs
    python sync_notion_table_to_smartsheet.py --page-id <page_id> --sheet-id <sheet_id>

    # Specify exact table block
    python sync_notion_table_to_smartsheet.py --table-block-id <block_id>

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


class NotionTableFetcher:
    """Fetch data from Notion table blocks."""

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

    def find_table_block(self, page_id: str) -> Optional[str]:
        """
        Find the first table block on a Notion page.

        Args:
            page_id: Notion page ID

        Returns:
            Table block ID, or None if not found
        """
        try:
            response = self._client.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children"
            )
            response.raise_for_status()
            data = response.json()

            for block in data.get("results", []):
                if block.get("type") == "table":
                    return block.get("id")

            return None
        except httpx.HTTPStatusError as e:
            print(f"Error fetching blocks: {e}")
            return None

    def fetch_table_data(
        self, table_block_id: str
    ) -> Tuple[List[str], List[List[str]]]:
        """
        Fetch table data from a Notion table block.

        Args:
            table_block_id: Notion table block ID

        Returns:
            Tuple of (column_names, rows) where:
            - column_names: List of column headers
            - rows: List of row data, each row is a list of cell values

        Raises:
            ValueError: If table block not found or empty
        """
        try:
            # Get table block metadata
            response = self._client.get(
                f"https://api.notion.com/v1/blocks/{table_block_id}"
            )
            response.raise_for_status()
            table_block = response.json()

            if table_block.get("type") != "table":
                raise ValueError(
                    f"Block {table_block_id} is not a table (type: {table_block.get('type')})"
                )

            table_width = table_block.get("table", {}).get("table_width", 0)
            has_column_header = table_block.get("table", {}).get(
                "has_column_header", False
            )

            if table_width == 0:
                raise ValueError("Table has no columns")

            # Get table row blocks
            response = self._client.get(
                f"https://api.notion.com/v1/blocks/{table_block_id}/children"
            )
            response.raise_for_status()
            data = response.json()

            table_rows = [
                block for block in data.get("results", [])
                if block.get("type") == "table_row"
            ]

            if not table_rows:
                raise ValueError("Table is empty (no rows found)")

            # Extract column names and data rows
            column_names = []
            data_rows = []

            for idx, row_block in enumerate(table_rows):
                cells = row_block.get("table_row", {}).get("cells", [])
                cell_values = []

                for cell in cells:
                    # Extract plain text from rich text array
                    text = "".join(
                        item.get("plain_text", "") for item in cell
                    )
                    cell_values.append(text)

                if idx == 0 and has_column_header:
                    column_names = cell_values
                else:
                    data_rows.append(cell_values)

            if not column_names:
                # If no header row, generate column names
                column_names = [f"Column {i+1}" for i in range(table_width)]

            if not data_rows:
                raise ValueError("Table has no data rows (only header)")

            return column_names, data_rows

        except httpx.HTTPStatusError as e:
            raise ValueError(f"Error fetching table data: {e}")


class NotionTableToSmartsheet:
    """Sync Notion table blocks to Smartsheet sheets."""

    def __init__(
        self,
        notion_api_key: Optional[str] = None,
        smartsheet_api_key: Optional[str] = None,
    ):
        load_dotenv()

        self.notion = NotionTableFetcher(api_key=notion_api_key)

        self.smartsheet_token = smartsheet_api_key or os.getenv(
            "SMARTSHEET_ACCESS_TOKEN"
        )
        if not self.smartsheet_token:
            raise ValueError(
                "Smartsheet API token required. Set SMARTSHEET_ACCESS_TOKEN env var."
            )

        self.smart = smartsheet.Smartsheet(self.smartsheet_token)
        self.smart.errors_as_exceptions(True)

    def sync_table_to_sheet(
        self,
        page_id: str,
        sheet_id: int,
        table_block_id: Optional[str] = None,
        column_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, int]:
        """
        Sync a Notion table to a Smartsheet sheet.

        Args:
            page_id: Notion page ID containing the table
            table_block_id: Optional table block ID (auto-detected if None)
            sheet_id: Smartsheet sheet ID
            column_mapping: Optional custom mapping {notion_col: smartsheet_col}

        Returns:
            Dictionary with counts: added, errors
        """
        # Find table block if not provided
        if not table_block_id:
            print(f"Searching for table block on page {page_id}...")
            table_block_id = self.notion.find_table_block(page_id)
            if not table_block_id:
                raise ValueError(
                    f"No table block found on page {page_id}. "
                    "Provide --table-block-id explicitly."
                )
            print(f"Found table block: {table_block_id}")

        # Fetch table data
        print(f"Fetching table data...")
        column_names, rows = self.notion.fetch_table_data(table_block_id)
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
            print(f"⚠️  Warning: Expected columns not found in table: {missing_cols}")
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
                    # Find value in row
                    if notion_col in column_names:
                        col_idx = column_names.index(notion_col)
                        if col_idx < len(row_data):
                            value = row_data[col_idx].strip()
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
        description="Sync Notion simple table to Smartsheet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use default IDs (page: 25c1a62c8a83801da570ff60bdad4bfa, sheet: 8970283403661188)
  python sync_notion_table_to_smartsheet.py

  # Override with custom IDs
  python sync_notion_table_to_smartsheet.py --page-id abc123 --sheet-id 1234567890

  # Specify exact table block
  python sync_notion_table_to_smartsheet.py --table-block-id def456

Environment Variables:
  NOTION_API_TOKEN        Notion API token
  SMARTSHEET_ACCESS_TOKEN Smartsheet API token

Default IDs:
  Page ID:  25c1a62c8a83801da570ff60bdad4bfa
  Sheet ID: 8970283403661188

Getting Custom IDs:
  Page ID:  From Notion URL - notion.so/PAGE_ID or notion.so/workspace/PAGE_ID
  Block ID: Right-click table → "Copy link to block" → extract ID from URL
  Sheet ID: From Smartsheet URL
        """,
    )

    parser.add_argument(
        "--page-id",
        metavar="ID",
        default="25c1a62c8a83801da570ff60bdad4bfa",
        help="Notion page ID containing the table (default: 25c1a62c8a83801da570ff60bdad4bfa)",
    )

    parser.add_argument(
        "--table-block-id",
        metavar="ID",
        help="Table block ID (auto-detected if not provided)",
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
        syncer = NotionTableToSmartsheet(
            notion_api_key=parsed.notion_key,
            smartsheet_api_key=parsed.smartsheet_key,
        )

        stats = syncer.sync_table_to_sheet(
            page_id=parsed.page_id,
            sheet_id=parsed.sheet_id,
            table_block_id=parsed.table_block_id,
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
