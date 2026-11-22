"""
KV cache performance monitoring and metrics collection.
"""

import time
import json
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from collections import defaultdict, deque
import threading

from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


class KVCacheMetrics:
    """
    Collect and analyze KV cache performance metrics.

    Tracks:
    - API usage patterns (chat vs generate)
    - Performance improvements
    - Cache hit rates
    - Error rates
    - Token usage estimates
    """

    def __init__(self, max_history: int = 1000):
        """
        Initialize metrics collector.

        Args:
            max_history: Maximum number of calls to keep in history
        """
        self.max_history = max_history
        self.history = deque(maxlen=max_history)
        self.summary_stats = defaultdict(int)
        self.lock = threading.RLock()
        self.logger = get_logger(f"{__name__}.metrics")

    def record_call(self, metadata: Dict[str, Any]) -> None:
        """
        Record a KV cache API call with its metadata.

        Args:
            metadata: Metadata from the API call
        """
        with self.lock:
            call_record = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'metadata': metadata.copy()
            }

            self.history.append(call_record)
            self._update_summary_stats(metadata)

    def _update_summary_stats(self, metadata: Dict[str, Any]) -> None:
        """Update rolling summary statistics."""
        api_used = metadata.get('api_used', 'unknown')
        kv_optimized = metadata.get('kv_cache_optimized', False)
        success = metadata.get('success', True)

        # Count API usage
        self.summary_stats[f'api_{api_used}'] += 1

        # Count optimization usage
        if kv_optimized:
            self.summary_stats['kv_cache_optimized'] += 1
        else:
            self.summary_stats['kv_cache_disabled'] += 1

        # Count successes/failures
        if success:
            self.summary_stats['calls_successful'] += 1
        else:
            self.summary_stats['calls_failed'] += 1

        # Count fallbacks
        if metadata.get('fallback_reason'):
            self.summary_stats['fallbacks'] += 1

    def get_summary_stats(self) -> Dict[str, Any]:
        """
        Get current summary statistics.

        Returns:
            Dictionary with summary metrics
        """
        with self.lock:
            total_calls = len(self.history)
            if total_calls == 0:
                return {'total_calls': 0}

            stats = dict(self.summary_stats)

            # Calculate percentages
            stats['kv_cache_usage_percent'] = round(
                (stats.get('kv_cache_optimized', 0) / total_calls) * 100, 1
            )
            stats['success_rate_percent'] = round(
                (stats.get('calls_successful', 0) / total_calls) * 100, 1
            )
            stats['fallback_rate_percent'] = round(
                (stats.get('fallbacks', 0) / total_calls) * 100, 1
            )

            # Calculate averages
            if self.history:
                durations = [r['metadata'].get('duration_ms', 0) for r in self.history]
                token_estimates = [r['metadata'].get('total_tokens_estimate', 0) for r in self.history]

                stats['avg_duration_ms'] = round(sum(durations) / len(durations), 1)
                stats['avg_tokens_estimate'] = round(sum(token_estimates) / len(token_estimates), 0)

                # Performance gain estimate
                optimized_calls = [r for r in self.history if r['metadata'].get('kv_cache_optimized')]
                if optimized_calls:
                    gains = [r['metadata'].get('performance_gain_pct', 0) for r in optimized_calls if 'performance_gain_pct' in r['metadata']]
                    if gains:
                        stats['avg_performance_gain_percent'] = round(sum(gains) / len(gains), 1)

            stats['total_calls'] = total_calls
            stats['history_capacity'] = self.max_history

            return stats

    def get_recent_calls(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get recent API calls.

        Args:
            limit: Maximum number of recent calls to return

        Returns:
            List of recent call records
        """
        with self.lock:
            return list(self.history)[-limit:]

    def get_performance_trends(self) -> Dict[str, Any]:
        """
        Analyze performance trends over time.

        Returns:
            Performance trend analysis
        """
        with self.lock:
            if len(self.history) < 2:
                return {'insufficient_data': True}

            # Group by API type
            chat_calls = [r for r in self.history if r['metadata'].get('api_used') == 'chat']
            generate_calls = [r for r in self.history if r['metadata'].get('api_used') == 'generate']

            trends = {}

            for api_name, calls in [('chat', chat_calls), ('generate', generate_calls)]:
                if calls:
                    durations = [c['metadata'].get('duration_ms', 0) for c in calls]
                    trends[f'{api_name}_avg_duration'] = round(sum(durations) / len(durations), 1)
                    trends[f'{api_name}_call_count'] = len(calls)

            # Calculate potential time savings
            if 'chat_avg_duration' in trends and 'generate_avg_duration' in trends:
                time_saved_per_call = trends['generate_avg_duration'] - trends['chat_avg_duration']
                if time_saved_per_call > 0:
                    trends['estimated_time_saved_ms_per_call'] = round(time_saved_per_call, 1)
                    trends['total_estimated_time_saved_ms'] = round(
                        time_saved_per_call * trends.get('chat_call_count', 0), 0
                    )

            return trends

    def export_metrics(self, filepath: str) -> None:
        """
        Export metrics to JSON file.

        Args:
            filepath: Path to export metrics to
        """
        with self.lock:
            metrics = {
                'summary': self.get_summary_stats(),
                'trends': self.get_performance_trends(),
                'recent_calls': self.get_recent_calls(50),
                'export_timestamp': datetime.now(timezone.utc).isoformat()
            }

            try:
                with open(filepath, 'w') as f:
                    json.dump(metrics, f, indent=2, ensure_ascii=False)
                self.logger.info(f"Metrics exported to {filepath}", filepath=filepath)
            except Exception as e:
                self.logger.error(f"Failed to export metrics: {e}", error=str(e))

    def reset(self) -> None:
        """Reset all metrics and history."""
        with self.lock:
            self.history.clear()
            self.summary_stats.clear()
            self.logger.info("Metrics reset")


# Global metrics instance
_kv_cache_metrics = None
_metrics_lock = threading.RLock()


def get_kv_cache_metrics() -> KVCacheMetrics:
    """
    Get the global KV cache metrics instance.

    Returns:
        Global metrics collector instance
    """
    global _kv_cache_metrics
    with _metrics_lock:
        if _kv_cache_metrics is None:
            _kv_cache_metrics = KVCacheMetrics()
        return _kv_cache_metrics


def record_kv_cache_call(metadata: Dict[str, Any]) -> None:
    """
    Record a KV cache API call in the global metrics.

    Args:
        metadata: Call metadata
    """
    metrics = get_kv_cache_metrics()
    metrics.record_call(metadata)


def get_kv_cache_stats() -> Dict[str, Any]:
    """
    Get current KV cache statistics.

    Returns:
        Current metrics summary
    """
    metrics = get_kv_cache_metrics()
    return metrics.get_summary_stats()


def export_kv_cache_metrics(filepath: str) -> None:
    """
    Export KV cache metrics to file.

    Args:
        filepath: Export file path
    """
    metrics = get_kv_cache_metrics()
    metrics.export_metrics(filepath)
