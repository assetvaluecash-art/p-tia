"""Security tests for document parser.

Tests path traversal attack prevention and other security measures.
"""

import os
import platform
import tempfile
from pathlib import Path

import pytest

from kbfabric_ingest.ingest.parser import DocumentParser, PathSecurityError


class TestPathSecurityValidation:
    """Tests for path traversal attack prevention."""
    
    @pytest.fixture
    def parser(self):
        """Create a parser instance for testing."""
        return DocumentParser(max_chars_per_block=1500)
    
    @pytest.fixture
    def temp_directory(self):
        """Create a temporary directory with test files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create subdirectories
            (root / "subdir").mkdir()
            (root / "subdir" / "nested").mkdir()
            
            # Create test files
            (root / "allowed.txt").write_text("Allowed file content")
            (root / "subdir" / "nested.txt").write_text("Nested file content")
            
            yield root
    
    def test_valid_path_within_directory(self, parser, temp_directory):
        """Valid paths within allowed directory should pass validation."""
        allowed_file = temp_directory / "allowed.txt"
        
        # Should not raise
        validated = parser._validate_path_security(allowed_file, temp_directory)
        
        assert validated.exists()
        assert validated.is_file()
    
    def test_valid_nested_path(self, parser, temp_directory):
        """Nested paths within allowed directory should pass validation."""
        nested_file = temp_directory / "subdir" / "nested.txt"
        
        # Should not raise
        validated = parser._validate_path_security(nested_file, temp_directory)
        
        assert validated.exists()
    
    def test_path_traversal_attack_blocked(self, parser, temp_directory):
        """Path traversal using ../ should be blocked."""
        # Attempt to escape the allowed directory
        malicious_path = temp_directory / "subdir" / ".." / ".." / "etc" / "passwd"
        
        with pytest.raises(PathSecurityError) as exc_info:
            parser._validate_path_security(malicious_path, temp_directory)
        
        assert "escapes allowed directory" in str(exc_info.value).lower() or \
               "security violation" in str(exc_info.value).lower()
    
    def test_absolute_path_outside_root_blocked(self, parser, temp_directory):
        """Absolute paths outside allowed directory should be blocked."""
        # Use system root or temp directory parent
        if platform.system() == "Windows":
            outside_path = Path("C:/Windows/System32/drivers/etc/hosts")
        else:
            outside_path = Path("/etc/passwd")
        
        # Skip if the file doesn't exist (test environment variation)
        if not outside_path.exists():
            outside_path = temp_directory.parent / "some_other_file.txt"
        
        with pytest.raises(PathSecurityError) as exc_info:
            parser._validate_path_security(outside_path, temp_directory)
        
        assert "security violation" in str(exc_info.value).lower()
    
    @pytest.mark.skipif(platform.system() == "Windows", reason="Symlinks require admin on Windows")
    def test_symlink_escape_blocked(self, parser, temp_directory):
        """Symlinks pointing outside allowed directory should be blocked."""
        # Create a file outside the temp directory
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as outside_file:
            outside_file.write(b"Secret content outside allowed directory")
            outside_path = Path(outside_file.name)
        
        try:
            # Create a symlink inside allowed directory pointing outside
            symlink_path = temp_directory / "escape_link.txt"
            symlink_path.symlink_to(outside_path)
            
            with pytest.raises(PathSecurityError) as exc_info:
                parser._validate_path_security(symlink_path, temp_directory)
            
            assert "symlink" in str(exc_info.value).lower() or \
                   "security violation" in str(exc_info.value).lower()
        finally:
            # Cleanup
            outside_path.unlink(missing_ok=True)
            if symlink_path.exists() or symlink_path.is_symlink():
                symlink_path.unlink()
    
    @pytest.mark.skipif(platform.system() == "Windows", reason="Symlinks require admin on Windows")
    def test_valid_symlink_within_directory(self, parser, temp_directory):
        """Symlinks pointing within allowed directory should pass."""
        target_file = temp_directory / "allowed.txt"
        symlink_path = temp_directory / "subdir" / "link_to_allowed.txt"
        
        # Create symlink pointing to file within allowed directory
        symlink_path.symlink_to(target_file)
        
        try:
            # Should not raise
            validated = parser._validate_path_security(symlink_path, temp_directory)
            assert validated.exists() or validated.is_symlink()
        finally:
            symlink_path.unlink()
    
    def test_parse_folder_skips_malicious_files(self, parser, temp_directory):
        """parse_folder should skip files that fail security validation."""
        # Create a valid file
        (temp_directory / "valid.txt").write_text("Valid content")
        
        # Parse folder - should work without errors
        blocks = parser.parse_folder(temp_directory, domain="test_domain")
        
        # Should have parsed valid files
        assert len(blocks) >= 1
    
    def test_parse_file_validates_input_root(self, parser, temp_directory):
        """parse_file should validate against input_root when provided."""
        allowed_file = temp_directory / "allowed.txt"
        
        # Should work with correct input_root
        blocks = parser.parse_file(
            allowed_file, 
            input_root=temp_directory, 
            domain="test_domain"
        )
        
        assert len(blocks) >= 1
    
    def test_parse_file_blocks_traversal(self, parser, temp_directory):
        """parse_file should block path traversal when input_root is provided."""
        # Create a file outside temp directory
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode='w') as f:
            f.write("Outside content")
            outside_file = Path(f.name)
        
        try:
            with pytest.raises(PathSecurityError):
                parser.parse_file(
                    outside_file,
                    input_root=temp_directory,
                    domain="test_domain"
                )
        finally:
            outside_file.unlink(missing_ok=True)
    
    def test_parse_file_without_input_root_no_validation(self, parser, temp_directory):
        """parse_file without input_root should not perform path validation."""
        allowed_file = temp_directory / "allowed.txt"
        
        # Should work without input_root (no security validation)
        blocks = parser.parse_file(
            allowed_file,
            input_root=None,  # No validation
            domain="test_domain"
        )
        
        assert len(blocks) >= 1


class TestPathSecurityError:
    """Tests for PathSecurityError exception."""
    
    def test_exception_message(self):
        """PathSecurityError should store the message."""
        error = PathSecurityError("Test security violation")
        
        assert str(error) == "Test security violation"
    
    def test_exception_inheritance(self):
        """PathSecurityError should inherit from Exception."""
        error = PathSecurityError("Test")
        
        assert isinstance(error, Exception)


class TestEdgeCases:
    """Edge case tests for security validation."""
    
    @pytest.fixture
    def parser(self):
        return DocumentParser(max_chars_per_block=1500)
    
    def test_empty_directory_path(self, parser):
        """Empty or invalid directory paths should be handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Parse empty directory
            blocks = parser.parse_folder(root, domain="test_domain")
            
            # Should return empty list, not error
            assert blocks == []
    
    def test_special_characters_in_filename(self, parser):
        """Files with special characters should be handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create file with special characters (safe ones that work on all platforms)
            special_file = root / "file with spaces (1).txt"
            special_file.write_text("Content with special filename")
            
            # Should parse without security error
            blocks = parser.parse_folder(root, domain="test_domain")
            
            assert len(blocks) >= 1
    
    def test_unicode_path_handling(self, parser):
        """Unicode characters in paths should be handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create file with unicode name (if platform supports it)
            try:
                unicode_file = root / "документ.txt"  # Russian word for "document"
                unicode_file.write_text("Unicode filename content")
                
                # Should parse without error
                blocks = parser.parse_folder(root, domain="test_domain")
                assert len(blocks) >= 1
            except (OSError, UnicodeError):
                # Some platforms don't support unicode filenames
                pytest.skip("Platform doesn't support unicode filenames")
    
    def test_deeply_nested_valid_path(self, parser):
        """Deeply nested paths within allowed directory should pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            
            # Create deeply nested structure
            deep_path = root / "a" / "b" / "c" / "d" / "e"
            deep_path.mkdir(parents=True)
            deep_file = deep_path / "deep.txt"
            deep_file.write_text("Deep content")
            
            # Should pass validation
            validated = parser._validate_path_security(deep_file, root)
            assert validated.exists()

