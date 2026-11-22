"""
EMF/WMF to PNG conversion utilities.

Designed for lift-and-shift to kbfabric_ingest/ingest/vision_utils/emf_conversion.py
"""

import os
import tempfile
import subprocess
import time
from pathlib import Path
from typing import Optional, List, Tuple

from PIL import Image
import io

from .image_preprocessing import ImagePreprocessor


def _wait_for_file_stable(file_path: Path, timeout_seconds: float = 5.0, check_interval: float = 0.05) -> tuple[bool, int]:
    """
    Wait for file to exist and have stable size (not being written to).
    
    Designed for lift-and-shift to main pipeline.
    Handles filesystem delays, network drives, WSL, and slow I/O.
    
    Args:
        file_path: Path to file to wait for
        timeout_seconds: Maximum time to wait (default: 5.0)
        check_interval: How often to check in seconds (default: 0.05 = 50ms)
        
    Returns:
        Tuple of (success: bool, file_size: int)
        - success: True if file exists and size is stable, False if timeout
        - file_size: Final file size in bytes (0 if file doesn't exist)
    """
    start_time = time.time()
    last_size = -1
    stable_count = 0
    required_stable_checks = 3  # File size must be same for 3 consecutive checks
    
    while (time.time() - start_time) < timeout_seconds:
        if not file_path.exists():
            time.sleep(check_interval)
            continue
        
        try:
            current_size = file_path.stat().st_size
        except (OSError, FileNotFoundError):
            # File disappeared or permission issue
            time.sleep(check_interval)
            continue
        
        # File exists and we can read its size
        if current_size == last_size:
            stable_count += 1
            if stable_count >= required_stable_checks:
                return True, current_size
        else:
            # Size changed, reset stability counter
            stable_count = 0
            last_size = current_size
        
        time.sleep(check_interval)
    
    # Timeout - return whatever size we have (might be 0 or incomplete)
    try:
        final_size = file_path.stat().st_size if file_path.exists() else 0
    except (OSError, FileNotFoundError):
        final_size = 0
    return False, final_size


def is_png_quality_acceptable(png_bytes: bytes, verbose: bool = False) -> tuple[bool, str]:
    """
    Check if converted PNG has sufficient quality for processing.
    
    Designed for lift-and-shift to main pipeline.
    
    Args:
        png_bytes: PNG image bytes to validate
        verbose: If True, print diagnostic messages
        
    Returns:
        Tuple of (is_acceptable: bool, reason: str)
        - is_acceptable: True if quality is sufficient for vision processing
        - reason: Diagnostic message explaining the result
    """
    try:
        img = Image.open(io.BytesIO(png_bytes))
        width, height = img.size

        # Basic quality checks with diagnostics
        if width < 100 or height < 100:
            reason = f"Image too small ({width}x{height}, need >=100x100)"
            if verbose:
                print(f"[EMF] Quality check failed: {reason}", flush=True)
            return False, reason

        # Color diversity check
        colors = img.getcolors(maxcolors=256)
        color_count = len(colors) if colors else 0
        if color_count < 2:
            reason = f"Single color or blank (only {color_count} color(s))"
            if verbose:
                print(f"[EMF] Quality check failed: {reason}", flush=True)
            return False, reason

        # Check for uniform/blank images (contrast check)
        extrema = img.convert('L').getextrema()
        contrast_diff = extrema[1] - extrema[0]
        if contrast_diff < 10:
            reason = f"Low contrast (difference={contrast_diff:.1f}, need >=10)"
            if verbose:
                print(f"[EMF] Quality check failed: {reason}", flush=True)
            return False, reason

        # Quality is acceptable
        if verbose:
            print(f"[EMF] Quality check passed: Size={width}x{height}, Colors={color_count if color_count > 0 else 'many'}, Contrast={contrast_diff:.1f}", flush=True)
        return True, "OK"

    except Exception as e:
        reason = f"Quality check exception: {e}"
        if verbose:
            print(f"[EMF] {reason}", flush=True)
        return False, reason


def _try_enhance_converted_image(converted_bytes: bytes, reason: str, converter_name: str) -> tuple[bytes, bool]:
    """
    Attempt to enhance a low-quality converted image using ImagePreprocessor.
    
    Designed for lift-and-shift to main pipeline.
    
    Args:
        converted_bytes: PNG bytes from conversion
        reason: Reason why quality check failed
        converter_name: Name of converter (for logging)
        
    Returns:
        Tuple of (enhanced_bytes, success)
    """
    # Don't try to enhance if image is completely blank or too small
    if "too small" in reason.lower() or "single color" in reason.lower():
        return converted_bytes, False
    
    try:
        print(f"[EMF] Attempting to enhance low-quality {converter_name} output...", flush=True)
        preprocessor = ImagePreprocessor()
        enhanced_bytes, _ = preprocessor.preprocess(converted_bytes)
        is_acceptable_after, _ = is_png_quality_acceptable(enhanced_bytes, verbose=True)
        if is_acceptable_after:
            print(f"[EMF] ✅ {converter_name} conversion enhanced successfully", flush=True)
            return enhanced_bytes, True
        else:
            return converted_bytes, False
    except Exception as e:
        print(f"[EMF] Enhancement failed: {e}", flush=True)
        return converted_bytes, False


def convert_emf_to_png(image_bytes: bytes, imagemagick_dpi_options: Optional[List[int]] = None) -> tuple[bytes, str]:
    """
    Convert EMF/WMF to PNG using LibreOffice, then ImageMagick as fallback.
    Includes quality validation, enhancement, and configurable DPI options.
    
    Designed for lift-and-shift to main pipeline.
    
    Args:
        image_bytes: Raw EMF/WMF image bytes
        imagemagick_dpi_options: List of DPI values to try for ImageMagick (default: [300, 200, 150, 96])
                                Higher DPI = better quality but larger file size
        
    Returns:
        Tuple of (converted_bytes, format_name)
        - converted_bytes: PNG bytes if conversion successful, original bytes otherwise
        - format_name: "PNG" if converted, original format name otherwise
    """
    # Default DPI options: start with highest quality, fall back to lower
    if imagemagick_dpi_options is None:
        imagemagick_dpi_options = [300, 200, 150, 96]

    format_name = "UNKNOWN"

    # Detect EMF/WMF format
    if len(image_bytes) >= 4:
        if image_bytes[:4] == b'\x01\x00\x00\x00':
            format_name = "EMF"
            print(f"[EMF] Detected EMF file signature", flush=True)
        elif image_bytes[:4] in [b'\x01\x00\x09\x00', b'\x02\x00\x09\x00', b'\x01\x09\x00\x00', b'\xD7\xCD\xC6\x9A']:
            format_name = "WMF"
            print(f"[EMF] Detected WMF file signature", flush=True)

    if format_name == "UNKNOWN":
        return image_bytes, "PNG"  # Already PNG

    print(f"[EMF] Detected {format_name} format, attempting conversion...", flush=True)

    # Try LibreOffice first with improved parameters
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{format_name.lower()}', delete=False) as emf_file:
            emf_file.write(image_bytes)
            emf_file.flush()
            os.fsync(emf_file.fileno())  # Force write to disk

            with tempfile.TemporaryDirectory() as temp_dir:
                # Improved LibreOffice command with explicit PNG export filter and resolution
                # Try multiple export formats for better quality
                libreoffice_formats = [
                    ('png', '--convert-to', 'png'),  # Standard PNG
                    ('png:writer_png_Export', '--convert-to', 'png:writer_png_Export'),  # Explicit PNG filter
                ]
                
                for format_desc, convert_flag, format_arg in libreoffice_formats:
                    cmd = [
                        'soffice',
                        '--headless',
                        '--nodefault',
                        '--nolockcheck',
                        '--nologo',
                        convert_flag,
                        format_arg,
                        '--outdir', temp_dir,
                        emf_file.name
                    ]
                    print(f"[EMF] Running LibreOffice conversion ({format_desc}): {' '.join(cmd)}", flush=True)
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                    if result.returncode == 0:
                        # Find the converted PNG
                        png_files = list(Path(temp_dir).glob('*.png'))
                        if png_files:
                            png_path = png_files[0]
                            
                            # Wait for file stability
                            file_ready, file_size = _wait_for_file_stable(png_path, timeout_seconds=3.0)
                            if not file_ready:
                                print(f"[EMF] LibreOffice conversion ({format_desc}) - file not ready after timeout", flush=True)
                                continue
                            
                            converted_bytes = png_path.read_bytes()

                            # Quality validation with diagnostics
                            is_acceptable, reason = is_png_quality_acceptable(converted_bytes, verbose=True)
                            
                            if is_acceptable:
                                # Enhance converted image quality
                                preprocessor = ImagePreprocessor()
                                enhanced_bytes, _ = preprocessor.preprocess(converted_bytes)
                                converted_bytes = enhanced_bytes
                                print(f"[EMF] ✅ LibreOffice conversion successful ({format_desc}, {len(converted_bytes)} bytes)", flush=True)
                                return converted_bytes, "PNG"
                            else:
                                print(f"[EMF] LibreOffice conversion ({format_desc}) produced low-quality PNG ({reason})", flush=True)
                                # Try to enhance anyway if not completely blank
                                enhanced_bytes, success = _try_enhance_converted_image(converted_bytes, reason, f"LibreOffice ({format_desc})")
                                if success:
                                    return enhanced_bytes, "PNG"
                                # Continue to next format or ImageMagick
                    else:
                        if result.stderr:
                            print(f"[EMF] LibreOffice conversion ({format_desc}) failed: {result.stderr[:200]}", flush=True)

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        print(f"[EMF] LibreOffice conversion failed: {e}, trying ImageMagick...", flush=True)

    # Fallback to ImageMagick (convert command) with configurable DPI options
    for dpi in imagemagick_dpi_options:
        try:
            with tempfile.NamedTemporaryFile(suffix=f'.{format_name.lower()}', delete=False) as emf_file:
                emf_file.write(image_bytes)
                emf_file.flush()
                os.fsync(emf_file.fileno())  # Force write to disk

                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as png_file:
                    png_path = Path(png_file.name)
                    
                    # ImageMagick convert command with quality options
                    cmd = [
                        'convert',
                        '-density', str(dpi),  # Set DPI/resolution
                        '-quality', '95',  # PNG quality (0-100)
                        '-colorspace', 'RGB',  # Ensure RGB colorspace
                        emf_file.name,
                        str(png_path)
                    ]
                    print(f"[EMF] Running ImageMagick conversion (DPI={dpi}): {' '.join(cmd)}", flush=True)
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                    if result.returncode == 0:
                        # Wait for file stability
                        file_ready, file_size = _wait_for_file_stable(png_path, timeout_seconds=5.0)
                        
                        if not file_ready:
                            print(f"[EMF] ImageMagick conversion at DPI={dpi} - file not ready after timeout (size={file_size} bytes)", flush=True)
                            if result.stdout:
                                print(f"[EMF] ImageMagick stdout: {result.stdout[:500]}", flush=True)
                            if result.stderr:
                                print(f"[EMF] ImageMagick stderr: {result.stderr[:500]}", flush=True)
                            if dpi != imagemagick_dpi_options[-1]:
                                print(f"[EMF] Trying next DPI option...", flush=True)
                                continue
                        
                        if file_size == 0:
                            print(f"[EMF] ImageMagick conversion at DPI={dpi} produced empty file (0 bytes)", flush=True)
                            if result.stdout:
                                print(f"[EMF] ImageMagick stdout: {result.stdout[:500]}", flush=True)
                            if result.stderr:
                                print(f"[EMF] ImageMagick stderr: {result.stderr[:500]}", flush=True)
                            if dpi != imagemagick_dpi_options[-1]:
                                print(f"[EMF] Trying next DPI option...", flush=True)
                                continue
                        
                        # Read file and validate PNG signature
                        try:
                            converted_bytes = png_path.read_bytes()
                        except Exception as e:
                            print(f"[EMF] Failed to read ImageMagick output file at DPI={dpi}: {e}", flush=True)
                            if dpi != imagemagick_dpi_options[-1]:
                                print(f"[EMF] Trying next DPI option...", flush=True)
                                continue
                        
                        # Validate PNG magic number (first 8 bytes)
                        if len(converted_bytes) < 8:
                            print(f"[EMF] ImageMagick conversion at DPI={dpi} produced file too small ({len(converted_bytes)} bytes, need >=8)", flush=True)
                            if dpi != imagemagick_dpi_options[-1]:
                                print(f"[EMF] Trying next DPI option...", flush=True)
                                continue
                        
                        png_signature = b'\x89PNG\r\n\x1a\n'
                        if converted_bytes[:8] != png_signature:
                            actual_sig = converted_bytes[:8].hex() if len(converted_bytes) >= 8 else 'too short'
                            print(f"[EMF] ImageMagick conversion at DPI={dpi} produced invalid PNG (file_size={file_size} bytes, signature={actual_sig}, expected PNG signature)", flush=True)
                            if result.stdout:
                                print(f"[EMF] ImageMagick stdout: {result.stdout[:500]}", flush=True)
                            if result.stderr:
                                print(f"[EMF] ImageMagick stderr: {result.stderr[:500]}", flush=True)
                            if dpi != imagemagick_dpi_options[-1]:
                                print(f"[EMF] Trying next DPI option...", flush=True)
                                continue
                        
                        # File exists, has content, and has valid PNG signature - proceed with quality check
                        is_acceptable, reason = is_png_quality_acceptable(converted_bytes, verbose=True)
                        
                        if is_acceptable:
                            # Enhance converted image quality
                            preprocessor = ImagePreprocessor()
                            enhanced_bytes, _ = preprocessor.preprocess(converted_bytes)
                            converted_bytes = enhanced_bytes
                            print(f"[EMF] ✅ ImageMagick conversion successful at DPI={dpi} ({len(converted_bytes)} bytes)", flush=True)
                            return converted_bytes, "PNG"
                        else:
                            print(f"[EMF] ImageMagick conversion at DPI={dpi} produced low-quality PNG ({reason})", flush=True)
                            # Try to enhance if not completely blank
                            enhanced_bytes, success = _try_enhance_converted_image(converted_bytes, reason, f"ImageMagick (DPI={dpi})")
                            if success:
                                return enhanced_bytes, "PNG"
                            
                            # Try next DPI if this one failed and not last option
                            if dpi != imagemagick_dpi_options[-1]:
                                print(f"[EMF] Trying next DPI option...", flush=True)
                                continue
                    else:
                        # Show ImageMagick error output
                        error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                        print(f"[EMF] ImageMagick conversion failed at DPI={dpi} (returncode={result.returncode}): {error_msg}", flush=True)
                        # Try next DPI if not last option
                        if dpi != imagemagick_dpi_options[-1]:
                            continue

        except FileNotFoundError:
            print(f"[EMF] ImageMagick not found - install with: sudo apt-get install imagemagick", flush=True)
            break  # Don't try other DPI options if ImageMagick isn't installed
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"[EMF] ImageMagick conversion exception at DPI={dpi}: {e}", flush=True)
            if dpi != imagemagick_dpi_options[-1]:
                continue

    # Final fallback: return original (let vision model handle it)
    print(f"[EMF] ⚠️ All conversion attempts failed, returning original {format_name}", flush=True)
    return image_bytes, format_name
