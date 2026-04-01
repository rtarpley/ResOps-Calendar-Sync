# Notion Simple Table to Smartsheet Sync

Sync rows from a Notion simple table (inline table block on a page) to a Smartsheet sheet.

## Setup

### 1. Install Dependencies

```bash
pip install httpx smartsheet-python-sdk python-dotenv
```

### 2. Configure Environment Variables

Create a `.env` file:

```bash
NOTION_API_TOKEN=secret_xxxxx
SMARTSHEET_ACCESS_TOKEN=xxxxx
```

### 3. Default Configuration

The script is pre-configured with these IDs:
- **Notion Page ID**: `25c1a62c8a83801da570ff60bdad4bfa`
- **Smartsheet Sheet ID**: `8970283403661188`

You can run it without any arguments:
```bash
python sync_notion_table_to_smartsheet.py
```

### 4. Get Custom IDs (Optional)

#### Notion Page ID

From your Notion URL:
```
https://www.notion.so/workspace/PAGE_ID
https://www.notion.so/PAGE_ID-page-title
```

Extract the `PAGE_ID` (32-character hex, usually with dashes).

#### Notion Table Block ID (Optional)

If auto-detection fails, get the exact block ID:

1. Right-click on the table in Notion
2. Select "Copy link to block"
3. Extract ID from URL: `https://www.notion.so/PAGE_ID#BLOCK_ID`

#### Smartsheet Sheet ID

From Smartsheet URL:
```
https://app.smartsheet.com/sheets/SHEET_ID
```

Or list all sheets:
```bash
python sync_to_smartsheet.py --list-sheets
```

## Usage

### Basic Sync (Use Defaults)

The script has default IDs configured:
- **Page ID**: `25c1a62c8a83801da570ff60bdad4bfa`
- **Sheet ID**: `8970283403661188`

```bash
# Run with defaults
python sync_notion_table_to_smartsheet.py
```

### Custom Page/Sheet IDs

```bash
python sync_notion_table_to_smartsheet.py \
  --page-id abc123def456 \
  --sheet-id 1234567890
```

### Specify Table Block ID

```bash
python sync_notion_table_to_smartsheet.py \
  --table-block-id xyz789
```

### Override API Keys

```bash
python sync_notion_table_to_smartsheet.py \
  --page-id abc123def456 \
  --sheet-id 1234567890 \
  --notion-key secret_xxxxx \
  --smartsheet-key xxxxx
```

## Column Mapping

The script maps Notion table columns to Smartsheet columns:

| Notion Column | Smartsheet Column |
|---------------|-------------------|
| name          | name              |
| Lab flow      | Lab flow          |
| date          | date              |
| operator      | operater          |
| tags          | tags              |
| workcell      | workcell          |
| #of plates    | # of plates       |

**Note:** Smartsheet has a typo: `operater` (not `operator`).

### Custom Mapping

Edit the `COLUMN_MAPPING` dictionary in [sync_notion_table_to_smartsheet.py:24-31](sync_notion_table_to_smartsheet.py#L24-L31):

```python
COLUMN_MAPPING = {
    "notion_column_name": "smartsheet_column_name",
    # Add more mappings...
}
```

## Error Handling

The script handles:

1. **Table block not found**: Auto-detects first table on page, or use `--table-block-id`
2. **Empty table**: Raises error if no data rows exist
3. **Column name mismatch**: Warns about missing columns but continues
4. **Smartsheet column not found**: Raises error with available column list
5. **Empty rows**: Skips rows with no data

## Requirements

- **Notion**: Table must have a header row (toggle "Has header" in table settings)
- **Smartsheet**: Target columns must exist before running sync
- **API Permissions**:
  - Notion: Read access to the page containing the table
  - Smartsheet: Write access to the target sheet

## Troubleshooting

### "No table block found on page"

- Verify the page ID is correct
- Ensure the Notion integration has access to the page
- If multiple tables exist, specify `--table-block-id` explicitly

### "Smartsheet columns not found"

- Check column names match exactly (case-sensitive)
- Verify Smartsheet sheet has all required columns
- List available columns in the error message

### "Table has no data rows (only header)"

- Ensure table has at least one data row beyond the header
- Check "Has header" is enabled in Notion table settings

### Rate Limits

- Notion: 3 requests/second
- Smartsheet: Rate limits vary by plan
- Script uses batching (500 rows) to optimize Smartsheet API calls

## Architecture

### NotionTableFetcher

Handles Notion API calls:
- `find_table_block()`: Auto-detect table on page
- `fetch_table_data()`: Extract column names and row data

### NotionTableToSmartsheet

Main sync logic:
- Validates column mappings
- Maps table data to Smartsheet cells
- Batch inserts rows (500 per batch)

### Key Differences from Database Sync

Unlike `sync_to_smartsheet.py` (databases):
- No schema introspection (tables don't have typed properties)
- All values treated as text
- No `notion_id` tracking (fresh inserts each time)
- Simpler block API (not database query API)

## Example Output

```
Searching for table block on page abc123def456...
Found table block: xyz789
Fetching table data...
Found 7 columns, 15 rows
Columns: ['name', 'Lab flow', 'date', 'operator', 'tags', 'workcell', '#of plates']
Fetching Smartsheet schema...
Adding 15 rows to Smartsheet...
  Added rows 1 to 15
Sync complete: 15 added, 0 errors

✓ Sync successful!
  Rows added: 15
```
