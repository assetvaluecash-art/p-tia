"""
Base Parser Architecture - Unified pattern for all file parsers.

Provides common functionality for:
- Image extraction
- Embedded document handling
- Chart extraction
- Text chunking
- Error handling
- Path security validation
"""

import re
import tempfile
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.exceptions import PathSecurityError


class EmbeddedDocError(Exception):
    """Exception for embedded document extraction failures."""
    pass


class BaseParser(ABC):
    """Base class for all document parsers with unified patterns."""
    
    def __init__(self, max_chars_per_block: int = 1000):
        """
        Initialize base parser.
        
        Args:
            max_chars_per_block: Maximum characters per block for chunking
        """
        self.logger = get_logger(__name__)
        self.max_chars_per_block = max_chars_per_block
    
    @abstractmethod
    def _parse_content(self, path: Path, input_root: Optional[Path], domain: str, 
                      embedded_depth: int, parent_document: str, embedded_filename: str) -> List[CanonicalBlock]:
        """
        Parse main content of the file.
        
        Must be implemented by subclasses.
        """
        pass
    
    def parse_file(self, path: Path, input_root: Optional[Path] = None, domain: str = "",
                   embedded_depth: int = 0, parent_document: str = "", embedded_filename: str = "") -> List[CanonicalBlock]:
        """
        Unified entry point for parsing files.
        
        Args:
            path: Path to file
            input_root: Root directory for security validation
            domain: Domain name
            embedded_depth: Current embedding depth (for circular reference detection)
            parent_document: Parent document path (for embedded docs)
            embedded_filename: Embedded filename (for embedded docs)
            
        Returns:
            List of CanonicalBlock instances
        """
        if not domain:
            raise ValueError("domain parameter is required")
        
        # Circular reference detection
        if embedded_depth > 3:
            self.logger.warning(
                "Embedded document depth limit exceeded",
                file=str(path),
                depth=embedded_depth,
                parent=parent_document
            )
            return []
        
        # Security validation
        if input_root is not None:
            path = self._validate_path_security(path, input_root)
        
        self.logger.info(
            "Starting file parsing",
            file=str(path),
            domain=domain,
            embedded_depth=embedded_depth,
            parent_document=parent_document,
            embedded_filename=embedded_filename
        )
        
        try:
            # Parse main content
            blocks = self._parse_content(path, input_root, domain, embedded_depth, parent_document, embedded_filename)
            
            # Process embedded documents if any
            embedded_docs = self._extract_embedded_docs(path, embedded_depth, parent_document)
            if embedded_docs:
                blocks.extend(self._process_embedded_docs(embedded_docs, domain, embedded_depth, parent_document, str(path)))
            
            return blocks
            
        except Exception as e:
            self._handle_errors(e, {
                "file": str(path),
                "domain": domain,
                "embedded_depth": embedded_depth
            })
            return []
    
    def _validate_path_security(self, path: Path, input_root: Path) -> Path:
        """
        Validate that path is within allowed root directory.
        
        Args:
            path: Path to validate
            input_root: Allowed root directory
            
        Returns:
            Validated path
            
        Raises:
            PathSecurityError: If path is outside allowed root
        """
        try:
            resolved_path = path.resolve()
            resolved_root = input_root.resolve()
            
            if not resolved_path.is_relative_to(resolved_root):
                raise PathSecurityError(
                    f"Path {resolved_path} is outside allowed root {resolved_root}"
                )
            
            return resolved_path
        except Exception as e:
            if isinstance(e, PathSecurityError):
                raise
            raise PathSecurityError(f"Path validation failed: {str(e)}")
    
    def _extract_images(self, path: Path) -> Dict[int, bytes]:
        """
        Extract images from file.
        
        Must be implemented by subclasses that support images.
        
        Args:
            path: Path to file
            
        Returns:
            Dictionary mapping image index to image bytes
        """
        return {}
    
    def _extract_embedded_docs(self, path: Path, embedded_depth: int, parent_document: str) -> Dict[int, Dict[str, Any]]:
        """
        Extract embedded documents from file.
        
        Must be implemented by subclasses that support embedded docs.
        
        Args:
            path: Path to file
            embedded_depth: Current embedding depth
            parent_document: Parent document path
            
        Returns:
            Dictionary mapping embedded index to embedded document info
        """
        return {}
    
    def _extract_charts(self, path: Path) -> List[bytes]:
        """
        Extract charts/diagrams from file.
        
        Must be implemented by subclasses that support charts (Excel, PPT).
        
        Args:
            path: Path to file
            
        Returns:
            List of chart image bytes
        """
        return []
    
    def _process_embedded_docs(self, embedded_docs: Dict[int, Dict[str, Any]], domain: str,
                               embedded_depth: int, parent_document: str, current_file: str) -> List[CanonicalBlock]:
        """
        Process embedded documents recursively.
        
        Args:
            embedded_docs: Dictionary of embedded document info
            domain: Domain name
            embedded_depth: Current embedding depth
            parent_document: Parent document path
            current_file: Current file path
            
        Returns:
            List of CanonicalBlock instances from embedded documents
        """
        embedded_blocks = []
        
        for embedded_idx, embedded_info in embedded_docs.items():
            try:
                # Create temporary file
                with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp_file:
                    tmp_file.write(embedded_info['bytes'])
                    tmp_file_path = tmp_file.name
                
                try:
                    # Detect file type and route to appropriate parser
                    content_type = embedded_info.get('content_type', 'unknown')
                    file_path = Path(tmp_file_path)
                    
                    # Determine parser based on MIME type or file extension
                    suffix = self._detect_file_type_from_mime(content_type, embedded_info.get('filename', ''))
                    if suffix:
                        file_path = file_path.with_suffix(suffix)
                    
                    # Recursively parse embedded document
                    from kbfabric_ingest.ingest.parser import DocumentParser
                    parser = DocumentParser(max_chars_per_block=self.max_chars_per_block)
                    
                    embedded_blocks_result = parser.parse_file(
                        file_path,
                        domain=domain,
                        embedded_depth=embedded_depth + 1,
                        parent_document=current_file,
                        embedded_filename=embedded_info.get('filename', f'embedded_{embedded_idx}')
                    )
                    
                    # Mark all embedded blocks with lineage metadata
                    for block in embedded_blocks_result:
                        if not hasattr(block, 'metadata') or block.metadata is None:
                            block.metadata = {}
                        block.metadata.update({
                            'is_embedded': True,
                            'lineage_path': f"{current_file} → {embedded_info.get('filename', f'embedded_{embedded_idx}')}",
                            'embedding_depth': embedded_depth + 1,
                            'parent_document': current_file,
                            'embedded_filename': embedded_info.get('filename', f'embedded_{embedded_idx}'),
                            'content_type': content_type,
                            'extraction_method': 'embedded_ole_object'
                        })
                    
                    embedded_blocks.extend(embedded_blocks_result)
                    
                    self.logger.info(
                        "Processed embedded document",
                        parent=current_file,
                        embedded=embedded_info.get('filename', f'embedded_{embedded_idx}'),
                        blocks_created=len(embedded_blocks_result)
                    )
                    
                finally:
                    # Clean up temporary file
                    try:
                        os.unlink(tmp_file_path)
                    except Exception:
                        pass
                        
            except Exception as e:
                self.logger.warning(
                    "Failed to process embedded document",
                    error=str(e),
                    parent=current_file,
                    embedded=embedded_info.get('filename', f'embedded_{embedded_idx}')
                )
        
        return embedded_blocks
    
    def _detect_file_type_from_mime(self, content_type: str, filename: str) -> Optional[str]:
        """
        Detect file extension from MIME type or filename.
        
        Args:
            content_type: MIME type
            filename: Filename hint
            
        Returns:
            File extension (e.g., '.xlsx') or None
        """
        mime_to_ext = {
            'application/vnd.ms-excel': '.xls',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
            'application/pdf': '.pdf',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
            'application/json': '.json',
            'text/plain': '.txt',
            'text/csv': '.csv'
        }
        
        # Try MIME type first
        if content_type and content_type in mime_to_ext:
            return mime_to_ext[content_type]
        
        # Fallback to filename extension
        if filename:
            ext = Path(filename).suffix.lower()
            if ext:
                return ext
        
        return None
    
    def _chunk_text(self, text: str, block_index_start: int) -> List[Tuple[str, int]]:
        """
        Chunk text into blocks respecting max_chars_per_block.
        
        Args:
            text: Text to chunk
            block_index_start: Starting block index
            
        Returns:
            List of (chunk_text, block_index) tuples
        """
        if len(text) <= self.max_chars_per_block:
            return [(text, block_index_start)]
        
        # Split by sentences
        sentences = self._split_text_by_sentences(text)
        chunks: List[Tuple[str, int]] = []
        current_chunk = ""
        current_index = block_index_start
        
        for sentence in sentences:
            if len(current_chunk) + len(sentence) <= self.max_chars_per_block:
                current_chunk += sentence + " "
            else:
                if current_chunk:
                    chunks.append((current_chunk.strip(), current_index))
                    current_index += 1
                # If single sentence is too long, split it anyway
                if len(sentence) > self.max_chars_per_block:
                    remaining = sentence
                    while len(remaining) > self.max_chars_per_block:
                        chunks.append((remaining[:self.max_chars_per_block], current_index))
                        remaining = remaining[self.max_chars_per_block:]
                        current_index += 1
                    current_chunk = remaining + " "
                else:
                    current_chunk = sentence + " "
        
        if current_chunk:
            chunks.append((current_chunk.strip(), current_index))
        
        return chunks
    
    def _split_text_by_sentences(self, text: str) -> List[str]:
        """
        Split text into sentences.
        
        Args:
            text: Text to split
            
        Returns:
            List of sentences
        """
        # Simple sentence splitting by punctuation
        sentences = []
        current = ""
        
        for part in re.split(r'([.!?]\s+)', text):
            if part.strip():
                if re.match(r'^[.!?]\s+$', part):
                    if current:
                        sentences.append(current + part[0])
                        current = part[1:].lstrip()
                    else:
                        current = part
                else:
                    current += part
        
        if current:
            sentences.append(current)
        
        return sentences if sentences else [text]
    
    def _handle_errors(self, error: Exception, context: Dict[str, Any]) -> None:
        """
        Handle errors gracefully with logging.
        
        Args:
            error: Exception that occurred
            context: Context information for logging
        """
        self.logger.error(
            "Parser error",
            error=str(error),
            error_type=type(error).__name__,
            **context
        )

