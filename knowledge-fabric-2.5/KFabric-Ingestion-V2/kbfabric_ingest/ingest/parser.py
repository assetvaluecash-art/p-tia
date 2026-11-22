"""Document parser for DOCX, PDF, XLSX, TXT, CSV, JSON, and PPTX files.

This module provides document parsing capabilities with security protections
against path traversal attacks.
"""

import hashlib
import json
import re
import tempfile
import uuid
import zipfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from docx import Document as DocxDocument
import fitz  # PyMuPDF
from openpyxl import load_workbook
from unstructured.partition.docx import partition_docx

# Optional imports with graceful fallback
try:
    from pptx import Presentation
    PPTX_AVAILABLE = True
except ImportError:
    PPTX_AVAILABLE = False
    Presentation = None

from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.ingest.base_parser import BaseParser
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.ingestion_observability import log_stage_start, log_stage_end, log_stage_error
from kbfabric_ingest._common.telemetry import record_document_parse
from kbfabric_ingest.ingest.vision_utils.image_filtering import should_process_image
from kbfabric_ingest.utils.exceptions import PathSecurityError

logger = get_logger(__name__)

# Namespace UUID for deterministic block_id generation
BLOCK_ID_NAMESPACE = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')  # Standard namespace URL


def generate_block_id(source_file: str, block_index: int, domain: str, content: str = "") -> str:
    """
    Generate deterministic UUID5 block_id from stable attributes.
    
    Uses content-based hashing for resilience against document edits.
    When content is provided, the block ID is based on a hash of the content,
    so the same content always gets the same ID regardless of its position.
    This prevents duplicate blocks when paragraphs are added/removed from documents.
    
    Args:
        source_file: Relative path to source file
        block_index: Sequential index of block within file (used as fallback)
        domain: Domain name (e.g., "trade_finance")
        content: Text content of the block (recommended for upsert stability)
        
    Returns:
        Deterministic UUID5 string
    """
    # Use content hash for stability across document edits.
    # Full content is hashed (not just a prefix) to avoid collisions on
    # multi-row spreadsheet entries that share the same TC metadata prefix.
    # block_index is always included as a disambiguator so rows with
    # identical text still get distinct IDs.
    if content and content.strip():
        content_signature = hashlib.sha256(content.encode()).hexdigest()[:20]
    else:
        content_signature = "empty"
    
    stable_string = f"{domain}:{source_file}:{block_index}:{content_signature}"
    return str(uuid.uuid5(BLOCK_ID_NAMESPACE, stable_string))


def _detect_table_header(rows: list[list[str | None]]) -> tuple[bool, list[str]]:
    """Detect whether the first row of a table is a header and return column names.

    Args:
        rows: List of rows, each a list of cell values (may contain None).

    Returns:
        (header_detected, column_names) — if header_detected is False,
        column_names are synthetic (Col1, Col2, ...).
    """
    if not rows or not rows[0]:
        return False, []

    first_row = rows[0]
    num_cols = len(first_row)
    non_empty = [str(v).strip() for v in first_row if v is not None and str(v).strip()]

    if not non_empty:
        return False, [f"Col{i + 1}" for i in range(num_cols)]

    looks_like_header = True
    for val in non_empty:
        # Pure numbers, dates, or ID-like values (e.g. TC_001, #N/A) → data, not header
        stripped = val.replace(",", "").replace(".", "").replace("-", "").replace("_", "")
        if stripped.isdigit():
            looks_like_header = False
            break
        if val.startswith("#"):
            looks_like_header = False
            break
        if len(val) > 80:
            looks_like_header = False
            break

    if looks_like_header and len(rows) > 1:
        second_row_non_empty = [str(v).strip() for v in rows[1] if v is not None and str(v).strip()]
        if second_row_non_empty == non_empty:
            looks_like_header = False

    if looks_like_header:
        col_names = []
        for i, v in enumerate(first_row):
            if v is not None and str(v).strip():
                col_names.append(str(v).strip())
            else:
                col_names.append(f"Col{i + 1}")
        return True, col_names
    else:
        return False, [f"Col{i + 1}" for i in range(num_cols)]


def _sanitize_col_name(name: str) -> str:
    """Shorten long column headers and strip parenthetical suffixes that
    produce malformed entity names like 'Rsd)=Bank_Fsd_Corporate Loan'."""
    import re as _re
    name = _re.sub(r'\([^)]*\)\s*$', '', name).strip()
    if len(name) > 60:
        name = name[:60].rstrip()
    return name or "Col"


def _build_table_row_text(col_names: list[str], row_values: list, separator: str = ", ") -> str:
    """Build 'ColName: Value' text from column names and row values."""
    parts = []
    for i, val in enumerate(row_values):
        if val is not None and str(val).strip():
            name = col_names[i] if i < len(col_names) else f"Col{i + 1}"
            name = _sanitize_col_name(name)
            parts.append(f"{name}: {val}")
    return separator.join(parts)


class DocumentParser(BaseParser):
    """Parser for various document formats with chunking support.
    
    Includes security protections against path traversal attacks when
    parsing files from user-specified directories.
    """

    def __init__(self, max_chars_per_block: int = 1000, artifacts_dir: Path | None = None):
        """
        Initialize parser.
        
        Args:
            max_chars_per_block: Maximum characters per block before splitting
            artifacts_dir: Optional directory to save artifacts (PDFs, uplifted images)
        """
        super().__init__(max_chars_per_block=max_chars_per_block)
        self.artifacts_dir = artifacts_dir
        self.logger = get_logger(__name__)
        
        # Create artifacts subdirectories if specified
        if self.artifacts_dir:
            self.pdfs_dir = self.artifacts_dir / "pdfs"
            self.images_dir = self.artifacts_dir / "images"
            self.pdfs_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Artifacts will be saved to: {self.artifacts_dir}")

    # NOTE: EMF/WMF conversion intentionally removed.
    # These formats are treated as unsupported and skipped with explicit audit entries.

    def _optimize_image_for_vision(self, image_bytes: bytes) -> bytes | None:
        """
        Optimize image for vision model BEFORE sending to API.
        
        SELECTIVE OPTIMIZATION - Skip if:
        - Image is already high-quality (>= 1024px on both dims AND high sharpness/contrast)
        - Extreme aspect ratio (likely decorative line/border)
        - Very small image (< 50px on either dimension - decorative)
        
        Enhancements (when applied):
        - Upscale if resolution < 1024px (vision models need detail)
        - Increase contrast by 15-20% for better text readability
        - Apply mild sharpening filter
        - Convert to RGB if grayscale
        - Set DPI to 300 if available
        """
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            import io
            import numpy as np
            
            img = Image.open(io.BytesIO(image_bytes))
            original_size = img.size
            
            # SELECTIVE CRITERIA: Skip optimization for problematic images
            aspect_ratio = max(img.width, img.height) / min(img.width, img.height)
            
            # Skip if extreme aspect ratio (>20:1) - likely decorative line/border
            # ALSO mark to skip vision IE entirely (no value from decorative elements)
            if aspect_ratio > 20:
                print(f"[Parser] Skipping optimization AND vision IE (extreme aspect ratio {aspect_ratio:.1f}:1): {original_size}", flush=True)
                # Return original bytes but caller should mark block metadata
                return image_bytes  # Caller will set skip_vision_ie=True
            
            # Skip if very small (decorative icon/spacer)
            # ALSO mark to skip vision IE entirely
            if img.width < 50 or img.height < 50:
                print(f"[Parser] Skipping optimization AND vision IE (too small): {original_size}", flush=True)
                # Return original bytes but caller should mark block metadata
                return image_bytes  # Caller will set skip_vision_ie=True
            
            # NEW: Detect if image is already high-quality (crisp, good contrast)
            if img.width >= 800 and img.height >= 800:
                # Convert to grayscale for quality analysis
                gray = img.convert('L')
                gray_array = np.array(gray)
                
                # Measure contrast (standard deviation of pixel values)
                contrast = gray_array.std()
                
                # Measure sharpness (Laplacian variance - edge detection)
                from scipy import ndimage
                laplacian = ndimage.laplace(gray_array)
                sharpness = laplacian.var()
                
                # Skip if already high-quality (good contrast AND sharp)
                if contrast > 50 and sharpness > 100:
                    print(f"[Parser] Skipping optimization (already crisp): {original_size}, contrast={contrast:.1f}, sharpness={sharpness:.1f}", flush=True)
                    # Still convert to RGB and save as PNG for consistency
                    if img.mode != 'RGB':
                        if img.mode == 'RGBA':
                            rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                            rgb_img.paste(img, mask=img.split()[3])
                            img = rgb_img
                        else:
                            img = img.convert('RGB')
                    output = io.BytesIO()
                    img.save(output, format='PNG', dpi=(300, 300))
                    return output.getvalue()
            
            # Proceed with optimization for low-quality images
            print(f"[Parser] Applying optimization (low-quality detected): {original_size}", flush=True)
            
            # Upscale if too small (vision models work better with higher resolution)
            if img.width < 1024 or img.height < 1024:
                target_min_dim = 1024
                max_upscale_dim = 2048
                scale_target = max(target_min_dim / img.width, target_min_dim / img.height)
                scale_cap = max_upscale_dim / max(img.width, img.height)
                scale_factor = min(scale_target, max(1.0, scale_cap))
                if scale_factor > 1.0:
                    new_size = (int(img.width * scale_factor), int(img.height * scale_factor))
                    img = img.resize(new_size, Image.LANCZOS)
                    print(f"[Parser] Upscaled image: {original_size} → {new_size}", flush=True)
            
            # Enhance contrast (15% increase for better text readability)
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(1.15)
            
            # Mild sharpening (unsharp mask)
            img = img.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3))
            
            # Convert to RGB if needed (some vision models prefer RGB)
            if img.mode != 'RGB':
                if img.mode == 'RGBA':
                    # Preserve transparency by converting to RGB with white background
                    rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                    rgb_img.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                    img = rgb_img
                else:
                    img = img.convert('RGB')
            
            # Save with quality settings (300 DPI)
            output = io.BytesIO()
            img.save(output, format='PNG', dpi=(300, 300))
            optimized_bytes = output.getvalue()
            
            return optimized_bytes
            
        except Exception as e:
            self.logger.warning(f"Image optimization failed, using original", error=str(e))
            print(f"[Parser] Warning: Image optimization failed: {e}", flush=True)
            return image_bytes  # Return original if optimization fails

    def _validate_converted_image(self, image_bytes: bytes) -> tuple[bool, str]:
        """Validate converted image is not garbage."""
        if not image_bytes or len(image_bytes) == 0:
            return False, "EMPTY_OUTPUT"
        
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(image_bytes))
            width, height = img.size
            
            if width == 0 or height == 0:
                return False, "ZERO_DIMENSIONS"
            
            # Check if image is completely blank (all white/black)
            # Sample a few pixels - if all are same color, might be blank
            if width > 10 and height > 10:
                sample_pixels = [
                    img.getpixel((width // 4, height // 4)),
                    img.getpixel((width // 2, height // 2)),
                    img.getpixel((3 * width // 4, 3 * height // 4))
                ]
                if len(set(sample_pixels)) == 1:
                    # All pixels same - might be blank, but don't fail (could be intentional)
                    pass
            
            img.close()
            return True, "VALID"
        except Exception as e:
            return False, f"VALIDATION_ERROR: {str(e)}"

    # ---------------------------------------------------------------------------
    # Image tiling — for tall smask composites that exceed the vision LM pixel budget
    # ---------------------------------------------------------------------------
    _TILE_ASPECT_THRESHOLD: float = 2.5   # height:width ratio that triggers tiling
    _TILE_MIN_PIXELS: int = 4_000_000     # only tile native-resolution composites (>4M px)
    _TILE_NUM_STRIPS: int = 3             # split into this many vertical strips
    _TILE_OVERLAP: float = 0.15           # 15% overlap between adjacent strips

    def _tile_image_into_strips(self, img_bytes: bytes) -> list[bytes]:
        """
        Split a tall image into N overlapping vertical strips.

        Each strip is returned as raw PNG bytes.  The 15 % overlap ensures that
        entities near strip boundaries are visible to the Vision LM in at least
        one strip at near-native resolution rather than being cut off.

        Tiling only runs when the caller detects a tall image
        (height/width > _TILE_ASPECT_THRESHOLD AND pixels > _TILE_MIN_PIXELS).
        """
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size
        strip_h = h // self._TILE_NUM_STRIPS
        overlap_px = int(strip_h * self._TILE_OVERLAP)

        strips = []
        for i in range(self._TILE_NUM_STRIPS):
            top = max(0, i * strip_h - (overlap_px if i > 0 else 0))
            bottom = min(h, (i + 1) * strip_h + (overlap_px if i < self._TILE_NUM_STRIPS - 1 else 0))
            strip_img = img.crop((0, top, w, bottom))
            buf = io.BytesIO()
            strip_img.save(buf, format="PNG")
            strips.append(buf.getvalue())
            print(
                f"[Parser][tile] Strip {i + 1}/{self._TILE_NUM_STRIPS}: "
                f"rows {top}–{bottom} ({w}×{bottom - top} px)",
                flush=True,
            )

        return strips

    # ------------------------------------------------------------------ #
    # Tile thresholds (three-tier, applied to both PDF and standalone PNG) #
    # ------------------------------------------------------------------ #
    # Tier 1 — extreme tall   (h/w > 2.5 AND px > 8M): vertical strips
    # Tier 2 — extreme wide   (w/h > 2.5 AND px > 8M): horizontal strips
    # Tier 3 — large moderate-aspect (px > 8M, neither axis extreme):
    #          tile along the longer axis (covers squares, 1.5:1 images)
    # Tier 4 — fits in Vision LM budget (px ≤ 8M): single block, no tiling
    # All tiers share the same strip count and overlap parameters.

    def _tile_image_into_horizontal_strips(self, img_bytes: bytes) -> list[bytes]:
        """
        Split a wide image into N overlapping horizontal strips (left → right).

        Mirrors the logic of _tile_image_into_strips (vertical) but operates
        along the width axis.  Intended for landscape architecture/system
        diagrams where the Vision LM pixel budget would otherwise force an
        aggressive downscale.
        """
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(img_bytes))
        w, h = img.size
        strip_w = w // self._TILE_NUM_STRIPS
        overlap_px = int(strip_w * self._TILE_OVERLAP)

        strips: list[bytes] = []
        for i in range(self._TILE_NUM_STRIPS):
            left  = max(0, i * strip_w - (overlap_px if i > 0 else 0))
            right = min(w, (i + 1) * strip_w + (overlap_px if i < self._TILE_NUM_STRIPS - 1 else 0))
            strip_img = img.crop((left, 0, right, h))
            buf = io.BytesIO()
            strip_img.save(buf, format="PNG")
            strips.append(buf.getvalue())
            print(
                f"[Parser][tile-H] Strip {i + 1}/{self._TILE_NUM_STRIPS}: "
                f"cols {left}–{right} ({right - left}×{h} px)",
                flush=True,
            )

        return strips

    def _parse_png(
        self,
        path: Path,
        input_root: Path | None = None,
        domain: str = "",
    ) -> list["CanonicalBlock"]:
        """
        Parse a standalone PNG (or JPEG) image file into CanonicalBlocks.

        Processing steps
        ----------------
        1. Read raw bytes, derive a relative source path.
        2. Run OCR (Tesseract) on native-resolution image.
        3. Decide tiling strategy (three-tier):
           a. Extreme wide  (w/h > 2.5  AND px > 8M) → horizontal strips
           b. Extreme tall  (h/w > 2.5  AND px > 8M) → vertical strips
           c. Large moderate-aspect (px > 8M, neither axis extreme)
              → tile along longer axis (squares, 1.5:1 landscapes/portraits)
           d. Otherwise → single block, no tiling
        4. For every image/strip: create one CanonicalBlock with image_bytes,
           ocr_text, and (if tiled) tile_strip_index / tile_strip_total /
           tile_direction metadata.
        5. Save strip artifacts when artifacts_dir is configured.
        """
        import io as _io
        import hashlib as _hl
        from PIL import Image

        raw_bytes = path.read_bytes()
        source_file_rel = (
            str(path.relative_to(input_root)) if input_root else str(path)
        )
        file_type = path.suffix.lstrip(".").lower()  # "png" or "jpg"

        img = Image.open(_io.BytesIO(raw_bytes))
        w, h = img.size
        total_px = w * h
        wide_aspect = w / h  # > 2.5 → horizontal tile candidate
        tall_aspect = h / w  # > 2.5 → vertical   tile candidate

        print(
            f"[Parser][PNG] {path.name}: {w}×{h}, {total_px / 1e6:.2f}M px, "
            f"wide_aspect={wide_aspect:.2f}, tall_aspect={tall_aspect:.2f}",
            flush=True,
        )

        # OCR on the full native image (before any tiling / resizing)
        ocr_text = self._extract_ocr_text(raw_bytes)
        if ocr_text:
            print(
                f"[Parser][PNG] OCR extracted {len(ocr_text)} chars from {path.name}",
                flush=True,
            )

        # Convert to PNG bytes if input is JPEG (normalise format)
        if img.format and img.format.upper() in ("JPEG", "JPG"):
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            raw_bytes = buf.getvalue()

        # Three-tier tiling decision.
        # Tier 1: extreme wide  (w/h > threshold)  → horizontal strips
        # Tier 2: extreme tall  (h/w > threshold)  → vertical strips
        # Tier 3: large moderate-aspect (px > budget, neither axis extreme)
        #         → tile along the longer axis so labels stay readable
        # Tier 4: small enough to fit in Vision LM budget → single block
        should_tile_h = wide_aspect > self._TILE_ASPECT_THRESHOLD and total_px > self._TILE_MIN_PIXELS
        should_tile_v = tall_aspect  > self._TILE_ASPECT_THRESHOLD and total_px > self._TILE_MIN_PIXELS
        should_tile_moderate = total_px > self._TILE_MIN_PIXELS  # catches squares, 1.5:1 etc.

        if should_tile_h:
            strips = self._tile_image_into_horizontal_strips(raw_bytes)
            tile_dir = "horizontal"
            print(
                f"[Parser][PNG] Tier-1 horizontal tiling {path.name} "
                f"({w}×{h}, {total_px / 1e6:.1f}M px, w/h={wide_aspect:.2f}) "
                f"into {len(strips)} strips",
                flush=True,
            )
            self.logger.info(
                "Wide PNG tiled horizontally (tier-1)",
                file=str(path), native_size=f"{w}x{h}", strips=len(strips),
            )
        elif should_tile_v:
            strips = self._tile_image_into_strips(raw_bytes)
            tile_dir = "vertical"
            print(
                f"[Parser][PNG] Tier-2 vertical tiling {path.name} "
                f"({w}×{h}, {total_px / 1e6:.1f}M px, h/w={tall_aspect:.2f}) "
                f"into {len(strips)} strips",
                flush=True,
            )
            self.logger.info(
                "Tall PNG tiled vertically (tier-2)",
                file=str(path), native_size=f"{w}x{h}", strips=len(strips),
            )
        elif should_tile_moderate:
            # Large image with moderate aspect — tile along its longer axis.
            if w >= h:
                strips = self._tile_image_into_horizontal_strips(raw_bytes)
                tile_dir = "horizontal"
            else:
                strips = self._tile_image_into_strips(raw_bytes)
                tile_dir = "vertical"
            print(
                f"[Parser][PNG] Tier-3 moderate-aspect tiling {path.name} "
                f"({w}×{h}, {total_px / 1e6:.1f}M px, w/h={wide_aspect:.2f}) "
                f"→ {tile_dir} into {len(strips)} strips",
                flush=True,
            )
            self.logger.info(
                "Moderate-aspect PNG tiled along longer axis (tier-3)",
                file=str(path), native_size=f"{w}x{h}",
                direction=tile_dir, strips=len(strips),
            )
        else:
            strips = [raw_bytes]
            tile_dir = None

        blocks: list[CanonicalBlock] = []
        total_strips = len(strips)

        for strip_idx, strip_bytes in enumerate(strips):
            metadata: dict = {
                "source": "standalone_image",
                "original_format": file_type,
                "native_width": w,
                "native_height": h,
            }
            if ocr_text:
                metadata["ocr_text"] = ocr_text
            if total_strips > 1:
                metadata["tile_strip_index"] = strip_idx + 1
                metadata["tile_strip_total"] = total_strips
                metadata["tile_direction"] = tile_dir

            # Save strip artifact
            if self.artifacts_dir and self.images_dir:
                try:
                    strip_hash = _hl.md5(strip_bytes).hexdigest()[:8]
                    prefix = (
                        f"tile_{tile_dir[0]}"
                        f"_strip{strip_idx + 1}of{total_strips}_{strip_hash}"
                        if total_strips > 1
                        else f"png_{_hl.md5(raw_bytes).hexdigest()[:8]}"
                    )
                    artifact_path = self.images_dir / f"{prefix}.png"
                    artifact_path.write_bytes(strip_bytes)
                    self.logger.info(
                        "Saved PNG artifact",
                        path=str(artifact_path),
                        strip=f"{strip_idx + 1}/{total_strips}",
                    )
                except Exception as _ae:
                    self.logger.warning("Failed to save PNG artifact", error=str(_ae))

            blocks.append(
                CanonicalBlock(
                    block_id=generate_block_id(
                        source_file_rel, strip_idx, domain, content=""
                    ),
                    source_file=source_file_rel,
                    file_type=file_type,
                    domain=domain,
                    page_number=1,
                    block_index=strip_idx,
                    block_type="image",
                    text="",
                    image_bytes=strip_bytes,
                    metadata=metadata,
                )
            )

        self.logger.info(
            "PNG parsed",
            file=str(path),
            blocks=len(blocks),
            tiled=total_strips > 1,
        )
        return blocks

    def _extract_ocr_text(self, image_bytes: bytes) -> str:
        """
        Run Tesseract OCR on an image and return all detected text.

        The result is stored in block metadata and injected into the Vision LM
        context_text so the model can verify or fill entity names it cannot read
        at reduced resolution.

        Fails silently (returns "") if pytesseract is not installed or Tesseract
        binary is absent — the rest of the pipeline is unaffected.
        """
        try:
            import pytesseract
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(image_bytes))
            # PSM 6: assume a single uniform block of text (good for dense diagrams)
            text = pytesseract.image_to_string(img, config="--psm 6")
            return text.strip()
        except ImportError:
            return ""   # pytesseract not installed — OCR silently disabled
        except Exception as e:
            self.logger.warning("OCR text extraction failed", error=str(e))
            return ""

    def _convert_image_to_png(self, image_bytes: bytes) -> tuple[bytes | None, str, bool]:
        """
        Convert/normalize raster images to PNG using Pillow.

        IMPORTANT: EMF/WMF conversion is intentionally NOT supported.
        If an EMF/WMF image is encountered, it must be skipped before calling this method.

        Args:
            image_bytes: Raw image bytes

        Returns:
            Tuple of (converted_bytes, format_name, skip_vision_ie) where:
            - converted_bytes is None if conversion failed
            - format_name is detected format or failure reason
            - skip_vision_ie is True if image should skip vision IE (decorative/tiny)
        """
        try:
            from PIL import Image
            import io

            # For regular image formats, use PIL as before
            img = Image.open(io.BytesIO(image_bytes))
            detected_format = img.format or "UNKNOWN"
            original_size = img.size

            # Convert to PNG if not already
            if detected_format.upper() not in ("PNG", "JPEG", "JPG"):
                png_buffer = io.BytesIO()
                # Convert RGBA to RGB if needed for formats that don't support transparency
                if img.mode == "RGBA":
                    rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                    rgb_img.paste(img, mask=img.split()[3])
                    img = rgb_img
                img.save(png_buffer, format="PNG")
                image_bytes = png_buffer.getvalue()
                img = Image.open(io.BytesIO(image_bytes))  # Reload for optimization

            # Check if image should skip vision IE (decorative/tiny)
            aspect_ratio = max(img.width, img.height) / min(img.width, img.height)
            skip_vision_ie = False
            if aspect_ratio > 20 or img.width < 50 or img.height < 50:
                skip_vision_ie = True
                print(f"[Parser] Image marked to skip vision IE (decorative): {original_size}, aspect={aspect_ratio:.1f}", flush=True)
            
            # OPTIMIZE IMAGE QUALITY FOR VISION MODEL (upscale, contrast, sharpening)
            optimized_bytes = self._optimize_image_for_vision(image_bytes)
            if optimized_bytes:
                final_size = Image.open(io.BytesIO(optimized_bytes)).size
                print(f"[Parser] Image optimized: {original_size} → {final_size}", flush=True)
                
                # Save enhanced image to logs/review_and_delete/enhanced_images/ for review
                try:
                    enhanced_dir = Path("logs/review_and_delete/enhanced_images")
                    enhanced_dir.mkdir(parents=True, exist_ok=True)
                    # Generate filename from hash or timestamp
                    import hashlib
                    img_hash = hashlib.md5(optimized_bytes).hexdigest()[:8]
                    enhanced_path = enhanced_dir / f"enhanced_{img_hash}_{detected_format.lower()}.png"
                    enhanced_path.write_bytes(optimized_bytes)
                    self.logger.info("Saved enhanced image for review", path=str(enhanced_path), size=final_size)
                except Exception as save_err:
                    self.logger.warning("Failed to save enhanced image", error=str(save_err))
                
                # ALSO save to artifacts directory if specified
                if self.artifacts_dir and self.images_dir:
                    try:
                        import hashlib
                        img_hash = hashlib.md5(optimized_bytes).hexdigest()[:8]
                        artifact_img_path = self.images_dir / f"uplifted_{img_hash}.png"
                        artifact_img_path.write_bytes(optimized_bytes)
                        self.logger.info(f"Saved uplifted image artifact: {artifact_img_path}", size=final_size)
                    except Exception as artifact_err:
                        self.logger.warning(f"Failed to save image artifact: {artifact_err}")
                
                return optimized_bytes, "PNG", skip_vision_ie
            
            return image_bytes, detected_format, skip_vision_ie

        except Exception as e:
            print(f"[Parser] Image conversion failed: {e}", flush=True)
            self.logger.error("Image conversion exception - returning None to skip vision processing", error=str(e))
            return None, "CONVERSION_FAILED", False

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

    def _chunk_text(self, text: str, block_index_start: int) -> list[tuple[str, int]]:
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
        chunks: list[tuple[str, int]] = []
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

    def _split_text_by_sentences(self, text: str) -> list[str]:
        """
        Split text into sentences by punctuation.
        
        Args:
            text: Text to split
            
        Returns:
            List of sentences
        """
        sentences = []
        current = ""
        
        for part in re.split(r'([.!?]\s+)', text):
            if part.strip():
                if re.match(r'^[.!?]\s+$', part):
                    # This is a separator, add to current and start new
                    if current:
                        sentences.append(current + part[0])  # Include punctuation
                        current = part[1:].lstrip()  # Start new sentence
                    else:
                        current = part
                else:
                    current += part
        
        if current:
            sentences.append(current)
        
        return sentences if sentences else [text]

    def parse_folder(self, input_dir: Path, domain: str = "") -> list[CanonicalBlock]:
        """
        Parse all supported files in a directory.

        Args:
            input_dir: Directory containing files to parse
            domain: Domain name for processing

        Returns:
            List of all CanonicalBlock instances from all files
        """
        all_blocks = []

        # Supported file extensions (matches parse_file capabilities)
        supported_extensions = {
            ".docx", ".pdf", ".xlsx", ".txt", ".csv", ".json", ".pptx",
            ".png", ".jpg", ".jpeg",  # standalone raster images
        }

        # Find all files with supported extensions
        for file_path in input_dir.glob("*.*"):
            if file_path.suffix.lower() in supported_extensions:
                try:
                    self.logger.info(f"Parsing file: {file_path.name}", domain=domain)
                    blocks = self.parse_file(file_path, input_root=input_dir, domain=domain)
                    all_blocks.extend(blocks)
                    self.logger.info(f"Parsed {len(blocks)} blocks from {file_path.name}", domain=domain)
                except Exception as e:
                    self.logger.error(f"Failed to parse {file_path}: {e}", domain=domain)

        self.logger.info(f"Parsed {len(all_blocks)} total blocks from directory", domain=domain, directory=str(input_dir))
        return all_blocks

    def _parse_content(
        self,
        path: Path,
        input_root: Optional[Path],
        domain: str,
        embedded_depth: int,
        parent_document: str,
        embedded_filename: str,
    ) -> list[CanonicalBlock]:
        """Parse main content of the file (dispatches by suffix). Required by BaseParser."""
        import time

        suffix = path.suffix.lower()
        parse_start = time.time()

        if suffix == ".docx":
            blocks = self._parse_docx(path, input_root, domain, embedded_depth, parent_document)
        elif suffix == ".pdf":
            blocks = self._parse_pdf(path, input_root, domain)
        elif suffix == ".xlsx":
            blocks = self._parse_xlsx(path, input_root, domain, embedded_depth, parent_document)
        elif suffix == ".txt":
            blocks = self._parse_txt(path, input_root, domain)
        elif suffix == ".csv":
            blocks = self._parse_csv(path, input_root, domain)
        elif suffix == ".json":
            blocks = self._parse_json(path, input_root, domain)
        elif suffix == ".pptx":
            blocks = self._parse_pptx(path, input_root, domain, embedded_depth, parent_document)
        elif suffix in (".png", ".jpg", ".jpeg"):
            blocks = self._parse_png(path, input_root, domain)
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

        parse_duration = time.time() - parse_start
        record_document_parse(
            domain=domain,
            file_type=suffix.lstrip("."),
            duration_seconds=parse_duration,
            block_count=len(blocks),
        )
        self.logger.info(
            "Document parsed with telemetry",
            file=str(path),
            domain=domain,
            file_type=suffix,
            duration_seconds=round(parse_duration, 3),
            block_count=len(blocks),
        )
        return blocks

    def _extract_embedded_docs(
        self, path: Path, embedded_depth: int, parent_document: str
    ) -> dict[int, dict[str, Any]]:
        """Extract embedded documents at parse_file level. None at this level; DOCX handles its own."""
        return {}

    def parse_file(self, path: Path, input_root: Path | None = None, domain: str = "", embedded_depth: int = 0, parent_document: str = "", embedded_filename: str = "") -> list[CanonicalBlock]:
        """
        Parse a single file.
        
        When input_root is provided, validates that the file path is within
        the allowed root directory to prevent path traversal attacks.
        
        Args:
            path: Path to file
            input_root: Root directory (optional, for relative path calculation and security validation)
            domain: Domain name (required, e.g., "trade_finance", "wealth_portfolio", "consumer_payments")
            embedded_depth: Current embedding depth (for circular reference detection)
            parent_document: Parent document path (for embedded docs)
            embedded_filename: Embedded filename (for embedded docs)
            
        Returns:
            List of CanonicalBlock instances
            
        Raises:
            ValueError: If domain parameter is not provided
            PathSecurityError: If path is outside allowed input_root directory
        """
        if not domain:
            raise ValueError("domain parameter is required")

        if embedded_depth > 3:
            self.logger.warning(
                "Embedded document depth limit exceeded",
                file=str(path),
                depth=embedded_depth,
                parent=parent_document
            )
            return []

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

        suffix = path.suffix.lower().lstrip(".")
        parse_file_obs_start = log_stage_start(
            self.logger, "ingestion.parse.file",
            domain=domain, source_file=str(path), file_type=suffix,
            embedded_depth=embedded_depth,
        )

        try:
            blocks = self._parse_content(path, input_root, domain, embedded_depth, parent_document, embedded_filename)
            embedded_docs = self._extract_embedded_docs(path, embedded_depth, parent_document)
            if embedded_docs:
                blocks = list(blocks)
                blocks.extend(self._process_embedded_docs(embedded_docs, domain, embedded_depth, parent_document, str(path)))

            # Derive safe aggregate counts for observability (no document contents).
            image_count = 0
            table_count = 0
            page_numbers: set = set()
            for blk in blocks or []:
                if getattr(blk, "image_bytes", None):
                    image_count += 1
                if "table" in str(getattr(blk, "block_type", "")).lower():
                    table_count += 1
                page_no = getattr(blk, "page_number", None)
                if page_no is not None:
                    page_numbers.add(page_no)

            log_stage_end(
                self.logger, "ingestion.parse.file", parse_file_obs_start,
                domain=domain, source_file=str(path), file_type=suffix,
                block_count=len(blocks or []),
                image_count=image_count,
                table_count=table_count,
                page_count=(len(page_numbers) or None),
            )
            return blocks
        except Exception as e:
            log_stage_error(
                self.logger, "ingestion.parse.file", e,
                domain=domain, source_file=str(path), file_type=suffix,
                started_at=parse_file_obs_start,
            )
            self._handle_errors(e, {
                "file": str(path),
                "domain": domain,
                "embedded_depth": embedded_depth
            })
            raise

    def _extract_docx_images_in_order_via_zip(self, path: Path) -> list[dict[str, Any]]:
        """
        Robust DOCX image extraction that does NOT depend on python-docx's paragraph/run model.

        Why this exists:
        - Some DOCX images (notably EMF/WMF drawings, header/footer shapes, or certain DrawingML variants)
          can be present in `word/media/*` and referenced from XML but not discoverable via python-docx runs.

        Approach:
        - Treat DOCX as a zip
        - Parse `word/document.xml` and (if present) `word/header*.xml` / `word/footer*.xml`
        - Collect embedded relationship IDs in XML traversal order
        - Resolve `rId -> Target` via the corresponding `*.rels`
        - Read bytes for resolved targets
        - Append any `word/media/*` images not referenced in XML at the end (diagnostic safety net)
        """
        import posixpath
        import xml.etree.ElementTree as ET

        def _safe_read(zf: zipfile.ZipFile, name: str) -> bytes | None:
            try:
                return zf.read(name)
            except KeyError:
                return None

        def _rels_path_for(xml_name: str) -> str:
            base_dir = posixpath.dirname(xml_name)
            base_file = posixpath.basename(xml_name)
            return posixpath.join(base_dir, "_rels", f"{base_file}.rels")

        def _parse_rels(rels_xml: bytes | None) -> dict[str, dict[str, str]]:
            if not rels_xml:
                return {}
            try:
                root = ET.fromstring(rels_xml)
            except Exception:
                return {}
            out: dict[str, dict[str, str]] = {}
            for rel in root.iter():
                if rel.tag.endswith("Relationship"):
                    rid = rel.attrib.get("Id")
                    if not rid:
                        continue
                    out[rid] = {
                        "target": rel.attrib.get("Target", ""),
                        "type": rel.attrib.get("Type", ""),
                    }
            return out

        def _iter_embed_rids_with_captions(xml_bytes: bytes | None) -> list[dict[str, Any]]:
            """
            Return embedded relationship IDs in XML encounter order with a best-effort caption.

            Captions in DOCX are often plain paragraphs immediately preceding an image, so we track the most
            recent non-empty paragraph text and attach it as `caption_text_raw`.
            """
            if not xml_bytes:
                return []
            try:
                root = ET.fromstring(xml_bytes)
            except Exception:
                return []

            # Iterate paragraph nodes in document order
            rid_entries: list[dict[str, Any]] = []
            last_nonempty_para_text: str = ""
            prev_nonempty_para_text: str = ""

            for para in root.iter():
                if not para.tag.endswith("}p"):
                    continue

                # Collect text in this paragraph
                texts: list[str] = []
                for t in para.iter():
                    if t.tag.endswith("}t") and isinstance(t.text, str) and t.text.strip():
                        texts.append(t.text.strip())
                para_text = " ".join(texts).strip()

                # Find all embedded image rel IDs in this paragraph
                rids: list[str] = []
                for el in para.iter():
                    for k, v in el.attrib.items():
                        # DrawingML: r:embed, VML: r:id
                        if k.endswith("}embed") or k.endswith("}id"):
                            if isinstance(v, str) and v.startswith("rId"):
                                rids.append(v)

                if rids:
                    # Best-effort caption: prefer paragraph text if it looks like a caption; else use previous
                    caption = para_text or last_nonempty_para_text or prev_nonempty_para_text or ""
                    for rid in rids:
                        rid_entries.append({"rId": rid, "caption_text_raw": caption})

                # Update caption history
                if para_text:
                    prev_nonempty_para_text = last_nonempty_para_text
                    last_nonempty_para_text = para_text

            return rid_entries

        def _resolve_target(xml_name: str, target: str) -> str:
            base_dir = posixpath.dirname(xml_name)
            norm = posixpath.normpath(posixpath.join(base_dir, target))
            if norm.startswith("./"):
                norm = norm[2:]
            return norm

        def _has_emf_wmf_sig(b: bytes) -> bool:
            return len(b) >= 4 and (
                b[:4] == b"\x01\x00\x00\x00" or b[:4] in (b"\x01\x09\x00\x00", b"\xD7\xCD\xC6\x9A")
            )

        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()

                part_xmls: list[str] = []
                if "word/document.xml" in names:
                    part_xmls.append("word/document.xml")

                def _sorted_parts(prefix: str) -> list[str]:
                    parts = [n for n in names if n.startswith(f"word/{prefix}") and n.endswith(".xml")]
                    def _key(p: str) -> tuple[int, str]:
                        m = re.search(rf"{re.escape(prefix)}(\d+)\.xml$", p)
                        return (int(m.group(1)) if m else 9999, p)
                    return sorted(parts, key=_key)

                part_xmls.extend(_sorted_parts("header"))
                part_xmls.extend(_sorted_parts("footer"))

                results: list[dict[str, Any]] = []
                seen_resolved: set[str] = set()
                xml_order = 0

                for xml_name in part_xmls:
                    rels_name = _rels_path_for(xml_name)
                    xml_bytes = _safe_read(zf, xml_name)
                    rels_bytes = _safe_read(zf, rels_name)
                    rels_map = _parse_rels(rels_bytes)
                    embed_entries = _iter_embed_rids_with_captions(xml_bytes)

                    for entry in embed_entries:
                        rid = entry.get("rId")
                        if not rid:
                            continue
                        rel_info = rels_map.get(rid) or {}
                        target = rel_info.get("target", "")
                        rel_type = rel_info.get("type", "")
                        if not target:
                            continue
                        # Only keep image relationships (or obvious media targets)
                        if ("image" not in rel_type.lower()) and ("/media/" not in target.lower()) and (not target.lower().startswith("media/")):
                            continue

                        resolved = _resolve_target(xml_name, target)
                        img_bytes = _safe_read(zf, resolved)
                        if not img_bytes:
                            continue

                        if resolved in seen_resolved:
                            continue
                        seen_resolved.add(resolved)

                        results.append({
                            "source": "docx_zip_xml",
                            "xml_name": xml_name,
                            "rels_name": rels_name,
                            "part_name": posixpath.basename(xml_name),
                            "rId": rid,
                            "target": target,
                            "resolved_target": resolved,
                            "image_bytes_raw": img_bytes,
                            "xml_order": xml_order,
                            "likely_emf_wmf": _has_emf_wmf_sig(img_bytes),
                            "caption_text_raw": (entry.get("caption_text_raw") or "").strip()[:500] or None,
                        })
                        xml_order += 1

                # Append any media images not referenced in XML (diagnostic net)
                media_files = [n for n in names if n.startswith("word/media/")]
                for media_name in sorted(media_files):
                    if media_name in seen_resolved:
                        continue
                    img_bytes = _safe_read(zf, media_name)
                    if not img_bytes:
                        continue
                    results.append({
                        "source": "docx_zip_media_orphan",
                        "xml_name": None,
                        "rels_name": None,
                        "part_name": "word/media",
                        "rId": None,
                        "target": media_name,
                        "resolved_target": media_name,
                        "image_bytes_raw": img_bytes,
                        "xml_order": xml_order,
                        "likely_emf_wmf": _has_emf_wmf_sig(img_bytes),
                    })
                    xml_order += 1

                return results
        except Exception as e:
            self.logger.warning("DOCX zip image extraction failed", file=str(path), error=str(e))
            return []

    def _parse_docx(self, path: Path, input_root: Path | None = None, domain: str = "", 
                    embedded_depth: int = 0, parent_document: str = "") -> list[CanonicalBlock]:
        """Parse DOCX file with embedded document support.

        Native DOCX parsing is primary (text, tables, standard images).
        DOCX→PDF conversion is supplementary — only used to capture vector
        shapes/diagrams and problem image formats (WMF/EMF) that python-docx
        cannot extract.  Supplementary blocks are image-only IE helpers, not
        first-class provenance citizens.
        """
        blocks: list[CanonicalBlock] = []
        block_index = 0
        table_rows_emitted = False
        table_rows_fallback: list[dict[str, Any]] = []
        
        # Parse text using unstructured
        elements = partition_docx(str(path))
        
        # Extract images and embedded documents using python-docx
        image_map: dict[int, dict[str, Any]] = {}
        embedded_docs: dict[int, dict] = {}
        
        try:
            docx_doc = DocxDocument(str(path))
            
            # Build ordered list of images by iterating through paragraphs and runs
            # This preserves document order unlike iterating rel.values()
            image_order_list: list[dict[str, Any]] = []  # list of image descriptors
            
            for para_idx, paragraph in enumerate(docx_doc.paragraphs):
                for run_idx, run in enumerate(paragraph.runs):
                    # Check for inline images in this run
                    if hasattr(run, '_element'):
                        for drawing in run._element.findall('.//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing', run._element.nsmap if hasattr(run._element, 'nsmap') else {}):
                            for blip in drawing.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip', drawing.nsmap if hasattr(drawing, 'nsmap') else {}):
                                embed_rId = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                                if embed_rId:
                                    try:
                                        image_part = docx_doc.part.related_parts[embed_rId]
                                        image_bytes_raw = image_part.blob
                                        image_order_list.append({
                                            "source": "python_docx_runs",
                                            "rId": embed_rId,
                                            "image_bytes_raw": image_bytes_raw,
                                            "para_idx": para_idx,
                                            "run_idx": run_idx,
                                        })
                                    except:
                                        pass
            
            # Also check for images in tables
            for table_idx, table in enumerate(docx_doc.tables):
                for row_idx, row in enumerate(table.rows):
                    for cell_idx, cell in enumerate(row.cells):
                        for para_idx, paragraph in enumerate(cell.paragraphs):
                            for run_idx, run in enumerate(paragraph.runs):
                                if hasattr(run, '_element'):
                                    for drawing in run._element.findall('.//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing', run._element.nsmap if hasattr(run._element, 'nsmap') else {}):
                                        for blip in drawing.findall('.//{http://schemas.openxmlformats.org/drawingml/2006/main}blip', drawing.nsmap if hasattr(drawing, 'nsmap') else {}):
                                            embed_rId = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed')
                                            if embed_rId:
                                                try:
                                                    image_part = docx_doc.part.related_parts[embed_rId]
                                                    image_bytes_raw = image_part.blob
                                                    image_order_list.append({
                                                        "source": "python_docx_tables",
                                                        "rId": embed_rId,
                                                        "image_bytes_raw": image_bytes_raw,
                                                        "para_idx": para_idx,
                                                        "run_idx": run_idx,
                                                    })
                                                except:
                                                    pass
            
            # Fallback: collect DOCX table rows directly (if unstructured misses tables)
            for table_idx, table in enumerate(docx_doc.tables):
                raw_rows: list[list[str | None]] = []
                for row in table.rows:
                    cells = []
                    for cell in row.cells:
                        ct = (cell.text or "").strip()
                        cells.append(ct if ct else None)
                    if any(c for c in cells):
                        raw_rows.append(cells)
                if not raw_rows:
                    continue
                header_detected, col_names = _detect_table_header(raw_rows)
                header_candidate = ", ".join(col_names) if header_detected else _build_table_row_text(col_names, raw_rows[0])
                start = 1 if header_detected else 0
                for row_idx in range(start, len(raw_rows)):
                    row_text = _build_table_row_text(col_names, raw_rows[row_idx])
                    if len(row_text.strip()) < 3:
                        continue
                    table_rows_fallback.append({
                        "table_idx": table_idx,
                        "row_idx": row_idx,
                        "row_text": row_text,
                        "header_candidate": header_candidate,
                        "header_is_self": (row_idx == 0 and not header_detected),
                    })
            
            # Fallback: if no images found via structure walk, use old method
            if not image_order_list:
                for rel in docx_doc.part.rels.values():
                    if "image" in rel.target_ref:
                        try:
                            image_part = rel.target_part
                            image_bytes_raw = image_part.blob
                            image_order_list.append({
                                "source": "python_docx_rels_fallback",
                                "rId": rel.rId,
                                "image_bytes_raw": image_bytes_raw,
                                "para_idx": -1,
                                "run_idx": -1,
                            })
                        except:
                            pass

            # ZIP/XML fallback: catch images python-docx does not expose (common for EMF/headers/alt content)
            zip_images = self._extract_docx_images_in_order_via_zip(path)
            if zip_images:
                py_count = len(image_order_list)
                zip_count = len(zip_images)

                def _top_sizes(descs: list[dict[str, Any]]) -> list[int]:
                    return sorted([len(d.get("image_bytes_raw") or b"") for d in descs], reverse=True)[:5]

                def _has_emf_wmf(descs: list[dict[str, Any]]) -> bool:
                    for d in descs:
                        if d.get("likely_emf_wmf"):
                            return True
                        b = d.get("image_bytes_raw") or b""
                        if len(b) >= 4 and (b[:4] == b"\x01\x00\x00\x00" or b[:4] in (b"\x01\x09\x00\x00", b"\xD7\xCD\xC6\x9A")):
                            return True
                    return False

                prefer_zip = False
                if zip_count > py_count:
                    prefer_zip = True
                elif _has_emf_wmf(zip_images) and not _has_emf_wmf(image_order_list):
                    prefer_zip = True

                self.logger.info(
                    "DOCX image extraction comparison (python-docx vs zip/xml)",
                    file=str(path),
                    python_docx_images=py_count,
                    zip_xml_images=zip_count,
                    python_docx_top_sizes=_top_sizes(image_order_list),
                    zip_xml_top_sizes=_top_sizes(zip_images),
                    prefer_zip=prefer_zip,
                )

                if prefer_zip:
                    image_order_list = [
                        {
                            "source": d.get("source", "docx_zip"),
                            "rId": d.get("rId") or "unknown",
                            "image_bytes_raw": d.get("image_bytes_raw") or b"",
                            "para_idx": -1,
                            "run_idx": -1,
                            "part_name": d.get("part_name"),
                            "resolved_target": d.get("resolved_target"),
                            "xml_name": d.get("xml_name"),
                            "xml_order": d.get("xml_order"),
                            "caption_text_raw": d.get("caption_text_raw"),
                        }
                        for d in zip_images
                        if d.get("image_bytes_raw") is not None
                    ]
            
            # Build page number map from unstructured elements (for images)
            # Match images to unstructured elements by paragraph position
            page_number_map: dict[int, int | None] = {}  # para_idx -> page_number
            for idx, element in enumerate(elements):
                if hasattr(element, "metadata") and element.metadata:
                    if hasattr(element.metadata, "page_number") and element.metadata.page_number:
                        # Try to match element to paragraph index (rough approximation)
                        # unstructured elements are roughly in document order
                        page_number_map[idx] = element.metadata.page_number
            
            # Process images in document order (PRESERVE ORDER)
            total_images_found = len(image_order_list)
            print(f"[Parser] Found {total_images_found} images in document - processing in order...", flush=True)
            self.logger.info(
                "Starting image extraction from DOCX",
                file=str(path),
                total_images_found=total_images_found
            )
            
            images_processed = 0
            images_filtered = 0
            images_conversion_failed = 0
            
            for img_idx, img_desc in enumerate(image_order_list):
                try:
                    rId = img_desc.get("rId", "unknown")
                    image_bytes_raw = img_desc.get("image_bytes_raw", b"")
                    para_idx = img_desc.get("para_idx", -1)
                    run_idx = img_desc.get("run_idx", -1)
                    part_name = img_desc.get("part_name")
                    resolved_target = img_desc.get("resolved_target")

                    original_size = len(image_bytes_raw)
                    image_position = f"para_{para_idx}_run_{run_idx}" if para_idx >= 0 else "unknown"
                    
                    print(f"[Parser] Processing image {img_idx + 1}/{total_images_found} at position {image_position} ({original_size} bytes)...", flush=True)
                    
                    # Detect image format
                    format_detected = "UNKNOWN"
                    is_emf_wmf = False
                    if len(image_bytes_raw) >= 4:
                        if image_bytes_raw[:4] == b'\x01\x00\x00\x00':
                            format_detected = "EMF"
                            is_emf_wmf = True
                        elif image_bytes_raw[:4] in (b'\x01\x09\x00\x00', b'\xD7\xCD\xC6\x9A'):
                            format_detected = "WMF"
                            is_emf_wmf = True
                    
                    # Skip unsupported EMF/WMF images (audit only)
                    is_emf_wmf = False
                    if resolved_target and isinstance(resolved_target, str):
                        lower = resolved_target.lower()
                        if lower.endswith(".emf") or lower.endswith(".wmf"):
                            is_emf_wmf = True
                    if len(image_bytes_raw) >= 4 and image_bytes_raw[:4] in (b"\x01\x00\x00\x00", b"\x01\x09\x00\x00", b"\xD7\xCD\xC6\x9A"):
                        is_emf_wmf = True

                    if is_emf_wmf:
                        images_filtered += 1
                        original_sha256 = hashlib.sha256(image_bytes_raw).hexdigest()
                        image_map[img_idx] = {
                            "skipped_image": True,
                            "skip_reason": "unsupported_emf_wmf",
                            "rId": rId,
                            "document_order": img_idx,
                            "part_name": part_name,
                            "resolved_target": resolved_target,
                            "caption_text_raw": (img_desc.get("caption_text_raw") or "").strip()[:500] or None,
                            "original_size_bytes": original_size,
                            "original_sha256": original_sha256,
                        }
                        self.logger.warning(
                            "Skipped unsupported EMF/WMF image (audit only)",
                            file=str(path),
                            resolved_target=resolved_target,
                            rId=rId,
                            document_order=img_idx,
                            original_size=original_size,
                            original_sha256=original_sha256,
                        )
                        print(f"[Parser] Skipping unsupported image (EMF/WMF): {resolved_target} ({original_size} bytes) order={img_idx}", flush=True)
                        continue

                    # Normalize raster formats to PNG (Pillow)
                    image_bytes, format_name, skip_vision_ie = self._convert_image_to_png(image_bytes_raw)
                    
                    # Skip if conversion failed (returns None)
                    if image_bytes is None:
                        images_conversion_failed += 1
                        self.logger.warning(
                            "Image conversion failed - image skipped",
                            source_file=str(path),
                            format=format_name,
                            original_size=original_size,
                            image_index=img_idx + 1,
                            document_order=img_idx,
                            para_idx=para_idx,
                            run_idx=run_idx
                        )
                        print(f"[Parser] Image conversion failed for {format_name} ({original_size} bytes) - image skipped (order: {img_idx})", flush=True)
                        continue
                    
                    # Content-based icon filtering (DISABLED)
                    # We intentionally ingest ALL images to avoid accidentally dropping real content.
                    # Keep the heuristic check for observability only (no skipping).
                    try:
                        should_process, reason = should_process_image(image_bytes)
                    except Exception as filter_err:
                        should_process, reason = True, f"icon_filter_error:{filter_err}"
                    if not should_process:
                        self.logger.info(
                            "Icon heuristic would filter image, but filtering is disabled",
                            file=str(path),
                            original_format=format_detected,
                            original_size=original_size,
                            converted_size=len(image_bytes),
                            reason=reason,
                            image_index=img_idx + 1,
                            total_images_found=total_images_found,
                            document_order=img_idx,
                            para_idx=para_idx,
                            run_idx=run_idx,
                            rId=rId,
                        )
                        print(
                            f"[Parser] ⚠️ Icon heuristic would filter (but disabled): {reason} "
                            f"({format_detected} {original_size} bytes -> {len(image_bytes)} bytes, order {img_idx})",
                            flush=True,
                        )
                    
                    # Store in document order with metadata (para_idx for page number matching)
                    images_processed += 1
                    image_map[img_idx] = {
                        "bytes": image_bytes,
                        "para_idx": para_idx,
                        "format": format_name,
                        "original_format": format_detected,
                        "rId": rId,
                        "document_order": img_idx,
                        "part_name": part_name,
                        "resolved_target": resolved_target,
                        "caption_text_raw": (img_desc.get("caption_text_raw") or "").strip()[:500] or None,
                        "original_size_bytes": original_size,
                        "converted_size_bytes": len(image_bytes),
                        "original_sha256": hashlib.sha256(image_bytes_raw).hexdigest(),
                        "converted_sha256": hashlib.sha256(image_bytes).hexdigest(),
                    }
                    self.logger.info(
                        "Extracted and converted image from DOCX",
                        file=str(path),
                        format=format_name,
                        original_format=format_detected,
                        original_size=original_size,
                        final_size=len(image_bytes),
                        document_order=img_idx,
                        image_index=img_idx + 1,
                        total_images_found=total_images_found,
                        images_processed=images_processed,
                        para_idx=para_idx,
                        run_idx=run_idx,
                        rId=rId
                    )
                    print(f"[Parser] Image {img_idx+1}/{total_images_found} (order {img_idx}): {format_detected}→{format_name} ({original_size}→{len(image_bytes)} bytes) at para_{para_idx}_run_{run_idx}", flush=True)
                    
                except Exception as e:
                    self.logger.warning("Failed to extract image from DOCX", error=str(e), file=str(path), image_index=img_idx)
            
            # Extract embedded OLE objects (documents, PDFs, Excel, etc.)
            embedded_count = 0
            for rel in docx_doc.part.rels.values():
                if hasattr(rel, 'target_ref') and rel.target_ref:
                    if "oleObject" in rel.target_ref or "embedded" in rel.target_ref:
                        try:
                            embedded_part = rel.target_part
                            if embedded_part and hasattr(embedded_part, 'blob'):
                                embedded_bytes = embedded_part.blob
                                
                                content_type = getattr(embedded_part, 'content_type', 'unknown')
                                filename = getattr(rel, 'target_ref', f'embedded_{embedded_count}')
                                
                                embedded_docs[embedded_count] = {
                                    'filename': filename,
                                    'content_type': content_type,
                                    'bytes': embedded_bytes,
                                    'size': len(embedded_bytes)
                                }
                                embedded_count += 1
                                
                                self.logger.info(
                                    "Extracted embedded document from DOCX",
                                    file=str(path),
                                    filename=filename,
                                    content_type=content_type,
                                    size=len(embedded_bytes)
                                )
                        except Exception as e:
                            self.logger.warning("Failed to extract embedded document", error=str(e), file=str(path))
            
            # Log final image extraction summary
            if image_map:
                # Build extraction summary
                summary_parts = []
                for idx in sorted(image_map.keys()):
                    img_data = image_map[idx]
                    if isinstance(img_data, dict):
                        img_size = len(img_data.get("bytes", b""))
                    else:
                        img_size = len(img_data)
                    size_str = f"{img_size/1024:.1f}KB" if img_size >= 1024 else f"{img_size}B"
                    summary_parts.append(f"order_{idx}:{size_str}")
                print(f"[Parser] Extracted {len(image_map)}/{total_images_found} images in document order: [{', '.join(summary_parts)}]", flush=True)
                self.logger.info(
                    "DOCX image extraction complete",
                    file=str(path),
                    total_images_found=total_images_found,
                    images_processed=images_processed,
                    images_filtered=images_filtered,
                    images_conversion_failed=images_conversion_failed,
                    images_stored=len(image_map),
                    image_sizes=summary_parts
                )
            else:
                self.logger.warning(
                    "No images extracted from DOCX",
                    file=str(path),
                    total_images_found=total_images_found,
                    images_filtered=images_filtered,
                    images_conversion_failed=images_conversion_failed
                )
            
            if embedded_docs:
                self.logger.info("DOCX embedded document extraction complete", file=str(path), total_embedded=len(embedded_docs))
        finally:
            del docx_doc
        
        # Process elements
        for element in elements:
            element_type = type(element).__name__
            text = str(element).strip()
            
            if not text and element_type != "Image":
                continue
            
            metadata_dict = {}
            
            # Determine block_type
            if element_type in ("Title", "NarrativeText") and hasattr(element, "metadata"):
                metadata = element.metadata
                if metadata and getattr(metadata, "category", None) == "Title":
                    block_type = "heading"
                else:
                    block_type = "paragraph"
            elif element_type == "ListItem":
                block_type = "paragraph"
                metadata_dict = {"list": True}
            elif element_type == "Table":
                raw_rows: list[list[str]] = []
                for row_el in getattr(element, "elements", []):
                    cells = []
                    for cell in getattr(row_el, "elements", []):
                        cell_text = str(cell).strip()
                        cells.append(cell_text if cell_text else None)
                    if any(c for c in cells):
                        raw_rows.append(cells)
                if raw_rows:
                    table_rows_emitted = True
                    header_detected, col_names = _detect_table_header(raw_rows)
                    header_candidate = ", ".join(col_names) if header_detected else _build_table_row_text(col_names, raw_rows[0])
                    page_num = getattr(element.metadata, "page_number", None) if hasattr(element, "metadata") else None
                    source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
                    start = 1 if header_detected else 0
                    for row_idx in range(start, len(raw_rows)):
                        row_text = _build_table_row_text(col_names, raw_rows[row_idx])
                        if len(row_text.strip()) < 3:
                            continue
                        blocks.append(CanonicalBlock(
                            block_id=generate_block_id(source_file_rel, block_index, domain, content=row_text),
                            source_file=source_file_rel,
                            file_type="docx",
                            domain=domain,
                            page_number=page_num,
                            block_index=block_index,
                            block_type="table_row",
                            text=row_text,
                            image_bytes=None,
                            metadata={
                                "table_row_index": row_idx,
                                "table_header_candidate": header_candidate,
                                "table_header_is_self": (row_idx == 0 and not header_detected),
                            }
                        ))
                        block_index += 1
                else:
                    self.logger.warning(
                        "Unstructured Table element contained no rows; falling back to python-docx table parsing",
                        file=str(path),
                    )
                continue
            elif element_type == "Image":
                # IMPORTANT: Do NOT rely on unstructured's Image elements for DOCX image selection/pairing.
                # We create DOCX image blocks directly from ZIP/XML ordered extraction later (deterministic).
                continue
            else:
                block_type = "paragraph"
            
            # Chunk text if needed
            chunks = self._chunk_text(text, block_index)
            for chunk_text, chunk_idx in chunks:
                block_metadata = metadata_dict.copy()
                if hasattr(element, "metadata") and element.metadata:
                    if hasattr(element.metadata, "page_number"):
                        block_metadata["page_number"] = element.metadata.page_number
                
                source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, chunk_idx, domain, content=chunk_text),
                    source_file=source_file_rel,
                    file_type="docx",
                    domain=domain,
                    page_number=block_metadata.get("page_number"),
                    block_index=chunk_idx,
                    block_type=block_type,
                    text=chunk_text,
                    image_bytes=None,
                    metadata=block_metadata
                ))
                block_index = chunk_idx + 1
        
        # Fallback: emit DOCX table rows from python-docx if unstructured missed them
        if not table_rows_emitted and table_rows_fallback:
            source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
            for row in table_rows_fallback:
                row_text = row.get("row_text", "")
                if not row_text:
                    continue
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, block_index, domain, content=row_text),
                    source_file=source_file_rel,
                    file_type="docx",
                    domain=domain,
                    page_number=None,
                    block_index=block_index,
                    block_type="table_row",
                    text=row_text,
                    image_bytes=None,
                    metadata={
                        "table_row_index": row.get("row_idx"),
                        "table_header_candidate": row.get("header_candidate"),
                        "table_header_is_self": row.get("header_is_self", False),
                        "table_source": "python_docx_tables"
                    }
                ))
                block_index += 1

        # Add DOCX images deterministically from ZIP/XML extraction order (not unstructured matching)
        if image_map:
            for img_key in sorted(image_map.keys()):
                image_metadata = image_map[img_key]

                # Emit skipped-image audit blocks (no vision/Weaviate/Neo4j write later)
                if image_metadata.get("skipped_image"):
                    source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
                    resolved_target = image_metadata.get("resolved_target") or "unknown_target"
                    original_sha256 = image_metadata.get("original_sha256") or "unknown_hash"
                    stable_for_id = f"skipped_image|{resolved_target}|{original_sha256}"
                    blocks.append(CanonicalBlock(
                        block_id=generate_block_id(source_file_rel, block_index, domain, content=stable_for_id),
                        source_file=source_file_rel,
                        file_type="docx",
                        domain=domain,
                        page_number=None,
                        block_index=block_index,
                        block_type="image_skipped",
                        text="",
                        image_bytes=None,
                        metadata={
                            "skipped_image": True,
                            "skip_reason": image_metadata.get("skip_reason") or "unknown",
                            "document_order": image_metadata.get("document_order"),
                            "rId": image_metadata.get("rId"),
                            "resolved_target": resolved_target,
                            "part_name": image_metadata.get("part_name"),
                            "caption_text_raw": image_metadata.get("caption_text_raw"),
                            "original_size_bytes": image_metadata.get("original_size_bytes"),
                            "original_sha256": original_sha256,
                        }
                    ))
                    block_index += 1
                    continue

                image_bytes = image_metadata.get("bytes")
                if not image_bytes:
                    continue

                source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
                # Deterministic image block id: stable across doc edits that shift block_index.
                resolved_target = image_metadata.get("resolved_target") or "unknown_target"
                original_sha256 = image_metadata.get("original_sha256") or "unknown_hash"
                stable_for_id = f"img|{resolved_target}|{original_sha256}"
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, block_index, domain, content=stable_for_id),
                    source_file=source_file_rel,
                    file_type="docx",
                    domain=domain,
                    page_number=None,
                    block_index=block_index,
                    block_type="image",
                    text="",
                    image_bytes=image_bytes,
                    metadata={
                        "source": "docx_zip_xml_image",
                        "document_order": image_metadata.get("document_order"),
                        "rId": image_metadata.get("rId"),
                        "resolved_target": resolved_target,
                        "part_name": image_metadata.get("part_name"),
                        "caption_text_raw": image_metadata.get("caption_text_raw"),
                        "original_size_bytes": image_metadata.get("original_size_bytes"),
                        "converted_size_bytes": image_metadata.get("converted_size_bytes"),
                        "original_sha256": original_sha256,
                        "converted_sha256": image_metadata.get("converted_sha256"),
                    }
                ))
                block_index += 1
        
        # Process embedded documents recursively
        for embedded_idx, embedded_info in embedded_docs.items():
            try:
                import os
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp_file:
                    tmp_file.write(embedded_info['bytes'])
                    tmp_file_path = tmp_file.name
                
                try:
                    # Detect file type from MIME type or filename
                    content_type = embedded_info.get('content_type', 'unknown')
                    filename = embedded_info.get('filename', f'embedded_{embedded_idx}')
                    
                    # Determine file extension from MIME type
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
                    
                    if content_type in mime_to_ext:
                        suffix = mime_to_ext[content_type]
                    else:
                        suffix = Path(filename).suffix.lower() if filename else '.tmp'
                    
                    # Rename temp file with proper extension
                    if suffix and suffix != '.tmp':
                        new_tmp_path = tmp_file_path + suffix
                        os.rename(tmp_file_path, new_tmp_path)
                        tmp_file_path = new_tmp_path
                    
                    # Recursively parse embedded document
                    embedded_blocks = self.parse_file(
                        Path(tmp_file_path),
                        domain=domain,
                        embedded_depth=embedded_depth + 1,
                        parent_document=str(path),
                        embedded_filename=filename
                    )
                    
                    # Mark all embedded blocks with lineage metadata
                    for block in embedded_blocks:
                        if not hasattr(block, 'metadata') or block.metadata is None:
                            block.metadata = {}
                        block.metadata.update({
                            'is_embedded': True,
                            'lineage_path': f"{path} → {filename}",
                            'embedding_depth': embedded_depth + 1,
                            'parent_document': str(path),
                            'embedded_filename': filename,
                            'content_type': content_type,
                            'extraction_method': 'embedded_ole_object'
                        })
                    
                    blocks.extend(embedded_blocks)
                    self.logger.info(
                        "Processed embedded document",
                        parent=str(path),
                        embedded=filename,
                        blocks_created=len(embedded_blocks)
                    )
                    
                finally:
                    try:
                        os.unlink(tmp_file_path)
                    except:
                        pass
                        
            except Exception as e:
                self.logger.warning(
                    "Failed to process embedded document",
                    error=str(e),
                    parent=str(path),
                    embedded=embedded_info.get('filename', f'embedded_{embedded_idx}')
                )
        
        # Supplementary: DOCX→PDF conversion for shapes/diagrams + problem images only.
        # These are IE helpers — not first-class provenance blocks.
        supplementary = self._extract_docx_shapes_via_pdf(path, input_root, domain, blocks)
        if supplementary:
            blocks.extend(supplementary)

        return blocks

    def _extract_docx_shapes_via_pdf(
        self,
        path: Path,
        input_root: Path | None,
        domain: str,
        existing_blocks: list[CanonicalBlock],
    ) -> list[CanonicalBlock] | None:
        """Extract vector shapes/diagrams from DOCX via PDF conversion.

        Converts DOCX to PDF using headless LibreOffice, then uses PyMuPDF to
        detect and rasterize:
          1. Vector diagram pages (many drawing objects, little text).
          2. Pages containing native-failed images (EMF/WMF) — rasterised at
             page level so the rendered image is captured without cross-format
             deduplication issues.

        Individual image extraction is NOT performed here — native DOCX parsing
        is the single source for standard raster images, eliminating duplicates.

        Returns:
            Image-only blocks if conversion succeeds, otherwise None.
        """
        native_failed_pages: set[int] = set()
        for b in existing_blocks:
            if b.image_bytes is None and b.metadata:
                if b.metadata.get("skipped_image") and b.metadata.get("skip_reason") == "unsupported_emf_wmf":
                    page_num = b.metadata.get("page_number") or b.page_number
                    if page_num is not None:
                        native_failed_pages.add(page_num)

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_dir_path = Path(tmp_dir)
                output_pdf = tmp_dir_path / (path.stem + ".pdf")

                cmd = [
                    "soffice",
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", str(tmp_dir_path),
                    str(path)
                ]
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False
                )

                if result.returncode != 0 or not output_pdf.exists():
                    self.logger.warning(
                        "DOCX→PDF conversion failed; shapes/problem images may be missed",
                        file=str(path),
                        returncode=result.returncode,
                        stderr=result.stderr[:500] if result.stderr else ""
                    )
                    return None

                source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)

                if self.artifacts_dir and self.pdfs_dir:
                    import shutil
                    artifact_pdf_path = self.pdfs_dir / output_pdf.name
                    shutil.copy2(output_pdf, artifact_pdf_path)
                    self.logger.info(f"Saved converted PDF artifact: {artifact_pdf_path}")

                supplementary_blocks: list[CanonicalBlock] = []
                block_index = max((b.block_index for b in existing_blocks), default=-1) + 1

                with fitz.open(str(output_pdf)) as pdf_doc:
                    for page_num, page in enumerate(pdf_doc):
                        pdf_page_1based = page_num + 1

                        # --- Rasterize vector diagrams ---
                        drawings = page.get_drawings()
                        page_text = page.get_text().strip()
                        try:
                            page_tables = page.find_tables()
                            has_tables = bool(page_tables and page_tables.tables)
                        except Exception:
                            has_tables = False

                        drawing_threshold = 50 if has_tables else 10
                        is_diagram_page = (
                            len(drawings) > drawing_threshold
                            and len(page_text) < 300
                        )

                        needs_raster = is_diagram_page or (pdf_page_1based in native_failed_pages)
                        raster_reason = (
                            "vector_diagram" if is_diagram_page
                            else "native_failed_image" if pdf_page_1based in native_failed_pages
                            else None
                        )

                        if needs_raster:
                            self.logger.info(
                                "DOCX supplementary: rasterizing page",
                                file=str(path), page=pdf_page_1based,
                                reason=raster_reason,
                                vector_shapes=len(drawings),
                            )
                            mat = fitz.Matrix(2, 2)
                            pix = page.get_pixmap(matrix=mat)
                            raster_bytes = pix.tobytes("png")
                            optimized = self._optimize_image_for_vision(raster_bytes)
                            if optimized and optimized != raster_bytes:
                                raster_bytes = optimized

                            supplementary_blocks.append(CanonicalBlock(
                                block_id=generate_block_id(source_file_rel, block_index, domain, content=""),
                                source_file=source_file_rel,
                                file_type="docx",
                                domain=domain,
                                page_number=pdf_page_1based,
                                block_index=block_index,
                                block_type="image",
                                text="",
                                image_bytes=raster_bytes,
                                metadata={
                                    "source": f"docx_pdf_{raster_reason}_raster",
                                    "provenance": "docx_pdf_ie_helper",
                                    "vector_shape_count": len(drawings) if is_diagram_page else 0,
                                    "pdf_page_number": pdf_page_1based,
                                },
                            ))
                            block_index += 1

                if supplementary_blocks:
                    self.logger.info(
                        "DOCX supplementary extraction complete",
                        file=str(path),
                        rasterized_pages=len(supplementary_blocks),
                    )
                return supplementary_blocks if supplementary_blocks else None

        except FileNotFoundError:
            self.logger.warning(
                "LibreOffice not found (soffice); shapes/problem images may be missed",
                file=str(path)
            )
            return None
        except Exception as e:
            self.logger.warning(
                "DOCX→PDF supplementary extraction failed",
                file=str(path),
                error=str(e)
            )
            return None

    def _parse_pdf(
        self,
        path: Path,
        input_root: Path | None = None,
        domain: str = "",
        source_file_override: str | None = None,
        file_type_override: str | None = None,
        metadata_override: dict[str, Any] | None = None
    ) -> list[CanonicalBlock]:
        """Parse PDF file.

        Text segmentation uses unstructured (fast strategy — text only).
        Tables, images, and diagram detection use PyMuPDF (fitz).
        """
        blocks: list[CanonicalBlock] = []
        block_index = 0
        source_file_rel = source_file_override or (str(path.relative_to(input_root)) if input_root else str(path))
        file_type_val = file_type_override or "pdf"

         # Deferred import: unstructured PDF pulls OpenCV → libGL.so.1 (install libgl1 in the image).
        from unstructured.partition.pdf import partition_pdf

        # Text segmentation only — tables and images handled by PyMuPDF below.
        elements = partition_pdf(str(path), strategy="fast")

        # Extract tables, images, and detect diagrams using PyMuPDF
        image_map: dict[int, bytes] = {}
        extracted_xrefs: set[int] = set()
        full_page_rasters: dict[int, bytes] = {}
        ocr_map: dict[int, str] = {}
        strip_meta_map: dict[int, dict] = {}
        pdf_table_pages: set[int] = set()  # pages where PyMuPDF detected tables

        with fitz.open(str(path)) as pdf_doc:
            for page_num, page in enumerate(pdf_doc):
                # --- Table detection (PyMuPDF find_tables) ---
                try:
                    page_tables = page.find_tables()
                    if page_tables and page_tables.tables:
                        pdf_table_pages.add(page_num)
                        for tbl in page_tables.tables:
                            raw_rows = tbl.extract()
                            if not raw_rows:
                                continue
                            typed_rows: list[list[str | None]] = [
                                [(c if c and c.strip() else None) for c in row]
                                for row in raw_rows
                                if any(c and c.strip() for c in row)
                            ]
                            if not typed_rows:
                                continue
                            header_detected, col_names = _detect_table_header(typed_rows)
                            header_candidate = ", ".join(col_names) if header_detected else _build_table_row_text(col_names, typed_rows[0])
                            start = 1 if header_detected else 0
                            for row_idx in range(start, len(typed_rows)):
                                row_text = _build_table_row_text(col_names, typed_rows[row_idx])
                                if len(row_text.strip()) < 3:
                                    continue
                                row_meta: dict[str, Any] = {"table_source": "pymupdf_find_tables"}
                                if metadata_override:
                                    row_meta.update(metadata_override)
                                row_meta["table_row_index"] = row_idx
                                row_meta["table_header_candidate"] = header_candidate
                                row_meta["table_header_is_self"] = (row_idx == 0 and not header_detected)
                                row_meta["pdf_page_number"] = page_num + 1
                                blocks.append(CanonicalBlock(
                                    block_id=generate_block_id(source_file_rel, block_index, domain, content=row_text),
                                    source_file=source_file_rel,
                                    file_type=file_type_val,
                                    domain=domain,
                                    page_number=page_num + 1,
                                    block_index=block_index,
                                    block_type="table_row",
                                    text=row_text,
                                    image_bytes=None,
                                    metadata=row_meta,
                                ))
                                block_index += 1
                except Exception as e:
                    self.logger.warning("PyMuPDF find_tables failed", error=str(e), file=str(path), page=page_num + 1)

                # --- Diagram detection (vector shapes) ---
                drawings = page.get_drawings()
                page_text = page.get_text().strip()
                has_tables_on_page = page_num in pdf_table_pages
                # Table borders produce many vector shapes; raise threshold when tables detected.
                # Only rasterize pages with minimal text — genuine diagrams have shape labels
                # but not paragraphs of prose.  Styled/decorated text pages (fills, borders)
                # must NOT trigger rasterization regardless of drawing count.
                drawing_threshold = 50 if has_tables_on_page else 10
                is_diagram_page = (
                    len(drawings) > drawing_threshold
                    and len(page_text) < 300
                )
                if is_diagram_page:
                    print(f"[Parser] Page {page_num + 1} has {len(drawings)} vector shapes, {len(page_text)} text chars - rasterizing for Vision IE", flush=True)
                    self.logger.info(
                        "Rasterizing full page (vector-based diagram detected)",
                        file=str(path),
                        page=page_num + 1,
                        vector_shape_count=len(drawings),
                        page_text_chars=len(page_text)
                    )
                    
                    mat = fitz.Matrix(2, 2)
                    pix = page.get_pixmap(matrix=mat)
                    full_page_img_bytes = pix.tobytes("png")
                    
                    print(f"[Parser] Checking if full-page raster needs optimization (page {page_num + 1})...", flush=True)
                    optimized_bytes = self._optimize_image_for_vision(full_page_img_bytes)
                    if optimized_bytes and optimized_bytes != full_page_img_bytes:
                        from PIL import Image
                        import io
                        final_size = Image.open(io.BytesIO(optimized_bytes)).size
                        print(f"[Parser] Full-page raster optimized: {pix.width}x{pix.height} → {final_size[0]}x{final_size[1]}", flush=True)
                        full_page_img_bytes = optimized_bytes
                    else:
                        print(f"[Parser] Full-page raster optimization skipped (already high-quality)", flush=True)
                    
                    full_page_rasters[page_num] = {
                        "bytes": full_page_img_bytes,
                        "vector_shape_count": len(drawings),
                        "width": pix.width,
                        "height": pix.height
                    }
                    
                    print(f"[Parser] Full-page raster created: {len(full_page_img_bytes)} bytes, resolution: {pix.width}x{pix.height}, vectors: {len(drawings)}", flush=True)
                    
                elif len(drawings) > drawing_threshold:
                    print(f"[Parser] Page {page_num + 1} has {len(drawings)} vectors but {len(page_text)} text chars - skipping rasterization (text-heavy page)", flush=True)
                
                # Extract individual images (normal processing)
                image_list = page.get_images(full=True)

                # Detect if any image on this page uses a soft mask (smask).
                # PDFs with smask store image content as a transparency/ink layer — extracting
                # the raw xref gives near-black pixel data (mean~1.6/255) rather than the
                # correct composited rendering. Page-level rendering via get_pixmap() correctly
                # composites all layers and produces the visible white-background image.
                page_has_smask = any(img[1] != 0 for img in image_list)

                if page_has_smask:
                    smask_count = sum(1 for img in image_list if img[1] != 0)
                    print(
                        f"[Parser] Page {page_num + 1}: smask detected on {smask_count}"
                        f"/{len(image_list)} image(s) — compositing at native resolution",
                        flush=True
                    )
                    self.logger.info(
                        "Smask images detected — compositing at native resolution",
                        file=str(path),
                        page=page_num + 1,
                        smask_count=smask_count,
                        total_images=len(image_list),
                    )

                    # Native-resolution smask compositing.
                    # PDF smask images: xref=base (ink/content, appears near-black alone),
                    # smask=alpha mask (opacity: 255=opaque, 0=transparent).
                    # Compositing base onto white using smask as alpha gives the correctly
                    # coloured diagram at the embedded image's native DPI (e.g. 590 DPI for
                    # Temenos A4 PDFs), far sharper than a page-level get_pixmap() render.
                    import io  # ensure io is bound in this function scope before first use
                    from PIL import Image as _PIL_Image
                    composite_slot = 0  # keys: page*1000 + 997, 996, 995 …
                    page_render_fallback_needed = False

                    for img_info in image_list:
                        xref_id = img_info[0]
                        smask_id = img_info[1]
                        if smask_id == 0:
                            continue  # non-smask images handled in the xref loop below

                        if xref_id in extracted_xrefs:
                            self.logger.debug(
                                "Skipping smask xref already composited on earlier page",
                                file=str(path), page=page_num + 1, xref=xref_id,
                            )
                            continue

                        try:
                            base_data = pdf_doc.extract_image(xref_id)
                            smask_data = pdf_doc.extract_image(smask_id)

                            base_pil = _PIL_Image.open(
                                io.BytesIO(base_data["image"])
                            ).convert("RGB")
                            smask_pil = _PIL_Image.open(
                                io.BytesIO(smask_data["image"])
                            ).convert("L")

                            # Align dimensions (should match, but guard against edge cases)
                            if smask_pil.size != base_pil.size:
                                smask_pil = smask_pil.resize(base_pil.size, _PIL_Image.LANCZOS)

                            # Composite: white canvas + base image painted through the smask
                            white_bg = _PIL_Image.new("RGB", base_pil.size, (255, 255, 255))
                            white_bg.paste(base_pil, mask=smask_pil)

                            _MIN_DIAGRAM_DIM = 200
                            if base_pil.width < _MIN_DIAGRAM_DIM or base_pil.height < _MIN_DIAGRAM_DIM:
                                self.logger.info(
                                    "Skipping small smask image (logo/icon)",
                                    file=str(path), page=page_num + 1,
                                    xref=xref_id, size=f"{base_pil.width}x{base_pil.height}",
                                )
                                extracted_xrefs.add(xref_id)
                                continue

                            buf = io.BytesIO()
                            white_bg.save(buf, format="PNG")
                            composite_raw = buf.getvalue()

                            print(
                                f"[Parser] Smask composite xref={xref_id}+smask={smask_id}: "
                                f"{base_pil.width}×{base_pil.height} native resolution",
                                flush=True
                            )
                            self.logger.info(
                                "Smask composite created at native resolution",
                                file=str(path),
                                page=page_num + 1,
                                xref=xref_id,
                                smask=smask_id,
                                native_size=f"{base_pil.width}x{base_pil.height}",
                                bytes=len(composite_raw),
                            )

                            # Pipe through standard pipeline (optimize + save artifacts)
                            converted_bytes, fmt, _skip_ie = self._convert_image_to_png(composite_raw)
                            if converted_bytes is not None:

                                # --- OCR: extract text from native composite for Vision LM context ---
                                # Run on the raw composite (not the optimised/resized output) to get
                                # the best OCR quality. Stored in ocr_map keyed by page_num so all
                                # strip blocks on this page share the same OCR context.
                                if page_num not in ocr_map:
                                    ocr_text = self._extract_ocr_text(composite_raw)
                                    if ocr_text:
                                        ocr_map[page_num] = ocr_text
                                        print(
                                            f"[Parser] OCR: extracted {len(ocr_text)} chars "
                                            f"from page {page_num + 1} native composite",
                                            flush=True,
                                        )
                                        self.logger.info(
                                            "OCR text extracted from smask composite",
                                            file=str(path),
                                            page=page_num + 1,
                                            ocr_chars=len(ocr_text),
                                        )

                                # --- Tiling: split tall composites into overlapping strips ---
                                # Tall diagrams (e.g. Temenos BB flowcharts) are 2666×6893 px.
                                # After _resize_image_for_vision the full image becomes ~1057×2732
                                # (scale 0.40×), shrinking box labels to ~12 px — below readable.
                                # Each 1/3-height strip (2666×2500 px) resizes to ~1754×1645 px
                                # (scale 0.66×), giving ~23 px labels — crossing the readable line.
                                # Three-tier tiling (mirrors _parse_png logic):
                                # Tier 1: extreme tall  (h/w > threshold) → vertical strips
                                # Tier 2: extreme wide  (w/h > threshold) → horizontal strips
                                # Tier 3: large moderate-aspect (px > budget)
                                #          → tile along longer axis
                                # Tier 4: fits in budget → single block
                                try:
                                    pil_check = _PIL_Image.open(io.BytesIO(converted_bytes))
                                    _pw, _ph = pil_check.width, pil_check.height
                                    img_total_px = _pw * _ph
                                    _tall_a = _ph / max(_pw, 1)
                                    _wide_a = _pw / max(_ph, 1)
                                    _tile_v = _tall_a > self._TILE_ASPECT_THRESHOLD and img_total_px > self._TILE_MIN_PIXELS
                                    _tile_h = _wide_a > self._TILE_ASPECT_THRESHOLD and img_total_px > self._TILE_MIN_PIXELS
                                    _tile_mod = img_total_px > self._TILE_MIN_PIXELS  # moderate-aspect fallback
                                except Exception:
                                    _pw, _ph, img_total_px = 0, 0, 0
                                    _tall_a = _wide_a = 0.0
                                    _tile_v = _tile_h = _tile_mod = False

                                if _tile_v:
                                    _strips = self._tile_image_into_strips(converted_bytes)
                                    _tile_dir = "vertical"
                                    print(
                                        f"[Parser] Tier-1 vertical tiling smask "
                                        f"({_pw}×{_ph}, {img_total_px / 1e6:.1f}M px, "
                                        f"h/w={_tall_a:.2f}) into {len(_strips)} strips",
                                        flush=True,
                                    )
                                    self.logger.info(
                                        "Tall smask composite tiled vertically (tier-1)",
                                        file=str(path), page=page_num + 1,
                                        native_size=f"{_pw}x{_ph}", strips=len(_strips),
                                    )
                                elif _tile_h:
                                    _strips = self._tile_image_into_horizontal_strips(converted_bytes)
                                    _tile_dir = "horizontal"
                                    print(
                                        f"[Parser] Tier-2 horizontal tiling smask "
                                        f"({_pw}×{_ph}, {img_total_px / 1e6:.1f}M px, "
                                        f"w/h={_wide_a:.2f}) into {len(_strips)} strips",
                                        flush=True,
                                    )
                                    self.logger.info(
                                        "Wide smask composite tiled horizontally (tier-2)",
                                        file=str(path), page=page_num + 1,
                                        native_size=f"{_pw}x{_ph}", strips=len(_strips),
                                    )
                                elif _tile_mod:
                                    if _pw >= _ph:
                                        _strips = self._tile_image_into_horizontal_strips(converted_bytes)
                                        _tile_dir = "horizontal"
                                    else:
                                        _strips = self._tile_image_into_strips(converted_bytes)
                                        _tile_dir = "vertical"
                                    print(
                                        f"[Parser] Tier-3 moderate-aspect tiling smask "
                                        f"({_pw}×{_ph}, {img_total_px / 1e6:.1f}M px) "
                                        f"→ {_tile_dir} into {len(_strips)} strips",
                                        flush=True,
                                    )
                                    self.logger.info(
                                        "Moderate-aspect smask composite tiled along longer axis (tier-3)",
                                        file=str(path), page=page_num + 1,
                                        native_size=f"{_pw}x{_ph}",
                                        direction=_tile_dir, strips=len(_strips),
                                    )
                                else:
                                    _strips = None
                                    _tile_dir = None

                                if _strips is not None:
                                    for strip_idx, strip_bytes in enumerate(_strips):
                                        key = page_num * 1000 + 997 - composite_slot
                                        image_map[key] = strip_bytes
                                        strip_meta_map[key] = {
                                            "tile_strip_index": strip_idx + 1,
                                            "tile_strip_total": len(_strips),
                                            "tile_direction": _tile_dir,
                                        }
                                        if self.artifacts_dir and self.images_dir:
                                            try:
                                                import hashlib as _hl
                                                strip_hash = _hl.md5(strip_bytes).hexdigest()[:8]
                                                strip_path = (
                                                    self.images_dir
                                                    / f"tile_{_tile_dir[0]}"
                                                      f"_p{page_num + 1}"
                                                      f"_strip{strip_idx + 1}of{len(_strips)}"
                                                      f"_{strip_hash}.png"
                                                )
                                                strip_path.write_bytes(strip_bytes)
                                                self.logger.info(
                                                    "Saved tile strip artifact",
                                                    path=str(strip_path),
                                                    strip=f"{strip_idx + 1}/{len(_strips)}",
                                                )
                                            except Exception as _sae:
                                                self.logger.warning(
                                                    "Failed to save tile strip artifact",
                                                    error=str(_sae),
                                                )
                                        composite_slot += 1
                                else:
                                    key = page_num * 1000 + 997 - composite_slot
                                    image_map[key] = converted_bytes
                                    composite_slot += 1

                                extracted_xrefs.add(xref_id)  # prevent xref loop re-processing
                                page_render_fallback_needed = False

                        except Exception as composite_err:
                            page_render_fallback_needed = True
                            self.logger.warning(
                                "Smask compositing failed — will fall back to page render",
                                file=str(path),
                                page=page_num + 1,
                                xref=xref_id,
                                error=str(composite_err),
                            )
                            print(f"[Parser] Smask composite failed for xref={xref_id}: {composite_err}", flush=True)

                    # Fall back to 3x page render only if all composites failed
                    if page_render_fallback_needed:
                        print(f"[Parser] Falling back to 3x page render for page {page_num + 1}", flush=True)
                        mat = fitz.Matrix(3, 3)
                        pix = page.get_pixmap(matrix=mat)
                        page_render_bytes = pix.tobytes("png")
                        converted_bytes, fmt, _skip_ie = self._convert_image_to_png(page_render_bytes)
                        fallback_key = page_num * 1000 + 998
                        image_map[fallback_key] = converted_bytes if converted_bytes is not None else page_render_bytes
                        self.logger.info(
                            "Page render fallback saved for smask page",
                            file=str(path),
                            page=page_num + 1,
                            render_size=f"{pix.width}x{pix.height}",
                        )

                # Extract individual xref images (skip smask ones — handled above via page render)
                for img_idx, img in enumerate(image_list):
                    try:
                        xref = img[0]
                        smask = img[1]

                        # Skip if already extracted (LibreOffice often embeds same image on multiple pages)
                        if xref in extracted_xrefs:
                            self.logger.debug(
                                "Skipping duplicate image (already extracted by xref)",
                                file=str(path),
                                page=page_num + 1,
                                xref=xref
                            )
                            continue

                        # Skip smask images — raw extract_image() gives only the ink/mask data
                        # (near-black, mean~1.6/255). Correctly composited via page render above.
                        if smask != 0:
                            self.logger.debug(
                                "Skipping smask xref image (captured via page render)",
                                file=str(path),
                                page=page_num + 1,
                                xref=xref,
                                smask=smask,
                            )
                            continue

                        extracted_xrefs.add(xref)
                        base_image = pdf_doc.extract_image(xref)
                        image_bytes_raw = base_image["image"]
                        
                        # Convert image format if needed (WMF/EMF → PNG)
                        converted_image_bytes, format_name, skip_vision_ie = self._convert_image_to_png(image_bytes_raw)
                        
                        # Skip if conversion failed (returns None)
                        if converted_image_bytes is None:
                            self.logger.warning(f"Skipping image due to conversion failure: {format_name}", file=str(path), page=page_num + 1, image_index=img_idx)
                            print(f"[Parser] Skipping image from PDF due to conversion failure: {format_name}", flush=True)
                            continue
                        
                        image_map[page_num * 1000 + img_idx] = converted_image_bytes
                        self.logger.info(
                            "Extracted and converted image from PDF",
                            file=str(path),
                            page=page_num + 1,
                            image_index=img_idx,
                            xref=xref,
                            image_size_bytes=len(converted_image_bytes),
                            format=format_name
                        )
                    except Exception as e:
                        self.logger.warning("Failed to extract image from PDF", error=str(e), file=str(path), page=page_num + 1)
            
            if image_map:
                self.logger.info("PDF image extraction complete", file=str(path), total_images=len(image_map))
        
        # Process elements
        for element in elements:
            element_type = type(element).__name__
            text = str(element).strip()
            
            if not text and element_type != "Image":
                continue
            
            # Determine block_type
            if element_type in ("Title", "NarrativeText"):
                if hasattr(element, "metadata") and element.metadata:
                    category = getattr(element.metadata, "category", None)
                    if category == "Title":
                        block_type = "heading"
                    else:
                        block_type = "paragraph"
                else:
                    block_type = "paragraph"
            elif element_type == "Table":
                # Tables are handled by PyMuPDF find_tables() above — skip to avoid
                # double-processing. With strategy="fast", unstructured rarely produces
                # Table elements, but this guard is here for safety.
                continue
            elif element_type == "Image":
                page_num = getattr(element.metadata, "page_number", None) if hasattr(element, "metadata") else None
                image_key = (page_num or 0) * 1000
                image_bytes = image_map.pop(image_key, None) if image_map else None
                base_metadata = {"source": "pdf_image"}
                if metadata_override:
                    base_metadata.update(metadata_override)
                if page_num is not None:
                    base_metadata["pdf_page_number"] = page_num
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, block_index, domain, content=""),
                    source_file=source_file_rel,
                    file_type=file_type_val,
                    domain=domain,
                    page_number=page_num,
                    block_index=block_index,
                    block_type="image",
                    text="",
                    image_bytes=image_bytes,
                    metadata=base_metadata
                ))
                block_index += 1
                continue
            else:
                block_type = "paragraph"
            
            # Chunk text if needed
            chunks = self._chunk_text(text, block_index)
            for chunk_text, chunk_idx in chunks:
                block_metadata = {}
                if hasattr(element, "metadata") and element.metadata:
                    if hasattr(element.metadata, "page_number"):
                        block_metadata["page_number"] = element.metadata.page_number
                if metadata_override:
                    block_metadata.update(metadata_override)
                if block_metadata.get("page_number") is not None:
                    block_metadata["pdf_page_number"] = block_metadata.get("page_number")
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, chunk_idx, domain, content=chunk_text),
                    source_file=source_file_rel,
                    file_type=file_type_val,
                    domain=domain,
                    page_number=block_metadata.get("page_number"),
                    block_index=chunk_idx,
                    block_type=block_type,
                    text=chunk_text,
                    image_bytes=None,
                    metadata=block_metadata
                ))
                block_index = chunk_idx + 1
        
        # Add any remaining images
        if image_map:
            self.logger.info("Adding remaining images not detected by unstructured", file=str(path), remaining_images=len(image_map))
            sorted_images = sorted(image_map.items(), key=lambda x: x[0])
            for image_key, image_bytes in sorted_images:
                page_num = image_key // 1000
                base_metadata = {"source": "pdf_image", "unstructured_missed": True}
                if metadata_override:
                    base_metadata.update(metadata_override)
                base_metadata["pdf_page_number"] = page_num
                # Inject OCR text extracted from native smask composite for Vision LM context.
                # All tiles from the same page share the same full-page OCR text.
                if page_num in ocr_map:
                    base_metadata["ocr_text"] = ocr_map[page_num]
                # Annotate tiled strips with their position so downstream code (ie_client.py,
                # reporting) can identify which strip a block came from.
                if image_key in strip_meta_map:
                    base_metadata.update(strip_meta_map[image_key])
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, block_index, domain, content=""),
                    source_file=source_file_rel,
                    file_type=file_type_val,
                    domain=domain,
                    page_number=page_num,
                    block_index=block_index,
                    block_type="image",
                    text="",
                    image_bytes=image_bytes,
                    metadata=base_metadata
                ))
                block_index += 1
        
        # Add full-page rasterizations (vector-based diagrams)
        if full_page_rasters:
            self.logger.info("Adding full-page rasterizations", file=str(path), raster_count=len(full_page_rasters))
            print(f"[Parser] Adding {len(full_page_rasters)} full-page rasterizations for vector diagrams", flush=True)
            for page_num, raster_data in sorted(full_page_rasters.items()):
                
                # Extract raster data
                raster_bytes = raster_data["bytes"]
                vector_count = raster_data["vector_shape_count"]
                width = raster_data["width"]
                height = raster_data["height"]
                
                # SAVE TO ARTIFACTS (if enabled)
                if self.artifacts_dir and self.images_dir:
                    try:
                        artifact_img_path = self.images_dir / f"vector_diagram_page{page_num + 1}.png"
                        artifact_img_path.write_bytes(raster_bytes)
                        self.logger.info(f"Saved full-page raster artifact: {artifact_img_path}", page=page_num + 1, size_kb=len(raster_bytes)/1024)
                        print(f"[Parser] Saved full-page raster: {artifact_img_path.name} ({len(raster_bytes)/1024:.1f} KB)", flush=True)
                    except Exception as artifact_err:
                        self.logger.warning(f"Failed to save full-page raster artifact: {artifact_err}")
                
                # Build metadata with explainability
                base_metadata = {
                    "source": "pdf_full_page_raster_vector_diagram",
                    "rasterization": "full_page",
                    "vector_shape_count": vector_count,
                    "raster_width": width,
                    "raster_height": height,
                    "reason": "vector_based_diagram_detected"
                }
                if metadata_override:
                    base_metadata.update(metadata_override)
                base_metadata["pdf_page_number"] = page_num + 1
                
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, block_index, domain, content=""),
                    source_file=source_file_rel,
                    file_type=file_type_val,
                    domain=domain,
                    page_number=page_num + 1,
                    block_index=block_index,
                    block_type="image",
                    text="",
                    image_bytes=raster_bytes,
                    metadata=base_metadata
                ))
                block_index += 1
        
        return blocks

    def _parse_xlsx(self, path: Path, input_root: Path | None = None, domain: str = "",
                    embedded_depth: int = 0, parent_document: str = "") -> list[CanonicalBlock]:
        """Parse XLSX file — all sheets, header-aware, one block per row (no splitting)."""
        blocks: list[CanonicalBlock] = []
        block_index = 0
        source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)

        with open(path, 'rb') as xlsx_file:
            workbook = load_workbook(xlsx_file, data_only=True)
            try:
                for sheet in workbook.worksheets:
                    # Pass 1: find last row with actual data (avoids openpyxl max_row inflation)
                    used_row = 0
                    for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                        if any(v is not None and str(v).strip() != "" for v in row):
                            used_row = row_idx
                    if used_row == 0:
                        continue

                    # Pass 2: collect only the rows with actual data
                    data_rows: list[list] = []
                    for row in sheet.iter_rows(min_row=1, max_row=used_row, values_only=True):
                        data_rows.append(list(row))

                    if not data_rows:
                        continue

                    header_detected, col_names = _detect_table_header(data_rows)

                    if header_detected:
                        header_candidate_text = ", ".join(col_names)
                    else:
                        header_candidate_text = _build_table_row_text(col_names, data_rows[0])

                    start = 1 if header_detected else 0
                    for i in range(start, len(data_rows)):
                        row_text = _build_table_row_text(col_names, data_rows[i])
                        if len(row_text.strip()) < 3:
                            continue

                        blocks.append(CanonicalBlock(
                            block_id=generate_block_id(source_file_rel, block_index, domain, content=row_text),
                            source_file=source_file_rel,
                            file_type="xlsx",
                            domain=domain,
                            page_number=None,
                            block_index=block_index,
                            block_type="table_row",
                            text=row_text,
                            image_bytes=None,
                            metadata={
                                "table_row_index": i + 1,
                                "sheet": sheet.title,
                                "table_header_candidate": header_candidate_text,
                                "table_header_is_self": (i == 0 and not header_detected),
                            }
                        ))
                        block_index += 1
            finally:
                workbook.close()

        return blocks

    def _parse_txt(self, path: Path, input_root: Path | None = None, domain: str = "") -> list[CanonicalBlock]:
        """Parse TXT file."""
        blocks: list[CanonicalBlock] = []
        block_index = 0
        
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Split into paragraphs (blank lines)
        paragraphs = re.split(r'\n\s*\n', content)
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            # Chunk if needed
            chunks = self._chunk_text(para, block_index)
            for chunk_text, chunk_idx in chunks:
                source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, chunk_idx, domain, content=chunk_text),
                    source_file=source_file_rel,
                    file_type="txt",
                    domain=domain,
                    page_number=None,
                    block_index=chunk_idx,
                    block_type="paragraph",
                    text=chunk_text,
                    image_bytes=None,
                    metadata={}
                ))
                block_index = chunk_idx + 1
        
        return blocks

    def _parse_csv(self, path: Path, input_root: Path | None = None, domain: str = "") -> list[CanonicalBlock]:
        """Parse CSV file."""
        import csv
        
        blocks: list[CanonicalBlock] = []
        block_index = 0
        source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
        
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                # Detect delimiter
                sample = f.read(4096)
                f.seek(0)
                
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                    reader = csv.reader(f, dialect)
                except csv.Error:
                    reader = csv.reader(f, delimiter=",")
                
                # Read header row
                try:
                    headers = next(reader)
                    headers = [h.strip() for h in headers]
                except StopIteration:
                    self.logger.warning("Empty CSV file", path=str(path))
                    return blocks
                
                # Process each row
                for row_idx, row in enumerate(reader):
                    if not row or all(cell.strip() == "" for cell in row):
                        continue
                    
                    row_parts = []
                    for col_idx, cell in enumerate(row):
                        if col_idx < len(headers) and cell.strip():
                            header = headers[col_idx] if col_idx < len(headers) else f"Column{col_idx}"
                            row_parts.append(f"{header}: {cell.strip()}")
                    
                    if not row_parts:
                        continue
                    
                    row_text = " | ".join(row_parts)
                    
                    # Chunk if needed
                    chunks = self._chunk_text(row_text, block_index)
                    for chunk_text, chunk_idx in chunks:
                        blocks.append(CanonicalBlock(
                            block_id=generate_block_id(source_file_rel, chunk_idx, domain, content=chunk_text),
                            source_file=source_file_rel,
                            file_type="csv",
                            domain=domain,
                            page_number=None,
                            block_index=chunk_idx,
                            block_type="table_row",
                            text=chunk_text,
                            image_bytes=None,
                            metadata={"row_number": row_idx + 1, "headers": headers}
                        ))
                        block_index = chunk_idx + 1
                
        except Exception as e:
            self.logger.error("Failed to parse CSV", path=str(path), error=str(e))
            raise
        
        self.logger.info("Parsed CSV file", path=str(path), blocks=len(blocks))
        return blocks

    def _parse_json(self, path: Path, input_root: Path | None = None, domain: str = "") -> list[CanonicalBlock]:
        """Parse JSON file - handles single object, array, nested structures."""
        blocks: list[CanonicalBlock] = []
        block_index = 0
        source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            def extract_text(obj, prefix="", depth=0):
                """Recursively extract text from JSON structure."""
                if depth > 10:  # Prevent infinite recursion
                    return []
                
                texts = []
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        if isinstance(value, (str, int, float, bool)):
                            texts.append(f"{prefix}{key}: {value}")
                        elif isinstance(value, (dict, list)):
                            texts.extend(extract_text(value, f"{prefix}{key}.", depth+1))
                elif isinstance(obj, list):
                    for idx, item in enumerate(obj):
                        if isinstance(item, (str, int, float, bool)):
                            texts.append(f"{prefix}[{idx}]: {item}")
                        elif isinstance(item, (dict, list)):
                            texts.extend(extract_text(item, f"{prefix}[{idx}].", depth+1))
                elif isinstance(obj, (str, int, float, bool)):
                    texts.append(f"{prefix}{obj}")
                
                return texts
            
            # Extract all text content
            text_parts = extract_text(data)
            combined_text = "\n".join(text_parts)
            
            # Chunk if needed
            chunks = self._chunk_text(combined_text, block_index)
            for chunk_text, chunk_idx in chunks:
                blocks.append(CanonicalBlock(
                    block_id=generate_block_id(source_file_rel, chunk_idx, domain, content=chunk_text),
                    source_file=source_file_rel,
                    file_type="json",
                    domain=domain,
                    page_number=None,
                    block_index=chunk_idx,
                    block_type="paragraph",
                    text=chunk_text,
                    image_bytes=None,
                    metadata={"json_structure": "preserved", "depth": 0}
                ))
                block_index = chunk_idx + 1
        
        except Exception as e:
            self.logger.error("Failed to parse JSON", path=str(path), error=str(e))
            raise
        
        return blocks

    def _parse_pptx(self, path: Path, input_root: Path | None = None, domain: str = "", 
                    embedded_depth: int = 0, parent_document: str = "") -> list[CanonicalBlock]:
        """Parse PPTX file - extracts slides, notes, images, embedded files."""
        blocks: list[CanonicalBlock] = []
        block_index = 0
        source_file_rel = str(path.relative_to(input_root)) if input_root else str(path)
        
        if not PPTX_AVAILABLE:
            self.logger.warning("python-pptx not installed, skipping PPTX file", path=str(path))
            return blocks
        
        try:
            prs = Presentation(str(path))
            
            # Extract slides
            for slide_idx, slide in enumerate(prs.slides):
                slide_text_parts = []
                
                # Extract text from shapes
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_text_parts.append(shape.text.strip())
                
                # Extract notes
                if slide.has_notes_slide:
                    notes_slide = slide.notes_slide
                    for shape in notes_slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            slide_text_parts.append(f"[Notes] {shape.text.strip()}")
                
                # Create block for slide
                if slide_text_parts:
                    slide_text = "\n".join(slide_text_parts)
                    chunks = self._chunk_text(slide_text, block_index)
                    for chunk_text, chunk_idx in chunks:
                        blocks.append(CanonicalBlock(
                            block_id=generate_block_id(source_file_rel, chunk_idx, domain, content=chunk_text),
                            source_file=source_file_rel,
                            file_type="pptx",
                            domain=domain,
                            page_number=slide_idx + 1,
                            block_index=chunk_idx,
                            block_type="paragraph",
                            text=chunk_text,
                            image_bytes=None,
                            metadata={"slide_number": slide_idx + 1, "total_slides": len(prs.slides)}
                        ))
                        block_index = chunk_idx + 1
        
        except Exception as e:
            self.logger.error("Failed to parse PPTX", path=str(path), error=str(e))
            raise
        
        return blocks
