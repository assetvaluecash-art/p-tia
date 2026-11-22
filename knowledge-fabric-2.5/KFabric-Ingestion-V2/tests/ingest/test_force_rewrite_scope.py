"""Force-rewrite safety and scope contracts."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from kbfabric_ingest._common import env_config
from kbfabric_ingest.ingest import weaviate_writer as weaviate_writer_module
from kbfabric_ingest.ingest.kg_writer import KGWriter
from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
from kbfabric_ingest.scheduler import api
from kbfabric_ingest.security.auth import AuthenticatedActor


def test_force_rewrite_environment_switch_defaults_off_and_can_be_enabled(monkeypatch):
    """Objective: verify force rewrite is disabled unless explicitly enabled in environment config.

    This matters because a rewrite deletes stored ingestion data. The invariant is that a missing
    KBFABRIC_FORCE_REWRITE_ENABLED setting resolves to false and only an explicit true enables it.
    """
    monkeypatch.setattr(env_config, "load_dotenv_file", lambda: None)
    for name, _path, caster, _allow_empty in env_config.ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
        if caster is env_config._as_bool:
            value = "false"
        elif caster is env_config._as_int:
            value = "1"
        elif caster is env_config._as_float:
            value = "1.0"
        elif caster is env_config._as_str_list:
            value = "alpha,beta"
        elif caster is env_config._as_hitl_mode:
            value = "review_ambiguous"
        else:
            value = "configured"
        monkeypatch.setenv(name, value)

    monkeypatch.delenv("KBFABRIC_FORCE_REWRITE_ENABLED", raising=False)
    assert env_config.build_app_config()["ingestion"]["force_rewrite_enabled"] is False
    monkeypatch.setenv("KBFABRIC_FORCE_REWRITE_ENABLED", "true")
    assert env_config.build_app_config()["ingestion"]["force_rewrite_enabled"] is True


def test_force_rewrite_requires_both_global_switch_and_actor_permission(monkeypatch):
    """Objective: verify rewrite requests require both the environment gate and permission claim.

    This matters because the global setting is an operational kill switch while user permission
    controls who may invoke data deletion. Invariant: either missing control returns HTTP 403.
    """
    actor = AuthenticatedActor(
        user_id="reviewer-1",
        auth_source="kong_jwt",
        permissions=frozenset({"ingestion:force_rewrite"}),
    )
    monkeypatch.setattr(api, "get_app_config", lambda: {"ingestion": {"force_rewrite_enabled": False}})
    with pytest.raises(HTTPException) as disabled:
        api._require_force_rewrite_permission(actor)
    assert disabled.value.status_code == 403

    monkeypatch.setattr(api, "get_app_config", lambda: {"ingestion": {"force_rewrite_enabled": True}})
    no_permission = AuthenticatedActor(user_id="reviewer-2", auth_source="kong_jwt")
    with pytest.raises(HTTPException) as forbidden:
        api._require_force_rewrite_permission(no_permission)
    assert forbidden.value.status_code == 403
    api._require_force_rewrite_permission(actor)


def test_weaviate_product_line_cleanup_filters_all_four_scope_dimensions(monkeypatch):
    """Objective: verify Weaviate deletion is constrained to one customer, tenant, domain, and product line.

    This matters because Weaviate classes are shared by domain. Invariant: cleanup uses all four
    exact-match predicates and never deletes the class or other customer/product-line objects.
    """
    delete_objects = Mock(return_value={"matches": 7, "failed": 0})
    writer = object.__new__(WeaviateWriter)
    writer.client = SimpleNamespace(batch=SimpleNamespace(delete_objects=delete_objects))
    writer.logger = Mock()
    monkeypatch.setattr(weaviate_writer_module, "get_store_name_for_domain", lambda domain: "Consumer_retail")

    result = writer.clear_product_line(
        customer_id="customer-1",
        tenant_id="tenant-1",
        domain_id="consumer_retail",
        product_line_id="DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
    )

    assert result == {"status": "cleared", "class_name": "Consumer_retail", "deleted": 7}
    kwargs = delete_objects.call_args.kwargs
    assert kwargs["class_name"] == "Consumer_retail"
    operands = kwargs["where"]["operands"]
    assert {(x["path"][0], x["valueString"]) for x in operands} == {
        ("customer_id", "customer-1"),
        ("tenant_id", "tenant-1"),
        ("domain_id", "consumer_retail"),
        ("ingestion_product_line_id", "DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards"),
    }


def test_neo4j_product_line_cleanup_preserves_spine_and_shared_scopes():
    """Objective: verify graph cleanup removes only the selected product-line associations.

    This matters because ontology spine nodes and cross-line graph records are shared. Invariant:
    the query is customer/domain scoped, protects spine labels, and preserves shared relationships.
    """
    queries = []
    values = [
        {"deleted": 2},
        {"deleted": 3},
        {"retained_shared_relationships": 1},
        {"orphan_entities_deleted": 4},
        {"documents_unscoped": 2},
    ]

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def single(self):
            return self.value

    class FakeSession:
        def run(self, query, **params):
            queries.append((query, params))
            return FakeResult(values[len(queries) - 1])

    class FakeDriver:
        @contextmanager
        def session(self):
            yield FakeSession()

        def close(self):
            pass

    writer = object.__new__(KGWriter)
    writer.driver = FakeDriver()
    writer.logger = Mock()
    result = writer.clear_product_line(
        customer_id="customer-1",
        tenant_id="tenant-1",
        domain_id="consumer_retail",
        product_line_id="DOMAIN:consumer_retail|PRODUCT_LINE:Retail Cards",
    )

    assert result["status"] == "cleared"
    assert result["shared_relationships_retained"] == 1
    assert len(queries) == 5
    for query, params in queries:
        assert params["customer_id"] == "customer-1"
        assert params["tenant_id"] == "tenant-1"
        assert params["domain_id"] == "consumer_retail"
        assert params["product_line_id"].endswith("Retail Cards")
    node_cleanup = queries[3][0]
    assert "none(label IN labels(n) WHERE label IN $protected_labels)" in node_cleanup
    assert "remaining_product_lines" in node_cleanup
    assert "non_domain_relationships = 0" in node_cleanup
