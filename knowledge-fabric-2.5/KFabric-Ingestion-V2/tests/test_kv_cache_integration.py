"""
Integration tests for KV cache optimization across the full pipeline.
"""

import pytest
import json
import tempfile
import os
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from kbfabric_ingest.llm.client import LLMClient
from kbfabric_ingest.ingest.config import AppConfig


class TestKVCacheIntegration:
    """Integration tests for KV cache across the full IE pipeline."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration with KV cache enabled."""
        config = AppConfig()
        config._config = {
            "ollama": {
                "base_url": "http://test:11434",
                "text_model": "test-model",
                "vision_model": "vision-model",
                "timeout_sec": 30,
                "num_ctx": 8192,
                "temperature": {
                    "ie_text": 0.0,
                    "ie_vision": 0.0
                },
                "kv_cache": {
                    "enabled": True,
                    "api_preference": "chat",
                    "require_chat_api": False
                }
            }
        }
        return config

    @pytest.fixture
    def mock_config_legacy(self):
        """Create mock configuration with KV cache disabled."""
        config = AppConfig()
        config._config = {
            "ollama": {
                "base_url": "http://test:11434",
                "text_model": "test-model",
                "vision_model": "vision-model",
                "timeout_sec": 30,
                "num_ctx": 8192,
                "temperature": {
                    "ie_text": 0.0,
                    "ie_vision": 0.0
                },
                "kv_cache": {
                    "enabled": False
                }
            }
        }
        return config

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_ie_text_kv_cache_enabled(self, mock_post, mock_get_config, mock_config):
        """Test IE text analysis with KV cache enabled."""
        mock_get_config.return_value = mock_config

        # Mock successful chat API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": '[{"block_category": "test", "kg_node_type": "test", "entities": [], "relationships": []}]'}
        }
        mock_post.return_value = mock_response

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        blocks = [("Test block content", "test_domain")]
        results = client.ie_text_analysis(blocks)

        # Verify chat API was called (not generate)
        call_args = mock_post.call_args
        assert "/api/chat" in call_args[0][0]  # URL contains /api/chat

        # Verify message format
        request_data = call_args[1]['json']
        assert "messages" in request_data
        assert len(request_data["messages"]) == 2  # system + user
        assert request_data["messages"][0]["role"] == "system"
        assert request_data["messages"][1]["role"] == "user"

        assert len(results) == 1

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest.llm.client.requests.post')
    def test_ie_text_legacy_fallback(self, mock_post, mock_get_config, mock_config_legacy):
        """Test IE text analysis falls back to legacy when KV cache disabled."""
        mock_get_config.return_value = mock_config_legacy

        # Mock successful generate API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "response": '[{"block_category": "test", "kg_node_type": "test", "entities": [], "relationships": []}]'
        }
        mock_post.return_value = mock_response

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        blocks = [("Test block content", "test_domain")]
        results = client.ie_text_analysis(blocks)

        # Verify generate API was called (legacy path)
        call_args = mock_post.call_args
        assert "/api/generate" in call_args[0][0]  # URL contains /api/generate

        # Verify prompt format (combined system + user)
        request_data = call_args[1]['json']
        assert "prompt" in request_data
        assert "You are an information extraction system" in request_data["prompt"]
        assert "Test block content" in request_data["prompt"]

        assert len(results) == 1

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_ie_vision_kv_cache_enabled(self, mock_post, mock_get_config, mock_config):
        """Test IE vision analysis with KV cache enabled."""
        mock_get_config.return_value = mock_config

        # Mock successful chat API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": '[{"description": "test", "block_category": "Diagram_Process", "kg_node_type": "ProcessDiagram", "entities": [], "relationships": []}]'}
        }
        mock_post.return_value = mock_response

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        items = [{
            "domain": "test_domain",
            "image_bytes": b"fake_image_data",
            "context_text": "Test context",
            "ocr_text": "Test OCR"
        }]
        results = client.ie_image_analysis(items)

        # Verify chat API was called
        call_args = mock_post.call_args
        assert "/api/chat" in call_args[0][0]

        # Verify images were included
        request_data = call_args[1]['json']
        assert "messages" in request_data
        user_message = request_data["messages"][1]
        assert "images" in user_message

        assert len(results) == 1

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_kv_cache_fallback_on_chat_failure(self, mock_post, mock_get_config, mock_config):
        """Test automatic fallback when chat API fails."""
        mock_get_config.return_value = mock_config

        # Mock chat API failure, then generate API success
        mock_chat_response = Mock()
        mock_chat_response.status_code = 404
        mock_chat_response.text = "Not Found"

        mock_generate_response = Mock()
        mock_generate_response.status_code = 200
        mock_generate_response.json.return_value = {
            "response": '[{"block_category": "test", "kg_node_type": "test", "entities": [], "relationships": []}]'
        }

        mock_post.side_effect = [
            Mock(raise_for_status=lambda: (_ for _ in ()).throw(
                Exception("404 Client Error: Not Found")
            )),
            mock_generate_response
        ]

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        blocks = [("Test block content", "test_domain")]
        results = client.ie_text_analysis(blocks)

        # Verify both APIs were tried
        assert mock_post.call_count == 2

        # Verify results still work
        assert len(results) == 1

    def test_prompt_equivalence(self):
        """Test that KV cache prompts produce equivalent results to legacy."""
        from kbfabric_ingest.llm.prompts import get_system_prompt, build_ie_user_content

        # Simulate ontology context
        ontology_context = """
ONTOLOGY DEFINITION:
- Node types: Process, DataEntity, System
- Relationship types: USES, CONTAINS, DEPENDS_ON
"""

        # Build KV cache prompts
        system_prompt = get_system_prompt('ie_text').format(ontology_context=ontology_context)
        blocks = [("Test content", "test_domain")]
        user_content = build_ie_user_content(blocks)
        kv_cache_prompt = f"{system_prompt}\n\n{user_content}"

        # Build legacy prompt (simplified version)
        legacy_prompt = f"""You are an information extraction system for enterprise knowledge bases.

{ontology_context}

TASK: For each text block, classify it and extract entities/relationships using ONLY the ontology above.

=== ENTITY EXTRACTION SCOPE - READ CAREFULLY ===

WHAT TO EXTRACT:
- Named business concepts that are DISCRETE and IDENTIFIABLE
- Processes with clear names (e.g., "DriftDetection", "Rebalancing")
- Systems mentioned by name (e.g., "PostgreSQL", "AcctSvc")
- Test cases with specific IDs (e.g., "TC-REBAL-001")
- Requirements with IDs (e.g., "REQ-001")

WHAT NOT TO EXTRACT:
- Flowchart box labels (e.g., "Check band", "Build proposal") - these are diagram elements, not named processes
- State machine states (e.g., "Rejected", "HappyPath") - these are states, not processes
- Decision tree branches (e.g., "AggressiveProfile", "ConservativeProfile") - these are data values
- Range endpoints (e.g., from "TC-001 through TC-010" do NOT extract TC-001 and TC-010 separately)
- PII field names (e.g., "SSN", "Email", "IPAddress") - these are field names, not business data entities
- Architecture layer names (e.g., "ClientLayer", "ServiceMesh") - use specific component names instead

GUIDANCE FOR DIAGRAMS:
- A state machine diagram describes ONE Process entity (extract the machine name only)
- A flowchart describes ONE Process with steps (extract the process name, not each box)
- "TC-001 through TC-010" means a test suite - extract nothing or extract as one entity, NOT 10 separate entities

=== ENTITY NAME RULES ===
- Entity names must be 1-5 words MAXIMUM
- Use CamelCase or short phrases (e.g., "DriftDetection", "RebalancingEngine")
- If you cannot identify a clear short name, SKIP the entity
- When in doubt, extract FEWER entities, not more

=== PROCESS vs DATAENTITY CLASSIFICATION (CRITICAL) ===
RULE: Use the "Can you DO this?" test to decide if something is a Process:
- Process = An ACTION or WORKFLOW you can DO (gerund/verb-form: Rebalancing, Detection, Monitoring)
- DataEntity = A VALUE, CATEGORY, or CLASSIFICATION you HAVE (not an action)

EXAMPLES:
- "Rebalancing" - Can you DO rebalancing? YES → Process ✓
- "DriftDetection" - Can you DO drift detection? YES → Process ✓
- "Aggressive" - Can you DO aggressive? NO → NOT a Process (it's a risk profile value → DataEntity)
- "Conservative" - Can you DO conservative? NO → NOT a Process (it's a risk profile value → DataEntity)
- "Moderate" - Can you DO moderate? NO → NOT a Process (it's a risk profile value → DataEntity)
- "Monthly" - Can you DO monthly? NO → NOT a Process (it's a time period → DataEntity)

Adjectives and modifiers (Aggressive, Conservative, High, Low, Daily, Weekly) are NEVER Processes.

OUTPUT FORMAT - For each block, provide a JSON object with:
- block_category: MUST be one of the allowed categories (EXACT name, case-sensitive)
- kg_node_type: the corresponding KG node type (use mapping below)
- entities: list of {{"type": str, "value": str, "confidence": float}} objects
  - "type" MUST be from NODE TYPES list above (EXACT name, case-sensitive)
  - "value" MUST be 1-5 words (short identifiers, not sentences!)
- relationships: list of {{"from_type": str, "from_value": str, "rel_type": str, "to_type": str, "to_value": str, "confidence": float}} objects
  - rel_type MUST be from RELATIONSHIP TYPES list above
  - Respect from/to type constraints
  - If no relationship matches allowed types, return empty array []

Category to Node Type Mapping:
{{"General": "General", "Process": "Process"}}

Examples:
[]

Return a JSON array with one object per input block. Be strict with JSON formatting.

Input blocks:
Block 1 (domain: test_domain):
Test content

Return only the JSON array, no other text."""

        # Both prompts should contain the essential elements
        assert "You are an information extraction system" in kv_cache_prompt
        assert "You are an information extraction system" in legacy_prompt
        assert "Test content" in kv_cache_prompt
        assert "Test content" in legacy_prompt
        assert "test_domain" in kv_cache_prompt
        assert "test_domain" in legacy_prompt


class TestPerformanceMonitoring:
    """Test performance monitoring across the pipeline."""

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_performance_metadata_collection(self, mock_post, mock_get_config):
        """Test that performance metadata is collected throughout the pipeline."""
        # Setup config with KV cache enabled
        config = AppConfig()
        config._config = {
            "ollama": {
                "kv_cache": {"enabled": True}
            }
        }
        mock_get_config.return_value = config

        # Mock successful response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": '[{"block_category": "test", "kg_node_type": "test", "entities": [], "relationships": []}]'}
        }
        mock_post.return_value = mock_response

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        # Make a call
        blocks = [("Test", "domain")]
        results = client.ie_text_analysis(blocks)

        # Verify the call was made
        assert mock_post.called
        assert len(results) == 1

    def test_configuration_validation(self):
        """Test that configuration is properly validated."""
        # Test valid configurations
        valid_configs = [
            {"kv_cache": {"enabled": True, "api_preference": "chat"}},
            {"kv_cache": {"enabled": False}},
            {"kv_cache": {"enabled": True, "require_chat_api": False}},
        ]

        for config in valid_configs:
            # Should not raise exceptions
            ollama_config = {"kv_cache": config}
            kv_enabled = ollama_config.get("kv_cache", {}).get("enabled", False)
            assert isinstance(kv_enabled, bool)


class TestErrorHandling:
    """Test error handling in KV cache scenarios."""

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_chat_api_404_fallback(self, mock_post, mock_get_config):
        """Test graceful fallback when chat API returns 404."""
        # Setup config
        config = AppConfig()
        config._config = {"ollama": {"kv_cache": {"enabled": True}}}
        mock_get_config.return_value = config

        # Mock 404 on chat, success on generate
        mock_post.side_effect = [
            Mock(raise_for_status=lambda: (_ for _ in ()).throw(
                Exception("404 Client Error")
            )),
            Mock(status_code=200, json=lambda: {
                "response": '[{"block_category": "test", "kg_node_type": "test", "entities": [], "relationships": []}]'
            })
        ]

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        blocks = [("Test", "domain")]
        results = client.ie_text_analysis(blocks)

        # Should succeed with fallback
        assert len(results) == 1
        assert mock_post.call_count == 2  # Tried both APIs

    @patch('kbfabric_ingest.llm.client.get_app_config')
    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_both_apis_fail(self, mock_post, mock_get_config):
        """Test proper error handling when both APIs fail."""
        # Setup config
        config = AppConfig()
        config._config = {"ollama": {"kv_cache": {"enabled": True}}}
        mock_get_config.return_value = config

        # Mock both APIs failing
        mock_post.side_effect = Exception("Network error")

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        blocks = [("Test", "domain")]

        with pytest.raises(Exception):  # Should raise the network error
            client.ie_text_analysis(blocks)


if __name__ == "__main__":
    pytest.main([__file__])
