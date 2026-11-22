"""Vision model health check utilities for Ollama."""

import requests
from typing import Optional, Dict, Any
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


def check_ollama_vision_model(
    base_url: str,
    model_name: str,
    timeout: int = 5
) -> Dict[str, Any]:
    """
    Check if Ollama vision model is available and loaded.
    
    Args:
        base_url: Ollama base URL (e.g., "http://localhost:11434")
        model_name: Vision model name (e.g., "gemma3:12b-cloud")
        timeout: Request timeout in seconds
        
    Returns:
        Dictionary with:
        - available: bool - Whether model is available
        - loaded: bool - Whether model is loaded
        - error: Optional[str] - Error message if check failed
        - available_models: List[str] - List of available models
    """
    result = {
        "available": False,
        "loaded": False,
        "error": None,
        "available_models": []
    }
    
    try:
        # Check if Ollama server is reachable
        try:
            response = requests.get(f"{base_url}/api/tags", timeout=timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            result["error"] = f"Ollama server not reachable at {base_url}: {str(e)}"
            logger.warning("Ollama server check failed", base_url=base_url, error=str(e))
            return result
        
        # Get list of available models
        try:
            models_data = response.json()
            available_models = [m.get("name", "") for m in models_data.get("models", [])]
            result["available_models"] = available_models
            
            # Check if our model is in the list
            if model_name in available_models:
                result["available"] = True
                
                # Try to get model details to verify it's loaded
                try:
                    show_response = requests.post(
                        f"{base_url}/api/show",
                        json={"name": model_name},
                        timeout=timeout
                    )
                    if show_response.status_code == 200:
                        result["loaded"] = True
                        logger.info("Vision model is available and loaded", model=model_name)
                    else:
                        result["error"] = f"Model exists but show API returned {show_response.status_code}"
                        logger.warning("Model show API failed", model=model_name, status_code=show_response.status_code)
                except Exception as e:
                    result["error"] = f"Failed to check model details: {str(e)}"
                    logger.warning("Model details check failed", model=model_name, error=str(e))
            else:
                result["error"] = f"Model '{model_name}' not found in available models. Available models: {available_models[:5]}"
                logger.warning("Vision model not found", model=model_name, available_models=available_models[:10])
                
        except (KeyError, ValueError) as e:
            result["error"] = f"Failed to parse Ollama models response: {str(e)}"
            logger.warning("Failed to parse models response", error=str(e))
            
    except Exception as e:
        result["error"] = f"Unexpected error checking vision model: {str(e)}"
        logger.exception("Vision model health check failed", error=str(e))
    
    return result


def verify_vision_model_before_call(
    base_url: str,
    model_name: str,
    timeout: int = 5,
    raise_on_missing: bool = False
) -> bool:
    """
    Verify vision model is available before making API call.
    
    Args:
        base_url: Ollama base URL
        model_name: Vision model name
        timeout: Request timeout
        raise_on_missing: If True, raise exception if model not available
        
    Returns:
        True if model is available and loaded, False otherwise
        
    Raises:
        RuntimeError: If raise_on_missing=True and model is not available
    """
    check_result = check_ollama_vision_model(base_url, model_name, timeout)
    
    if not check_result["available"]:
        error_msg = (
            f"Vision model '{model_name}' is not available. "
            f"{check_result.get('error', 'Unknown error')}. "
            f"To load the model, run: ollama pull {model_name}"
        )
        logger.error(error_msg, check_result=check_result)
        if raise_on_missing:
            raise RuntimeError(error_msg)
        return False
    
    if not check_result["loaded"]:
        error_msg = (
            f"Vision model '{model_name}' exists but may not be fully loaded. "
            f"{check_result.get('error', 'Unknown error')}"
        )
        logger.warning(error_msg, check_result=check_result)
        # Don't raise here - model might still work
    
    return True


