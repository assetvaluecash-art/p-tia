"""
Unit tests for KV cache optimization functionality.
"""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

from kbfabric_ingest._common.chat_client import ChatLLMClient
from kbfabric_ingest.llm.prompts import get_system_prompt, build_ie_user_content


class TestChatLLMClient:
    """Test the ChatLLMClient with KV cache optimization."""

    @pytest.fixture
    def chat_client(self):
        """Create a chat client for testing."""
        return ChatLLMClient(
            base_url="http://test:11434",
            model_name="test-model",
            timeout_sec=30
        )

    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_chat_api_success(self, mock_post, chat_client):
        """Test successful /api/chat call."""
        # Mock successful chat API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": "Test response"}
        }
        mock_post.return_value = mock_response

        response_text, metadata = chat_client._call_chat_api(
            messages=[
                {"role": "system", "content": "You are helpful"},
                {"role": "user", "content": "Hello"}
            ],
            temperature=0.0,
            num_ctx=8192
        )

        assert response_text == "Test response"
        assert metadata['endpoint'] == '/api/chat'
        assert metadata['status_code'] == 200
        assert metadata['success'] is True
        assert 'duration_ms' in metadata

    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_chat_api_fallback_to_generate(self, mock_post, chat_client):
        """Test fallback from chat API to generate API."""
        # Mock chat API failure (404 - not supported)
        mock_chat_response = Mock()
        mock_chat_response.status_code = 404
        mock_chat_response.text = "Not Found"

        # Mock generate API success
        mock_generate_response = Mock()
        mock_generate_response.status_code = 200
        mock_generate_response.json.return_value = {
            "response": "Fallback response"
        }

        # Configure mock to return chat failure first, then generate success
        mock_post.side_effect = [
            Mock(raise_for_status=lambda: (_ for _ in ()).throw(
                Exception("404 Client Error: Not Found")
            )),
            mock_generate_response
        ]

        response_text, metadata = chat_client.call_with_system_user(
            system_prompt="You are helpful",
            user_content="Hello",
            temperature=0.0
        )

        assert response_text == "Fallback response"
        assert metadata['api_used'] == 'generate'
        assert metadata['kv_cache_optimized'] is False
        assert 'fallback_reason' in metadata

    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_call_with_system_user_chat_success(self, mock_post, chat_client):
        """Test call_with_system_user with successful chat API."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": "Chat response"}
        }
        mock_post.return_value = mock_response

        response_text, metadata = chat_client.call_with_system_user(
            system_prompt="System prompt",
            user_content="User content",
            temperature=0.5
        )

        assert response_text == "Chat response"
        assert metadata['api_used'] == 'chat'
        assert metadata['kv_cache_optimized'] is True
        assert metadata['system_prompt_length'] == len("System prompt")
        assert metadata['user_content_length'] == len("User content")


class TestPromptEquivalence:
    """Test that prompts are equivalent between old and new approaches."""

    def test_system_prompt_extraction(self):
        """Test that system prompts are correctly extracted."""
        system_prompt = get_system_prompt('ie_text')
        assert "You are an information extraction system" in system_prompt
        assert "{ontology_context}" in system_prompt  # Template variables should be present

    def test_user_content_building(self):
        """Test user content building for IE."""
        blocks = [
            ("Block 1 text", "domain1"),
            ("Block 2 text", "domain2")
        ]

        user_content = build_ie_user_content(blocks)
        assert "Block 1 (domain: domain1):" in user_content
        assert "Block 2 (domain: domain2):" in user_content
        assert "Block 1 text" in user_content
        assert "Block 2 text" in user_content
        assert "Return only the JSON array" in user_content

    def test_prompt_reconstruction(self):
        """Test that system + user reconstructs the original prompt structure."""
        system_prompt = get_system_prompt('ie_text')
        # Remove template variables for testing
        system_prompt = system_prompt.replace("{ontology_context}", "Test ontology")

        blocks = [("Test block", "test_domain")]
        user_content = build_ie_user_content(blocks)

        # The reconstructed prompt should contain all elements
        reconstructed = f"{system_prompt}\n\n{user_content}"

        assert "You are an information extraction system" in reconstructed
        assert "Test ontology" in reconstructed
        assert "Block 1 (domain: test_domain):" in reconstructed
        assert "Test block" in reconstructed


class TestPerformanceMetrics:
    """Test performance monitoring and metrics."""

    @pytest.fixture
    def chat_client(self):
        """Create a chat client for testing."""
        return ChatLLMClient(
            base_url="http://test:11434",
            model_name="test-model",
            timeout_sec=30
        )

    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_performance_metadata(self, mock_post, chat_client):
        """Test that performance metadata is correctly captured."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": "Response"}
        }
        mock_post.return_value = mock_response

        response_text, metadata = chat_client.call_with_system_user(
            system_prompt="Short system prompt",
            user_content="Short user content",
            temperature=0.0
        )

        # Check that all expected metadata fields are present
        required_fields = [
            'api_used', 'kv_cache_optimized', 'system_prompt_length',
            'user_content_length', 'total_tokens_estimate', 'duration_ms',
            'request_timestamp', 'response_timestamp', 'status_code', 'success'
        ]

        for field in required_fields:
            assert field in metadata, f"Missing metadata field: {field}"

        # Check token estimates
        assert metadata['total_tokens_estimate'] > 0
        assert metadata['system_prompt_length'] == len("Short system prompt")
        assert metadata['user_content_length'] == len("Short user content")

    def test_performance_gain_calculation(self):
        """Test performance gain estimation logic."""
        from kbfabric_ingest.llm.client import LLMClient

        client = LLMClient(
            base_url="http://test:11434",
            text_model="test-model",
            vision_model="vision-model"
        )

        # Test various scenarios
        test_cases = [
            (1000, 100),  # 1s actual, 100ms estimated gain
            (5000, 500),   # 5s actual, 500ms estimated gain
            (0, 0),       # Edge case
        ]

        for actual_ms, total_chars in test_cases:
            gain = client._calculate_performance_gain(actual_ms, total_chars)
            assert isinstance(gain, float)
            assert gain >= 0.0


class TestHealthCheck:
    """Test health check functionality."""

    @pytest.fixture
    def chat_client(self):
        """Create a chat client for testing."""
        return ChatLLMClient(
            base_url="http://test:11434",
            model_name="test-model",
            timeout_sec=30
        )

    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_health_check_success(self, mock_post, chat_client):
        """Test successful health check."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": {"content": "OK"}
        }
        mock_post.return_value = mock_response

        health = chat_client.health_check()

        assert health['healthy'] is True
        assert health['model'] == "test-model"
        assert 'response_time_ms' in health
        assert 'kv_cache_supported' in health

    @patch('kbfabric_ingest._common.chat_client.requests.post')
    def test_health_check_failure(self, mock_post, chat_client):
        """Test health check failure."""
        mock_post.side_effect = Exception("Connection failed")

        health = chat_client.health_check()

        assert health['healthy'] is False
        assert health['model'] == "test-model"
        assert 'error' in health
        assert 'error_type' in health


if __name__ == "__main__":
    pytest.main([__file__])
