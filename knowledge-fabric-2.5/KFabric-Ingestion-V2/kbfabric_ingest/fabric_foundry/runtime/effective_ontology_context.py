from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kbfabric_ingest.fabric_foundry.runtime.industry_config import (
    IndustryConfigLoader,
)
from kbfabric_ingest.fabric_foundry.knowledge_contextualization.repository import (
    CustomerOntologyNotFoundError,
    CustomerOntologyRecord,
    CustomerOntologyRepository,
)


class EffectiveDomainOntologyNotFoundError(Exception):
    """Raised when the requested customer/domain ontology context cannot be resolved."""


def _customer_authoritative_name(
    canonical_name: str, state: dict[str, Any] | None,
) -> str:
    """Use the first persisted customer alias, otherwise the supplied name."""
    if isinstance(state, dict):
        aliases = state.get("aliases", [])
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    return alias.strip()
    return canonical_name


def compile_customer_runtime_ontology(
    definition: dict[str, Any],
    *,
    domain_id: str,
) -> dict[str, Any]:
    """Compile persisted Customer Ontology into the ingestion runtime ontology.

    Only configured + applicable customer concepts enter runtime. Customer
    aliases replace canonical names in the runtime vocabulary. Customer-added
    concepts are included when applicable. The output preserves the spine
    structure consumed by IE and KGWriter.
    """
    domains = definition.get("domains")
    if not isinstance(domains, list):
        raise EffectiveDomainOntologyNotFoundError(
            "Customer Ontology definition must contain a domains list"
        )

    source_domain = next(
        (
            item
            for item in domains
            if isinstance(item, dict) and item.get("name") == domain_id
        ),
        None,
    )
    if source_domain is None:
        raise EffectiveDomainOntologyNotFoundError(
            f"Domain not found in Customer Ontology: {domain_id}"
        )

    customer_context = source_domain.get("customer_context", {})
    if not isinstance(customer_context, dict):
        customer_context = {}

    spine_context = customer_context.get("spine", {})
    if not isinstance(spine_context, dict):
        spine_context = {}

    product_line_states = spine_context.get("product_lines", {})
    function_states = spine_context.get("functions", {})
    sub_function_states = spine_context.get("sub_functions", {})

    if not isinstance(product_line_states, dict):
        product_line_states = {}
    if not isinstance(function_states, dict):
        function_states = {}
    if not isinstance(sub_function_states, dict):
        sub_function_states = {}

    runtime_spine: list[dict[str, Any]] = []

    # Canonical spine items that the customer explicitly selected.
    for product_line in source_domain.get("spine", []):
        if not isinstance(product_line, dict):
            continue

        canonical_pl = product_line.get("ProductLine")
        if not isinstance(canonical_pl, str) or not canonical_pl.strip():
            continue

        pl_state = product_line_states.get(canonical_pl)
        if not (
            isinstance(pl_state, dict)
            and pl_state.get("applicable") is True
        ):
            continue

        runtime_pl = {
            "ProductLine": _customer_authoritative_name(canonical_pl, pl_state),
            "functions": [],
        }

        fn_states = function_states.get(canonical_pl, {})
        if not isinstance(fn_states, dict):
            fn_states = {}

        sf_states_for_pl = sub_function_states.get(canonical_pl, {})
        if not isinstance(sf_states_for_pl, dict):
            sf_states_for_pl = {}

        for function in product_line.get("functions", []):
            if not isinstance(function, dict):
                continue

            canonical_fn = function.get("Function")
            if not isinstance(canonical_fn, str) or not canonical_fn.strip():
                continue

            fn_state = fn_states.get(canonical_fn)
            if not (
                isinstance(fn_state, dict)
                and fn_state.get("applicable") is True
            ):
                continue

            runtime_fn = {
                "Function": _customer_authoritative_name(canonical_fn, fn_state),
                "subfunctions": [],
            }

            sf_states = sf_states_for_pl.get(canonical_fn, {})
            if not isinstance(sf_states, dict):
                sf_states = {}

            for canonical_sf in function.get("subfunctions", []):
                if not isinstance(canonical_sf, str):
                    continue

                sf_state = sf_states.get(canonical_sf)
                if not (
                    isinstance(sf_state, dict)
                    and sf_state.get("applicable") is True
                ):
                    continue

                runtime_fn["subfunctions"].append(
                    _customer_authoritative_name(canonical_sf, sf_state)
                )

            runtime_pl["functions"].append(runtime_fn)

        runtime_spine.append(runtime_pl)

    # Customer-added Product Lines.
    additions = customer_context.get("additions", {})
    if not isinstance(additions, dict):
        additions = {}

    added_product_lines = additions.get("product_lines", [])
    if not isinstance(added_product_lines, list):
        added_product_lines = []

    # Map canonical product-line IDs to runtime spine objects so customer-added
    # functions can be attached without relying on display names/aliases.
    runtime_product_lines_by_canonical_id: dict[str, dict[str, Any]] = {}

    for product_line in source_domain.get("spine", []):
        if not isinstance(product_line, dict):
            continue

        canonical_pl = product_line.get("ProductLine")
        if not isinstance(canonical_pl, str):
            continue

        canonical_id = (
            f"DOMAIN:{domain_id}|PRODUCT_LINE:{canonical_pl}"
        )

        for runtime_pl in runtime_spine:
            if runtime_pl["ProductLine"] == _customer_authoritative_name(
                canonical_pl,
                product_line_states.get(canonical_pl),
            ):
                runtime_product_lines_by_canonical_id[canonical_id] = runtime_pl
                break

    for item in added_product_lines:
        if not isinstance(item, dict):
            continue
        if item.get("applicable") is not True:
            continue

        name = item.get("name")
        item_id = item.get("id")
        if not isinstance(name, str) or not name.strip():
            continue

        runtime_pl = {
            "ProductLine": _customer_authoritative_name(name.strip(), item),
            "functions": [],
        }
        runtime_spine.append(runtime_pl)

        if isinstance(item_id, str):
            runtime_product_lines_by_canonical_id[item_id] = runtime_pl

    # Customer-added Functions.
    added_functions = additions.get("functions", [])
    if not isinstance(added_functions, list):
        added_functions = []

    added_subfunctions = additions.get("sub_functions", [])
    if not isinstance(added_subfunctions, list):
        added_subfunctions = []

    runtime_functions_by_id: dict[str, dict[str, Any]] = {}

    for item in added_functions:
        if not isinstance(item, dict):
            continue
        if item.get("applicable") is not True:
            continue

        parent_id = item.get("parent_product_line_id")
        name = item.get("name")
        function_id = item.get("id")

        if not (
            isinstance(parent_id, str)
            and isinstance(name, str)
            and name.strip()
        ):
            continue

        parent = runtime_product_lines_by_canonical_id.get(parent_id)
        if parent is None:
            continue

        runtime_fn = {
            "Function": _customer_authoritative_name(name.strip(), item),
            "subfunctions": [],
        }
        parent["functions"].append(runtime_fn)

        if isinstance(function_id, str):
            runtime_functions_by_id[function_id] = runtime_fn

    # Customer-added SubFunctions.
    for item in added_subfunctions:
        if not isinstance(item, dict):
            continue
        if item.get("applicable") is not True:
            continue

        parent_id = item.get("parent_function_id")
        name = item.get("name")

        if not (
            isinstance(parent_id, str)
            and isinstance(name, str)
            and name.strip()
        ):
            continue

        parent = runtime_functions_by_id.get(parent_id)
        if parent is not None:
            parent["subfunctions"].append(_customer_authoritative_name(name.strip(), item))

    # The persisted Customer Ontology is the authoritative runtime contract.
    # Preserve the complete top-level canonical model and replace only the
    # selected domain's customer-contextualized spine.
    runtime_domain = {
        key: value
        for key, value in source_domain.items()
        if key != "customer_context"
    }
    canonical_domain_display_name = (
        source_domain.get("display_name") or source_domain.get("name") or domain_id
    )
    runtime_domain["display_name"] = _customer_authoritative_name(
        str(canonical_domain_display_name),
        {"aliases": customer_context.get("aliases", [])},
    )
    runtime_domain["spine"] = runtime_spine

    canonical_structure_keys = {
        "node_types",
        "block_categories",
        "relationship_types",
        "vision_ie_type_mapping",
        "path_format",
    }
    runtime_definition = {
        key: value
        for key, value in definition.items()
        if key != "domains" and key not in canonical_structure_keys
    }
    runtime_definition["domains"] = [
        runtime_domain
        if isinstance(item, dict) and item.get("name") == domain_id
        else item
        for item in domains
    ]

    return runtime_definition


@dataclass(frozen=True)
class OntologyRuntimeContext:
    """Frozen authoritative ontology context for one ingestion run.

    Ontology authority is scoped to customer + domain. Product line is runtime
    ingestion scope selected by the user and is carried as immutable lineage
    metadata throughout the run.
    """

    customer_id: str
    tenant_id: str
    domain_id: str

    customer_ontology_id: str
    customer_ontology_version: int

    ontology_id: str
    ontology_version: str

    domain_definition: dict[str, Any]
    canonical_model: dict[str, Any]
    extraction_policy: dict[str, Any]
    product_line_id: str | None = None
    product_line_name: str | None = None

    @classmethod
    def from_customer_ontology(
        cls,
        record: CustomerOntologyRecord,
        *,
        domain_id: str,
        product_line_id: str | None = None,
        product_line_name: str | None = None,
    ) -> "OntologyRuntimeContext":
        normalized_domain = domain_id.strip()
        if not normalized_domain:
            raise ValueError("domain_id must not be blank")

        domains = record.definition.get("domains")
        if not isinstance(domains, list):
            raise EffectiveDomainOntologyNotFoundError(
                "Customer Ontology definition must contain a domains list"
            )

        domain = next(
            (
                item
                for item in domains
                if isinstance(item, dict)
                and item.get("name") == normalized_domain
            ),
            None,
        )

        if domain is None:
            raise EffectiveDomainOntologyNotFoundError(
                f"Domain not found in Customer Ontology: {normalized_domain}"
            )

        normalized_product_line_id = (
            product_line_id.strip() if product_line_id else None
        )
        normalized_product_line_name = (
            product_line_name.strip() if product_line_name else None
        )

        if not normalized_product_line_id:
            raise ValueError(
                "product_line_id is required for an authoritative ingestion run"
            )

        # Product-line authority is resolved entirely from the selected
        # customer/domain ontology. The LLM and document content do not
        # participate in this decision.
        selected_product_line = None

        # 1. Canonical product lines.
        for product_line in domain.get("spine", []):
            if not isinstance(product_line, dict):
                continue

            name = product_line.get("ProductLine")
            if not isinstance(name, str) or not name.strip():
                continue

            canonical_id = (
                f"DOMAIN:{normalized_domain}|PRODUCT_LINE:{name}"
            )

            if normalized_product_line_id in {canonical_id, name}:
                selected_product_line = {
                    "id": canonical_id,
                    "name": name,
                    "configured": False,
                    "applicable": False,
                }
                break

        # 2. Customer-added product lines.
        if selected_product_line is None:
            customer_context = domain.get("customer_context", {})
            if isinstance(customer_context, dict):
                additions = customer_context.get("additions", {})
                if isinstance(additions, dict):
                    for product_line in additions.get("product_lines", []):
                        if not isinstance(product_line, dict):
                            continue

                        if product_line.get("id") == normalized_product_line_id:
                            selected_product_line = product_line
                            break

        if selected_product_line is None:
            raise EffectiveDomainOntologyNotFoundError(
                "Product line is not configured in the selected customer/domain "
                f"ontology: {normalized_product_line_id}"
            )

        # Customer configuration is authoritative. A product line must be
        # explicitly configured and applicable before ingestion can use it.
        customer_context = domain.get("customer_context", {})
        spine_context = (
            customer_context.get("spine", {})
            if isinstance(customer_context, dict)
            else {}
        )
        product_line_states = (
            spine_context.get("product_lines", {})
            if isinstance(spine_context, dict)
            else {}
        )

        state = (
            product_line_states.get(selected_product_line.get("name"), {})
            if isinstance(product_line_states, dict)
            else {}
        )

        is_configured = bool(
            isinstance(state, dict) and state.get("configured", True)
        )
        is_applicable = bool(
            isinstance(state, dict)
            and state.get("applicable") is True
        )

        # Customer-added product lines are explicitly applicable/configured
        # by contextualization. Canonical product lines require the customer's
        # explicit applicability decision.
        if selected_product_line.get("id", "").startswith("customer-"):
            is_configured = True
            is_applicable = selected_product_line.get("applicable") is True

        if not is_configured or not is_applicable:
            raise EffectiveDomainOntologyNotFoundError(
                "Product line is not applicable/configured for the selected "
                f"customer/domain: {normalized_product_line_id}"
            )

        resolved_name = _customer_authoritative_name(
            str(selected_product_line.get("name") or normalized_product_line_name or ""),
            state if isinstance(state, dict) else selected_product_line,
        )

        if (
            normalized_product_line_name
            and resolved_name
            and normalized_product_line_name != resolved_name
        ):
            raise ValueError(
                "product_line_name does not match the authoritative "
                f"product_line_id: {normalized_product_line_id}"
            )

        industry_config = IndustryConfigLoader().load(record.ontology_id)

        return cls(
            customer_id=record.customer_id,
            tenant_id=record.tenant_id,
            domain_id=normalized_domain,
            customer_ontology_id=str(record.customer_ontology_id),
            customer_ontology_version=record.customer_ontology_version,
            ontology_id=record.ontology_id,
            ontology_version=record.ontology_version,
            domain_definition=compile_customer_runtime_ontology(
                record.definition,
                domain_id=normalized_domain,
            ),
            canonical_model=industry_config.model,
            extraction_policy=industry_config.extraction_policy,
            product_line_id=normalized_product_line_id,
            product_line_name=resolved_name,
        )


class EffectiveDomainOntologyResolver:
    """Resolve the authoritative active customer/domain ontology context."""

    def __init__(
        self,
        repository: CustomerOntologyRepository | None = None,
    ) -> None:
        self.repository = repository or CustomerOntologyRepository()

    def resolve(
        self,
        *,
        customer_id: str,
        domain_id: str,
        product_line_id: str | None = None,
        product_line_name: str | None = None,
        customer_ontology_version: int | None = None,
    ) -> OntologyRuntimeContext:
        normalized_customer_id = customer_id.strip()
        normalized_domain_id = domain_id.strip()

        if not normalized_customer_id:
            raise ValueError("customer_id must not be blank")
        if not normalized_domain_id:
            raise ValueError("domain_id must not be blank")

        if customer_ontology_version is None:
            record = self.repository.get_active(normalized_customer_id)
            missing_message = f"Active Customer Ontology not found: {normalized_customer_id}"
        else:
            record = self.repository.get_by_version(
                normalized_customer_id, customer_ontology_version
            )
            missing_message = (
                f"Customer Ontology version {customer_ontology_version} not found: "
                f"{normalized_customer_id}"
            )
        if record is None:
            raise CustomerOntologyNotFoundError(missing_message)

        return OntologyRuntimeContext.from_customer_ontology(
            record,
            domain_id=normalized_domain_id,
            product_line_id=product_line_id,
            product_line_name=product_line_name,
        )
