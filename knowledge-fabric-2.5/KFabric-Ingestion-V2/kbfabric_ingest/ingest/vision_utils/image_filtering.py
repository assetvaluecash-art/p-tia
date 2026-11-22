"""
Image filtering utilities to detect and filter simple icons/logos.

Designed for lift-and-shift to kbfabric_ingest/ingest/image_filtering.py
"""

import io
from typing import Optional

from PIL import Image
import numpy as np


def is_simple_icon(
    image_bytes: bytes,
    min_dimension: int = 200,
    max_colors: int = 10,
    min_text_length: int = 50
) -> tuple[bool, str]:
    """
    Detect if image is a simple icon/logo that should be filtered out.
    
    Criteria:
    - Very small dimensions (< min_dimension px in either dimension)
    - Low color complexity (< max_colors distinct colors)
    - Simple structure (high uniformity)
    
    Args:
        image_bytes: Raw image bytes
        min_dimension: Minimum dimension (width or height) to be considered non-icon
        max_colors: Maximum distinct colors for icon classification
        min_text_length: Minimum text length (not used, kept for API compatibility)
        
    Returns:
        (is_icon: bool, reason: str)
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        width, height = image.size
        
        # Criterion 1: Very small dimensions
        if width < min_dimension or height < min_dimension:
            return True, f"Small dimensions ({width}x{height})"
        
        # Criterion 2: Low color complexity
        if image.mode in ('RGB', 'RGBA'):
            # Count distinct colors (sample-based for performance)
            # Resize to max 256x256 for performance if larger
            if width > 256 or height > 256:
                sample_image = image.resize((256, 256), Image.Resampling.LANCZOS)
            else:
                sample_image = image
            
            colors = sample_image.getcolors(maxcolors=256*256*256)
            if colors:
                distinct_colors = len(colors)
                if distinct_colors < max_colors:
                    return True, f"Low color complexity ({distinct_colors} distinct colors)"
        
        # Criterion 3: High uniformity (simple icon has uniform background)
        if image.mode in ('RGB', 'RGBA', 'L'):
            img_array = np.array(image.convert('L'))
            std_dev = float(np.std(img_array))
            if std_dev < 20:  # Very uniform (low variance)
                return True, f"High uniformity (std_dev={std_dev:.1f})"
        
        return False, "Not an icon"
        
    except Exception as e:
        # If analysis fails, don't filter (err on side of processing)
        return False, f"Analysis failed: {e}"


def should_process_image(image_bytes: bytes) -> tuple[bool, str]:
    """
    Determine if image should be processed by vision IE pipeline.
    
    Args:
        image_bytes: Raw image bytes
        
    Returns:
        (should_process: bool, reason: str)
    """
    is_icon, reason = is_simple_icon(image_bytes)
    if is_icon:
        return False, f"Filtered out as icon: {reason}"
    return True, "Processable image"
