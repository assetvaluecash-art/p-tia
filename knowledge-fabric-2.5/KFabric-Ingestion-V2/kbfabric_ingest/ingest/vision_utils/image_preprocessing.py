"""
Adaptive image preprocessing for OCR enhancement.

Designed for lift-and-shift to kbfabric_ingest/ingest/image_preprocessing.py
"""

import io
from typing import Optional
from dataclasses import dataclass

from PIL import Image, ImageEnhance
import numpy as np


@dataclass
class PreprocessingMetrics:
    """Metrics from image preprocessing for validation."""
    contrast_factor: float = 1.0
    brightness_adjusted: bool = False
    brightness_factor: float = 1.0
    quality_score: float = 0.0
    mean_brightness: float = 0.0
    std_contrast: float = 0.0
    preprocessing_applied: bool = False


class ImagePreprocessor:
    """
    Adaptive image preprocessing for OCR enhancement.
    
    Analyzes image characteristics and applies appropriate contrast/brightness
    adjustments to improve OCR accuracy.
    
    Designed for lift-and-shift to ingestion pipeline.
    """
    
    def __init__(
        self,
        contrast_factor_range: tuple[float, float] = (1.5, 3.0),
        brightness_threshold: int = 100,
        contrast_threshold: tuple[float, float] = (30.0, 80.0),
        default_contrast: float = 2.0
    ):
        """
        Initialize preprocessor with configurable thresholds.
        
        Args:
            contrast_factor_range: (min, max) contrast enhancement range
            brightness_threshold: Mean brightness below which to brighten
            contrast_threshold: (low, high) std dev thresholds for contrast adjustment
            default_contrast: Default contrast factor if adaptive logic doesn't apply
        """
        self.contrast_range = contrast_factor_range
        self.brightness_threshold = brightness_threshold
        self.contrast_threshold = contrast_threshold
        self.default_contrast = default_contrast
    
    def preprocess(self, image_bytes: bytes) -> tuple[bytes, PreprocessingMetrics]:
        """
        Preprocess image with adaptive enhancement.
        
        Args:
            image_bytes: Raw image bytes
            
        Returns:
            (processed_bytes, metrics)
        """
        try:
            image = Image.open(io.BytesIO(image_bytes))
            
            # Convert to grayscale for analysis
            if image.mode != 'L':
                image_gray = image.convert('L')
            else:
                image_gray = image
            
            # Measure image statistics
            img_array = np.array(image_gray)
            mean_brightness = float(np.mean(img_array))
            std_contrast = float(np.std(img_array))
            
            metrics = PreprocessingMetrics(
                mean_brightness=mean_brightness,
                std_contrast=std_contrast
            )
            
            # Adaptive brightness adjustment
            if mean_brightness < self.brightness_threshold:
                enhancer = ImageEnhance.Brightness(image_gray)
                brightness_factor = 1.5  # Brighten dark images
                image_gray = enhancer.enhance(brightness_factor)
                metrics.brightness_adjusted = True
                metrics.brightness_factor = brightness_factor
            
            # Adaptive contrast adjustment
            if std_contrast < self.contrast_threshold[0]:  # Low contrast
                contrast_factor = min(self.contrast_range[1], 2.5)  # High enhancement
            elif std_contrast > self.contrast_threshold[1]:  # Already high contrast
                contrast_factor = max(self.contrast_range[0], 1.2)  # Slight enhancement
            else:  # Medium contrast
                contrast_factor = self.default_contrast
            
            enhancer = ImageEnhance.Contrast(image_gray)
            image_processed = enhancer.enhance(contrast_factor)
            metrics.contrast_factor = contrast_factor
            metrics.preprocessing_applied = True
            
            # Calculate quality score (simple heuristic)
            # Higher score = better quality for OCR
            quality_score = min(
                (mean_brightness / 255.0) * 0.4 +  # Brightness component
                (std_contrast / 100.0) * 0.6,      # Contrast component
                1.0
            )
            metrics.quality_score = quality_score
            
            # Save back to bytes
            output_buffer = io.BytesIO()
            image_processed.save(output_buffer, format='PNG')
            return output_buffer.getvalue(), metrics
            
        except Exception as e:
            # If preprocessing fails, return original with error in metrics
            metrics = PreprocessingMetrics()
            metrics.quality_score = 0.0
            return image_bytes, metrics
