---
name: xlsx
description: "Comprehensive spreadsheet creation, editing, and analysis with support for formulas, formatting, data analysis, and visualization. Use when working with spreadsheets (.xlsx, .xlsm, .csv, .tsv) for: (1) Creating new spreadsheets with formulas and formatting, (2) Reading or analyzing data, (3) Modifying existing spreadsheets while preserving formulas, or (4) Data analysis and visualization"
---

# Requirements for Outputs

## All Excel files

### Zero Formula Errors
- Every Excel model MUST be delivered with ZERO formula errors (#REF!, #DIV/0!, #VALUE!, #N/A, #NAME?)

### Preserve Existing Templates (when updating templates)
- Study and EXACTLY match existing format, style, and conventions when modifying files
- Never impose standardized formatting on files with established patterns
- Existing template conventions ALWAYS override these guidelines

## Financial models

### Color Coding Standards
Unless otherwise stated by the user or existing template

#### Industry-Standard Color Conventions
- **Blue text (RGB: 0,0,255)**: Hardcoded inputs, and numbers users will change for scenarios
- **Black text (RGB: 0,0,0)**: ALL formulas and calculations
- **Green text (RGB: 0,128,0)**: Links pulling from other worksheets within same workbook
- **Red text (RGB: 255,0,0)**: External links to other files
- **Yellow background (RGB: 255,255,0)**: Key assumptions needing attention or cells that need to be updated

### Number Formatting Standards

#### Required Format Rules
- **Years**: Format as text strings (e.g., "2024" not "2,024")
- **Currency**: Use $#,##0 format; ALWAYS specify units in headers ("Revenue ($mm)")
- **Zeros**: Use number formatting to make all zeros "-", including percentages (e.g., "$#,##0;($#,##0);-")
- **Percentages**: Default to 0.0% format (one decimal)
- **Multiples**: Format as 0.0x for valuation multiples (EV/EBITDA, P/E)
- **Negative numbers**: Use parentheses (123) not minus -123

### Formula Construction Rules

#### Assumptions Placement
- Place ALL assumptions (growth rates, margins, multiples, etc.) in separate assumption cells
- Use cell references instead of hardcoded values in formulas
- Example: Use =B5*(1+$B$6) instead of =B5*1.05

#### Formula Error Prevention
- Verify all cell references are correct
- Check for off-by-one errors in ranges
- Ensure consistent formulas across all projection periods
- Test with edge cases (zero values, negative numbers)
- Verify no unintended circular references

#### Documentation Requirements for Hardcodes
- Comment or in cells beside (if end of table). Format: "Source: [System/Document], [Date], [Specific Reference], [URL if applicable]"
- Examples:
  - "Source: Company 10-K, FY2024, Page 45, Revenue Note, [SEC EDGAR URL]"
  - "Source: Company 10-Q, Q2 2025, Exhibit 99.1, [SEC EDGAR URL]"
  - "Source: Bloomberg Terminal, 8/15/2025, AAPL US Equity"
  - "Source: FactSet, 8/20/2025, Consensus Estimates Screen"

# XLSX creation, editing, and analysis

## Overview

A user may ask you to create, edit, or analyze the contents of an .xlsx file. You have different tools and workflows available for different tasks.

## Reading and analyzing data

### Data analysis with pandas
For data analysis, visualization, and basic operations, use **pandas** which provides powerful data manipulation capabilities:

```python
import pandas as pd

# Read Excel
df = pd.read_excel('file.xlsx')  # Default: first sheet
all_sheets = pd.read_excel('file.xlsx', sheet_name=None)  # All sheets as dict

# Analyze
df.head()      # Preview data
df.info()      # Column info
df.describe()  # Statistics

# Write Excel
df.to_excel('output.xlsx', index=False)
```

## Excel File Workflows

## CRITICAL: Use Formulas, Not Hardcoded Values

**Always use Excel formulas instead of calculating values in Python and hardcoding them.** This ensures the spreadsheet remains dynamic and updateable.

### WRONG - Hardcoding Calculated Values
```python
# Bad: Calculating in Python and hardcoding result
total = df['Sales'].sum()
sheet['B10'] = total  # Hardcodes 5000

# Bad: Computing growth rate in Python
growth = (df.iloc[-1]['Revenue'] - df.iloc[0]['Revenue']) / df.iloc[0]['Revenue']
sheet['C5'] = growth  # Hardcodes 0.15
```

### CORRECT - Using Excel Formulas
```python
# Good: Let Excel calculate the sum
sheet['B10'] = '=SUM(B2:B9)'

# Good: Growth rate as Excel formula
sheet['C5'] = '=(C4-C2)/C2'

# Good: Average using Excel function
sheet['D20'] = '=AVERAGE(D2:D19)'
```

This applies to ALL calculations - totals, percentages, ratios, differences, etc. The spreadsheet should be able to recalculate when source data changes.

## Common Workflow
1. **Choose tool**: pandas for data, openpyxl for formulas/formatting
2. **Create/Load**: Create new workbook or load existing file
3. **Modify**: Add/edit data, formulas, and formatting
4. **Save**: Write to file (existing files: `receipt=True`)
5. **Recalculate**: `oracle.recalc` — zero errors before delivery

**Note**: Formulas are stored as strings with no computed value until recalculated — always run the oracle (see "Recalculate and Verify") before delivery.

### Creating new Excel files

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = Workbook()
sheet = wb.active

# Add data
sheet['A1'] = 'Hello'
sheet['B1'] = 'World'
sheet.append(['Row', 'of', 'data'])

# Add formula
sheet['B2'] = '=SUM(A1:A10)'

# Formatting
sheet['A1'].font = Font(bold=True, color='FF0000')
sheet['A1'].fill = PatternFill('solid', start_color='FFFF00')
sheet['A1'].alignment = Alignment(horizontal='center')

# Column width
sheet.column_dimensions['A'].width = 20

wb.save('output.xlsx')
```

### Editing existing Excel files

`openpyxl` here is **paper-xlsx** — an import-compatible fork (same `import openpyxl`, all stock
APIs work). Existing workbooks load in **preserve mode** by default: the original package bytes
stay the source of truth and save patches only what you edited. Stock openpyxl silently drops
what it doesn't model (sparklines, text boxes, shapes, extensions); preserve mode keeps them.

```python
from openpyxl import load_workbook
from openpyxl.errors import PaperRefusal

wb = load_workbook('existing.xlsx')          # preserve mode
sheet = wb['Model']

sheet['B2'] = 1200                           # dependent formula caches are invalidated automatically
remap = sheet.insert_rows(5)                 # rewrites formulas, names, tables, filters, charts
sheet.append_table_row('Sales', ['2029', 1500])   # expands a named table atomically

wb.validate()                                # dry-run the save plan, raises PaperRefusal if unsafe
receipt = wb.save('modified.xlsx', receipt=True)
receipt.to_dict()['cells_changed']           # evidence of exactly what changed
```

**A `PaperRefusal` is final.** It means the edit cannot be saved without corrupting something.
Do not retry via `preserve=False`, generic row insertion, or direct ZIP/XML editing — report
the refusal (`exc.kind`, `exc.anchor`, `exc.options`) to the user and pick a different edit.

Other helpers: `wb.search(...)`, `ws.allowed_values(cell)` (list-validation vocabulary),
`openpyxl.preserve.copy_format(ws, 'A1', 'A2:A20')`, `openpyxl.preserve.diff_workbooks(before, after)`,
`chart.repoint(0, "'Model'!$B$2:$B$9")`, `ws.replace_image(target, 'new.png')`.

## Recalculate and Verify (required before delivery)

openpyxl writes formulas without computing them. Before handing over any workbook with
formulas, recalculate with the LibreOffice oracle — it enforces the Zero Formula Errors rule:

```python
from openpyxl import oracle

r = oracle.recalc('model.xlsx', output_path='model_final.xlsx')   # source is never modified
if r.status == r.FORMULA_ERRORS_DETECTED:
    print(r.errors)   # [{'sheet': 'Model', 'cell': 'H30', 'value': '#DIV/0!'}] -> fix and rerun
```

Deliver `model_final.xlsx`: cached values are filled in (so pandas/previews show numbers) while
the original package structure is kept. Check `r.errors` itself — don't rely on
`scan_errors` over the recalculated copy.

What-if checks without touching the file:

```python
ev = oracle.evaluate('model.xlsx', set={"Model!B6": 0.07}, read=["Model!E2"])
ev.outputs, ev.errors
```

## Formula Verification Checklist

Quick checks to ensure formulas work correctly:

### Essential Verification
- [ ] **Test 2-3 sample references**: Verify they pull correct values before building full model
- [ ] **Column mapping**: Confirm Excel columns match (e.g., column 64 = BL, not BK)
- [ ] **Row offset**: Remember Excel rows are 1-indexed (DataFrame row 5 = Excel row 6)

### Common Pitfalls
- [ ] **NaN handling**: Check for null values with `pd.notna()`
- [ ] **Far-right columns**: FY data often in columns 50+
- [ ] **Multiple matches**: Search all occurrences, not just first
- [ ] **Division by zero**: Check denominators before using `/` in formulas (#DIV/0!)
- [ ] **Wrong references**: Verify all cell references point to intended cells (#REF!)
- [ ] **Cross-sheet references**: Use correct format (Sheet1!A1) for linking sheets

## Best Practices

### Library Selection
- **pandas**: Best for data analysis, bulk operations, and simple data export
- **openpyxl**: Best for complex formatting, formulas, and Excel-specific features

### Working with openpyxl
- Cell indices are 1-based (row=1, column=1 refers to cell A1)
- Use `data_only=True` to read calculated values: `load_workbook('file.xlsx', data_only=True)`
- **Warning**: Never save a workbook opened with `data_only=True` — preserve mode refuses it (unless `allow_formula_loss=True`), stock mode destroys formulas
- For large files: Use `read_only=True` for reading or `write_only=True` for writing (both use the stock, non-preserve path)

### Working with pandas
- Specify data types to avoid inference issues: `pd.read_excel('file.xlsx', dtype={'id': str})`
- For large files, read specific columns: `pd.read_excel('file.xlsx', usecols=['A', 'C', 'E'])`
- Handle dates properly: `pd.read_excel('file.xlsx', parse_dates=['date_column'])`

## Code Style Guidelines
**IMPORTANT**: When generating Python code for Excel operations:
- Write minimal, concise Python code without unnecessary comments
- Avoid verbose variable names and redundant operations
- Avoid unnecessary print statements

**For Excel files themselves**:
- Add comments to cells with complex formulas or important assumptions
- Document data sources for hardcoded values
- Include notes for key calculations and model sections

## Dependencies

- **paper-xlsx**: `pip uninstall -y openpyxl && pip install paper-xlsx` (provides `import openpyxl` — never install both)
- **LibreOffice** (`soffice`): required by `openpyxl.oracle` recalc/evaluate
