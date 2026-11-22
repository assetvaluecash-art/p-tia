"""Read-only, customer-scoped context retrieval for candidate adjudication."""

from __future__ import annotations

from typing import Any

from kbfabric_ingest.ingest.config import get_store_name_for_domain
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


class WeaviateContextReader:
    """Lazily fetch prior masked chunks from the same customer document."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._client: Any | None = None
        self._cache: dict[tuple[str, str, str, str], list[str]] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            import weaviate

            timeout = self.config.get("timeout", {}) or {}
            timeout_config = (timeout.get("query_sec", 10), timeout.get("insert_sec", 30))
            api_key = self.config.get("api_key") or None
            kwargs = {"timeout_config": timeout_config}
            if api_key:
                kwargs["auth_client_secret"] = weaviate.AuthApiKey(api_key)
            try:
                self._client = weaviate.Client(self.config["url"], **kwargs)
            except TypeError:
                kwargs.pop("timeout_config", None)
                self._client = weaviate.Client(self.config["url"], **kwargs)
        return self._client

    def fetch_document_context(
        self, *, domain: str, customer_id: str, tenant_id: str, source_file: str,
        current_block_id: str | None = None, product_line_id: str | None = None,
        limit: int = 8,
    ) -> list[str]:
        """Return bounded masked text from matching customer/document chunks only."""
        if not all((domain, customer_id, tenant_id, source_file)):
            return []
        key = (domain, customer_id, tenant_id, source_file)
        if key not in self._cache:
            self._cache[key] = []
            try:
                operands = [
                    {"path": ["customer_id"], "operator": "Equal", "valueString": customer_id},
                    {"path": ["tenant_id"], "operator": "Equal", "valueString": tenant_id},
                    {"path": ["domain"], "operator": "Equal", "valueString": domain},
                    {"path": ["source_file"], "operator": "Equal", "valueString": source_file},
                ]
                if product_line_id:
                    operands.append({"path": ["ingestion_product_line_id"],
                                     "operator": "Equal", "valueString": product_line_id})
                where = {"operator": "And", "operands": operands}
                response = (self._get_client().query
                    .get(get_store_name_for_domain(domain), ["masked_text", "block_id"])
                    .with_where(where).with_limit(limit).do())
                rows = (response.get("data", {}).get("Get", {}) or {}).get(
                    get_store_name_for_domain(domain), []
                ) or []
                for row in rows:
                    block_id = str(row.get("block_id") or "")
                    text = str(row.get("masked_text") or "").strip()
                    if text and block_id != current_block_id:
                        self._cache[key].append(text[:1800])
                logger.info("context_evidence.weaviate.read", domain=domain,
                            customer_id=customer_id, tenant_id=tenant_id, source_file=source_file,
                            returned_chunks=len(self._cache[key]))
            except Exception as exc:  # noqa: BLE001 — evidence enrichment must fail closed to base evidence
                logger.info("context_evidence.weaviate.unavailable", domain=domain,
                            customer_id=customer_id, source_file=source_file, error=str(exc))
        return list(self._cache[key])[:limit]
