"""
Production-ready hybrid retriever combining vector search and graph traversal.

Encapsulates all Weaviate (chunk search), Neo4j (multi-hop graph expansion),
embedding, reranking, and pattern-resolution logic behind a single
``retrieve(intent, query, domain)`` call.

The caller never sees distance metrics, vector formats, Cypher, or Weaviate
class names.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from kbfabric_ingest.ingest.config import (
    get_app_config,
    get_ontology,
    get_store_name_for_domain,
)
from kbfabric_ingest.retrieval.node_display import (
    format_node_for_retrieval,
    ontology_type_descriptions,
)
from custom_ml.embeddings.embeddings_client import EmbeddingsClient
from custom_ml.reranking.reranker_client import RerankerClient

_INFRA_REL_TYPES = [
    "NODE_IN_DOMAIN",
    "BLOCK_BELONGS_TO_DOCUMENT",
    "BLOCK_MENTIONS_ENTITY",
    "DOCUMENT_IN_DOMAIN",
]

_DEFAULT_PATTERN: dict[str, Any] = {
    "description": "General query",
    "primary_categories": [],
    "secondary_categories": [],
    "relationship_types": [],
    "max_depth": 2,
}

# Properties fetched from Weaviate for every chunk
# (block_id is the Weaviate UUID itself — fetched via _additional.id)
_WEAVIATE_PROPS = [
    "masked_text", "block_category", "kg_node_type",
    "source_file", "page_number", "domain",
    "block_index", "block_type",
    "extracted_fields", "extracted_knowledge",
]


class HybridRetriever:
    """Production-ready hybrid retriever.

    Callers provide *intent*, *query*, *domain*, and caps.  All Weaviate /
    Neo4j / embedding / reranking details are encapsulated.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(
        self,
        config_path: str = "config/retrieval_patterns.yaml",
    ) -> None:
        self._config: dict[str, Any] = get_app_config()
        self._ontology: dict[str, Any] = get_ontology()
        # Prefer explicit patterns path; fall back beside ontology config dir
        patterns_path = Path(config_path)
        if not patterns_path.is_absolute() and not patterns_path.exists():
            from kbfabric_ingest.ingest.config import BASE_DIR
            patterns_path = BASE_DIR / "config" / "retrieval_patterns.yaml"
        self._config_path = str(patterns_path)

        self._patterns = self._load_patterns(str(patterns_path))
        self._rel_natural: dict[str, str] = (
            self._ontology
            .get("path_format", {})
            .get("rel_type_to_natural", {})
        )
        self._type_descriptions: dict[str, str] = ontology_type_descriptions(
            self._ontology
        )

        self._wv = self._connect_weaviate()
        self._neo4j = self._connect_neo4j()

        self._embedder = EmbeddingsClient(self._config)
        self._embedder.load_model()

        self._reranker = RerankerClient(self._config)
        self._reranker.load_model()

    def close(self) -> None:
        """Release database connections."""
        try:
            self._neo4j.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(
        self,
        intent: str,
        query: str,
        domain: str = "corporate_commercial",
        *,
        top_k_semantic: int = 10,
        top_k_graph: int = 10,
    ) -> dict[str, Any]:
        """Main entry point.

        Returns dict with ``query``, ``domain``, ``detected_domains``,
        ``chunks``, ``reranked_chunks``, ``paths``, ``reranked_paths``.
        """
        t0 = time.perf_counter()

        class_name = get_store_name_for_domain(domain)
        pattern = self._resolve_pattern(intent)
        search_text = f"Intent: {pattern['description']}\nQuery: {query}"

        errors: list[str] = []

        # --- vector search ------------------------------------------------
        try:
            query_vec = self._embedder.encode_single(search_text)
            chunks_raw = self._search_chunks(
                query_vec,
                top_k_semantic * 2,
                class_name=class_name,
                primary_categories=pattern.get("primary_categories"),
                secondary_categories=pattern.get("secondary_categories"),
            )
        except Exception as exc:
            chunks_raw = []
            errors.append(f"chunk_search: {exc}")

        # --- graph expansion ----------------------------------------------
        try:
            paths_raw = self._expand_paths(
                search_text, top_k_graph, pattern,
            )
        except Exception as exc:
            paths_raw = []
            errors.append(f"path_expansion: {exc}")

        # --- reranking ----------------------------------------------------
        chunks_reranked = self._rerank_chunks(query, chunks_raw, top_k_semantic)
        paths_reranked = self._rerank_paths(query, paths_raw, top_k_graph)

        latency_ms = (time.perf_counter() - t0) * 1000

        return {
            "query": query,
            "domain": domain,
            "detected_domains": [{"domain": domain, "confidence": 1.0}],
            "chunks": [self._format_chunk(c) for c in chunks_raw[:top_k_semantic]],
            "reranked_chunks": [self._format_chunk(c) for c in chunks_reranked],
            "paths": [self._format_path(p) for p in paths_raw[:top_k_graph]],
            "reranked_paths": [self._format_path(p) for p in paths_reranked],
            "metadata": {
                "intent": intent,
                "pattern": pattern.get("description", intent),
                "max_depth": pattern.get("max_depth", 2),
                "latency_ms": round(latency_ms, 1),
                "chunks_considered": len(chunks_raw),
                "paths_considered": len(paths_raw),
                **({"errors": errors} if errors else {}),
            },
        }

    # ------------------------------------------------------------------
    # Private — connections
    # ------------------------------------------------------------------

    def _connect_weaviate(self):
        import weaviate
        wv_cfg = self._config["weaviate"]
        url = wv_cfg["url"]
        api_key = wv_cfg.get("api_key") or None
        if api_key:
            return weaviate.Client(
                url=url,
                auth_client_secret=weaviate.AuthApiKey(api_key),
            )
        return weaviate.Client(url=url)

    def _connect_neo4j(self):
        from neo4j import GraphDatabase
        neo_cfg = self._config["neo4j"]
        uri = neo_cfg["uri"]
        user = neo_cfg["user"]
        password = neo_cfg["password"]
        return GraphDatabase.driver(uri, auth=(user, password))

    # ------------------------------------------------------------------
    # Private — pattern resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _load_patterns(config_path: str) -> dict[str, Any]:
        patterns_path = Path(config_path)
        if patterns_path.name != "retrieval_patterns.yaml":
            patterns_path = patterns_path.parent / "retrieval_patterns.yaml"
        if not patterns_path.exists():
            return {}
        with patterns_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("traversal_patterns", {})

    def _resolve_pattern(self, intent: str) -> dict[str, Any]:
        if intent in self._patterns:
            return self._patterns[intent]
        return self._patterns.get("any", dict(_DEFAULT_PATTERN))

    # ------------------------------------------------------------------
    # Private — chunk search
    # ------------------------------------------------------------------

    def _search_chunks(
        self,
        query_vector: list[float],
        limit: int,
        *,
        class_name: str,
        primary_categories: list[str] | None = None,
        secondary_categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(query_vector, np.ndarray):
            query_vector = query_vector.tolist()
        elif not isinstance(query_vector, list):
            query_vector = list(query_vector)

        result = (
            self._wv.query
            .get(class_name, _WEAVIATE_PROPS)
            .with_near_vector({"vector": query_vector})
            .with_limit(limit)
            .with_additional(["distance", "id"])
            .do()
        )

        if "errors" in result:
            msgs = [e.get("message", str(e)) for e in result["errors"]]
            raise RuntimeError(f"Weaviate query errors: {'; '.join(msgs)}")

        items = result.get("data", {}).get("Get", {}).get(class_name, [])

        primary = set(primary_categories or [])
        secondary = set(secondary_categories or [])

        chunks: list[dict[str, Any]] = []
        for item in items:
            additional = item.pop("_additional", {})
            dist = additional.get("distance")
            base_score = max(1.0 - (dist or 0.0), 0.0)
            cat = item.get("block_category", "")

            if primary:
                if cat in primary:
                    boosted = base_score * 1.2
                elif cat in secondary:
                    boosted = base_score * 1.0
                else:
                    boosted = base_score * 0.9
            else:
                boosted = base_score

            chunks.append({
                **item,
                "_base_score": base_score,
                "_boosted_score": round(min(boosted, 1.0), 6),
                "_weaviate_id": additional.get("id"),
            })

        chunks.sort(key=lambda c: c["_boosted_score"], reverse=True)
        return chunks

    # ------------------------------------------------------------------
    # Private — graph expansion
    # ------------------------------------------------------------------

    def _expand_paths(
        self,
        query_text: str,
        top_k: int,
        pattern: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rel_types = pattern.get("relationship_types", [])
        max_depth = pattern.get("max_depth", 2)

        query_vec = self._embedder.encode_single(query_text)

        with self._neo4j.session() as session:
            entity_result = session.run(
                "MATCH (e) "
                "WHERE e.name IS NOT NULL "
                "  AND NOT e:Block AND NOT e:Document "
                "  AND e.kg_node_type IS NOT NULL "
                "RETURN e.name AS name, e.kg_node_type AS type, "
                "       e.entity_id AS eid, e.pagerank AS pr "
                "ORDER BY coalesce(e.pagerank, 0) DESC "
                "LIMIT 50"
            )
            entities = [dict(r) for r in entity_result]

        if not entities:
            return []

        entity_names = [e["name"] for e in entities]
        entity_vecs = self._embedder.encode(entity_names)

        query_np = np.array(query_vec)
        scored: list[tuple[int, float]] = []
        for i, ev in enumerate(entity_vecs):
            ev_np = np.array(ev)
            norm_product = np.linalg.norm(query_np) * np.linalg.norm(ev_np) + 1e-9
            sim = float(np.dot(query_np, ev_np) / norm_product)
            scored.append((i, sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        seed_entities = [entities[i] for i, _ in scored[:5]]
        seed_eids = [e["eid"] for e in seed_entities if e.get("eid")]
        if not seed_eids:
            return []

        with self._neo4j.session() as session:
            if rel_types:
                rel_filter = (
                    "AND ALL(r IN relationships(path) WHERE type(r) IN $rel_types)"
                )
            else:
                rel_filter = (
                    "AND NONE(r IN relationships(path) "
                    "WHERE type(r) IN $infra)"
                )

            cypher = (
                f"MATCH path = (seed)-[*1..{max_depth}]-(target) "
                f"WHERE seed.entity_id IN $seed_eids "
                f"  AND NOT target:Block AND NOT target:Document "
                f"  {rel_filter} "
                f"WITH path, "
                f"     reduce(score = 1.0, r IN relationships(path) | score * 0.9) AS decay_score, "
                f"     [n IN nodes(path) | {{ "
                f"        name: coalesce(n.name, labels(n)[0]), "
                f"        description: coalesce(n.description, ''), "
                f"        type: coalesce(n.kg_node_type, labels(n)[0]) "
                f"     }}] AS node_chain, "
                f"     [r IN relationships(path) | type(r)] AS rel_chain, "
                f"     [r IN relationships(path) | {{ "
                f"        source_block_id: r.source_block_id, "
                f"        source_file: r.source_file, "
                f"        source_page: r.source_page "
                f"     }}] AS prov_chain "
                f"RETURN node_chain, rel_chain, prov_chain, decay_score "
                f"ORDER BY decay_score DESC "
                f"LIMIT $limit"
            )

            params: dict[str, Any] = {
                "seed_eids": seed_eids,
                "limit": top_k * 3,
                "infra": _INFRA_REL_TYPES,
            }
            if rel_types:
                params["rel_types"] = rel_types

            result = session.run(cypher, **params)

            paths: list[dict[str, Any]] = []
            for record in result:
                node_chain = record["node_chain"]
                rel_chain = record["rel_chain"]
                prov_chain = record["prov_chain"] or []
                decay = record["decay_score"]

                sym_parts: list[str] = []
                natural_parts: list[str] = []
                for i, node in enumerate(node_chain):
                    canonical = node.get("name") or "Unknown"
                    display = format_node_for_retrieval(
                        canonical,
                        instance_description=node.get("description"),
                        ontology_type_description=self._type_descriptions.get(
                            node.get("type") or ""
                        ),
                    )
                    node["display_name"] = display
                    # Symbolic path keeps canonical IDs for stable path identity
                    sym_parts.append(f'({node["type"]}:"{canonical}")')
                    if i < len(rel_chain):
                        sym_parts.append(f"-[:{rel_chain[i]}]→")
                    if i > 0:
                        rt = rel_chain[i - 1]
                        verb = self._rel_natural.get(
                            rt, rt.lower().replace("_", " ")
                        )
                        prev_display = node_chain[i - 1].get(
                            "display_name", node_chain[i - 1].get("name", "")
                        )
                        natural_parts.append(
                            f"{prev_display} {verb} {display}"
                        )

                symbolic = "".join(sym_parts)
                if natural_parts:
                    natural = " → ".join(natural_parts)
                elif node_chain:
                    natural = (
                        node_chain[0].get("display_name")
                        or node_chain[0].get("name", "")
                    )
                else:
                    natural = ""

                first_prov = prov_chain[0] if prov_chain else {}

                paths.append({
                    "_node_chain": node_chain,
                    "_rel_chain": rel_chain,
                    "_prov_chain": prov_chain,
                    "chain_symbolic": symbolic,
                    "chain_natural": natural,
                    "_decay_score": round(decay, 4),
                    "hops": len(rel_chain),
                    "_combined_pr": sum(
                        n.get("pr", 0) for n in node_chain if isinstance(n.get("pr"), (int, float))
                    ),
                    "_source_block_id": first_prov.get("source_block_id"),
                    "_source_file": first_prov.get("source_file"),
                    "_source_page": first_prov.get("source_page"),
                })

        return paths

    # ------------------------------------------------------------------
    # Private — reranking
    # ------------------------------------------------------------------

    def _rerank_chunks(
        self, query: str, chunks: list[dict], top_k: int,
    ) -> list[dict]:
        if not chunks:
            return []
        texts = [c.get("masked_text", "") or "" for c in chunks]
        ranked = self._reranker.rerank(query, texts, top_k=top_k)
        reranked: list[dict] = []
        for idx, _doc_text, score in ranked:
            entry = dict(chunks[idx])
            entry["_rerank_score"] = round(float(score), 4)
            reranked.append(entry)
        return reranked

    def _rerank_paths(
        self, query: str, paths: list[dict], top_k: int,
    ) -> list[dict]:
        if not paths:
            return []
        texts = [p.get("chain_natural", "") for p in paths]
        ranked = self._reranker.rerank(query, texts, top_k=top_k)
        reranked: list[dict] = []
        for idx, _doc_text, score in ranked:
            entry = dict(paths[idx])
            entry["_rerank_score"] = round(float(score), 4)
            reranked.append(entry)
        return reranked

    # ------------------------------------------------------------------
    # Private — output formatting (clean shapes, no internals leaked)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_json_str(val: Any) -> str | None:
        """Return a JSON string or None for extracted_fields / extracted_knowledge."""
        if val is None:
            return None
        if isinstance(val, str):
            return val
        return json.dumps(val)

    @classmethod
    def _format_chunk(cls, c: dict) -> dict[str, Any]:
        score = c.get("_rerank_score", c.get("_boosted_score", 0.0))
        return {
            "text": c.get("masked_text") or "",
            "source_file": c.get("source_file") or "",
            "category": c.get("block_category") or "",
            "score": round(float(score), 4),
            "weaviate_uuid": c.get("_weaviate_id") or "",
            "block_id": c.get("_weaviate_id") or "",
            "page_number": c.get("page_number"),
            "block_index": c.get("block_index"),
            "block_type": c.get("block_type") or "",
            "kg_node_type": c.get("kg_node_type") or "",
            "domain": c.get("domain") or "",
            "extracted_fields": cls._safe_json_str(c.get("extracted_fields")),
            "extracted_knowledge": cls._safe_json_str(c.get("extracted_knowledge")),
        }

    @staticmethod
    def _format_path(p: dict) -> dict[str, Any]:
        score = p.get("_rerank_score", p.get("_decay_score", 0.0))
        node_chain = p.get("_node_chain", [])
        rel_chain = p.get("_rel_chain", [])

        from_entity = node_chain[0] if node_chain else {}
        to_entity = node_chain[-1] if len(node_chain) > 1 else {}
        first_rel = rel_chain[0] if rel_chain else ""

        def _entity_out(ent: dict) -> dict[str, str]:
            canonical = ent.get("name", "") or ""
            return {
                "name": ent.get("display_name") or canonical,
                "canonical_name": canonical,
                "type": ent.get("type", "") or "",
            }

        return {
            "path_text": p.get("chain_symbolic") or "",
            "score": round(float(score), 4),
            "from_entity": _entity_out(from_entity),
            "relationship_type": first_rel,
            "to_entity": _entity_out(to_entity),
            "source_block_id": p.get("_source_block_id") or "",
            "source_file": p.get("_source_file") or "",
            "source_page": p.get("_source_page"),
            "source_evidence": p.get("chain_natural") or "",
        }
