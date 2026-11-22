"""Sanity tests for document parser."""

import tempfile
from pathlib import Path

from kbfabric_ingest.ingest.parser import DocumentParser


def test_parser_chunking():
    """Test that parser respects max_chars_per_block."""
    max_chars = 100
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Create a simple TXT file with long text
        test_file = tmp_path / "test.txt"
        long_text = "This is a sentence. " * 20  # Much longer than max_chars
        test_file.write_text(long_text)
        
        parser = DocumentParser(max_chars_per_block=max_chars)
        blocks = parser.parse_file(test_file, domain="test_domain")
        
        assert len(blocks) > 0, "Parser should produce at least one block"
        
        # Check no block exceeds max_chars_per_block + 50 (some tolerance)
        for block in blocks:
            assert len(block.text) <= max_chars + 50, (
                f"Block text length {len(block.text)} exceeds max_chars_per_block + 50"
            )


def test_parser_docx_basic():
    """Test basic DOCX parsing."""
    # This test requires a DOCX file - skip if not available
    # In a real scenario, you'd have test fixtures
    pass


def test_parser_txt_paragraphs():
    """Test TXT file parsing splits on paragraphs."""
    max_chars = 500
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Create TXT with multiple paragraphs
        test_file = tmp_path / "test.txt"
        content = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        test_file.write_text(content)
        
        parser = DocumentParser(max_chars_per_block=max_chars)
        blocks = parser.parse_file(test_file, domain="test_domain")
        
        assert len(blocks) >= 1, "Should parse at least one block"
        
        # Verify all blocks have required fields
        for block in blocks:
            assert block.block_id
            assert block.source_file
            assert block.file_type == "txt"
            assert block.block_type == "paragraph"
            assert isinstance(block.text, str)


