"""PII masking service using Presidio with optional Faker-based pseudonymization."""

import hashlib
import logging
import time
import re
from typing import Any

from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

from kbfabric_ingest._common.telemetry import record_pii_detection, record_pii_masking
from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.utils.logger import get_logger

# Faker import with graceful fallback
try:
    from faker import Faker
    FAKER_AVAILABLE = True
except ImportError:
    FAKER_AVAILABLE = False
    Faker = None

logger = get_logger(__name__)

_ARTIFACT_ID_RE = re.compile(
    r"\b(?:DEF|INC|TC|REQ|CTRL|RULE|AC|JIRA)"
    r"-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b"
)


def _artifact_id_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of all artifact ID matches in *text*."""
    return [(m.start(), m.end()) for m in _ARTIFACT_ID_RE.finditer(text)]


def _span_overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    """Return True if [start, end) overlaps with any span in *spans*."""
    return any(start < s_end and s_start < end for s_start, s_end in spans)


class PIIService:
    """Service for detecting and masking PII in text.
    
    Supports two modes:
    1. Token mode (default): Replace PII with tokens like [PER-001]
    2. Faker mode: Replace PII with realistic fake data for AI governance
    
    Both modes maintain an audit trail mapping original → replacement.
    """

    def __init__(self, config: dict[str, Any]):
        """
        Initialize PII service.
        
        Args:
            config: PII configuration dictionary
        """
        self.config = config
        self.logger = get_logger(__name__)
        
        # Reduce Presidio noise (it logs many non-actionable registry warnings at INFO/WARNING)
        logging.getLogger("presidio_analyzer").setLevel(logging.ERROR)
        logging.getLogger("presidio_anonymizer").setLevel(logging.ERROR)

        # Configure tldextract to use cached data only (no external HTTP requests)
        # This prevents SSL errors behind corporate firewalls
        # tldextract is used by Presidio's EmailRecognizer and UrlRecognizer
        try:
            import tldextract
            # Disable external URL fetching - use only cached/snapshot data
            # This prevents SSL certificate errors when behind corporate firewalls
            tldextract.extract = tldextract.TLDExtract(suffix_list_urls=())
            self.logger.debug("Configured tldextract to use cached data only (no external requests)")
        except Exception as e:
            # Non-critical - tldextract will fall back to snapshot data
            self.logger.warning(f"Failed to configure tldextract (non-critical): {e}")

        # Initialize Presidio with local models only (no network downloads).
        # Explicitly load the spaCy NLP engine so Presidio never tries to
        # download models, avoiding SSL failures behind corporate proxies.
        try:
            from presidio_analyzer.nlp_engine import SpacyNlpEngine
            nlp_engine = SpacyNlpEngine(
                models=[{"lang_code": "en", "model_name": "en_core_web_lg"}]
            )
            nlp_engine.load()
            self.analyzer = AnalyzerEngine(
                supported_languages=["en"],
                nlp_engine=nlp_engine,
            )
            self.anonymizer = AnonymizerEngine()
            self.logger.info("PII engines initialized (offline mode, no downloads)")
        except Exception as e:
            self.logger.error(f"Failed to initialize PII engines: {e}")
            raise RuntimeError(
                "PII initialization failed. Ensure 'en_core_web_lg' is installed: "
                "pip install en_core_web_lg"
            ) from e
        
        # Token counters for format-preserving masking (e.g., [PER-001], [PER-002])
        # Reset per document to keep tokens manageable
        self.token_counters: dict[str, int] = {}
        
        # Mapping of token IDs/fake values to original values (for audit/unmask capability)
        self.token_mapping: dict[str, str] = {}
        
        # Faker mode configuration
        self.use_faker = config.get("use_faker", False)
        self._faker_instance: Any = None  # Lazy initialization
        self._faker_cache: dict[str, str] = {}  # Cache for consistent replacements
        
        if self.use_faker and not FAKER_AVAILABLE:
            self.logger.warning(
                "use_faker=True but Faker library not installed. "
                "Falling back to token mode. Install with: pip install Faker"
            )
            self.use_faker = False
        
        # Entities to detect - read from config for SME editability
        # Falls back to default list if not configured
        default_entities = [
            "PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS", "CREDIT_CARD",
            "IBAN_CODE", "IP_ADDRESS", "US_SSN", "US_BANK_NUMBER",
            "LOCATION", "DATE_TIME", "US_DRIVER_LICENSE", "US_PASSPORT"
        ]
        self.entities = config.get("entities", default_entities)
        
        # Engine: currently only "presidio" is used; "regex" etc. reserved for future
        self._engine = config.get("engine", "presidio")
        if self._engine != "presidio":
            self.logger.info("PII engine=%s; only 'presidio' is active, other values reserved", self._engine)
        
        # Mask policy: "mask" = redact + faker (dual output); "redact_only" = redacted for both outputs
        self._mask_policy = config.get("mask_policy", "mask")
        if self._mask_policy not in ("mask", "redact_only"):
            self.logger.warning("PII mask_policy=%s; using 'mask'. Valid: mask, redact_only", self._mask_policy)
            self._mask_policy = "mask"
        
        # Short prefixes for token IDs (3 chars for readability)
        # Maps entity type to a 3-letter prefix for token generation
        self.entity_prefix = {
            # Existing entities
            "PERSON": "PER",
            "PHONE_NUMBER": "PHN",
            "EMAIL_ADDRESS": "EML",
            "CREDIT_CARD": "CRD",
            "IBAN_CODE": "IBN",
            "IP_ADDRESS": "IPA",
            # New banking-focused entities
            "US_SSN": "SSN",
            "US_BANK_NUMBER": "ACT",
            "LOCATION": "ADR",
            "DATE_TIME": "DTE",
            "US_DRIVER_LICENSE": "DLN",
            "US_PASSPORT": "PSP",
        }
    
    def _get_faker(self) -> Any:
        """Get or create a seeded Faker instance for consistent replacements (always available for dual-mode PII)."""
        if self._faker_instance is None and FAKER_AVAILABLE:
            # Use a fixed seed for reproducibility within a document
            # Seed can be overridden via config
            seed = self.config.get("faker_seed", 42)
            self._faker_instance = Faker()
            Faker.seed(seed)
        elif self._faker_instance is None and not FAKER_AVAILABLE:
            # Log warning if Faker not available (required for dual-mode PII)
            self.logger.warning("Faker library not available - install with: pip install Faker")
        return self._faker_instance
    
    def _apply_format_pattern(self, original: str, fake_raw: str) -> str:
        """
        Apply the format pattern from original to the fake value.
        
        Preserves positions of hyphens, spaces, and other separators.
        Example: "3782-822463-10005" pattern applied to "4948389577535424" -> "4948-389577-535424"
        
        Args:
            original: Original text with format (e.g., "3782-822463-10005")
            fake_raw: Raw fake value without format (e.g., "4948389577535424")
            
        Returns:
            Formatted fake value matching original pattern
        """
        import re
        
        # Extract separator positions and types from original
        separators = []
        for i, char in enumerate(original):
            if not char.isalnum():
                separators.append((i, char))
        
        if not separators:
            # No format to preserve
            return fake_raw
        
        # Remove non-alphanumeric from fake_raw for clean digits/chars
        fake_clean = re.sub(r'[^a-zA-Z0-9]', '', fake_raw)
        
        # Insert separators at corresponding positions
        result = list(fake_clean)
        offset = 0
        for pos, sep in separators:
            adjusted_pos = pos + offset - offset  # Position in clean string
            if adjusted_pos < len(result):
                result.insert(adjusted_pos, sep)
            elif adjusted_pos == len(result):
                result.append(sep)
        
        return ''.join(result[:len(original)])  # Truncate to original length
    
    def _generate_fake_value(self, entity_type: str, original_text: str) -> str:
        """
        Generate a realistic fake value for a PII entity using Faker.
        
        Uses a hash of the original text as seed to ensure consistent
        replacement across multiple occurrences (same input → same output).
        
        Preserves format patterns (hyphens, spaces) from the original text.
        
        Args:
            entity_type: The Presidio entity type (e.g., "PERSON")
            original_text: The original detected text
            
        Returns:
            Realistic fake value matching the entity type and format
        """
        # Check cache first for consistency
        cache_key = f"{entity_type}:{original_text}"
        if cache_key in self._faker_cache:
            return self._faker_cache[cache_key]
        
        faker = self._get_faker()
        if not faker:
            # Fallback to token if Faker not available
            return self._generate_token_id(entity_type, original_text)
        
        # Use hash of original text to seed for consistent replacement
        text_hash = int(hashlib.md5(original_text.encode()).hexdigest()[:8], 16)
        faker.seed_instance(text_hash)
        
        # Map entity types to Faker methods
        fake_generators = {
            "PERSON": lambda: faker.name(),
            "PHONE_NUMBER": lambda: faker.phone_number(),
            "EMAIL_ADDRESS": lambda: faker.email(),
            "CREDIT_CARD": lambda: faker.credit_card_number(),
            "IBAN_CODE": lambda: faker.iban(),
            "IP_ADDRESS": lambda: faker.ipv4(),
            "US_SSN": lambda: faker.ssn(),
            "US_BANK_NUMBER": lambda: faker.bban(),
            "LOCATION": lambda: faker.address().replace('\n', ', '),
            "DATE_TIME": lambda: faker.date_of_birth(minimum_age=18, maximum_age=80).strftime("%Y-%m-%d"),
            "US_DRIVER_LICENSE": lambda: f"DL{faker.random_number(digits=9, fix_len=True)}",
            "US_PASSPORT": lambda: f"P{faker.random_number(digits=8, fix_len=True)}",
        }
        
        generator = fake_generators.get(entity_type)
        if generator:
            try:
                fake_raw = generator()
                
                # Apply format preservation for types with common separators
                format_types = {"CREDIT_CARD", "IBAN_CODE", "PHONE_NUMBER", "US_SSN"}
                if entity_type in format_types:
                    fake_value = self._apply_format_pattern(original_text, fake_raw)
                else:
                    fake_value = fake_raw
            except Exception as e:
                self.logger.warning(f"Faker generation failed for {entity_type}: {e}")
                fake_value = self._generate_token_id(entity_type, original_text)
        else:
            # Fallback to token for unknown types
            fake_value = self._generate_token_id(entity_type, original_text)
        
        # Cache for consistency and store mapping for audit
        self._faker_cache[cache_key] = fake_value
        self.token_mapping[fake_value] = original_text
        
        return fake_value

    def _regex_scan(self, text: str) -> list[dict[str, Any]]:
        """Fallback regex scan for PII patterns that Presidio may miss."""
        patterns: list[tuple[str, str]] = [
            ("EMAIL_ADDRESS", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            ("PHONE_NUMBER", r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b"),
            ("US_SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
            ("CREDIT_CARD", r"\b(?:\d[ -]*?){13,19}\b"),
            ("IP_ADDRESS", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
            ("IBAN_CODE", r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
        ]
        spans: list[dict[str, Any]] = []
        for entity_type, pattern in patterns:
            if entity_type not in self.entities:
                continue
            for match in re.finditer(pattern, text):
                spans.append({
                    "entity_type": entity_type,
                    "start": match.start(),
                    "end": match.end(),
                    "text": match.group(0),
                    "detector": "regex"
                })
        return spans
    
    def reset_token_counters(self) -> None:
        """Reset token counters for a new document. Call before processing each document."""
        self.token_counters = {}
        self.token_mapping = {}
        self._faker_cache = {}  # Also reset Faker cache for new document
    
    def _generate_token_id(self, entity_type: str, original_text: str) -> str:
        """
        Generate a format-preserving token ID for a PII entity.
        
        Example: "John Smith" -> "[PER-001]"
        
        Args:
            entity_type: The Presidio entity type (e.g., "PERSON")
            original_text: The original detected text
            
        Returns:
            Token ID string like "[PER-001]"
        """
        prefix = self.entity_prefix.get(entity_type, entity_type[:3].upper())
        
        # Check if we've seen this exact text before (dedup)
        for token_id, orig in self.token_mapping.items():
            if orig == original_text:
                return token_id
        
        # Increment counter for this entity type
        count = self.token_counters.get(entity_type, 0) + 1
        self.token_counters[entity_type] = count
        
        # Generate token ID
        token_id = f"[{prefix}-{count:03d}]"
        
        # Store mapping for audit trail
        self.token_mapping[token_id] = original_text
        
        return token_id

    @classmethod
    def from_config(cls) -> "PIIService":
        """
        Create PIIService from application config.

        Returns:
            PIIService instance
        """
        import warnings

        # Suppress ResourceWarnings from Presidio during initialization
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ResourceWarning, module="presidio_analyzer.*")

            config = get_app_config()
            pii_config = config.get("pii", {})
            return cls(pii_config)

    def mask_text(self, text: str, domain: str = "unknown") -> tuple[str, str, list[dict[str, Any]]]:
        """
        Mask PII in text and return both redacted and faker versions.
        
        Enterprise strategy:
        - Redacted version: Uses [REDACTED_EMAIL], [REDACTED_PERSON] etc. for audit logs
        - Faker version: Uses realistic fake data (e.g., john.smith@example.com) for retrieval/LLM
        
        Args:
            text: Input text
            domain: Domain name for telemetry
            
        Returns:
            Tuple of (text_redacted, text_faker, pii_spans)
            - text_redacted: Text with [REDACTED_TYPE] placeholders
            - text_faker: Text with realistic faker replacements
            - pii_spans: List of dicts with keys: entity_type, start, end, text, redacted, faker
        """
        if not text:
            return "", "", []
        
        start_time = time.time()
        
        # Get confidence threshold from config (quarantine_threshold)
        # Default Presidio threshold is 0.5, but we allow config override
        score_threshold = self.config.get("quarantine_threshold", 0.5)
        # If quarantine_threshold is 0.0, use Presidio default (0.5)
        if score_threshold == 0.0:
            score_threshold = 0.5
        
        # Analyze text for PII
        # Presidio's analyze() method filters results by score_threshold automatically
        try:
            results = self.analyzer.analyze(
                text=text,
                entities=self.entities,
                language="en",
                score_threshold=score_threshold
            )
        except Exception as e:
            # Fail-closed: never return raw text on analyzer failures
            self.logger.error("PII analyzer failure; returning redacted text", error=str(e))
            return "[REDACTED_TEXT_UNAVAILABLE]", "[REDACTED_TEXT_UNAVAILABLE]", []
        
        # Post-filter: remove results that overlap with known artifact ID patterns
        # (e.g. DEF-2024-0567, INC-LOAN-089) which are not real PII
        artifact_spans = _artifact_id_spans(text)
        if artifact_spans:
            pre_count = len(results)
            results = [
                r for r in results
                if not _span_overlaps_any(r.start, r.end, artifact_spans)
            ]
            removed = pre_count - len(results)
            if removed:
                self.logger.info(
                    "Excluded PII detections overlapping artifact IDs",
                    removed_count=removed,
                    artifact_id_count=len(artifact_spans),
                )
        
        # Build replacement spans with BOTH redacted and faker versions
        pii_spans = []
        
        for result in results:
            entity_type = result.entity_type
            start = result.start
            end = result.end
            detected_text = text[start:end]
            
            # Generate redacted replacement (always)
            redacted = f"[REDACTED_{entity_type}]"
            # Faker replacement: use redacted when mask_policy is redact_only
            if self._mask_policy == "redact_only":
                faker_value = redacted
            else:
                faker_value = self._generate_fake_value(entity_type, detected_text)
            
            # Record span with BOTH replacements for traceability
            pii_spans.append({
                "entity_type": entity_type,
                "start": start,
                "end": end,
                "text": detected_text,
                "redacted": redacted,  # Redacted version for audit
                "faker": faker_value,  # Faker or redacted (when redact_only)
                "mode": "redact_only" if self._mask_policy == "redact_only" else "dual",
                "detector": self._engine if self._engine == "presidio" else "presidio"
            })

        # Fallback regex scan to catch common patterns missed by Presidio
        regex_spans = self._regex_scan(text)
        if regex_spans:
            def _overlaps(existing: list[dict[str, Any]], start: int, end: int) -> bool:
                return any(not (end <= s["start"] or start >= s["end"]) for s in existing)

            for span in regex_spans:
                if _overlaps(pii_spans, span["start"], span["end"]):
                    continue
                if artifact_spans and _span_overlaps_any(span["start"], span["end"], artifact_spans):
                    continue
                entity_type = span["entity_type"]
                detected_text = span["text"]
                redacted = f"[REDACTED_{entity_type}]"
                faker_value = redacted if self._mask_policy == "redact_only" else self._generate_fake_value(entity_type, detected_text)
                pii_spans.append({
                    "entity_type": entity_type,
                    "start": span["start"],
                    "end": span["end"],
                    "text": detected_text,
                    "redacted": redacted,
                    "faker": faker_value,
                    "mode": "redact_only" if self._mask_policy == "redact_only" else "dual",
                    "detector": "regex"
                })
        
        # Apply replacements manually (from end to start to preserve positions)
        # Generate BOTH versions
        text_redacted = text
        text_faker = text
        
        if pii_spans:
            # Sort by start position descending to replace from end first
            sorted_spans = sorted(pii_spans, key=lambda x: x["start"], reverse=True)
            
            for span in sorted_spans:
                # Apply redacted version
                text_redacted = text_redacted[:span["start"]] + span["redacted"] + text_redacted[span["end"]:]
                # Apply faker version
                text_faker = text_faker[:span["start"]] + span["faker"] + text_faker[span["end"]:]
        
        # Record telemetry
        duration = time.time() - start_time
        record_pii_masking(domain, duration)
        
        # Record PII detections by type
        for span in pii_spans:
            record_pii_detection(domain, span["entity_type"])
        
        return text_redacted, text_faker, pii_spans

    def get_audit_mapping(self) -> dict[str, str]:
        """
        Get the complete audit mapping of replacements to original values.
        
        This is essential for AI governance - enables unmasking for
        authorized personnel and audit compliance.
        
        Returns:
            Dictionary mapping replacement values to original PII values
        """
        return self.token_mapping.copy()

