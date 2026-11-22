"""Environment-only configuration for KnowledgeFabric Ingestion.

All runtime settings come from a dotenv file at the **repository root**
(``env`` by default). There are no YAML or in-code defaults for app config.

Env file selection (first match wins):
  1. ``KBF_ENV_FILE`` — absolute path or path relative to repo root
  2. ``KBF_ENV=<name>`` — loads ``env.<name>`` (e.g. ``KBF_ENV=test`` → ``env.test``)
  3. ``env`` in the repository root

Real OS / ``docker -e`` environment variables take precedence over the dotenv
file (``load_dotenv(..., override=False)``). TEST pipelines force Ray via
``-e RAY_DASHBOARD_*`` / ``RAY_NODE_IP_ADDRESS`` so a stale host file cannot
point Ray at DEV (.17).
"""

from __future__ import annotations

import os
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Package root: .../KFabric-Ingestion-V2
BASE_DIR = Path(__file__).resolve().parents[2]
# Repository root: .../KBFabricV2 (contains env / env.test / Dockerfile)
REPO_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# Type coercion helpers
# ---------------------------------------------------------------------------
def _as_str(value: str) -> str:
    return value


def _as_int(value: str) -> int:
    return int(value)


def _as_float(value: str) -> float:
    return float(value)


def _as_confidence_threshold(value: str) -> float:
    threshold = float(value)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("must be a finite number between 0 and 1")
    return threshold


_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def _as_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(f"Cannot parse boolean from value: {value!r}")


def _as_str_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


_ALLOWED_HITL_MODES = {"auto_write", "review_ambiguous", "review_all"}


def _as_hitl_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in _ALLOWED_HITL_MODES:
        raise ValueError(
            f"Invalid HITL mode {value!r}. "
            f"Expected one of: {sorted(_ALLOWED_HITL_MODES)}"
        )
    return mode


# ---------------------------------------------------------------------------
# Declarative mapping: (ENV_VAR, dotted.config.path, caster, allow_empty)
# ---------------------------------------------------------------------------
# allow_empty=True: key must be present in the environment but may be "".
# OLLAMA_URL is optional; when set it overrides OLLAMA_BASE_URL after build.
ENV_KEYS: List[Tuple[str, str, Callable[[str], Any], bool]] = [
    # Tenant
    ("KBFABRIC_TENANT_ID", "tenant_id", _as_str, False),

    # Weaviate
    ("WEAVIATE_URL", "weaviate.url", _as_str, False),
    ("WEAVIATE_API_KEY", "weaviate.api_key", _as_str, True),
    ("WEAVIATE_EF_CONSTRUCTION", "weaviate.index.ef_construction", _as_int, False),
    ("WEAVIATE_MAX_CONNECTIONS", "weaviate.index.max_connections", _as_int, False),
    ("WEAVIATE_EF", "weaviate.index.ef", _as_int, False),
    ("WEAVIATE_DISTANCE", "weaviate.index.distance", _as_str, False),
    ("WEAVIATE_TIMEOUT_QUERY_SEC", "weaviate.timeout.query_sec", _as_int, False),
    ("WEAVIATE_TIMEOUT_INSERT_SEC", "weaviate.timeout.insert_sec", _as_int, False),
    ("WEAVIATE_BATCH_SIZE", "weaviate.batch_size", _as_int, False),

    # PostgreSQL
    ("POSTGRES_HOST", "postgres.host", _as_str, False),
    ("POSTGRES_PORT", "postgres.port", _as_int, False),
    ("POSTGRES_DB", "postgres.db", _as_str, False),
    ("POSTGRES_USER", "postgres.user", _as_str, False),
    ("POSTGRES_PASSWORD", "postgres.password", _as_str, False),
    ("POSTGRES_POOL_SIZE", "postgres.pool_size", _as_int, False),

    # Neo4j
    ("NEO4J_URI", "neo4j.uri", _as_str, False),
    ("NEO4J_USER", "neo4j.user", _as_str, False),
    ("NEO4J_PASSWORD", "neo4j.password", _as_str, False),
    ("NEO4J_VECTOR_SIMILARITY_FUNCTION", "neo4j.vector_index.similarity_function", _as_str, False),
    ("NEO4J_POOL_MAX_SIZE", "neo4j.connection_pool.max_size", _as_int, False),
    ("NEO4J_POOL_ACQUISITION_TIMEOUT", "neo4j.connection_pool.acquisition_timeout", _as_int, False),
    ("NEO4J_POOL_CONNECTION_TIMEOUT", "neo4j.connection_pool.connection_timeout", _as_int, False),
    ("NEO4J_POOL_MAX_LIFETIME", "neo4j.connection_pool.max_lifetime", _as_int, False),
    ("NEO4J_BATCH_SIZE", "neo4j.batch_size", _as_int, False),

    # Scheduler / artifacts
    ("SCHEDULER_MAX_CONCURRENT_JOBS", "scheduler.max_concurrent_jobs", _as_int, False),
    ("ARTIFACTS_MUTE_RAY_DELETION", "artifacts.mute_ray_deletion", _as_bool, False),

    # Ollama
    ("OLLAMA_BASE_URL", "ollama.base_url", _as_str, False),
    ("OLLAMA_TEXT_MODEL", "ollama.text_model", _as_str, False),
    ("OLLAMA_VISION_MODEL", "ollama.vision_model", _as_str, False),
    ("OLLAMA_TIMEOUT_SEC", "ollama.timeout_sec", _as_int, False),
    ("OLLAMA_HEALTHCHECK_TIMEOUT_SEC", "ollama.healthcheck_timeout_sec", _as_int, False),
    ("OLLAMA_ENABLE_LLM_LOGGING", "ollama.enable_llm_logging", _as_bool, False),
    ("OLLAMA_TEXT_NUM_CTX", "ollama.text.num_ctx", _as_int, False),
    ("OLLAMA_TEXT_MAX_CONTEXT_TOKENS", "ollama.text.max_context_tokens", _as_int, False),
    ("OLLAMA_TEXT_BATCH_SIZE", "ollama.text.batch_size", _as_int, False),
    ("OLLAMA_TEXT_BATCH_DELAY_SECONDS", "ollama.text.batch_delay_seconds", _as_float, False),
    ("OLLAMA_TEXT_SINGLE_BLOCK_THRESHOLD", "ollama.text.single_block_threshold", _as_int, False),
    ("OLLAMA_TEXT_CHAT_FALLBACK_THRESHOLD", "ollama.text.chat_fallback_threshold", _as_int, False),
    ("OLLAMA_VISION_NUM_CTX", "ollama.vision.num_ctx", _as_int, False),
    ("OLLAMA_VISION_MAX_CONTEXT_TOKENS", "ollama.vision.max_context_tokens", _as_int, False),
    ("OLLAMA_VISION_BATCH_SIZE", "ollama.vision.batch_size", _as_int, False),
    ("OLLAMA_VISION_BATCH_DELAY_SECONDS", "ollama.vision.batch_delay_seconds", _as_float, False),
    ("OLLAMA_VISION_LLM_FALLBACK", "ollama.vision.vision_llm_fallback", _as_str, False),
    ("OLLAMA_VISION_MAX_IMAGE_PIXELS", "ollama.vision.max_image_pixels", _as_int, False),
    ("OLLAMA_TOKEN_BUFFER_PERCENT", "ollama.token_buffer_percent", _as_int, False),
    ("OLLAMA_TEMP_IE_TEXT", "ollama.temperature.ie_text", _as_float, False),
    ("OLLAMA_TEMP_IE_VISION", "ollama.temperature.ie_vision", _as_float, False),
    ("OLLAMA_TEMP_ANSWER_GEN", "ollama.temperature.answer_gen", _as_float, False),
    ("OLLAMA_MAX_RETRIES", "ollama.max_retries", _as_int, False),
    ("OLLAMA_RETRY_BACKOFF_FACTOR", "ollama.retry_backoff_factor", _as_float, False),

    # LiteLLM Model Router (empty URLs/token allowed when disabled)
    ("MODEL_ROUTER_ENABLED", "model_router.enabled", _as_bool, False),
    ("MODEL_ROUTER_CHAT_URL", "model_router.chat_url", _as_str, True),
    ("MODEL_ROUTER_GENERATE_URL", "model_router.generate_url", _as_str, True),
    ("MODEL_ROUTER_AUTH_TOKEN", "model_router.auth_token", _as_str, True),
    ("MODEL_ROUTER_TEXT_MODEL", "model_router.text_model", _as_str, False),
    ("MODEL_ROUTER_MAX_TOKENS", "model_router.max_tokens", _as_int, False),
    ("MODEL_ROUTER_MAX_RETRIES", "model_router.max_retries", _as_int, False),

    # OpenAI (RAGAs)
    ("OPENAI_API_KEY", "openai.api_key", _as_str, True),
    ("OPENAI_MODEL", "openai.model", _as_str, False),
    ("OPENAI_EMBEDDING_MODEL", "openai.embedding_model", _as_str, False),
    ("OPENAI_TIMEOUT_SEC", "openai.timeout_sec", _as_int, False),

    # PII
    ("PII_ENGINE", "pii.engine", _as_str, False),
    ("PII_MASK_POLICY", "pii.mask_policy", _as_str, False),
    ("PII_QUARANTINE_THRESHOLD", "pii.quarantine_threshold", _as_float, False),
    ("PII_USE_FAKER", "pii.use_faker", _as_bool, False),
    ("PII_FAKER_SEED", "pii.faker_seed", _as_int, False),
    ("PII_ENTITIES", "pii.entities", _as_str_list, False),

    # Ingestion
    ("INGESTION_HITL_MODE", "ingestion.hitl_mode", _as_hitl_mode, True),
    ("INGESTION_MAX_CHARS_PER_BLOCK", "ingestion.max_chars_per_block", _as_int, False),
    ("INGESTION_TEXT_IE_MODE", "ingestion.text_ie.mode", _as_str, False),
    ("INGESTION_ENTITY_MAPPING_ENABLED", "ingestion.entity_mapping.enabled", _as_bool, False),
    ("INGESTION_ENTITY_MAPPING_LLM_ADJUDICATION", "ingestion.entity_mapping.llm_adjudication", _as_bool, False),
    ("INGESTION_AUTO_RESOLUTION_CONFIDENCE_THRESHOLD", "ingestion.entity_mapping.auto_resolution_confidence_threshold", _as_confidence_threshold, False),
    ("INGESTION_SEMANTIC_GROUPING_ENABLED", "ingestion.semantic_grouping.enabled", _as_bool, False),
    ("INGESTION_LIST_GROUPING_ENABLED", "ingestion.semantic_grouping.list_grouping_enabled", _as_bool, False),
    ("INGESTION_PARAGRAPH_STREAK_ENABLED", "ingestion.semantic_grouping.paragraph_streak_enabled", _as_bool, False),
    ("INGESTION_PARAGRAPH_STREAK_MAX_BLOCKS", "ingestion.semantic_grouping.paragraph_streak_max_blocks", _as_int, False),
    ("INGESTION_PARAGRAPH_STREAK_SAME_PAGE", "ingestion.semantic_grouping.paragraph_streak_same_page", _as_bool, False),
    ("INGESTION_ALIASES_ENABLED", "ingestion.aliases.enabled", _as_bool, False),
    ("INGESTION_ALIASES_MAX_PER_ENTITY", "ingestion.aliases.max_per_entity", _as_int, False),
    ("INGESTION_REPORTS_NODE_NAMES_PER_TYPE", "ingestion.reports.node_names_per_type", _as_int, False),
    ("INGESTION_REPORTS_INCLUDE_DIAGRAM_VISION_RAW", "ingestion.reports.include_diagram_vision_raw", _as_bool, False),
    ("INGESTION_REPORTS_INCLUDE_DIAGRAM_VISION_MASKED", "ingestion.reports.include_diagram_vision_masked", _as_bool, False),
    ("INGESTION_REPORTS_INCLUDE_DIAGRAM_CAPTION_RAW", "ingestion.reports.include_diagram_caption_raw", _as_bool, False),
    ("INGESTION_REPORTS_INCLUDE_DIAGRAM_CAPTION_MASKED", "ingestion.reports.include_diagram_caption_masked", _as_bool, False),
    ("INGESTION_REPORTS_INCLUDE_DIAGRAM_ENTITIES_RELATIONSHIPS", "ingestion.reports.include_diagram_entities_relationships", _as_bool, False),

    # Observability
    ("OBSERVABILITY_INGESTION_LOGGING_ENABLED", "observability.ingestion_logging.enabled", _as_bool, False),
    ("OBSERVABILITY_INGESTION_LOGGING_LEVEL", "observability.ingestion_logging.level", _as_str, False),
    ("OBSERVABILITY_LOG_BLOCK_DEBUG", "observability.ingestion_logging.log_block_debug", _as_bool, False),
    ("OBSERVABILITY_LOG_LLM_PAYLOADS", "observability.ingestion_logging.log_llm_payloads_to_server_logs", _as_bool, False),
    ("OBSERVABILITY_MAX_PREVIEW_CHARS", "observability.ingestion_logging.max_preview_chars", _as_int, False),

    # Graph
    ("GRAPH_LEAN_MODE", "graph.lean_mode", _as_bool, False),

    # Embeddings
    ("EMBEDDINGS_MODEL_NAME", "embeddings.model_name", _as_str, False),
    ("EMBEDDINGS_LOCAL_MODEL_PATH", "embeddings.local_model_path", _as_str, False),
    ("EMBEDDINGS_DIMENSION", "embeddings.dimension", _as_int, False),
    ("EMBEDDINGS_DEVICE", "embeddings.device", _as_str, False),
    ("EMBEDDINGS_BATCH_SIZE", "embeddings.batch_size", _as_int, False),
    ("EMBEDDINGS_MAX_LENGTH", "embeddings.max_length", _as_int, False),

    # Reranking
    ("RERANKING_MODEL_NAME", "reranking.model_name", _as_str, False),
]


def _set_nested(config: Dict[str, Any], dotted_path: str, value: Any) -> None:
    """Set a value at a dotted path, creating intermediate dicts as needed."""
    keys = dotted_path.split(".")
    node = config
    for key in keys[:-1]:
        existing = node.get(key)
        if not isinstance(existing, dict):
            existing = {}
            node[key] = existing
        node = existing
    node[keys[-1]] = value


def resolve_env_file() -> Path:
    """Resolve which dotenv file to load (repo-root ``env`` / ``env.test``)."""
    explicit = (os.environ.get("KBF_ENV_FILE") or "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else REPO_ROOT / path

    env_name = (os.environ.get("KBF_ENV") or "").strip().lower()
    if env_name and env_name not in ("local", "default", "dev"):
        return REPO_ROOT / f"env.{env_name}"

    return REPO_ROOT / "env"


def load_dotenv_file() -> Path:
    """Load the selected dotenv file into os.environ (without overriding existing vars)."""
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ImportError(
            "python-dotenv is required to load configuration. "
            "Install it with: pip install python-dotenv"
        ) from exc

    env_path = resolve_env_file()
    if not env_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {env_path}. "
            f"Copy env.example to env, or use KBF_ENV=test / KBF_ENV_FILE=env.test."
        )
    load_dotenv(dotenv_path=env_path, override=False)
    # Aura credential downloads use NEO4J_USERNAME; this app expects NEO4J_USER.
    if not (os.environ.get("NEO4J_USER") or "").strip():
        aura_user = (os.environ.get("NEO4J_USERNAME") or "").strip()
        if aura_user:
            os.environ["NEO4J_USER"] = aura_user
    return env_path


def require_env(name: str, *, allow_empty: bool = False) -> str:
    """Read a required environment variable (no defaults)."""
    if name not in os.environ:
        raise ValueError(
            f"Missing required environment variable: {name}. "
            f"Set it in {resolve_env_file()} (or export it in the process environment)."
        )
    value = os.environ[name]
    if value == "" and not allow_empty:
        raise ValueError(
            f"Environment variable {name} is empty. "
            f"Provide a value in {resolve_env_file()}."
        )
    return value


def build_app_config() -> Dict[str, Any]:
    """Build the nested app config dict entirely from environment variables.

    Raises:
        FileNotFoundError: dotenv file missing
        ValueError: required variable missing/empty or type coercion fails
    """
    load_dotenv_file()
    config: Dict[str, Any] = {}
    missing: List[str] = []

    for env_var, dotted_path, caster, allow_empty in ENV_KEYS:
        if env_var not in os.environ:
            missing.append(env_var)
            continue
        raw = os.environ[env_var]
        if raw == "":
            if not allow_empty:
                missing.append(f"{env_var} (empty)")
                continue
            if caster is _as_str_list:
                _set_nested(config, dotted_path, [])
            else:
                _set_nested(config, dotted_path, "")
            continue
        try:
            _set_nested(config, dotted_path, caster(raw))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Invalid value for environment variable {env_var}={raw!r}: {exc}"
            ) from exc

    if missing:
        raise ValueError(
            "Missing required environment variables (no code defaults). "
            f"Set them in {resolve_env_file()}:\n  - " + "\n  - ".join(missing)
        )

    # Force rewrite is a global kill switch and defaults to disabled.
    force_rewrite_raw = os.environ.get("KBFABRIC_FORCE_REWRITE_ENABLED", "false")
    try:
        _set_nested(
            config, "ingestion.force_rewrite_enabled", _as_bool(force_rewrite_raw)
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "Invalid value for environment variable "
            f"KBFABRIC_FORCE_REWRITE_ENABLED={force_rewrite_raw!r}: {exc}"
        ) from exc

    # HITL policy defaults to review_ambiguous when the optional
    # environment override is absent. This preserves existing deployments
    # while making the production-safe policy explicit.
    if "ingestion" not in config:
        config["ingestion"] = {}
    config["ingestion"].setdefault("hitl_mode", "review_ambiguous")

    # Optional alias: OLLAMA_URL overrides OLLAMA_BASE_URL when non-empty
    ollama_url = (os.environ.get("OLLAMA_URL") or "").strip()
    if ollama_url:
        _set_nested(config, "ollama.base_url", ollama_url)

    # Optional alias: QIBA_AUTH_TOKEN overrides MODEL_ROUTER_AUTH_TOKEN when non-empty
    qiba_token = (os.environ.get("QIBA_AUTH_TOKEN") or "").strip()
    if qiba_token:
        _set_nested(config, "model_router.auth_token", qiba_token)

    return config


# Back-compat name used by older call sites (now builds from env, does not patch YAML).
def apply_env_overrides(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deprecated: returns env-built config. Ignores any passed YAML config."""
    return build_app_config()
