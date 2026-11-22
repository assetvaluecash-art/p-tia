"""Tests for multi-sheet XLSX ingestion via DocumentParser._parse_xlsx.

Covers:
  - Multi-sheet workbooks (all sheets ingested, correct metadata)
  - Header detection per sheet
  - Empty sheets are skipped
  - Mixed: sheets with/without headers, data-only sheets
  - Real golden file (test_case_generation smoke file with 3 sheets)
"""

from pathlib import Path

import openpyxl
import pytest

from kbfabric_ingest.ingest.parser import (
    DocumentParser,
    _build_table_row_text,
    _detect_table_header,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def parser():
    return DocumentParser(max_chars_per_block=2000)


def _write_xlsx(path: Path, sheets: dict[str, list[list]]):
    """Helper: write a dict of {sheet_name: [[row], ...]} to an xlsx file."""
    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet(title=name)
        if first:
            ws.title = name
            first = False
        for row in rows:
            ws.append(row)
    wb.save(path)
    wb.close()


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestDetectTableHeader:
    def test_string_header_detected(self):
        rows = [
            ["Name", "Age", "City"],
            ["Alice", 30, "NYC"],
        ]
        detected, cols = _detect_table_header(rows)
        assert detected is True
        assert cols == ["Name", "Age", "City"]

    def test_numeric_first_row_not_header(self):
        rows = [
            [1, 2, 3],
            [4, 5, 6],
        ]
        detected, cols = _detect_table_header(rows)
        assert detected is False
        assert cols == ["Col1", "Col2", "Col3"]

    def test_empty_rows(self):
        detected, cols = _detect_table_header([])
        assert detected is False
        assert cols == []

    def test_none_values_in_header(self):
        rows = [
            ["Name", None, "City"],
            ["Alice", "x", "NYC"],
        ]
        detected, cols = _detect_table_header(rows)
        assert detected is True
        assert cols == ["Name", "Col2", "City"]

    def test_long_value_not_header(self):
        rows = [
            ["x" * 81, "Header2"],
            ["data1", "data2"],
        ]
        detected, cols = _detect_table_header(rows)
        assert detected is False

    def test_duplicate_first_two_rows_not_header(self):
        rows = [
            ["A", "B"],
            ["A", "B"],
            ["C", "D"],
        ]
        detected, cols = _detect_table_header(rows)
        assert detected is False


class TestBuildTableRowText:
    def test_basic(self):
        result = _build_table_row_text(["Name", "Age"], ["Alice", 30])
        assert result == "Name=Alice, Age=30"

    def test_none_values_skipped(self):
        result = _build_table_row_text(["A", "B", "C"], ["x", None, "z"])
        assert result == "A=x, C=z"

    def test_extra_values_get_synthetic_col_names(self):
        result = _build_table_row_text(["A"], ["x", "y"])
        assert result == "A=x, Col2=y"


# ---------------------------------------------------------------------------
# Multi-sheet XLSX parsing tests
# ---------------------------------------------------------------------------

class TestParseXlsxMultiSheet:
    def test_three_sheets_all_ingested(self, parser, tmp_path):
        xlsx_path = tmp_path / "multi.xlsx"
        _write_xlsx(xlsx_path, {
            "Products": [
                ["Product", "Price", "Stock"],
                ["Widget", 9.99, 100],
                ["Gadget", 19.99, 50],
            ],
            "Customers": [
                ["Name", "Email"],
                ["Alice", "alice@example.com"],
                ["Bob", "bob@example.com"],
            ],
            "Orders": [
                ["OrderID", "Product", "Qty"],
                ["ORD-001", "Widget", 2],
                ["ORD-002", "Gadget", 1],
                ["ORD-003", "Widget", 5],
            ],
        })

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")

        sheets_seen = {b.metadata["sheet"] for b in blocks}
        assert sheets_seen == {"Products", "Customers", "Orders"}

        products = [b for b in blocks if b.metadata["sheet"] == "Products"]
        customers = [b for b in blocks if b.metadata["sheet"] == "Customers"]
        orders = [b for b in blocks if b.metadata["sheet"] == "Orders"]

        assert len(products) == 2
        assert len(customers) == 2
        assert len(orders) == 3

        assert "Widget" in products[0].text
        assert "alice@example.com" in customers[0].text
        assert "ORD-003" in orders[2].text

    def test_block_index_continuous_across_sheets(self, parser, tmp_path):
        xlsx_path = tmp_path / "continuous.xlsx"
        _write_xlsx(xlsx_path, {
            "Sheet1": [["H1"], ["row1"]],
            "Sheet2": [["H2"], ["row2"]],
            "Sheet3": [["H3"], ["row3"]],
        })

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")
        indices = [b.block_index for b in blocks]
        assert indices == list(range(len(blocks)))

    def test_empty_sheet_skipped(self, parser, tmp_path):
        xlsx_path = tmp_path / "with_empty.xlsx"
        _write_xlsx(xlsx_path, {
            "Data": [["Col", "Val"], ["a", 1]],
            "Empty": [],
            "MoreData": [["X"], ["y"]],
        })

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")
        sheets_seen = {b.metadata["sheet"] for b in blocks}
        assert "Empty" not in sheets_seen
        assert "Data" in sheets_seen
        assert "MoreData" in sheets_seen

    def test_sheet_with_only_whitespace_skipped(self, parser, tmp_path):
        xlsx_path = tmp_path / "whitespace.xlsx"
        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "Blanks"
        ws1.append(["  ", "", None])
        ws1.append([None, "   ", None])
        ws2 = wb.create_sheet("Real")
        ws2.append(["Header"])
        ws2.append(["data_value"])
        wb.save(xlsx_path)
        wb.close()

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")
        sheets_seen = {b.metadata["sheet"] for b in blocks}
        assert "Blanks" not in sheets_seen
        assert "Real" in sheets_seen

    def test_header_detected_per_sheet(self, parser, tmp_path):
        """One sheet has clear header, another has numeric first row."""
        xlsx_path = tmp_path / "mixed_headers.xlsx"
        _write_xlsx(xlsx_path, {
            "WithHeader": [
                ["Name", "Score"],
                ["Alice", 95],
                ["Bob", 88],
            ],
            "NoHeader": [
                [100, 200, 300],
                [400, 500, 600],
            ],
        })

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")

        with_header = [b for b in blocks if b.metadata["sheet"] == "WithHeader"]
        no_header = [b for b in blocks if b.metadata["sheet"] == "NoHeader"]

        assert len(with_header) == 2
        for b in with_header:
            assert b.metadata["table_header_is_self"] is False
            assert "Name" in b.metadata["table_header_candidate"]

        assert len(no_header) == 2
        assert no_header[0].metadata["table_header_is_self"] is True

    def test_metadata_sheet_field_matches_sheet_title(self, parser, tmp_path):
        xlsx_path = tmp_path / "named.xlsx"
        _write_xlsx(xlsx_path, {
            "Test Cases": [["TC", "Desc"], ["TC-1", "Login"]],
            "Requirements": [["Req", "Status"], ["REQ-1", "Open"]],
        })

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")
        for b in blocks:
            assert b.metadata["sheet"] in ("Test Cases", "Requirements")
            assert b.file_type == "xlsx"
            assert b.block_type == "table_row"

    def test_short_rows_filtered(self, parser, tmp_path):
        """Rows with less than 3 chars of text should be skipped."""
        xlsx_path = tmp_path / "short.xlsx"
        _write_xlsx(xlsx_path, {
            "Sheet1": [
                ["Header"],
                ["ab"],    # < 3 chars after build_table_row_text → skipped
                ["abcdef"],
            ],
        })

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="test")
        texts = [b.text for b in blocks]
        assert any("abcdef" in t for t in texts)

    def test_source_file_relative(self, parser, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        xlsx_path = sub / "data.xlsx"
        _write_xlsx(xlsx_path, {"S1": [["A"], ["val"]]})

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="d")
        assert blocks[0].source_file == "sub\\data.xlsx" or blocks[0].source_file == "sub/data.xlsx"

    def test_domain_propagated(self, parser, tmp_path):
        xlsx_path = tmp_path / "d.xlsx"
        _write_xlsx(xlsx_path, {"S1": [["Col"], ["val"]]})

        blocks = parser._parse_xlsx(xlsx_path, input_root=tmp_path, domain="lending")
        for b in blocks:
            assert b.domain == "lending"


# ---------------------------------------------------------------------------
# Golden file test (real smoke file)
# ---------------------------------------------------------------------------

GOLDEN_FILE = Path(__file__).resolve().parent.parent / (
    "golden_ingestion_files_1.0" / Path("smoke")
    / "test_case_generation_c03b663fd0f44fd3b90884cc66a124c4.xlsx"
)


class TestGoldenXlsx:
    @pytest.mark.skipif(not GOLDEN_FILE.exists(), reason="Golden file not present")
    def test_golden_multisheet_ingestion(self, parser):
        blocks = parser._parse_xlsx(
            GOLDEN_FILE,
            input_root=GOLDEN_FILE.parent.parent.parent,
            domain="smoke_test",
        )

        assert len(blocks) > 0, "Expected blocks from golden file"

        sheets_seen = sorted({b.metadata["sheet"] for b in blocks})
        assert len(sheets_seen) >= 2, (
            f"Expected at least 2 sheets in golden file, got {sheets_seen}"
        )

        for b in blocks:
            assert b.file_type == "xlsx"
            assert b.block_type == "table_row"
            assert b.domain == "smoke_test"
            assert b.metadata.get("sheet") in sheets_seen
            assert "table_header_candidate" in b.metadata
            assert isinstance(b.metadata["table_row_index"], int)

        indices = [b.block_index for b in blocks]
        assert indices == list(range(len(blocks))), "block_index must be continuous"

        print(f"\nGolden file: {len(blocks)} blocks across {len(sheets_seen)} sheets")
        for sheet in sheets_seen:
            count = sum(1 for b in blocks if b.metadata["sheet"] == sheet)
            print(f"  Sheet '{sheet}': {count} rows")
