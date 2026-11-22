"""
Image boundary detection utilities for handling cut-off images.

Designed for lift-and-shift to kbfabric_ingest/ingest/image_boundary.py
"""

import io
from typing import List
from dataclasses import dataclass

from PIL import Image
import numpy as np


@dataclass
class CutoffInfo:
    """Information about image cut-off detection."""
    is_cut_off: bool
    cut_off_edges: List[str]  # ["bottom", "top", "left", "right"]
    cut_off_severity: float  # 0.0 to 1.0
    visible_content_ratio: float  # 0.0 to 1.0


def detect_image_cutoff(image_bytes: bytes, whiteness_threshold: float = 0.8) -> CutoffInfo:
    """
    Detect if image is cut off at edges.
    
    Analyzes edges of image for abrupt white space or incomplete content,
    which indicates the image was cut off during extraction.
    
    Args:
        image_bytes: Raw image bytes
        whiteness_threshold: Threshold for considering edge as cut-off (0.0-1.0)
        
    Returns:
        CutoffInfo with detection results
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        img_array = np.array(image.convert('L'))  # Convert to grayscale
        
        height, width = img_array.shape
        cut_off_edges = []
        max_severity = 0.0
        
        # Check bottom edge (most common cut-off)
        if height > 20:
            bottom_rows = img_array[-10:, :]
            bottom_whiteness = float(np.mean(bottom_rows > 240))  # Very white pixels (>240)
            if bottom_whiteness > whiteness_threshold:
                cut_off_edges.append("bottom")
                max_severity = max(max_severity, bottom_whiteness)
        
        # Check top edge
        if height > 20:
            top_rows = img_array[:10, :]
            top_whiteness = float(np.mean(top_rows > 240))
            if top_whiteness > whiteness_threshold:
                cut_off_edges.append("top")
                max_severity = max(max_severity, top_whiteness)
        
        # Check left edge
        if width > 20:
            left_cols = img_array[:, :10]
            left_whiteness = float(np.mean(left_cols > 240))
            if left_whiteness > whiteness_threshold:
                cut_off_edges.append("left")
                max_severity = max(max_severity, left_whiteness)
        
        # Check right edge
        if width > 20:
            right_cols = img_array[:, -10:]
            right_whiteness = float(np.mean(right_cols > 240))
            if right_whiteness > whiteness_threshold:
                cut_off_edges.append("right")
                max_severity = max(max_severity, right_whiteness)
        
        is_cut_off = len(cut_off_edges) > 0
        
        # Estimate visible content ratio (simple heuristic)
        # If cut off at bottom, assume ~80% visible
        # If cut off at multiple edges, reduce further
        if is_cut_off:
            if "bottom" in cut_off_edges:
                visible_ratio = 0.8 - (len(cut_off_edges) - 1) * 0.1
            else:
                visible_ratio = 0.9 - (len(cut_off_edges) - 1) * 0.1
            visible_ratio = max(visible_ratio, 0.5)  # At least 50% visible
        else:
            visible_ratio = 1.0
        
        return CutoffInfo(
            is_cut_off=is_cut_off,
            cut_off_edges=cut_off_edges,
            cut_off_severity=max_severity,
            visible_content_ratio=visible_ratio
        )
        
    except Exception as e:
        # If detection fails, assume not cut off
        return CutoffInfo(
            is_cut_off=False,
            cut_off_edges=[],
            cut_off_severity=0.0,
            visible_content_ratio=1.0
        )
