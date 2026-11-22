"""Custom RAGAs providers for Ollama LLM, OpenAI LLM, and offline embeddings."""

from typing import Any, List
import requests
from langchain_community.llms import Ollama
from langchain_core.embeddings import Embeddings
from langchain_core.language_models.base import BaseLanguageModel
from kbfabric_ingest.ingest.config import get_app_config
from custom_ml.embeddings import EmbeddingsClient
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

# Try importing OpenAI classes
try:
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    ChatOpenAI = None
    OpenAIEmbeddings = None


class OllamaLLMProvider:
    """Custom LLM provider for RAGAs using Ollama.
    
    This wraps LangChain's Ollama to provide a RAGAs-compatible interface.
    Uses direct API calls with retry logic for reliability.
    """
    
    def __init__(self, model_name: str | None = None, base_url: str | None = None, timeout: int | None = None):
        """
        Initialize Ollama LLM provider from .env (via get_app_config) unless overridden.
        """
        ollama = get_app_config()["ollama"]
        self.model_name = model_name if model_name is not None else ollama["text_model"]
        self.base_url = base_url if base_url is not None else ollama["base_url"]
        self.timeout = timeout if timeout is not None else ollama["timeout_sec"]
        self.logger = get_logger(__name__)
        self.max_retries = ollama["max_retries"]
        self.retry_delay = 2  # seconds
        
        # Create LangChain Ollama instance - but without unsupported params
        try:
            # Use LangChain's Ollama - only pass supported params
            self.llm = Ollama(
                model=self.model_name, 
                base_url=self.base_url, 
                temperature=0.0
            )
        except Exception as e:
            logger.warning(f"Failed to initialize Ollama LLM: {e}, will use direct API calls")
            self.llm = None
    
    def _convert_prompt_to_string(self, prompt: Any) -> str:
        """Convert various prompt types to string."""
        if isinstance(prompt, str):
            return prompt
        # Handle LangChain StringPromptValue and similar objects
        if hasattr(prompt, 'to_string'):
            return prompt.to_string()
        if hasattr(prompt, 'text'):
            return prompt.text
        # Fallback to str conversion
        return str(prompt)
    
    def _call_api_with_retry(self, prompt_str: str) -> str:
        """Call Ollama API with retry logic for transient errors."""
        import time as time_module
        
        last_error = None
        delay = self.retry_delay
        
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    f"{self.base_url}/api/generate",
                    json={
                        "model": self.model_name,
                        "prompt": prompt_str,
                        "stream": False,
                        "options": {"temperature": 0.0}
                    },
                    timeout=self.timeout
                )
                response.raise_for_status()
                result = response.json()
                return result.get("response", "")
            except requests.HTTPError as e:
                last_error = e
                status = getattr(response, 'status_code', 0)
                if status in [429, 500, 502, 503, 504] and attempt < self.max_retries - 1:
                    self.logger.warning(
                        f"Ollama API error (attempt {attempt+1}/{self.max_retries}): {status}, retrying in {delay}s"
                    )
                    time_module.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    raise
            except requests.Timeout as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    self.logger.warning(
                        f"Ollama timeout (attempt {attempt+1}/{self.max_retries}), retrying in {delay}s"
                    )
                    time_module.sleep(delay)
                    delay *= 2
                else:
                    raise
        
        raise last_error or Exception("All retries failed")
    
    def generate(self, prompt: Any, **kwargs: Any) -> str:
        """Generate text from prompt."""
        prompt_str = self._convert_prompt_to_string(prompt)
        try:
            return self._call_api_with_retry(prompt_str)
        except Exception as e:
            self.logger.warning(f"Ollama generation failed: {e}")
            return ""
    
    def __call__(self, prompt: Any, **kwargs: Any) -> str:
        """Make provider callable."""
        return self.generate(prompt, **kwargs)
    
    def set_run_config(self, run_config: Any) -> None:
        """RAGAs compatibility method - no-op for Ollama."""
        pass
    
    def invoke(self, prompt: Any, **kwargs: Any) -> str:
        """Invoke the LLM (RAGAs compatibility)."""
        return self.generate(prompt, **kwargs)
    
    async def ainvoke(self, prompt: Any, **kwargs: Any) -> str:
        """Async invoke (uses sync for now)."""
        return self.generate(prompt, **kwargs)
    
    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to underlying LLM if available."""
        if name in ('set_run_config', 'invoke', 'ainvoke', 'generate'):
            return object.__getattribute__(self, name)
        if self.llm and hasattr(self.llm, name):
            return getattr(self.llm, name)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")


class OfflineEmbeddingsProvider(Embeddings):
    """Custom embeddings provider for RAGAs using offline embeddings."""
    
    def __init__(self):
        """Initialize offline embeddings provider."""
        config = get_app_config()
        self.embeddings_client = EmbeddingsClient(config)
        self.logger = get_logger(__name__)
        # Pre-load model
        try:
            self.embeddings_client.load_model()
            self.logger.info("Embedding model loaded for RAGAs")
        except Exception as e:
            self.logger.warning(f"Failed to pre-load embeddings: {e}")
    
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed a list of documents.
        
        Args:
            texts: List of text strings
            
        Returns:
            List of embedding vectors (768-dim each)
        """
        if not texts:
            return []
        try:
            return self.embeddings_client.encode(texts)
        except Exception as e:
            self.logger.warning(f"Embedding failed: {e}")
            # Return zero vectors as fallback
            return [[0.0] * 768] * len(texts)
    
    def embed_query(self, text: str) -> List[float]:
        """
        Embed a single query.
        
        Args:
            text: Query text string
            
        Returns:
            Embedding vector (768-dim)
        """
        if not text:
            return [0.0] * 768
        try:
            embeddings = self.embeddings_client.encode([text])
            return embeddings[0] if embeddings else [0.0] * 768
        except Exception as e:
            self.logger.warning(f"Query embedding failed: {e}")
            return [0.0] * 768


class OpenAILLMProvider:
    """Custom LLM provider for RAGAs using OpenAI.
    
    This wraps LangChain's ChatOpenAI to provide a RAGAs-compatible interface
    with the set_run_config method that RAGAs expects.
    """
    
    def __init__(self, model_name: str | None = None, api_key: str | None = None, timeout: int | None = None):
        """
        Initialize OpenAI LLM provider from .env (via get_app_config) unless overridden.
        """
        if not OPENAI_AVAILABLE:
            raise ImportError("langchain-openai not installed. Install with: pip install langchain-openai")

        openai_cfg = get_app_config()["openai"]
        self.model_name = model_name if model_name is not None else openai_cfg["model"]
        self.api_key = api_key if api_key is not None else openai_cfg["api_key"]
        self.timeout = timeout if timeout is not None else openai_cfg["timeout_sec"]
        self.logger = get_logger(__name__)
        
        # Create LangChain ChatOpenAI instance
        try:
            self.llm = ChatOpenAI(
                model=self.model_name,
                api_key=self.api_key,
                temperature=0.0,
                timeout=timeout
            )
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI LLM: {e}")
            raise
    
    def set_run_config(self, run_config: Any) -> None:
        """RAGAs compatibility method - no-op for OpenAI."""
        # No-op: OpenAI doesn't need run config, but RAGAs expects this method
        pass
    
    def __getattr__(self, name: str) -> Any:
        """Delegate attribute access to underlying LLM if available."""
        # Don't delegate set_run_config - we provide our own
        if name == 'set_run_config':
            return self.set_run_config
        if hasattr(self.llm, name):
            return getattr(self.llm, name)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
    
    def __call__(self, prompt: str, **kwargs: Any) -> Any:
        """Make provider callable."""
        return self.llm.invoke(prompt, **kwargs)
    
    def invoke(self, prompt: str, **kwargs: Any) -> Any:
        """Invoke the LLM."""
        return self.llm.invoke(prompt, **kwargs)
    
    def ainvoke(self, prompt: str, **kwargs: Any) -> Any:
        """Async invoke the LLM."""
        if hasattr(self.llm, 'ainvoke'):
            return self.llm.ainvoke(prompt, **kwargs)
        # Fallback to sync invoke
        return self.llm.invoke(prompt, **kwargs)


